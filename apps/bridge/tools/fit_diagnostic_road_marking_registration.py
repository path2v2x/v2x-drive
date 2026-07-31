#!/usr/bin/env python3
"""Fit a diagnostic visual-registration hypothesis to road paint and signals.

The fit uses a hash-bound retained image, the reviewed road-marking GLB extract,
and the selected signal-stack identity.  It freezes camera translation and
optimizes pitch/yaw/roll/FOV.  Four spatial leave-one-region-out paint checks
measure stability.  The deployed signal map and reviewed paint bundle are
different revisions with an unvalidated inter-frame transform, so fitted
projection values must not be interpreted as physical camera parameters.
This tool can never authorize deployment.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys

import cv2
import numpy as np

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from fit_diagnostic_visual_calibration import project  # noqa: E402


PARAMETER_SCALES = np.asarray([2.0, 2.0, 3.0, 4.0])
DELTA_BOUNDS = ((-4.0, 4.0), (-4.0, 4.0), (-5.0, 5.0), (-7.0, 7.0))


def group_signal_components(objects):
    groups = {}
    for item in objects:
        if item.get("category") != "TrafficLight" or max(item.get("extent", (999,))) > 0.2:
            continue
        point = item["center_world"]
        groups.setdefault((round(point[0], 1), round(point[1], 1)), []).append(item)
    output = []
    for key, values in groups.items():
        values.sort(key=lambda item: item["center_world"][2], reverse=True)
        if len(values) == 3:
            output.append({
                "id": f"signal-stack-{key[0]:.1f}-{key[1]:.1f}",
                "components": values,
            })
    return sorted(output, key=lambda item: item["id"])


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def white_paint_mask(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 135)).astype(np.uint8)
    height, width = mask.shape
    # The retained cameras look down from a pole.  This deterministic broad
    # ground ROI removes sky while intentionally retaining some false-positive
    # foliage; robust trimming below prevents it from dominating the fit.
    roi = np.zeros_like(mask)
    polygon = np.asarray([
        [0, round(0.22 * height)],
        [round(0.41 * width), round(0.17 * height)],
        [round(0.45 * width), round(0.075 * height)],
        [width - 1, round(0.075 * height)],
        [width - 1, height - 1],
        [0, height - 1],
    ], dtype=np.int32)
    cv2.fillPoly(roi, [polygon], 1)
    mask &= roi
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))


def spatial_regions(width, height):
    split_y = round(height / 3)
    split_x = round(width / 2)
    regions = {}
    for name, x0, x1, y0, y1 in (
        ("upper_left", 0, split_x, 0, split_y),
        ("upper_right", split_x, width, 0, split_y),
        ("lower_left", 0, split_x, split_y, height),
        ("lower_right", split_x, width, split_y, height),
    ):
        value = np.zeros((height, width), dtype=np.uint8)
        value[y0:y1, x0:x1] = 1
        regions[name] = value
    return regions


def render_markings(vertices, triangles, params, width, height):
    pixels, depth = project(vertices, params, width, height)
    mask = np.zeros((height, width), dtype=np.uint8)
    for triangle in triangles:
        triangle_pixels = pixels[triangle]
        if np.all(depth[triangle] > 0.1) and np.isfinite(triangle_pixels).all():
            integer = np.rint(triangle_pixels).astype(np.int32)
            if (
                integer[:, 0].max() >= 0
                and integer[:, 0].min() < width
                and integer[:, 1].max() >= 0
                and integer[:, 1].min() < height
            ):
                cv2.fillConvexPoly(mask, integer, 1)
    return mask


def paint_metrics(rendered, observed, region=None, boundary_buffer_px=4):
    selected = np.ones_like(observed, dtype=np.uint8) if region is None else region
    if region is not None and boundary_buffer_px:
        size = 2 * boundary_buffer_px + 1
        selected = cv2.erode(
            selected.astype(np.uint8), np.ones((size, size), np.uint8)
        )
    region_rendered = (rendered > 0).astype(np.uint8) & selected
    region_observed = (observed > 0).astype(np.uint8) & selected
    rendered_pixels = region_rendered > 0
    observed_pixels = region_observed > 0
    if np.count_nonzero(rendered_pixels) < 250 or np.count_nonzero(observed_pixels) < 250:
        return None
    observed_distance = cv2.distanceTransform(1 - region_observed, cv2.DIST_L2, 3)
    rendered_distance = cv2.distanceTransform(1 - region_rendered, cv2.DIST_L2, 3)
    model_values = np.minimum(observed_distance[rendered_pixels], 15.0)
    observed_values = np.sort(np.minimum(rendered_distance[observed_pixels], 15.0))
    # Only the closest 60% of segmented image pixels are used because the
    # deterministic white threshold deliberately retains bright foliage/fence.
    observed_values = observed_values[:max(1, int(0.60 * len(observed_values)))]
    intersection = np.count_nonzero(rendered_pixels & observed_pixels)
    union = np.count_nonzero(rendered_pixels | observed_pixels)
    iou = intersection / union if union else 0.0
    model_mean = float(np.mean(model_values))
    observed_trimmed_mean = float(np.mean(observed_values))
    untrimmed_values = np.minimum(rendered_distance[observed_pixels], 15.0)
    return {
        "model_to_observed_mean_px": model_mean,
        "observed_to_model_trimmed_mean_px": observed_trimmed_mean,
        "observed_to_model_untrimmed_mean_px": float(np.mean(untrimmed_values)),
        "iou": float(iou),
        "score": model_mean + 0.65 * observed_trimmed_mean - 2.0 * iou,
        "rendered_pixel_count": int(np.count_nonzero(rendered_pixels)),
        "observed_pixel_count": int(np.count_nonzero(observed_pixels)),
        "observed_trim_fraction": 0.60,
        "boundary_buffer_px": boundary_buffer_px if region is not None else 0,
    }


def signal_metrics(world, observed, params, width, height):
    pixels, depth = project(world, params, width, height)
    errors = np.linalg.norm(pixels - observed, axis=1)
    if np.any(depth <= 0.1) or not np.isfinite(errors).all():
        return {
            "valid": False,
            "rmse_px": 10_000.0,
            "median_px": 10_000.0,
            "max_px": 10_000.0,
        }
    return {
        "valid": True,
        "rmse_px": float(math.sqrt(np.mean(errors**2))),
        "median_px": float(np.median(errors)),
        "max_px": float(np.max(errors)),
    }


def expand(base, delta):
    params = np.asarray(base, dtype=float).copy()
    params[3:7] += np.asarray(delta, dtype=float)
    return params


def optimize_candidate(
    base,
    vertices,
    triangles,
    observed_paint,
    signal_world,
    signal_observed,
    fit_region,
    seed,
    renderer=render_markings,
):
    from scipy.optimize import differential_evolution

    height, width = observed_paint.shape

    def objective(delta):
        params = expand(base, delta)
        rendered = renderer(vertices, triangles, params, width, height)
        paint = paint_metrics(rendered, observed_paint, fit_region)
        signals = signal_metrics(
            signal_world, signal_observed, params, width * 2, height * 2
        )
        prior = 0.03 * float(np.sum((np.asarray(delta) / PARAMETER_SCALES) ** 2))
        paint_score = 100.0 if paint is None else paint["score"]
        return paint_score + 0.20 * signals["rmse_px"] + prior

    result = differential_evolution(
        objective,
        DELTA_BOUNDS,
        seed=seed,
        popsize=8,
        maxiter=28,
        polish=True,
        tol=0.01,
        updating="immediate",
        workers=1,
    )
    params = expand(base, result.x)
    rendered = renderer(vertices, triangles, params, width, height)
    bound_distances = {
        name: float(min(value - bounds[0], bounds[1] - value) / (bounds[1] - bounds[0]))
        for name, value, bounds in zip(
            ("pitch", "yaw", "roll", "fov"), result.x, DELTA_BOUNDS
        )
    }
    boundary_hits = [
        name for name, distance in bound_distances.items() if distance < 0.01
    ]
    return {
        "optimizer_success": bool(result.success),
        "optimizer_message": str(result.message),
        "evaluations": int(result.nfev),
        "boundary_hits": boundary_hits,
        "normalized_distance_to_nearest_bound": bound_distances,
        "delta_projection_hypothesis_pitch_yaw_roll_fov": result.x.tolist(),
        "render_projection_hypothesis": params.tolist(),
        "objective": float(result.fun),
        "paint_fit": paint_metrics(rendered, observed_paint, fit_region),
        "paint_all": paint_metrics(rendered, observed_paint),
        "signals": signal_metrics(
            signal_world, signal_observed, params, width * 2, height * 2
        ),
        "rendered": rendered,
    }


def signal_correspondences(geometry, observations, search, rank):
    result = search["results"][rank - 1]
    map_stacks = {
        item["id"]: item
        for item in group_signal_components(geometry["geometry"]["objects"])
    }
    real_stacks = {item["id"]: item for item in observations["stacks"]}
    world, real = [], []
    for assignment in result["assignment"]:
        map_stack = map_stacks[assignment["map_id"]]
        real_stack = real_stacks[assignment["real_id"]]
        world.extend(item["center_world"] for item in map_stack["components"])
        real.extend(real_stack["real_component_pixels"])
    return np.asarray(world, dtype=float), np.asarray(real, dtype=float)


def load_inputs(args):
    paths = {
        "geometry": args.geometry.resolve(),
        "signal_observations": args.signal_observations.resolve(),
        "candidate_search": args.candidate_search.resolve(),
        "markings_json": args.markings_json.resolve(),
        "markings_npz": args.markings_npz.resolve(),
        "real_frame": args.real_frame.resolve(),
    }
    values = {
        name: json.loads(path.read_bytes())
        for name, path in paths.items()
        if name in {"geometry", "signal_observations", "candidate_search", "markings_json"}
    }
    geometry_hash = sha256(paths["geometry"])
    frame_hash = sha256(paths["real_frame"])
    if values["geometry"].get("schema") != "v2x-map-calibration-geometry/v1":
        raise ValueError("map geometry schema is unsupported")
    if values["signal_observations"].get("schema") != "v2x-signal-hypothesis-observations/v1":
        raise ValueError("signal observation schema is unsupported")
    if values["candidate_search"].get("schema") != "v2x-signal-hypothesis-search/v1":
        raise ValueError("candidate search schema is unsupported")
    if values["markings_json"].get("schema") != "v2x-reviewed-gltf-road-markings/v1":
        raise ValueError("road-marking schema is unsupported")
    if any(values[name].get("acceptance_eligible") is not False for name in values):
        raise ValueError("every source must explicitly reject acceptance eligibility")
    if values["signal_observations"].get("map_geometry_sha256") != geometry_hash:
        raise ValueError("signal observations do not bind geometry")
    if values["candidate_search"].get("geometry_sha256") != geometry_hash:
        raise ValueError("candidate search does not bind geometry")
    if values["candidate_search"].get("observations_sha256") != sha256(paths["signal_observations"]):
        raise ValueError("candidate search does not bind signal observations")
    if values["candidate_search"].get("real_frame_sha256") != frame_hash:
        raise ValueError("candidate search does not bind the retained frame")
    if values["markings_json"].get("output_npz_sha256") != sha256(paths["markings_npz"]):
        raise ValueError("marking JSON does not bind the NPZ")
    if values["markings_json"].get("coordinate_alignment_validated") is not False:
        raise ValueError("unexpected road-marking alignment contract")
    if (
        values["markings_json"].get("site_config", {}).get("sha256")
        != values["geometry"].get("cameras_file_sha256")
    ):
        raise ValueError("marking site config does not bind the geometry camera config")
    if not 1 <= args.rank <= len(values["candidate_search"].get("results", [])):
        raise ValueError("candidate rank is outside the search result")
    selected = values["candidate_search"]["results"][args.rank - 1]
    if (
        selected.get("optimizer_success") is not True
        or selected.get("boundary_hits")
        or selected.get("identity_underconstrained") is not False
    ):
        raise ValueError("selected signal candidate is rejected or underconstrained")
    return paths, values


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geometry", type=Path, required=True)
    parser.add_argument("--signal-observations", type=Path, required=True)
    parser.add_argument("--candidate-search", type=Path, required=True)
    parser.add_argument("--markings-json", type=Path, required=True)
    parser.add_argument("--markings-npz", type=Path, required=True)
    parser.add_argument("--real-frame", type=Path, required=True)
    parser.add_argument("--camera", choices=("ch4",), required=True)
    parser.add_argument("--rank", type=int, default=1)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.output_dir.exists():
        raise SystemExit("refusing to overwrite diagnostic registration bundle")
    try:
        paths, values = load_inputs(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    geometry = values["geometry"]
    search = values["candidate_search"]
    if search.get("camera") != args.camera or values["signal_observations"].get("camera") != args.camera:
        raise SystemExit("camera binding failed")
    camera_geometry = geometry["cameras"][args.camera]
    if camera_geometry["real"].get("frame_sha256") != sha256(paths["real_frame"]):
        raise SystemExit("geometry camera does not bind the retained frame")
    image = cv2.imread(str(paths["real_frame"]), cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit("retained frame cannot be decoded")
    if image.shape[:2] != (
        camera_geometry["real"]["height"], camera_geometry["real"]["width"]
    ):
        raise SystemExit("retained frame dimensions differ from geometry")
    if image.shape[1] != 640 or image.shape[0] != 480:
        raise SystemExit("diagnostic road-paint fit currently requires 640x480 evidence")

    arrays = np.load(paths["markings_npz"], allow_pickle=False)
    metadata = json.loads(str(arrays["metadata"]))
    if metadata.get("source_glb_sha256") != values["markings_json"].get("source_glb_sha256"):
        raise SystemExit("marking NPZ and JSON source bindings differ")
    white_indices = {
        int(index) for index, name in metadata["material_index_to_name"].items()
        if name == "LaneMarking1_Marking"
    }
    if not white_indices:
        raise SystemExit("reviewed marking artifact has no white road-paint material")
    triangle_mask = np.isin(arrays["triangle_material"], list(white_indices))
    triangles = np.asarray(arrays["triangles"][triangle_mask], dtype=np.int64)
    if not len(triangles):
        raise SystemExit("reviewed marking artifact has no usable white-paint triangles")
    vertices = np.asarray(arrays["vertices_xyz"], dtype=float).copy()
    vertices[:, 1] *= -1.0  # candidate OpenDRIVE -> CARLA handedness

    observed_full = white_paint_mask(image)
    observed = (cv2.resize(
        observed_full, (320, 240), interpolation=cv2.INTER_AREA
    ) > 0.35).astype(np.uint8)
    signal_world, signal_observed = signal_correspondences(
        geometry, values["signal_observations"], search, args.rank
    )
    base = np.asarray(search["results"][args.rank - 1]["fitted_absolute"], dtype=float)
    final = optimize_candidate(
        base, vertices, triangles, observed, signal_world, signal_observed,
        None, args.seed,
    )
    regions = spatial_regions(320, 240)
    for name, region in regions.items():
        if paint_metrics(observed, observed, region) is None:
            raise SystemExit(f"{name}: insufficient observed paint for a spatial fold")
    holdouts = {}
    fold_params = []
    for index, (name, holdout_region) in enumerate(regions.items(), start=1):
        fit_region = 1 - holdout_region
        fold = optimize_candidate(
            base, vertices, triangles, observed, signal_world, signal_observed,
            fit_region, args.seed + index,
        )
        fold["paint_holdout"] = paint_metrics(
            fold.pop("rendered"), observed, holdout_region
        )
        fold_params.append(fold["render_projection_hypothesis"])
        holdouts[name] = fold
    final_rendered = final.pop("rendered")
    failed = []
    for name, result in [("final", final), *holdouts.items()]:
        if result.get("optimizer_success") is not True:
            failed.append(f"{name}:optimizer")
        if result.get("boundary_hits"):
            failed.append(f"{name}:boundary")
        if result.get("paint_fit") is None or result.get("paint_all") is None:
            failed.append(f"{name}:paint")
        if name != "final" and result.get("paint_holdout") is None:
            failed.append(f"{name}:holdout")
        if result["signals"].get("valid") is not True:
            failed.append(f"{name}:signal_depth")
        if not math.isfinite(float(result["signals"]["rmse_px"])):
            failed.append(f"{name}:signals")
    if failed:
        raise SystemExit("registration fit failed closed: " + ",".join(failed))
    parameter_spread = np.ptp(np.asarray(fold_params), axis=0)

    parent = args.output_dir.resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".{args.output_dir.name}.{os.getpid()}.tmp"
    temporary.mkdir(mode=0o700)
    try:
        mask_path = temporary / "white-paint-mask.png"
        overlay_path = temporary / "candidate-overlay.png"
        if not cv2.imwrite(str(mask_path), observed_full * 255):
            raise RuntimeError("failed to write paint mask")
        layer = np.zeros_like(image)
        full_render = cv2.resize(
            final_rendered, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_NEAREST
        )
        layer[full_render > 0] = (255, 0, 255)
        overlay = cv2.addWeighted(image, 0.72, layer, 0.65, 0.0)
        if not cv2.imwrite(str(overlay_path), overlay):
            raise RuntimeError("failed to write candidate overlay")
        report = {
            "schema": "v2x-diagnostic-road-marking-registration/v1",
            "created_at": utc_now(),
            "acceptance_eligible": False,
            "camera": args.camera,
            "interpretation": "joint_visual_registration_hypothesis_not_camera_calibration",
            "warning": (
                "projection values absorb unknown map-revision/frame error and must "
                "not be used as physical camera parameters or deployed"
            ),
            "candidate_rank": args.rank,
            "seed": args.seed,
            "source_hashes": {name: sha256(path) for name, path in paths.items()},
            "base_signal_projection_hypothesis": base.tolist(),
            "registration_hypothesis": final,
            "spatial_leave_one_region_out": holdouts,
            "fold_projection_parameter_range": parameter_spread.tolist(),
            "parameter_order": ["x", "y", "z", "pitch", "yaw", "roll", "fov"],
            "frozen_parameters": ["x", "y", "z"],
            "map_revision_binding": {
                "deployed_geometry_opendrive_sha256": geometry["opendrive_sha256"],
                "reviewed_bundle_xodr_sha256": values["markings_json"]["bundle_binding"]["xodr_sha256"],
                "same_revision": (
                    geometry["opendrive_sha256"]
                    == values["markings_json"]["bundle_binding"]["xodr_sha256"]
                ),
                "coordinate_alignment_validated": False,
            },
            "paint_segmentation": {
                "color_space": "OpenCV HSV",
                "threshold": "saturation<65 and value>135",
                "downsampled_width": 320,
                "downsampled_height": 240,
                "white_pixel_count": int(np.count_nonzero(observed)),
            },
            "artifacts": {
                "white_paint_mask": {"file": mask_path.name, "sha256": sha256(mask_path)},
                "candidate_overlay": {"file": overlay_path.name, "sha256": sha256(overlay_path)},
            },
            "limitations": [
                "gltf_to_opendrive_transform_not_independently_validated",
                "white_threshold_retains_bright_non_paint_pixels",
                "robust_trim_discards_40_percent_of_observed_mask_pixels",
                "spatial_holdouts_share_the_same_frame_and_signal_constraints",
                "signal_amber_points_are_interpolation_checks_not_independent_holdouts",
                "camera_translation_is_frozen",
                "no_lens_distortion_model_is_fitted",
                "not_surveyed_and_not_deployable",
                "unlabeled_repeated_paint_topology_can_match_the_wrong_structure",
            ],
        }
        report_path = temporary / "diagnostic-fit.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        os.rename(temporary, args.output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(args.output_dir / "diagnostic-fit.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
