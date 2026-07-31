#!/usr/bin/env python3
"""Fit proposal-only camera candidates from retained real/twin visual anchors.

This intentionally cannot emit acceptance-eligible evidence.  It resolves
proposal twin pixels through a hash-bound diagnostic depth cloud, optimizes a
bounded subset of the absolute UE5 camera model, evaluates frozen proposal
holdouts, and writes overlays plus candidate ``twin_pose`` values without
modifying the tracked or live camera configuration.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np


CAMERA_FREE_PARAMETERS = {
    # Independent per-camera translation is deliberately forbidden here. It
    # overfit the proposal lines while destroying the physical camera-cluster
    # geometry. Use fit_joint_diagnostic_visual_calibration.py when a bounded
    # common XYZ correction is diagnostically justified.
    "ch1": (3, 4, 5, 6),
    "ch2": (3, 4, 5, 6),
    "ch3": (3, 4, 5, 6),
    "ch4": (3, 4, 5, 6),
}
PARAMETER_NAMES = (
    "location_x", "location_y", "location_z",
    "pitch_deg", "yaw_deg", "roll_deg", "fov_deg",
)
PRIOR_SCALES = np.asarray((1.5, 1.5, 1.5, 6.0, 6.0, 3.0, 5.0))
LOWER_DELTAS = np.asarray((-8.0, -8.0, -5.0, -45.0, -60.0, -25.0, -35.0))
UPPER_DELTAS = np.asarray((8.0, 8.0, 5.0, 45.0, 60.0, 25.0, 35.0))


def rotation_matrix(pitch_deg, yaw_deg, roll_deg):
    pitch, yaw, roll = np.radians([pitch_deg, yaw_deg, roll_deg])
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    return np.array([
        [cp * cy, cy * sp * sr - sy * cr, -cy * sp * cr - sy * sr],
        [cp * sy, sy * sp * sr + cy * cr, -sy * sp * cr + cy * sr],
        [sp, -cp * sr, cp * cr],
    ])


def project(world_xyz, params, width, height):
    rotation = rotation_matrix(params[3], params[4], params[5])
    local = (rotation.T @ (world_xyz - params[:3]).T).T
    depth = local[:, 0]
    focal = (width / 2.0) / math.tan(math.radians(params[6]) / 2.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        pixels = np.column_stack((
            width / 2.0 + focal * local[:, 1] / depth,
            height / 2.0 - focal * local[:, 2] / depth,
        ))
    return pixels, depth


def nearest_world_points(cloud, twin_uv):
    grid_uv = cloud["grid_uv"]
    grid_world = cloud["grid_world_xyz"]
    # The grid is regular but a direct nearest lookup keeps the artifact format
    # robust if future collectors omit invalid/far pixels.
    points, distances = [], []
    for uv in twin_uv:
        squared = np.sum((grid_uv - uv) ** 2, axis=1)
        index = int(np.argmin(squared))
        points.append(grid_world[index])
        distances.append(math.sqrt(float(squared[index])))
    return np.asarray(points), np.asarray(distances)


def line_world_samples(cloud, annotations, samples_per_line=24):
    groups = []
    for item in annotations:
        vertices = np.asarray(item["twin_polyline"], dtype=float)
        if len(vertices) != 2:
            raise ValueError("diagnostic line proposals currently require two vertices")
        values = np.linspace(0.0, 1.0, samples_per_line)
        twin_uv = vertices[0] + values[:, None] * (vertices[1] - vertices[0])
        world, lookup = nearest_world_points(cloud, twin_uv)
        real = np.asarray(item["real_polyline"], dtype=float)
        direction = real[1] - real[0]
        norm = float(np.linalg.norm(direction))
        if norm < 1.0:
            raise ValueError(f"{item['id']}: real line is degenerate")
        normal = np.asarray((direction[1], -direction[0])) / norm
        groups.append({
            "annotation": item,
            "world_xyz": world,
            "real_origin": real[0],
            "real_end": real[1],
            "real_normal": normal,
            "lookup_max_px": float(np.max(lookup)),
        })
    return groups


def line_residual_values(groups, params, width, height, split=None):
    values = []
    for group in groups:
        item = group["annotation"]
        if split is not None and item["split"] != split:
            continue
        pixels, depth = project(group["world_xyz"], params, width, height)
        if item.get("bounded") is True:
            segment = group["real_end"] - group["real_origin"]
            length_squared = float(segment @ segment)
            along = np.clip(
                ((pixels - group["real_origin"]) @ segment) / length_squared,
                0.0,
                1.0,
            )
            closest = group["real_origin"] + along[:, None] * segment
            distance = np.linalg.norm(pixels - closest, axis=1)
        else:
            distance = (pixels - group["real_origin"]) @ group["real_normal"]
        distance[(depth <= 0.25) | ~np.isfinite(distance)] = 1000.0
        values.extend(distance.tolist())
    return np.asarray(values, dtype=float)


def line_metrics(groups, params, width, height):
    result = {}
    for split in ("fit", "holdout", "all"):
        selected = None if split == "all" else split
        values = np.abs(line_residual_values(groups, params, width, height, selected))
        result[split] = None if not len(values) else {
            "sample_count": int(len(values)),
            "rmse_px": float(math.sqrt(np.mean(values ** 2))),
            "median_px": float(np.median(values)),
            "max_px": float(np.max(values)),
        }
    return result


def metrics(observed, predicted, splits):
    errors = np.linalg.norm(predicted - observed, axis=1)
    result = {}
    for split in ("fit", "holdout", "all"):
        mask = np.ones(len(errors), dtype=bool) if split == "all" else splits == split
        values = errors[mask]
        result[split] = None if not len(values) else {
            "count": int(len(values)),
            "rmse_px": float(math.sqrt(np.mean(values ** 2))),
            "median_px": float(np.median(values)),
            "max_px": float(np.max(values)),
        }
    return result, errors


def candidate_twin_pose(camera, baseline, fitted):
    pose = dict(camera.get("twin_pose") or {})
    current_forward = float(pose.get("forward_offset_m", 0.0))
    current_right = float(pose.get("right_offset_m", 0.0))
    current_height = float(pose.get("height_offset_m", 0.0))
    yaw = math.radians(float(baseline[4]))
    anchor = np.asarray((
        baseline[0] - current_forward * math.cos(yaw) + current_right * math.sin(yaw),
        baseline[1] - current_forward * math.sin(yaw) - current_right * math.cos(yaw),
        baseline[2] - current_height,
    ))
    base_pitch = baseline[3] - float(pose.get("pitch_offset_deg", 0.0))
    base_yaw = baseline[4] - float(pose.get("yaw_offset_deg", 0.0))
    base_roll = baseline[5] - float(pose.get("roll_offset_deg", 0.0))
    base_fov = baseline[6] - float(pose.get("fov_offset_deg", 0.0))
    fitted_yaw = math.radians(float(fitted[4]))
    delta_x, delta_y = fitted[0] - anchor[0], fitted[1] - anchor[1]
    return {
        "forward_offset_m": float(delta_x * math.cos(fitted_yaw) + delta_y * math.sin(fitted_yaw)),
        "right_offset_m": float(-delta_x * math.sin(fitted_yaw) + delta_y * math.cos(fitted_yaw)),
        "height_offset_m": float(fitted[2] - anchor[2]),
        "pitch_offset_deg": float(fitted[3] - base_pitch),
        "yaw_offset_deg": float((fitted[4] - base_yaw + 180.0) % 360.0 - 180.0),
        "roll_offset_deg": float(fitted[5] - base_roll),
        "fov_offset_deg": float(fitted[6] - base_fov),
    }


def fit_camera(
    camera_id, annotations, line_annotations, cloud_path, real_path,
    camera_config, output_dir, expected,
):
    from scipy.optimize import least_squares

    cloud_bytes = cloud_path.read_bytes()
    cloud_sha256 = hashlib.sha256(cloud_bytes).hexdigest()
    cloud = np.load(cloud_path, allow_pickle=False)
    metadata = json.loads(str(cloud["metadata"]))
    binding_checks = {
        "camera": metadata.get("camera") == camera_id,
        "pair_manifest": metadata.get("pair_manifest_sha256") == expected["pair_sha256"],
        "twin_frame": metadata.get("twin_frame_sha256") == expected["twin_sha256"],
        "camera_config": metadata.get("camera_config_sha256") == expected["camera_config_sha256"],
        "cameras_json": metadata.get("cameras_json_sha256") == expected["cameras_json_sha256"],
        "width": metadata.get("image_width") == expected["twin_width"],
        "height": metadata.get("image_height") == expected["twin_height"],
        "fov": math.isclose(
            float(metadata.get("horizontal_fov_deg", math.nan)),
            float(expected["twin_fov"]), rel_tol=0.0, abs_tol=0.01,
        ),
    }
    if not all(binding_checks.values()):
        failed = [key for key, passed in binding_checks.items() if not passed]
        raise ValueError(f"{camera_id}: cloud binding failed: {failed}")
    if not len(cloud["grid_uv"]) or not len(cloud["grid_world_xyz"]):
        raise ValueError(f"{camera_id}: depth cloud is empty")
    baseline = np.asarray([
        *metadata["baseline_transform"]["location"],
        *metadata["baseline_transform"]["rotation"],
        metadata["horizontal_fov_deg"],
    ], dtype=float)
    image = cv2.imread(str(real_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"cannot decode {real_path}")
    height, width = image.shape[:2]
    for item in annotations:
        values = [*item.get("twin_uv", []), *item.get("real_uv", [])]
        if len(values) != 4 or not all(math.isfinite(float(value)) for value in values):
            raise ValueError(f"{camera_id}: invalid point proposal")
        if item.get("split") not in {"fit", "holdout"}:
            raise ValueError(f"{camera_id}: invalid point split")
        if not (0 <= values[0] < expected["twin_width"] and 0 <= values[1] < expected["twin_height"]):
            raise ValueError(f"{camera_id}: twin point outside retained frame")
        if not (0 <= values[2] < width and 0 <= values[3] < height):
            raise ValueError(f"{camera_id}: real point outside retained frame")
    for item in line_annotations:
        if item.get("split") not in {"fit", "holdout"}:
            raise ValueError(f"{camera_id}: invalid line split")
        twin_vertices = item.get("twin_polyline")
        real_vertices = item.get("real_polyline")
        if not (
            isinstance(twin_vertices, list) and len(twin_vertices) == 2
            and isinstance(real_vertices, list) and len(real_vertices) == 2
        ):
            raise ValueError(f"{camera_id}: line proposal must have two vertices")
        for x, y in twin_vertices:
            if not (
                math.isfinite(float(x)) and math.isfinite(float(y))
                and 0 <= x < expected["twin_width"]
                and 0 <= y < expected["twin_height"]
            ):
                raise ValueError(f"{camera_id}: twin line outside retained frame")
        for x, y in real_vertices:
            if not (
                math.isfinite(float(x)) and math.isfinite(float(y))
                and 0 <= x < width and 0 <= y < height
            ):
                raise ValueError(f"{camera_id}: real line outside retained frame")
    twin_uv = np.asarray([item["twin_uv"] for item in annotations], dtype=float).reshape(-1, 2)
    observed = np.asarray([item["real_uv"] for item in annotations], dtype=float).reshape(-1, 2)
    uncertainty = np.asarray([item["uncertainty_px"] for item in annotations], dtype=float)
    splits = np.asarray([item["split"] for item in annotations])
    world_xyz, lookup_error = nearest_world_points(cloud, twin_uv) if len(twin_uv) else (np.empty((0, 3)), np.empty(0))
    line_groups = line_world_samples(cloud, line_annotations)
    free = CAMERA_FREE_PARAMETERS[camera_id]
    fitted = baseline.copy()
    optimizer_success = False
    fit_mask = splits == "fit"

    fit_constraint_count = 2 * int(np.count_nonzero(fit_mask)) + sum(
        24 for group in line_groups if group["annotation"]["split"] == "fit"
    )
    if free and fit_constraint_count >= len(free):
        free = np.asarray(free, dtype=int)
        lower, upper = baseline[free] + LOWER_DELTAS[free], baseline[free] + UPPER_DELTAS[free]

        def expand(values):
            absolute = baseline.copy()
            absolute[free] = values
            return absolute

        def residual(values):
            absolute = expand(values)
            if np.count_nonzero(fit_mask):
                pixels, depth = project(world_xyz[fit_mask], absolute, width, height)
                reprojection = ((pixels - observed[fit_mask]) / uncertainty[fit_mask, None]).ravel()
                invalid = (~np.isfinite(reprojection)) | (np.repeat(depth <= 0.25, 2))
                reprojection[invalid] = 100.0
            else:
                reprojection = np.empty(0)
            line_values = []
            for group in line_groups:
                item = group["annotation"]
                if item["split"] != "fit":
                    continue
                pixels, depth = project(group["world_xyz"], absolute, width, height)
                if item.get("bounded") is True:
                    segment = group["real_end"] - group["real_origin"]
                    length_squared = float(segment @ segment)
                    along = np.clip(
                        ((pixels - group["real_origin"]) @ segment) / length_squared,
                        0.0,
                        1.0,
                    )
                    closest = group["real_origin"] + along[:, None] * segment
                    distance = np.linalg.norm(pixels - closest, axis=1)
                else:
                    distance = (pixels - group["real_origin"]) @ group["real_normal"]
                distance[(depth <= 0.25) | ~np.isfinite(distance)] = 1000.0
                line_values.extend((distance / float(item["uncertainty_px"])).tolist())
            # A weak initialization prior regularizes intrinsically correlated
            # height/translation/FOV axes; it never contributes to reported
            # image metrics.
            prior = (values - baseline[free]) / PRIOR_SCALES[free]
            return np.concatenate((reprojection, np.asarray(line_values), 0.2 * prior))

        seeds = [baseline[free]]
        rng = np.random.default_rng(20260711 + int(camera_id[-1]))
        for _ in range(32):
            seeds.append(rng.uniform(lower, upper))
        solutions = [least_squares(
            residual, seed, bounds=(lower, upper), loss="soft_l1", f_scale=1.0,
            max_nfev=3000,
        ) for seed in seeds]
        solution = min(solutions, key=lambda item: float(np.sum(residual(item.x) ** 2)))
        fitted = expand(solution.x)
        visual_jacobian = solution.jac[:-len(free), :]
        singular = np.linalg.svd(visual_jacobian, compute_uv=False)
        rank = int(np.linalg.matrix_rank(visual_jacobian))
        condition = None if not len(singular) or singular[-1] <= 1e-12 else float(singular[0] / singular[-1])
        optimizer_success = bool(solution.success)
    else:
        rank, condition = 0, None

    baseline_pixels, _ = project(world_xyz, baseline, width, height)
    fitted_pixels, depth = project(world_xyz, fitted, width, height)
    baseline_metrics, baseline_errors = metrics(observed, baseline_pixels, splits) if len(observed) else ({"fit": None, "holdout": None, "all": None}, np.empty(0))
    fitted_metrics, fitted_errors = metrics(observed, fitted_pixels, splits) if len(observed) else ({"fit": None, "holdout": None, "all": None}, np.empty(0))
    baseline_line_metrics = line_metrics(line_groups, baseline, width, height)
    fitted_line_metrics = line_metrics(line_groups, fitted, width, height)
    boundary_hits = []
    for index in free:
        delta = fitted[index] - baseline[index]
        if (
            abs(delta - LOWER_DELTAS[index]) <= 1e-4
            or abs(delta - UPPER_DELTAS[index]) <= 1e-4
        ):
            boundary_hits.append(PARAMETER_NAMES[index])
    holdout_comparisons = []
    for before, after in (
        (baseline_metrics.get("holdout"), fitted_metrics.get("holdout")),
        (baseline_line_metrics.get("holdout"), fitted_line_metrics.get("holdout")),
    ):
        if before is not None and after is not None:
            holdout_comparisons.append(after["rmse_px"] <= before["rmse_px"])
    proposal_holdout_improved = bool(holdout_comparisons) and all(holdout_comparisons)
    absolute_holdout_passes = []
    if fitted_metrics.get("holdout") is not None:
        absolute_holdout_passes.append(fitted_metrics["holdout"]["rmse_px"] <= 30.0)
    if fitted_line_metrics.get("holdout") is not None:
        absolute_holdout_passes.append(fitted_line_metrics["holdout"]["rmse_px"] <= 15.0)
    lookup_values = [float(value) for value in lookup_error]
    lookup_values.extend(group["lookup_max_px"] for group in line_groups)
    lookup_passed = bool(lookup_values) and max(lookup_values) <= 3.0
    diagnostic_candidate_passed = (
        optimizer_success
        and rank == len(free)
        and condition is not None
        and condition <= 100_000.0
        and not boundary_hits
        and proposal_holdout_improved
        and bool(absolute_holdout_passes)
        and all(absolute_holdout_passes)
        and lookup_passed
    )

    overlay = image.copy()
    road_xyz = cloud["world_xyz"][::max(1, len(cloud["world_xyz"]) // 6000)]
    before_road, before_depth = project(road_xyz, baseline, width, height)
    after_road, after_depth = project(road_xyz, fitted, width, height)
    for pixels, depths, color in ((before_road, before_depth, (255, 0, 255)), (after_road, after_depth, (0, 255, 0))):
        valid = (
            (depths > 0.25) & np.isfinite(pixels).all(axis=1)
            & (pixels[:, 0] >= 0) & (pixels[:, 0] < width)
            & (pixels[:, 1] >= 0) & (pixels[:, 1] < height)
        )
        for point in pixels[valid].astype(int):
            overlay[point[1], point[0]] = color
    for index, item in enumerate(annotations):
        actual = tuple(np.rint(observed[index]).astype(int))
        before = tuple(np.rint(baseline_pixels[index]).astype(int))
        after = tuple(np.rint(fitted_pixels[index]).astype(int))
        cv2.circle(overlay, actual, 5, (0, 255, 255), 2)
        cv2.circle(overlay, before, 4, (0, 0, 255), 2)
        cv2.circle(overlay, after, 4, (0, 255, 0), 2)
        cv2.putText(overlay, str(index + 1), actual, cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1, cv2.LINE_AA)
    overlay_path = output_dir / f"{camera_id}-diagnostic-overlay.jpg"
    cv2.imwrite(str(overlay_path), overlay)

    return {
        "camera": camera_id,
        "acceptance_eligible": False,
        "cloud_sha256": cloud_sha256,
        "cloud_binding_checks": binding_checks,
        "free_parameters": [PARAMETER_NAMES[index] for index in free],
        "anchor_count": len(annotations),
        "line_count": len(line_annotations),
        "depth_lookup_max_px": float(np.max(lookup_error)) if len(lookup_error) else None,
        "line_depth_lookup_max_px": max((group["lookup_max_px"] for group in line_groups), default=None),
        "baseline_absolute": dict(zip(PARAMETER_NAMES, (float(value) for value in baseline))),
        "fitted_absolute": dict(zip(PARAMETER_NAMES, (float(value) for value in fitted))),
        "delta_absolute": dict(zip(PARAMETER_NAMES, (float(value) for value in fitted - baseline))),
        "candidate_twin_pose": candidate_twin_pose(camera_config, baseline, fitted),
        "baseline_metrics": baseline_metrics,
        "fitted_metrics": fitted_metrics,
        "baseline_line_metrics": baseline_line_metrics,
        "fitted_line_metrics": fitted_line_metrics,
        "jacobian_rank": rank,
        "required_jacobian_rank": len(free),
        "jacobian_condition": condition,
        "optimizer_success": optimizer_success,
        "depth_lookup_passed": lookup_passed,
        "boundary_hits": boundary_hits,
        "proposal_holdout_improved": proposal_holdout_improved,
        "candidate_recommendation": (
            "continue_offline_render_review"
            if diagnostic_candidate_passed
            else "reject_or_expand_evidence"
        ),
        "overlay": overlay_path.name,
        "anchors": [{
            **item,
            "world_xyz": [float(value) for value in world_xyz[index]],
            "baseline_error_px": float(baseline_errors[index]),
            "fitted_error_px": float(fitted_errors[index]),
            "fitted_depth_m": float(depth[index]),
        } for index, item in enumerate(annotations)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchors", required=True)
    parser.add_argument("--pair-manifest", required=True)
    parser.add_argument("--cloud-dir", required=True)
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    anchors_path = Path(args.anchors).resolve()
    anchors_bytes = anchors_path.read_bytes()
    anchors = json.loads(anchors_bytes)
    if anchors.get("schema") != "v2x-diagnostic-anchor-proposals/v1" or anchors.get("acceptance_eligible") is not False:
        raise SystemExit("refusing anchors without the diagnostic proposal contract")
    pair_path = Path(args.pair_manifest).resolve()
    pair_bytes = pair_path.read_bytes()
    pair = json.loads(pair_bytes)
    cameras_path = Path(args.cameras_json).resolve()
    cameras_bytes = cameras_path.read_bytes()
    cameras = {item["id"]: item for item in json.loads(cameras_bytes)["cameras"]}
    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit("refusing to overwrite an existing calibration report directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for camera_id, camera_anchors in anchors["cameras"].items():
        camera_lines = (anchors.get("lines") or {}).get(camera_id, [])
        if not camera_anchors and not camera_lines:
            results[camera_id] = {
                "camera": camera_id,
                "acceptance_eligible": False,
                "status": "not_fitted",
                "reason": "no unique point anchors; use reduced-DOF polyline fit",
            }
            continue
        real = pair["cameras"][camera_id]["real"]
        real_path = pair_path.parent / real["file"]
        if hashlib.sha256(real_path.read_bytes()).hexdigest() != real["sha256"]:
            raise SystemExit(f"{camera_id}: retained real frame hash mismatch")
        cloud_path = Path(args.cloud_dir).resolve() / f"{camera_id}-roadline-cloud.npz"
        twin_model = pair["cameras"][camera_id]["twin"]["camera_model"]
        expected = {
            "pair_sha256": hashlib.sha256(pair_bytes).hexdigest(),
            "twin_sha256": pair["cameras"][camera_id]["twin"]["sha256"],
            "camera_config_sha256": pair["cameras"][camera_id]["twin"]["camera_config_sha256"],
            "cameras_json_sha256": hashlib.sha256(cameras_bytes).hexdigest(),
            "twin_width": int(twin_model["image"]["width"]),
            "twin_height": int(twin_model["image"]["height"]),
            "twin_fov": float(twin_model["image"]["horizontal_fov_deg"]),
        }
        results[camera_id] = fit_camera(
            camera_id, camera_anchors, camera_lines, cloud_path, real_path,
            cameras[camera_id], output_dir, expected,
        )
    report = {
        "schema": "v2x-diagnostic-visual-calibration/v1",
        "acceptance_eligible": False,
        "anchors_sha256": hashlib.sha256(anchors_bytes).hexdigest(),
        "pair_manifest_sha256": hashlib.sha256(pair_bytes).hexdigest(),
        "cameras_json_sha256": hashlib.sha256(cameras_bytes).hexdigest(),
        "warning": "candidate-only fit; independent held-out acceptance remains required",
        "cameras": results,
    }
    output_path = output_dir / "diagnostic-calibration.json"
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
