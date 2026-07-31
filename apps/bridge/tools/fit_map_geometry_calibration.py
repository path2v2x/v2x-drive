#!/usr/bin/env python3
"""Fit proposal-only camera models from globally identified UE5 map geometry.

Unlike image-to-image matcher proposals, every 3-D source in this fitter is a
named OpenDRIVE crosswalk vertex/edge or a named static environment object from
``export_map_calibration_geometry.py``.  Real-image observations remain
diagnostic visual annotations and therefore cannot produce acceptance-eligible
output, but they remove the circular twin-pixel/depth identity assumption.
"""

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys

import cv2
import numpy as np

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from fit_diagnostic_visual_calibration import (  # noqa: E402
    PARAMETER_NAMES,
    candidate_twin_pose,
    project,
)


PRIOR_SCALES = np.asarray((2.0, 2.0, 2.0, 8.0, 8.0, 5.0, 8.0))
LOWER_DELTAS = np.asarray((-8.0, -8.0, -5.0, -40.0, -40.0, -20.0, -25.0))
UPPER_DELTAS = np.asarray((8.0, 8.0, 5.0, 40.0, 40.0, 20.0, 25.0))

FOLD_TRANSLATION_RANGE_MAX_M = 0.10
FOLD_ROTATION_RANGE_MAX_DEG = 0.20
FOLD_FOV_RANGE_MAX_DEG = 0.50


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def indexes(geometry):
    return (
        {item["id"]: item for item in geometry["crosswalks"]},
        {item["id"]: item for item in geometry["objects"]},
        {item["id"]: item for item in geometry["lanes"]},
    )


def resolve_point(reference, crosswalks, objects):
    kind = reference.get("kind")
    if kind == "crosswalk_vertex":
        item = crosswalks.get(reference.get("crosswalk_id"))
        index = reference.get("vertex")
        if item is None or not isinstance(index, int) or not 0 <= index < len(item["world"]) - 1:
            raise ValueError("invalid crosswalk vertex reference")
        return np.asarray(item["world"][index], dtype=float)
    if kind == "object_center":
        item = objects.get(str(reference.get("object_id")))
        if item is None:
            raise ValueError("invalid static object reference")
        return np.asarray(item["center_world"], dtype=float)
    raise ValueError("unsupported world point reference")


def resolve_polyline(reference, crosswalks, lanes):
    kind = reference.get("kind")
    if kind == "crosswalk_edge":
        item = crosswalks.get(reference.get("crosswalk_id"))
        start, end = reference.get("start_vertex"), reference.get("end_vertex")
        if (
            item is None or not isinstance(start, int) or not isinstance(end, int)
            or not 0 <= start < len(item["world"]) - 1
            or not 0 <= end < len(item["world"]) - 1 or start == end
        ):
            raise ValueError("invalid crosswalk edge reference")
        return np.asarray([item["world"][start], item["world"][end]], dtype=float)
    if kind == "lane_boundary_segment":
        item = lanes.get(reference.get("lane_id"))
        boundary = reference.get("boundary")
        start, end = reference.get("start_index"), reference.get("end_index")
        keys = {
            "left": "left_boundary_world",
            "center": "center_world",
            "right": "right_boundary_world",
        }
        if (
            item is None or boundary not in keys
            or not isinstance(start, int) or not isinstance(end, int)
            or not 0 <= start < len(item[keys.get(boundary, "")])
            or not 0 <= end < len(item[keys.get(boundary, "")])
            or start == end
        ):
            raise ValueError("invalid lane boundary segment reference")
        values = item[keys[boundary]]
        step = 1 if end > start else -1
        return np.asarray(
            [values[index] for index in range(start, end + step, step)],
            dtype=float,
        )
    raise ValueError("unsupported world polyline reference")


def metric(errors):
    errors = np.asarray(errors, dtype=float)
    if not len(errors):
        return None
    return {
        "count": int(len(errors)),
        "rmse_px": float(math.sqrt(np.mean(errors ** 2))),
        "p95_px": float(np.percentile(errors, 95)),
        "median_px": float(np.median(errors)),
        "max_px": float(np.max(errors)),
    }


