#!/usr/bin/env python3
"""Fit a diagnostic camera projection from deployed UE5 roadline evidence.

Unlike the reviewed-GLB registration tool, this consumes a retained depth
cloud generated from the exact deployed Richmond UE5 camera pair and filters
its RGB-threshold fallback points against the deployed lane center geometry.
It remains non-deployable: labels are not semantic truth, spatial folds share
one real frame, physical optics are unmeasured, and no surveyed holdout exists.
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

from fit_diagnostic_road_marking_registration import (  # noqa: E402
    optimize_candidate,
    paint_metrics,
    signal_correspondences,
    spatial_regions,
    white_paint_mask,
)
from fit_diagnostic_visual_calibration import project  # noqa: E402


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def render_roadline_points(points, _unused, params, width, height):
    pixels, depth = project(points, params, width, height)
    integer = np.rint(pixels).astype(np.int64)
    keep = (
        (depth > 0.1)
        & np.isfinite(pixels).all(axis=1)
        & (integer[:, 0] >= 0)
        & (integer[:, 0] < width)
        & (integer[:, 1] >= 0)
        & (integer[:, 1] < height)
    )
    mask = np.zeros((height, width), dtype=np.uint8)
    integer = integer[keep]
    mask[integer[:, 1], integer[:, 0]] = 1
    return cv2.dilate(mask, np.ones((2, 2), np.uint8))


def filter_points_to_deployed_lanes(points, geometry, margin_m=0.75):
    centers, half_widths = [], []
    for lane in geometry["geometry"]["lanes"]:
        width = float(lane["lane_width_m"])
        if not math.isfinite(width) or width <= 0:
            raise ValueError("deployed lane geometry has an invalid width")
        for value in lane["center_world"]:
            centers.append(value[:2])
            half_widths.append(width / 2.0)
    if len(centers) < 100:
        raise ValueError("deployed lane geometry has too few center samples")
    centers = np.asarray(centers, dtype=float)
    indices, distances = [], []
    for start in range(0, len(points), 1024):
        delta = points[start:start + 1024, None, :2] - centers[None, :, :]
        squared = np.sum(delta * delta, axis=2)
        index = np.argmin(squared, axis=1)
        indices.append(index)
        distances.append(np.sqrt(squared[np.arange(len(index)), index]))
    index = np.concatenate(indices)
    distance = np.concatenate(distances)
    keep = (
        np.isfinite(points).all(axis=1)
        & (points[:, 2] < 7.0)
        & (distance <= np.asarray(half_widths)[index] + margin_m)
    )
    return points[keep], {
        "input_count": int(len(points)),
        "retained_count": int(np.count_nonzero(keep)),
        "retained_fraction": float(np.mean(keep)),
        "lane_margin_m": margin_m,
        "maximum_world_z_m": 7.0,
        "nearest_lane_distance_m": {
            "median": float(np.median(distance)),
            "p95": float(np.quantile(distance, 0.95)),
            "max": float(np.max(distance)),
        },
    }


def validate_bindings(paths, values, cloud_metadata, camera):
    geometry = values["geometry"]
    observations = values["signal_observations"]
    search = values["candidate_search"]
    pair = values["pair_manifest"]
    geometry_hash = sha256(paths["geometry"])
    frame_hash = sha256(paths["real_frame"])
    twin_hash = sha256(paths["twin_frame"])
    checks = {
        "geometry_schema": geometry.get("schema") == "v2x-map-calibration-geometry/v1",
        "observation_schema": observations.get("schema") == "v2x-signal-hypothesis-observations/v1",
        "search_schema": search.get("schema") == "v2x-signal-hypothesis-search/v1",
        "pair_schema": pair.get("schema") == "v2x-observational-calibration-pairs/v1",
        "cloud_schema": cloud_metadata.get("schema") == "v2x-diagnostic-roadline-cloud/v1",
        "camera": all(value == camera for value in (
            observations.get("camera"), search.get("camera"), cloud_metadata.get("camera")
        )),
        "geometry_observations": observations.get("map_geometry_sha256") == geometry_hash,
        "geometry_search": search.get("geometry_sha256") == geometry_hash,
        "observation_search": search.get("observations_sha256") == sha256(paths["signal_observations"]),
        "real_frame_search": search.get("real_frame_sha256") == frame_hash,
        "real_frame_pair": pair["cameras"][camera]["real"].get("sha256") == frame_hash,
        "real_frame_geometry": geometry["cameras"][camera]["real"].get("frame_sha256") == frame_hash,
        "twin_frame_pair": pair["cameras"][camera]["twin"].get("sha256") == twin_hash,
        "twin_frame_cloud": cloud_metadata.get("twin_frame_sha256") == twin_hash,
        "pair_cloud": cloud_metadata.get("pair_manifest_sha256") == sha256(paths["pair_manifest"]),
        "pair_geometry": geometry.get("pair_manifest_sha256") == sha256(paths["pair_manifest"]),
        "camera_config_cloud": (
            cloud_metadata.get("camera_config_sha256")
            == geometry["cameras"][camera].get("camera_config_sha256")
        ),
        "cameras_file_cloud": (
            cloud_metadata.get("cameras_json_sha256") == geometry.get("cameras_file_sha256")
        ),
        "map_name": cloud_metadata.get("carla_map") == geometry.get("map"),
        "default_pinhole_lens": pair["cameras"][camera]["twin"]["camera_model"].get("lens") == {
            "lens_circle_falloff": 5.0,
            "lens_circle_multiplier": 0.0,
            "lens_k": -1.0,
            "lens_kcube": 0.0,
            "lens_x_size": 0.08,
            "lens_y_size": 0.08,
        },
    }
    diagnostic_values = (geometry, observations, search, cloud_metadata)
    checks["diagnostic_contract"] = all(
        value.get("acceptance_eligible") is False for value in diagnostic_values
    )
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"deployed roadline binding failed: {failed}")
    selected = search["results"][0]
    if (
        selected.get("optimizer_success") is not True
        or selected.get("boundary_hits")
        or selected.get("identity_underconstrained") is not False
    ):
        raise ValueError("base signal candidate is rejected or underconstrained")
    return checks


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geometry", type=Path, required=True)
    parser.add_argument("--signal-observations", type=Path, required=True)
    parser.add_argument("--candidate-search", type=Path, required=True)
    parser.add_argument("--roadline-cloud", type=Path, required=True)
    parser.add_argument("--pair-manifest", type=Path, required=True)
    parser.add_argument("--twin-frame", type=Path, required=True)
    parser.add_argument("--real-frame", type=Path, required=True)
    parser.add_argument("--camera", choices=("ch4",), required=True)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise SystemExit("refusing to overwrite deployed-roadline calibration evidence")
    paths = {
        "geometry": args.geometry.resolve(),
        "signal_observations": args.signal_observations.resolve(),
        "candidate_search": args.candidate_search.resolve(),
        "roadline_cloud": args.roadline_cloud.resolve(),
        "pair_manifest": args.pair_manifest.resolve(),
        "twin_frame": args.twin_frame.resolve(),
        "real_frame": args.real_frame.resolve(),
    }
    values = {
        name: json.loads(paths[name].read_bytes())
        for name in ("geometry", "signal_observations", "candidate_search", "pair_manifest")
    }
    arrays = np.load(paths["roadline_cloud"], allow_pickle=False)
    metadata = json.loads(str(arrays["metadata"]))
    try:
        checks = validate_bindings(paths, values, metadata, args.camera)
        points, point_filter = filter_points_to_deployed_lanes(
            np.asarray(arrays["world_xyz"], dtype=float), values["geometry"]
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if len(points) < 1_000:
        raise SystemExit("too few deployed roadline proposal points survive lane filtering")

    image = cv2.imread(str(paths["real_frame"]), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (480, 640):
        raise SystemExit("camera-4 retained frame must decode as 640x480")
    observed_full = white_paint_mask(image)
    observed = (cv2.resize(
        observed_full, (320, 240), interpolation=cv2.INTER_AREA
    ) > 0.35).astype(np.uint8)
    signal_world, signal_observed = signal_correspondences(
        values["geometry"], values["signal_observations"],
        values["candidate_search"], 1,
    )
    base = np.asarray(
        values["candidate_search"]["results"][0]["fitted_absolute"], dtype=float
    )
    final = optimize_candidate(
        base, points, None, observed, signal_world, signal_observed, None,
        args.seed, renderer=render_roadline_points,
    )
    regions = spatial_regions(320, 240)
    for name, region in regions.items():
        if paint_metrics(observed, observed, region) is None:
            raise SystemExit(f"{name}: insufficient observed paint for spatial fold")
    holdouts, fold_params = {}, []
    for index, (name, holdout_region) in enumerate(regions.items(), start=1):
        fold = optimize_candidate(
            base, points, None, observed, signal_world, signal_observed,
            1 - holdout_region, args.seed + index,
            renderer=render_roadline_points,
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
            failed.append(f"{name}:signals")
    if failed:
        raise SystemExit("deployed-roadline fit failed closed: " + ",".join(failed))

    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".{output_dir.name}.{os.getpid()}.tmp"
    temporary.mkdir(mode=0o700)
    try:
        mask_path = temporary / "white-paint-mask.png"
        overlay_path = temporary / "candidate-overlay.png"
        if not cv2.imwrite(str(mask_path), observed_full * 255):
            raise RuntimeError("failed to write paint mask")
        full_render = cv2.resize(
            final_rendered, (640, 480), interpolation=cv2.INTER_NEAREST
        )
        layer = np.zeros_like(image)
        layer[full_render > 0] = (255, 0, 255)
        if not cv2.imwrite(
            str(overlay_path), cv2.addWeighted(image, 0.72, layer, 0.65, 0.0)
        ):
            raise RuntimeError("failed to write overlay")
        report = {
            "schema": "v2x-diagnostic-deployed-roadline-camera-fit/v1",
            "created_at": utc_now(),
            "acceptance_eligible": False,
            "camera": args.camera,
            "source_hashes": {name: sha256(path) for name, path in paths.items()},
            "binding_checks": checks,
            "deployed_map": {
                "name": values["geometry"]["map"],
                "opendrive_sha256": values["geometry"]["opendrive_sha256"],
                "cloud_and_geometry_share_pair_manifest": True,
            },
            "roadline_source": {
                "proposal_source": metadata["proposal_source"],
                "semantic_road_line_pixel_count": metadata["semantic_road_line_pixel_count"],
                "semantic_unique_tags": metadata["semantic_unique_tags"],
                "filter": point_filter,
            },
            "base_signal_candidate": base.tolist(),
            "diagnostic_camera_projection": final,
            "spatial_leave_one_region_out": holdouts,
            "fold_projection_parameter_range": np.ptp(
                np.asarray(fold_params), axis=0
            ).tolist(),
            "parameter_order": ["x", "y", "z", "pitch", "yaw", "roll", "fov"],
            "frozen_parameters": ["x", "y", "z"],
            "artifacts": {
                "white_paint_mask": {"file": mask_path.name, "sha256": sha256(mask_path)},
                "candidate_overlay": {"file": overlay_path.name, "sha256": sha256(overlay_path)},
            },
            "limitations": [
                "custom_map_semantics_failed_and_rgb_threshold_fallback_was_used",
                "roadline_identity_and_topology_are_unreviewed",
                "single_real_frame_and_spatial_folds_are_not_independent_days",
                "physical_camera_intrinsics_and_distortion_are_unmeasured",
                "signal_pixels_are_model_proposals",
                "translation_is_frozen",
                "not_surveyed_and_not_deployable",
            ],
        }
        (temporary / "diagnostic-fit.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
        os.rename(temporary, output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(output_dir / "diagnostic-fit.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
