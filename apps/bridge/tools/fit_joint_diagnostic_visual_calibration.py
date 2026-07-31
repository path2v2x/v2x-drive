#!/usr/bin/env python3
"""Fit a proposal-only four-camera rig while preserving cluster geometry.

The model permits exactly one shared XYZ translation for the complete camera
cluster and independent bounded pitch/yaw/roll/FOV deltas per camera.  It never
emits acceptance-eligible output: its visual anchors and depth clouds are
diagnostic proposals, and an independent map/survey/holdout gate is still
required before deployment.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np

from fit_diagnostic_visual_calibration import (
    PARAMETER_NAMES,
    candidate_twin_pose,
    line_metrics,
    line_world_samples,
    metrics,
    nearest_world_points,
    project,
)


CAMERA_IDS = ("ch1", "ch2", "ch3", "ch4")
SHARED_LOWER = np.asarray((-3.0, -3.0, -2.0))
SHARED_UPPER = np.asarray((3.0, 3.0, 2.0))
PER_CAMERA_LOWER = np.asarray((-20.0, -30.0, -12.0, -20.0))
PER_CAMERA_UPPER = np.asarray((20.0, 30.0, 12.0, 20.0))
PRIOR_SCALES = np.asarray((1.5, 1.5, 1.0) + (6.0, 8.0, 3.0, 5.0) * 4)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parameter_names(camera_ids=CAMERA_IDS):
    names = ["shared_location_x", "shared_location_y", "shared_location_z"]
    for camera_id in camera_ids:
        names.extend(
            f"{camera_id}_{name}"
            for name in ("pitch_deg", "yaw_deg", "roll_deg", "fov_deg")
        )
    return tuple(names)


def bounds(camera_ids=CAMERA_IDS):
    lower = [*SHARED_LOWER]
    upper = [*SHARED_UPPER]
    for _ in camera_ids:
        lower.extend(PER_CAMERA_LOWER)
        upper.extend(PER_CAMERA_UPPER)
    return np.asarray(lower), np.asarray(upper)


def expand_joint(values, baselines, camera_ids=CAMERA_IDS):
    """Convert joint delta parameters into an absolute 7-vector per camera."""
    values = np.asarray(values, dtype=float)
    expected = 3 + 4 * len(camera_ids)
    if values.shape != (expected,):
        raise ValueError(f"expected {expected} joint parameters")
    result = {}
    for index, camera_id in enumerate(camera_ids):
        absolute = np.asarray(baselines[camera_id], dtype=float).copy()
        absolute[:3] += values[:3]
        absolute[3:7] += values[3 + 4 * index:7 + 4 * index]
        result[camera_id] = absolute
    return result


def finite_difference_jacobian(function, values, lower, upper):
    """Numerically differentiate only the visual residuals, excluding priors."""
    values = np.asarray(values, dtype=float)
    base = function(values)
    jacobian = np.empty((len(base), len(values)), dtype=float)
    for index, value in enumerate(values):
        step = 1e-5 * max(1.0, abs(float(value)))
        left = values.copy()
        right = values.copy()
        left[index] = max(lower[index], value - step)
        right[index] = min(upper[index], value + step)
        denominator = right[index] - left[index]
        if denominator <= 0:
            jacobian[:, index] = 0.0
        else:
            jacobian[:, index] = (function(right) - function(left)) / denominator
    return jacobian


def _validate_points(camera_id, annotations, twin_width, twin_height, width, height):
    for item in annotations:
        if item.get("split") not in {"fit", "holdout"}:
            raise ValueError(f"{camera_id}: invalid point split")
        values = [*item.get("twin_uv", []), *item.get("real_uv", [])]
        if len(values) != 4 or not all(math.isfinite(float(value)) for value in values):
            raise ValueError(f"{camera_id}: invalid point proposal")
        if not (0 <= values[0] < twin_width and 0 <= values[1] < twin_height):
            raise ValueError(f"{camera_id}: twin point outside retained frame")
        if not (0 <= values[2] < width and 0 <= values[3] < height):
            raise ValueError(f"{camera_id}: real point outside retained frame")
        if not math.isfinite(float(item.get("uncertainty_px", math.nan))) or item["uncertainty_px"] <= 0:
            raise ValueError(f"{camera_id}: invalid point uncertainty")


def _validate_lines(camera_id, annotations, twin_width, twin_height, width, height):
    for item in annotations:
        if item.get("split") not in {"fit", "holdout"}:
            raise ValueError(f"{camera_id}: invalid line split")
        if float(item.get("uncertainty_px", 0)) <= 0:
            raise ValueError(f"{camera_id}: invalid line uncertainty")
        for key, max_x, max_y in (
            ("twin_polyline", twin_width, twin_height),
            ("real_polyline", width, height),
        ):
            vertices = item.get(key)
            if not isinstance(vertices, list) or len(vertices) != 2:
                raise ValueError(f"{camera_id}: line proposal must have two vertices")
            for x, y in vertices:
                if not (
                    math.isfinite(float(x)) and math.isfinite(float(y))
                    and 0 <= x < max_x and 0 <= y < max_y
                ):
                    raise ValueError(f"{camera_id}: {key} outside retained frame")


def load_camera_data(
    camera_id, camera_anchors, camera_lines, pair, pair_path, pair_sha,
    cloud_dir, cameras, cameras_sha,
):
    real = pair["cameras"][camera_id]["real"]
    real_path = pair_path.parent / real["file"]
    if sha256(real_path) != real["sha256"]:
        raise ValueError(f"{camera_id}: retained real frame hash mismatch")
    image = cv2.imread(str(real_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"{camera_id}: cannot decode retained real frame")
    height, width = image.shape[:2]
    twin = pair["cameras"][camera_id]["twin"]
    model = twin["camera_model"]["image"]
    twin_width, twin_height = int(model["width"]), int(model["height"])
    _validate_points(camera_id, camera_anchors, twin_width, twin_height, width, height)
    _validate_lines(camera_id, camera_lines, twin_width, twin_height, width, height)

    cloud_path = cloud_dir / f"{camera_id}-roadline-cloud.npz"
    cloud_sha = sha256(cloud_path)
    cloud = np.load(cloud_path, allow_pickle=False)
    metadata = json.loads(str(cloud["metadata"]))
    binding = {
        "camera": metadata.get("camera") == camera_id,
        "pair_manifest": metadata.get("pair_manifest_sha256") == pair_sha,
        "twin_frame": metadata.get("twin_frame_sha256") == twin["sha256"],
        "camera_config": metadata.get("camera_config_sha256") == twin["camera_config_sha256"],
        "cameras_json": metadata.get("cameras_json_sha256") == cameras_sha,
        "width": metadata.get("image_width") == twin_width,
        "height": metadata.get("image_height") == twin_height,
        "fov": math.isclose(
            float(metadata.get("horizontal_fov_deg", math.nan)),
            float(model["horizontal_fov_deg"]), rel_tol=0.0, abs_tol=0.01,
        ),
    }
    if not all(binding.values()):
        raise ValueError(
            f"{camera_id}: cloud binding failed: "
            f"{[name for name, passed in binding.items() if not passed]}"
        )
    if not len(cloud["grid_uv"]) or not len(cloud["grid_world_xyz"]):
        raise ValueError(f"{camera_id}: depth cloud is empty")

    baseline = np.asarray([
        *metadata["baseline_transform"]["location"],
        *metadata["baseline_transform"]["rotation"],
        metadata["horizontal_fov_deg"],
    ], dtype=float)
    twin_uv = np.asarray([item["twin_uv"] for item in camera_anchors], dtype=float).reshape(-1, 2)
    observed = np.asarray([item["real_uv"] for item in camera_anchors], dtype=float).reshape(-1, 2)
    uncertainty = np.asarray([item["uncertainty_px"] for item in camera_anchors], dtype=float)
    splits = np.asarray([item["split"] for item in camera_anchors])
    if len(twin_uv):
        world_xyz, lookup = nearest_world_points(cloud, twin_uv)
    else:
        world_xyz, lookup = np.empty((0, 3)), np.empty(0)
    line_groups = line_world_samples(cloud, camera_lines)
    return {
        "id": camera_id,
        "anchors": camera_anchors,
        "lines": camera_lines,
        "image": image,
        "width": width,
        "height": height,
        "cloud": cloud,
        "cloud_sha256": cloud_sha,
        "binding": binding,
        "baseline": baseline,
        "camera_config": cameras[camera_id],
        "world_xyz": world_xyz,
        "observed": observed,
        "uncertainty": uncertainty,
        "splits": splits,
        "lookup": lookup,
        "line_groups": line_groups,
    }


def camera_residual(data, absolute, split="fit"):
    mask = data["splits"] == split
    values = []
    if np.count_nonzero(mask):
        pixels, depth = project(
            data["world_xyz"][mask], absolute, data["width"], data["height"]
        )
        point = ((pixels - data["observed"][mask]) / data["uncertainty"][mask, None]).ravel()
        invalid = (~np.isfinite(point)) | np.repeat(depth <= 0.25, 2)
        point[invalid] = 100.0
        values.extend(point.tolist())
    for group in data["line_groups"]:
        item = group["annotation"]
        if item["split"] != split:
            continue
        pixels, depth = project(group["world_xyz"], absolute, data["width"], data["height"])
        if item.get("bounded") is True:
            segment = group["real_end"] - group["real_origin"]
            length_squared = float(segment @ segment)
            along = np.clip(
                ((pixels - group["real_origin"]) @ segment) / length_squared,
                0.0, 1.0,
            )
            closest = group["real_origin"] + along[:, None] * segment
            distance = np.linalg.norm(pixels - closest, axis=1)
        else:
            distance = (pixels - group["real_origin"]) @ group["real_normal"]
        distance[(depth <= 0.25) | ~np.isfinite(distance)] = 1000.0
        values.extend((distance / float(item["uncertainty_px"])).tolist())
    return np.asarray(values, dtype=float)


def fit_joint(data_by_camera, seed_count=48):
    from scipy.optimize import least_squares

    camera_ids = tuple(data_by_camera)
    baselines = {camera_id: data_by_camera[camera_id]["baseline"] for camera_id in camera_ids}
    lower, upper = bounds(camera_ids)
    initial = np.zeros(len(lower), dtype=float)

    def visual(values):
        absolute = expand_joint(values, baselines, camera_ids)
        return np.concatenate([
            camera_residual(data_by_camera[camera_id], absolute[camera_id], "fit")
            for camera_id in camera_ids
        ])

    def residual(values):
        return np.concatenate((visual(values), 0.2 * values / PRIOR_SCALES[:len(values)]))

    if len(visual(initial)) < len(initial):
        raise ValueError("joint rig has fewer fit constraints than parameters")
    seeds = [initial]
    rng = np.random.default_rng(20260712)
    for _ in range(seed_count):
        seeds.append(rng.uniform(lower, upper))
    solutions = [least_squares(
        residual, seed, bounds=(lower, upper), loss="soft_l1", f_scale=1.0,
        max_nfev=5000,
    ) for seed in seeds]
    solution = min(solutions, key=lambda item: float(np.sum(residual(item.x) ** 2)))
    visual_jacobian = finite_difference_jacobian(visual, solution.x, lower, upper)
    singular = np.linalg.svd(visual_jacobian, compute_uv=False)
    rank = int(np.linalg.matrix_rank(visual_jacobian))
    condition = None if not len(singular) or singular[-1] <= 1e-12 else float(singular[0] / singular[-1])
    return {
        "values": solution.x,
        "absolute": expand_joint(solution.x, baselines, camera_ids),
        "success": bool(solution.success),
        "cost": float(np.sum(visual(solution.x) ** 2)),
        "rank": rank,
        "condition": condition,
        "lower": lower,
        "upper": upper,
        "visual_residual_count": int(len(visual(solution.x))),
    }


def render_overlay(data, baseline, fitted, output_path):
    overlay = data["image"].copy()
    cloud_xyz = data["cloud"]["world_xyz"]
    road_xyz = cloud_xyz[::max(1, len(cloud_xyz) // 6000)]
    for absolute, color in ((baseline, (255, 0, 255)), (fitted, (0, 255, 0))):
        pixels, depth = project(road_xyz, absolute, data["width"], data["height"])
        valid = (
            (depth > 0.25) & np.isfinite(pixels).all(axis=1)
            & (pixels[:, 0] >= 0) & (pixels[:, 0] < data["width"])
            & (pixels[:, 1] >= 0) & (pixels[:, 1] < data["height"])
        )
        for point in pixels[valid].astype(int):
            overlay[point[1], point[0]] = color
    if len(data["observed"]):
        before, _ = project(data["world_xyz"], baseline, data["width"], data["height"])
        after, _ = project(data["world_xyz"], fitted, data["width"], data["height"])
        for index, actual in enumerate(data["observed"]):
            cv2.circle(overlay, tuple(np.rint(actual).astype(int)), 5, (0, 255, 255), 2)
            cv2.circle(overlay, tuple(np.rint(before[index]).astype(int)), 4, (0, 0, 255), 2)
            cv2.circle(overlay, tuple(np.rint(after[index]).astype(int)), 4, (0, 255, 0), 2)
    if not cv2.imwrite(str(output_path), overlay):
        raise RuntimeError(f"failed to write {output_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchors", required=True)
    parser.add_argument("--pair-manifest", required=True)
    parser.add_argument("--cloud-dir", required=True)
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--map-consistency-report", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    anchors_path = Path(args.anchors).resolve()
    pair_path = Path(args.pair_manifest).resolve()
    cameras_path = Path(args.cameras_json).resolve()
    cloud_dir = Path(args.cloud_dir).resolve()
    map_report_path = Path(args.map_consistency_report).resolve()
    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit("refusing to overwrite an existing calibration report directory")
    output_dir.mkdir(parents=True, exist_ok=True)

    anchors = json.loads(anchors_path.read_bytes())
    if anchors.get("schema") != "v2x-diagnostic-anchor-proposals/v1" or anchors.get("acceptance_eligible") is not False:
        raise SystemExit("refusing anchors without the diagnostic proposal contract")
    pair_bytes = pair_path.read_bytes()
    pair = json.loads(pair_bytes)
    pair_sha = hashlib.sha256(pair_bytes).hexdigest()
    cameras_bytes = cameras_path.read_bytes()
    cameras_sha = hashlib.sha256(cameras_bytes).hexdigest()
    cameras = {item["id"]: item for item in json.loads(cameras_bytes)["cameras"]}
    map_report = json.loads(map_report_path.read_bytes())
    if map_report.get("schema") != "v2x-diagnostic-planar-crosswalk-consistency/v1":
        raise SystemExit("unexpected map-consistency report schema")
    if tuple(sorted(anchors["cameras"])) != CAMERA_IDS:
        raise SystemExit("joint fit requires exactly ch1/ch2/ch3/ch4")

    data = {}
    for camera_id in CAMERA_IDS:
        data[camera_id] = load_camera_data(
            camera_id,
            anchors["cameras"][camera_id],
            (anchors.get("lines") or {}).get(camera_id, []),
            pair, pair_path, pair_sha, cloud_dir, cameras, cameras_sha,
        )
    fit = fit_joint(data)
    names = parameter_names()
    boundary_hits = [
        names[index] for index, value in enumerate(fit["values"])
        if abs(value - fit["lower"][index]) <= 1e-4
        or abs(value - fit["upper"][index]) <= 1e-4
    ]

    camera_reports = {}
    all_holdouts_improved = True
    for camera_id in CAMERA_IDS:
        item = data[camera_id]
        baseline = item["baseline"]
        fitted = fit["absolute"][camera_id]
        if len(item["observed"]):
            before_pixels, _ = project(item["world_xyz"], baseline, item["width"], item["height"])
            after_pixels, depth = project(item["world_xyz"], fitted, item["width"], item["height"])
            before_points, _ = metrics(item["observed"], before_pixels, item["splits"])
            after_points, _ = metrics(item["observed"], after_pixels, item["splits"])
        else:
            before_points = after_points = {"fit": None, "holdout": None, "all": None}
            depth = np.empty(0)
        before_lines = line_metrics(item["line_groups"], baseline, item["width"], item["height"])
        after_lines = line_metrics(item["line_groups"], fitted, item["width"], item["height"])
        comparisons = []
        for before, after in (
            (before_points["holdout"], after_points["holdout"]),
            (before_lines["holdout"], after_lines["holdout"]),
        ):
            if before is not None and after is not None:
                comparisons.append(after["rmse_px"] <= before["rmse_px"])
        camera_holdout_improved = bool(comparisons) and all(comparisons)
        all_holdouts_improved = all_holdouts_improved and camera_holdout_improved
        overlay_name = f"{camera_id}-joint-diagnostic-overlay.jpg"
        render_overlay(item, baseline, fitted, output_dir / overlay_name)
        lookup_values = [float(value) for value in item["lookup"]]
        lookup_values.extend(group["lookup_max_px"] for group in item["line_groups"])
        camera_reports[camera_id] = {
            "camera": camera_id,
            "acceptance_eligible": False,
            "cloud_sha256": item["cloud_sha256"],
            "cloud_binding_checks": item["binding"],
            "baseline_absolute": dict(zip(PARAMETER_NAMES, map(float, baseline))),
            "fitted_absolute": dict(zip(PARAMETER_NAMES, map(float, fitted))),
            "delta_absolute": dict(zip(PARAMETER_NAMES, map(float, fitted - baseline))),
            "candidate_twin_pose": candidate_twin_pose(item["camera_config"], baseline, fitted),
            "baseline_metrics": before_points,
            "fitted_metrics": after_points,
            "baseline_line_metrics": before_lines,
            "fitted_line_metrics": after_lines,
            "proposal_holdout_improved": camera_holdout_improved,
            "depth_lookup_max_px": max(lookup_values, default=None),
            "all_fitted_points_in_front": bool(len(depth) == 0 or np.all(depth > 0.25)),
            "overlay": overlay_name,
        }

    rank_passed = fit["rank"] == len(names)
    condition_passed = fit["condition"] is not None and fit["condition"] <= 1e8
    diagnostic_fit_passed = (
        fit["success"] and rank_passed and condition_passed
        and not boundary_hits and all_holdouts_improved
    )
    report = {
        "schema": "v2x-diagnostic-joint-visual-calibration/v1",
        "acceptance_eligible": False,
        "warning": (
            "proposal-only joint rig fit; deployed map consistency and independent "
            "survey/physical-intrinsics/untouched-holdout gates remain mandatory"
        ),
        "parameterization": {
            "shared_world_translation": ["location_x", "location_y", "location_z"],
            "per_camera": ["pitch_deg", "yaw_deg", "roll_deg", "fov_deg"],
            "independent_camera_translation_forbidden": True,
        },
        "anchors_sha256": sha256(anchors_path),
        "pair_manifest_sha256": pair_sha,
        "cameras_json_sha256": cameras_sha,
        "map_consistency_report_sha256": sha256(map_report_path),
        "map_consistency_acceptance_eligible": map_report.get("acceptance_eligible") is True,
        "joint_parameters": dict(zip(names, map(float, fit["values"]))),
        "optimizer_success": fit["success"],
        "visual_objective": fit["cost"],
        "visual_residual_count": fit["visual_residual_count"],
        "jacobian_rank": fit["rank"],
        "required_jacobian_rank": len(names),
        "jacobian_condition": fit["condition"],
        "boundary_hits": boundary_hits,
        "all_proposal_holdouts_improved": all_holdouts_improved,
        "diagnostic_fit_passed": diagnostic_fit_passed,
        "production_gate_passed": False,
        "candidate_recommendation": (
            "retain_for_corrected_map_comparison"
            if diagnostic_fit_passed else "reject_or_expand_independent_evidence"
        ),
        "cameras": camera_reports,
    }
    output_path = output_dir / "joint-diagnostic-calibration.json"
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