def point_errors(world, observed, params, width, height):
    if not len(world):
        return np.empty(0), np.empty((0, 2)), np.empty(0)
    pixels, depth = project(world, params, width, height)
    return np.linalg.norm(pixels - observed, axis=1), pixels, depth


def resample_path(points, samples):
    points = np.asarray(points, dtype=float)
    lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    if np.any(lengths <= 1e-9):
        raise ValueError("polyline contains a zero-length segment")
    cumulative = np.concatenate(([0.0], np.cumsum(lengths)))
    if cumulative[-1] <= 1e-9:
        raise ValueError("polyline has zero length")
    targets = np.linspace(0.0, cumulative[-1], samples)
    output = np.empty((samples, points.shape[1]), dtype=float)
    for index, target in enumerate(targets):
        segment = min(np.searchsorted(cumulative, target, side="right") - 1, len(lengths) - 1)
        fraction = (target - cumulative[segment]) / lengths[segment]
        output[index] = points[segment] + fraction * (points[segment + 1] - points[segment])
    return output


def sampled_lines(polylines, samples=24):
    output = []
    for item in polylines:
        world = resample_path(item["world"], samples)
        real = resample_path(item["real"], samples)
        output.append({
            **item,
            "sampled_world": world,
            "sampled_real": real,
            "correspondence": (
                "symmetric_curve"
                if item["world_ref"].get("kind") == "lane_boundary_segment"
                else "paired"
            ),
        })
    return output


def point_to_polyline_distances(points, polyline):
    points = np.asarray(points, dtype=float)
    polyline = np.asarray(polyline, dtype=float)
    left, right = polyline[:-1], polyline[1:]
    direction = right - left
    denominator = np.sum(direction * direction, axis=1)
    if np.any(denominator <= 1e-12):
        raise ValueError("polyline contains a zero-length segment")
    delta = points[:, None, :] - left[None, :, :]
    fraction = np.clip(
        np.sum(delta * direction[None, :, :], axis=2) / denominator[None, :],
        0.0,
        1.0,
    )
    closest = left[None, :, :] + fraction[:, :, None] * direction[None, :, :]
    return np.sqrt(np.min(np.sum((points[:, None, :] - closest) ** 2, axis=2), axis=1))


def line_residual_values(item, params, width, height):
    pixels, depth = project(item["sampled_world"], params, width, height)
    if np.any(depth <= 0.2) or not np.isfinite(pixels).all():
        count = len(item["sampled_world"])
        return np.full(count * 2, 10_000.0)
    if item["correspondence"] == "symmetric_curve":
        return np.concatenate((
            point_to_polyline_distances(pixels, item["sampled_real"]),
            point_to_polyline_distances(item["sampled_real"], pixels),
        ))
    return (pixels - item["sampled_real"]).ravel()


def fitted_image_line(points):
    homogeneous = np.column_stack((np.asarray(points, dtype=float), np.ones(len(points))))
    _u, _s, vectors = np.linalg.svd(homogeneous)
    line = vectors[-1]
    norm = float(np.linalg.norm(line[:2]))
    if norm <= 1e-12:
        raise ValueError("image trace does not define a line")
    return line / norm


def projected_vanishing_pixel(left_world, right_world, params, width, height):
    left_pixels, left_depth = project(left_world, params, width, height)
    right_pixels, right_depth = project(right_world, params, width, height)
    if (
        np.any(left_depth <= 0.2)
        or np.any(right_depth <= 0.2)
        or not np.isfinite(left_pixels).all()
        or not np.isfinite(right_pixels).all()
    ):
        return None
    left_line = fitted_image_line(left_pixels)
    right_line = fitted_image_line(right_pixels)
    intersection = np.cross(left_line, right_line)
    if abs(float(intersection[2])) <= 1e-9:
        return None
    pixel = intersection[:2] / intersection[2]
    return pixel if np.isfinite(pixel).all() else None


def line_error_values(lines, params, width, height, split):
    values = []
    for item in lines:
        if split != "all" and item["split"] != split:
            continue
        residuals = line_residual_values(item, params, width, height)
        if item["correspondence"] == "paired":
            residuals = np.linalg.norm(residuals.reshape(-1, 2), axis=1)
        values.extend(residuals.tolist())
    return np.asarray(values, dtype=float)


def delta_bounds(annotation):
    lower, upper = LOWER_DELTAS.copy(), UPPER_DELTAS.copy()
    configured = annotation.get("delta_bounds") or {}
    if not isinstance(configured, dict):
        raise ValueError("delta_bounds must be an object")
    unknown = set(configured) - set(PARAMETER_NAMES)
    if unknown:
        raise ValueError(f"unknown delta bound parameters: {sorted(unknown)}")
    for name, values in configured.items():
        index = PARAMETER_NAMES.index(name)
        if (
            not isinstance(values, list)
            or len(values) != 2
            or not all(math.isfinite(float(value)) for value in values)
        ):
            raise ValueError(f"invalid delta bounds for {name}")
        low, high = (float(value) for value in values)
        if low >= high or low < LOWER_DELTAS[index] or high > UPPER_DELTAS[index]:
            raise ValueError(f"unsafe delta bounds for {name}")
        lower[index], upper[index] = low, high
    return lower, upper


def boundary_hits(baseline, fitted, free, lower_deltas, upper_deltas):
    output = []
    for index in free:
        delta = fitted[index] - baseline[index]
        if (
            abs(delta - lower_deltas[index]) <= 1e-4
            or abs(delta - upper_deltas[index]) <= 1e-4
        ):
            output.append(PARAMETER_NAMES[index])
    return output


def validate_pixel(pixel, width, height, label):
    if (
        not isinstance(pixel, list) or len(pixel) != 2
        or not all(math.isfinite(float(value)) for value in pixel)
        or not 0 <= pixel[0] < width or not 0 <= pixel[1] < height
    ):
        raise ValueError(f"{label} is outside the retained real frame")


def validate_vanishing_pixel(pixel, width, height, label):
    if (
        not isinstance(pixel, list)
        or len(pixel) != 2
        or not all(math.isfinite(float(value)) for value in pixel)
        or not -10 * width <= pixel[0] <= 11 * width
        or not -10 * height <= pixel[1] <= 11 * height
    ):
        raise ValueError(f"{label} vanishing point is invalid")


def canonical_world_ref(reference):
    value = dict(reference)
    if value.get("kind") == "crosswalk_edge":
        endpoints = sorted((value.get("start_vertex"), value.get("end_vertex")))
        value["start_vertex"], value["end_vertex"] = endpoints
    elif value.get("kind") == "lane_boundary_segment":
        endpoints = sorted((value.get("start_index"), value.get("end_index")))
        value["start_index"], value["end_index"] = endpoints
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def canonical_pixels(values):
    return tuple(sorted(
        tuple(round(float(coordinate), 3) for coordinate in pixel)
        for pixel in values
    ))


def leave_one_polyline_annotations(annotation):
    polylines = annotation.get("polylines") or []
    if len(polylines) < 3:
        raise ValueError("leave-one-polyline-out requires at least three polylines")
    output = []
    for held in polylines:
        fold = deepcopy(annotation)
        fold["run_leave_one_polyline_out"] = False
        for item in fold["polylines"]:
            item["split"] = "holdout" if item["id"] == held["id"] else "fit"
        for item in fold.get("vanishing_points", []):
            if held["id"] in item.get("polyline_ids", []):
                item["split"] = "holdout"
        output.append((held["id"], fold))
    return output


def fold_consistency(folds):
    poses = [item["candidate_twin_pose"] for item in folds.values()]
    keys = (
        "forward_offset_m",
        "right_offset_m",
        "height_offset_m",
        "pitch_offset_deg",
        "yaw_offset_deg",
        "roll_offset_deg",
        "fov_offset_deg",
    )
    ranges = {
        key: float(np.ptp([pose[key] for pose in poses]))
        for key in keys
    }
    reasons = []
    for key in ("forward_offset_m", "right_offset_m", "height_offset_m"):
        if ranges[key] > FOLD_TRANSLATION_RANGE_MAX_M:
            reasons.append(f"unstable_{key}")
    for key in ("pitch_offset_deg", "yaw_offset_deg", "roll_offset_deg"):
        if ranges[key] > FOLD_ROTATION_RANGE_MAX_DEG:
            reasons.append(f"unstable_{key}")
    if ranges["fov_offset_deg"] > FOLD_FOV_RANGE_MAX_DEG:
        reasons.append("unstable_fov_offset_deg")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "parameter_ranges": ranges,
        "thresholds": {
            "translation_range_max_m": FOLD_TRANSLATION_RANGE_MAX_M,
            "rotation_range_max_deg": FOLD_ROTATION_RANGE_MAX_DEG,
            "fov_range_max_deg": FOLD_FOV_RANGE_MAX_DEG,
        },
    }


def fit_camera(camera_id, annotation, geometry_report, camera_config, output_dir):
    from scipy.optimize import least_squares

    camera_report = geometry_report["cameras"][camera_id]
    real = camera_report["real"]
    image = cv2.imread(real["frame"], cv2.IMREAD_COLOR)
    if image is None or file_sha256(real["frame"]) != real["frame_sha256"]:
        raise ValueError(f"{camera_id}: retained real frame binding failed")
    height, width = image.shape[:2]
    crosswalks, objects, lanes = indexes(geometry_report["geometry"])

    points = []
    seen_ids = set()
    seen_point_refs, seen_point_pixels = set(), set()
    for item in annotation.get("points", []):
        if item.get("id") in seen_ids or not item.get("id"):
            raise ValueError(f"{camera_id}: duplicate/blank point ID")
        seen_ids.add(item["id"])
        validate_pixel(item.get("real_uv"), width, height, item["id"])
        if item.get("split") not in {"fit", "holdout"}:
            raise ValueError(f"{camera_id}: invalid point split")
        uncertainty = float(item.get("uncertainty_px", math.nan))
        if not math.isfinite(uncertainty) or not 0.25 <= uncertainty <= 25.0:
            raise ValueError(f"{camera_id}: invalid point uncertainty")
        world_key = canonical_world_ref(item["world_ref"])
        pixel_key = canonical_pixels([item["real_uv"]])
        if world_key in seen_point_refs or pixel_key in seen_point_pixels:
            raise ValueError(f"{camera_id}: duplicate point geometry/pixel evidence")
        seen_point_refs.add(world_key)
        seen_point_pixels.add(pixel_key)
        points.append({
            **item,
            "world": resolve_point(item["world_ref"], crosswalks, objects),
            "real": np.asarray(item["real_uv"], dtype=float),
            "uncertainty": uncertainty,
        })

    polylines = []
    seen_line_refs, seen_line_pixels = set(), set()
    for item in annotation.get("polylines", []):
        if item.get("id") in seen_ids or not item.get("id"):
            raise ValueError(f"{camera_id}: duplicate/blank line ID")
        seen_ids.add(item["id"])
        real_line = item.get("real_polyline")
        if not isinstance(real_line, list) or len(real_line) < 2:
            raise ValueError(f"{camera_id}: line requires at least two points")
        for pixel in real_line:
            validate_pixel(pixel, width, height, item["id"])
        if item.get("split") not in {"fit", "holdout"}:
            raise ValueError(f"{camera_id}: invalid line split")
        uncertainty = float(item.get("uncertainty_px", math.nan))
        if not math.isfinite(uncertainty) or not 0.25 <= uncertainty <= 25.0:
            raise ValueError(f"{camera_id}: invalid line uncertainty")
        world_key = canonical_world_ref(item["world_ref"])
        pixel_key = canonical_pixels(real_line)
        if world_key in seen_line_refs or pixel_key in seen_line_pixels:
            raise ValueError(f"{camera_id}: duplicate line geometry/pixel evidence")
        seen_line_refs.add(world_key)
        seen_line_pixels.add(pixel_key)
        polylines.append({
            **item,
            "world": resolve_polyline(item["world_ref"], crosswalks, lanes),
            "real": np.asarray(real_line, dtype=float),
            "uncertainty": uncertainty,
        })
    lines = sampled_lines(polylines)
    line_index = {item["id"]: item for item in lines}
    vanishing_points = []
    for item in annotation.get("vanishing_points", []):
        if item.get("id") in seen_ids or not item.get("id"):
            raise ValueError(f"{camera_id}: duplicate/blank vanishing-point ID")
        seen_ids.add(item["id"])
        line_ids = item.get("polyline_ids")
        if (
            not isinstance(line_ids, list)
            or len(line_ids) != 2
            or line_ids[0] == line_ids[1]
            or any(line_id not in line_index for line_id in line_ids)
        ):
            raise ValueError(f"{camera_id}: invalid vanishing-point line identities")
        validate_vanishing_pixel(item.get("real_uv"), width, height, item["id"])
        if item.get("split") not in {"fit", "holdout"}:
            raise ValueError(f"{camera_id}: invalid vanishing-point split")
        uncertainty = float(item.get("uncertainty_px", math.nan))
        if not math.isfinite(uncertainty) or not 0.25 <= uncertainty <= 25.0:
            raise ValueError(f"{camera_id}: invalid vanishing-point uncertainty")
        vanishing_points.append({
            **item,
            "real": np.asarray(item["real_uv"], dtype=float),
            "uncertainty": uncertainty,
            "lines": [line_index[line_id] for line_id in line_ids],
        })
    fit_point_count = sum(item["split"] == "fit" for item in points)
    fit_polyline_count = sum(item["split"] == "fit" for item in polylines)
    if fit_point_count < 4 and fit_polyline_count < 2:
        raise ValueError(f"{camera_id}: insufficient fit points/polylines")
    if not any(item["split"] == "holdout" for item in points + polylines):
        raise ValueError(f"{camera_id}: no frozen holdout proposal")
    evidence_counts = {
        "fit_points": sum(item["split"] == "fit" for item in points),
        "holdout_points": sum(item["split"] == "holdout" for item in points),
        "fit_polylines": sum(item["split"] == "fit" for item in polylines),
        "holdout_polylines": sum(item["split"] == "holdout" for item in polylines),
        "fit_vanishing_points": sum(
            item["split"] == "fit" for item in vanishing_points
        ),
        "holdout_vanishing_points": sum(
            item["split"] == "holdout" for item in vanishing_points
        ),
    }
    evidence_reasons = []
    for key, minimum in (
        ("fit_points", 8),
        ("holdout_points", 4),
        ("fit_polylines", 3),
        ("holdout_polylines", 2),
    ):
        if evidence_counts[key] < minimum:
            evidence_reasons.append(f"insufficient_{key}")
    for split in ("fit", "holdout"):
        split_points = [item for item in points if item["split"] == split]
        if split_points:
            u_values = [item["real"][0] for item in split_points]
            v_values = [item["real"][1] for item in split_points]
            horizontal_span = (max(u_values) - min(u_values)) / width
            vertical_span = (max(v_values) - min(v_values)) / height
            world_xy = np.asarray([item["world"][:2] for item in split_points])
            world_rank = int(np.linalg.matrix_rank(world_xy - np.mean(world_xy, axis=0)))
        else:
            horizontal_span = vertical_span = 0.0
            world_rank = 0
        evidence_counts[f"{split}_horizontal_span"] = horizontal_span
        evidence_counts[f"{split}_vertical_span"] = vertical_span
        evidence_counts[f"{split}_world_xy_rank"] = world_rank
        if horizontal_span < 0.5 or vertical_span < 0.3:
            evidence_reasons.append(f"insufficient_{split}_image_coverage")
        if world_rank < 2:
            evidence_reasons.append(f"rank_deficient_{split}_world_geometry")
    evidence_gate = {
        "passed": not evidence_reasons,
        "counts": evidence_counts,
        "reasons": evidence_reasons,
        "sampled_line_residuals_are_not_independent_features": True,
    }

    baseline = np.asarray([
        *camera_report["baseline_transform"]["location"],
        *camera_report["baseline_transform"]["rotation"],
        camera_report["horizontal_fov_deg"],
    ], dtype=float)
    helper_baseline = np.asarray([
        *camera_report["tracked_helper_transform"]["location"],
        *camera_report["tracked_helper_transform"]["rotation"],
        camera_report["horizontal_fov_deg"]
        + camera_report["tracked_helper_delta"]["fov_deg"],
    ], dtype=float)
    free = np.asarray(annotation.get("free_parameters", list(range(7))), dtype=int)
    if not len(free) or len(set(free.tolist())) != len(free) or np.any((free < 0) | (free >= 7)):
        raise ValueError(f"{camera_id}: invalid free parameter list")
    lower_deltas, upper_deltas = delta_bounds(annotation)
    lower = baseline[free] + lower_deltas[free]
    upper = baseline[free] + upper_deltas[free]
    fit_points = [item for item in points if item["split"] == "fit"]
    fit_world = np.asarray([item["world"] for item in fit_points])
    fit_real = np.asarray([item["real"] for item in fit_points])
    fit_sigma = np.asarray([item["uncertainty"] for item in fit_points])

    def expand(values):
        absolute = baseline.copy()
        absolute[free] = values
        return absolute

    def visual_residual(values):
        absolute = expand(values)
        if len(fit_world):
            pixels, depth = project(fit_world, absolute, width, height)
            residual = ((pixels - fit_real) / fit_sigma[:, None]).ravel()
            residual[
                (~np.isfinite(residual)) | np.repeat(depth <= 0.2, 2)
            ] = 1000.0
        else:
            residual = np.empty(0, dtype=float)
        line_values = []
        for item in lines:
            if item["split"] != "fit":
                continue
            # Sampling a polyline more densely must not increase its total
            # influence.  Give each traced segment the weight of one 2-D point
            # pair while retaining dense residuals for shape sensitivity.
            values = line_residual_values(item, absolute, width, height)
            line_weight = math.sqrt(1.0 / len(values))
            values = line_weight * values / item["uncertainty"]
            values[~np.isfinite(values)] = 1000.0
            line_values.extend(values.tolist())
        for item in vanishing_points:
            if item["split"] != "fit":
                continue
            pixel = projected_vanishing_pixel(
                item["lines"][0]["sampled_world"],
                item["lines"][1]["sampled_world"],
                absolute,
                width,
                height,
            )
            if pixel is None:
                line_values.extend([1000.0, 1000.0])
            else:
                line_values.extend(((pixel - item["real"]) / item["uncertainty"]).tolist())
        return np.concatenate((residual, np.asarray(line_values)))

    def residual(values):
        prior = 0.15 * (values - baseline[free]) / PRIOR_SCALES[free]
        return np.concatenate((visual_residual(values), prior))

    rng = np.random.default_rng(20260711 + int(camera_id[-1]))
    seeds = [baseline[free]] + [rng.uniform(lower, upper) for _ in range(96)]
    solutions = [least_squares(
        residual, seed, bounds=(lower, upper), loss="soft_l1", f_scale=1.0,
        max_nfev=5000,
    ) for seed in seeds]
    solution = min(
        solutions,
        key=lambda value: float(np.sum(visual_residual(value.x) ** 2)),
    )
    fitted = expand(solution.x)
    visual_jacobian = solution.jac[:-len(free), :]
    singular = np.linalg.svd(visual_jacobian, compute_uv=False)
    rank = int(np.linalg.matrix_rank(visual_jacobian))
    condition = (
        None
        if not len(singular) or singular[-1] <= 1e-12
        else float(singular[0] / singular[-1])
    )

    point_world = np.asarray([item["world"] for item in points])
    point_real = np.asarray([item["real"] for item in points])
    baseline_point_errors, baseline_pixels, _ = point_errors(
        point_world, point_real, baseline, width, height
    )
    fitted_point_errors, fitted_pixels, fitted_depth = point_errors(
        point_world, point_real, fitted, width, height
    )
    splits = np.asarray([item["split"] for item in points])
    point_metrics = {}
    for name, errors in (("baseline", baseline_point_errors), ("fitted", fitted_point_errors)):
        point_metrics[name] = {
            split: metric(errors[splits == split]) for split in ("fit", "holdout")
        }
        point_metrics[name]["all"] = metric(errors)
    line_metrics = {
        name: {split: metric(line_error_values(lines, params, width, height, split))
               for split in ("fit", "holdout", "all")}
        for name, params in (("baseline", baseline), ("fitted", fitted))
    }
    vanishing_metrics = {}
    for name, parameters in (("baseline", baseline), ("fitted", fitted)):
        values = []
        for item in vanishing_points:
            pixel = projected_vanishing_pixel(
                item["lines"][0]["sampled_world"],
                item["lines"][1]["sampled_world"],
                parameters,
                width,
                height,
            )
            values.append({
                "id": item["id"],
                "split": item["split"],
                "real_uv": item["real"].tolist(),
                "projected_uv": None if pixel is None else pixel.tolist(),
                "error_px": (
                    10_000.0
                    if pixel is None
                    else float(np.linalg.norm(pixel - item["real"]))
                ),
            })
        vanishing_metrics[name] = values

    hits = boundary_hits(baseline, fitted, free, lower_deltas, upper_deltas)
    holdout_point = point_metrics["fitted"]["holdout"]
    holdout_line = line_metrics["fitted"]["holdout"]
    diagnostic_pass = (
        evidence_gate["passed"]
        and solution.success and rank == len(free) and condition is not None
        and condition <= 100_000.0 and not hits
        and holdout_point is not None
        and holdout_point["rmse_px"] <= 5.0
        and holdout_point["p95_px"] <= 8.0
        and holdout_point["max_px"] <= 12.0
        and (holdout_line is None or (
            holdout_line["rmse_px"] <= 3.0 and holdout_line["max_px"] <= 6.0
        ))
    )

    overlay = image.copy()
    for actual, before, after in zip(point_real, baseline_pixels, fitted_pixels):
        cv2.circle(overlay, tuple(np.rint(actual).astype(int)), 5, (0, 255, 255), 2)
        cv2.circle(overlay, tuple(np.rint(before).astype(int)), 4, (0, 0, 255), 2)
        cv2.circle(overlay, tuple(np.rint(after).astype(int)), 4, (0, 255, 0), 2)
    for item in lines:
        projected, _ = project(item["sampled_world"], fitted, width, height)
        cv2.polylines(
            overlay, [np.rint(projected).astype(np.int32)], False, (0, 255, 0), 2
        )
        cv2.polylines(
            overlay,
            [np.rint(item["sampled_real"]).astype(np.int32)],
            False,
            (0, 255, 255),
            2,
        )
    overlay_path = output_dir / f"{camera_id}-map-fit-overlay.jpg"
    cv2.imwrite(str(overlay_path), overlay)

    candidate = candidate_twin_pose(camera_config, helper_baseline, fitted)
    return {
        "camera": camera_id,
        "acceptance_eligible": False,
        "baseline_absolute": dict(zip(PARAMETER_NAMES, baseline.tolist())),
        "fitted_absolute": dict(zip(PARAMETER_NAMES, fitted.tolist())),
        "delta_absolute": dict(zip(PARAMETER_NAMES, (fitted - baseline).tolist())),
        "delta_bounds": {
            name: [float(lower_deltas[index]), float(upper_deltas[index])]
            for index, name in enumerate(PARAMETER_NAMES)
            if index in free
        },
        "candidate_twin_pose": candidate,
        "evidence_gate": evidence_gate,
        "point_metrics": point_metrics,
        "line_metrics": line_metrics,
        "vanishing_point_metrics": vanishing_metrics,
        "optimizer_success": bool(solution.success),
        "jacobian_rank": rank,
        "required_jacobian_rank": len(free),
        "jacobian_condition": condition,
        "boundary_hits": hits,
        "diagnostic_gate_passed": diagnostic_pass,
        "candidate_recommendation": (
            "continue_offline_render_review" if diagnostic_pass
            else "reject_or_expand_evidence"
        ),
        "overlay": overlay_path.name,
        "points": [{
            "id": item["id"],
            "split": item["split"],
            "world_xyz": item["world"].tolist(),
            "real_uv": item["real"].tolist(),
            "fitted_uv": fitted_pixels[index].tolist(),
            "fitted_error_px": float(fitted_point_errors[index]),
            "fitted_depth_m": float(fitted_depth[index]),
        } for index, item in enumerate(points)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geometry", required=True)
    parser.add_argument("--annotations", required=True)
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    geometry_path = Path(args.geometry).resolve()
    annotation_path = Path(args.annotations).resolve()
    cameras_path = Path(args.cameras_json).resolve()
    geometry_bytes = geometry_path.read_bytes()
    annotation_bytes = annotation_path.read_bytes()
    cameras_bytes = cameras_path.read_bytes()
    geometry = json.loads(geometry_bytes)
    annotations = json.loads(annotation_bytes)
    config = json.loads(cameras_bytes)
    if geometry.get("schema") != "v2x-map-calibration-geometry/v1":
        raise SystemExit("geometry schema is unsupported")
    if (
        annotations.get("schema") != "v2x-diagnostic-map-annotations/v1"
        or annotations.get("acceptance_eligible") is not False
    ):
        raise SystemExit("annotations lack the diagnostic proposal contract")
    if annotations.get("map_geometry_sha256") != hashlib.sha256(geometry_bytes).hexdigest():
        raise SystemExit("annotation geometry hash mismatch")
    if geometry.get("cameras_file_sha256") != hashlib.sha256(cameras_bytes).hexdigest():
        raise SystemExit("cameras file does not match geometry report")

    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit("refusing to overwrite a nonempty map-fit directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    camera_index = {item["id"]: item for item in config["cameras"]}
    results = {}
    for camera_id, annotation in annotations.get("cameras", {}).items():
        if camera_id not in camera_index or camera_id not in geometry["cameras"]:
            raise SystemExit(f"unknown camera {camera_id}")
        primary = fit_camera(
            camera_id, annotation, geometry, camera_index[camera_id], output_dir
        )
        if annotation.get("run_leave_one_polyline_out") is True:
            folds = {}
            for held_id, fold_annotation in leave_one_polyline_annotations(annotation):
                fold_dir = output_dir / f"fold-{held_id}"
                fold_dir.mkdir()
                result = fit_camera(
                    camera_id,
                    fold_annotation,
                    geometry,
                    camera_index[camera_id],
                    fold_dir,
                )
                folds[held_id] = {
                    key: result[key]
                    for key in (
                        "candidate_twin_pose",
                        "line_metrics",
                        "vanishing_point_metrics",
                        "optimizer_success",
                        "jacobian_rank",
                        "required_jacobian_rank",
                        "jacobian_condition",
                        "boundary_hits",
                    )
                }
            consistency = fold_consistency(folds)
            primary["leave_one_polyline_out"] = {
                "folds": folds,
                "consistency": consistency,
            }
            if not consistency["passed"]:
                primary["diagnostic_gate_passed"] = False
                primary["candidate_recommendation"] = "reject_or_expand_evidence"
        results[camera_id] = primary
    report = {
        "schema": "v2x-diagnostic-map-calibration/v1",
        "acceptance_eligible": False,
        "warning": (
            "map-bound diagnostic only; measured optics and independent "
            "acceptance evidence remain required"
        ),
        "geometry_sha256": hashlib.sha256(geometry_bytes).hexdigest(),
        "annotations_sha256": hashlib.sha256(annotation_bytes).hexdigest(),
        "cameras_json_sha256": hashlib.sha256(cameras_bytes).hexdigest(),
        "opendrive_sha256": geometry["opendrive_sha256"],
        "cameras": results,
    }
    output_path = output_dir / "map-calibration-fit.json"
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
