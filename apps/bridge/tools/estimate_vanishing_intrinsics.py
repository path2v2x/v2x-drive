#!/usr/bin/env python3
"""Estimate proposal-only focal lengths from orthogonal vanishing directions.

This is an initialization diagnostic, not an intrinsic calibration.  It binds
retained frames, fits each vanishing point from whole physical lines, evaluates
untouched lines, and reports leave-one-line and principal-point sensitivity.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np


SCHEMA = "v2x-vanishing-intrinsics-observations/v1"
OUTPUT_SCHEMA = "v2x-vanishing-intrinsics-initialization/v1"
MINIMUM_FIT_LINES = 3
MINIMUM_HOLDOUT_LINES = 2
MAXIMUM_CONDITION = 10_000.0
MAXIMUM_RELATIVE_FOCAL_LOO_SPREAD = 0.05
MAXIMUM_RELATIVE_PRINCIPAL_POINT_FOCAL_SPREAD = 0.05
MINIMUM_HORIZONTAL_FOV_DEG = 25.0
MAXIMUM_HORIZONTAL_FOV_DEG = 140.0
MAXIMUM_DUPLICATE_LINE_ANGLE_DEG = 0.25
MAXIMUM_DUPLICATE_LINE_OFFSET_PX = 2.0


class VanishingCalibrationError(ValueError):
    pass


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def normalized_line(endpoints):
    value = np.asarray(endpoints, dtype=float)
    if value.shape != (2, 2) or not np.isfinite(value).all():
        raise VanishingCalibrationError("line endpoints must be finite 2-D points")
    if float(np.linalg.norm(value[1] - value[0])) < 8.0:
        raise VanishingCalibrationError("line is too short for a stable direction")
    line = np.cross(np.r_[value[0], 1.0], np.r_[value[1], 1.0])
    norm = float(np.linalg.norm(line[:2]))
    if norm <= 1e-12:
        raise VanishingCalibrationError("line endpoints are degenerate")
    return line / norm


def fit_vanishing_point(lines):
    if len(lines) < 2:
        raise VanishingCalibrationError("a vanishing point requires at least two lines")
    rows, targets = [], []
    for item in lines:
        line = normalized_line(item["endpoints"])
        sigma = float(item.get("uncertainty_px", math.nan))
        if not math.isfinite(sigma) or not 0.25 <= sigma <= 25.0:
            raise VanishingCalibrationError("line uncertainty is invalid")
        rows.append(line[:2] / sigma)
        targets.append(-line[2] / sigma)
    matrix = np.asarray(rows, dtype=float)
    target = np.asarray(targets, dtype=float)
    singular = np.linalg.svd(matrix, compute_uv=False)
    rank = int(np.linalg.matrix_rank(matrix))
    condition = (
        math.inf
        if len(singular) < 2 or singular[-1] <= 1e-12
        else float(singular[0] / singular[-1])
    )
    if rank != 2 or condition > MAXIMUM_CONDITION:
        raise VanishingCalibrationError(
            "line family is rank deficient or ill-conditioned"
        )
    pixel, *_ = np.linalg.lstsq(matrix, target, rcond=None)
    residuals = []
    for item in lines:
        line = normalized_line(item["endpoints"])
        residuals.append(abs(float(line @ np.r_[pixel, 1.0])))
    return {
        "pixel": pixel,
        "rank": rank,
        "condition": condition,
        "residuals_px": np.asarray(residuals, dtype=float),
    }


def focal_from_orthogonal_vanishing_points(left, right, principal_point, width):
    left = np.asarray(left, dtype=float)
    right = np.asarray(right, dtype=float)
    centre = np.asarray(principal_point, dtype=float)
    focal_squared = -float((left - centre) @ (right - centre))
    if not math.isfinite(focal_squared) or focal_squared <= 0.0:
        raise VanishingCalibrationError(
            "orthogonal vanishing points imply a non-positive focal length"
        )
    focal = math.sqrt(focal_squared)
    horizontal_fov = math.degrees(2.0 * math.atan(float(width) / (2.0 * focal)))
    if not MINIMUM_HORIZONTAL_FOV_DEG <= horizontal_fov <= MAXIMUM_HORIZONTAL_FOV_DEG:
        raise VanishingCalibrationError(
            "implied horizontal FOV is physically implausible"
        )
    return {
        "focal_px": focal,
        "focal_squared_px2": focal_squared,
        "horizontal_fov_deg": horizontal_fov,
    }


def metric(values):
    values = np.asarray(values, dtype=float)
    if not len(values):
        return None
    return {
        "count": int(len(values)),
        "rmse_px": float(math.sqrt(np.mean(values ** 2))),
        "p95_px": float(np.percentile(values, 95)),
        "max_px": float(np.max(values)),
    }


def validate_line_set(lines, width, height):
    seen_ids, seen_physical_ids, seen_geometry = set(), set(), set()
    canonical_lines = []
    normalized = []
    for item in lines:
        if not isinstance(item, dict):
            raise VanishingCalibrationError("line annotation must be an object")
        line_id = item.get("id")
        if not isinstance(line_id, str) or not line_id or line_id in seen_ids:
            raise VanishingCalibrationError("line IDs are blank or duplicated")
        seen_ids.add(line_id)
        physical_line_id = item.get("physical_line_id")
        if (
            not isinstance(physical_line_id, str)
            or not physical_line_id
            or physical_line_id in seen_physical_ids
        ):
            raise VanishingCalibrationError(
                "physical line IDs are blank, duplicated, or reused across frames"
            )
        seen_physical_ids.add(physical_line_id)
        if item.get("split") not in {"fit", "holdout"}:
            raise VanishingCalibrationError("line split is invalid")
        endpoints = np.asarray(item.get("endpoints"), dtype=float)
        normalized_line(endpoints)
        if np.any(endpoints[:, 0] < 0.0) or np.any(endpoints[:, 0] >= width):
            raise VanishingCalibrationError(
                "line endpoint is outside the retained frame"
            )
        if np.any(endpoints[:, 1] < 0.0) or np.any(endpoints[:, 1] >= height):
            raise VanishingCalibrationError(
                "line endpoint is outside the retained frame"
            )
        geometry = tuple(np.round(endpoints.ravel(), 6))
        reverse = tuple(np.round(endpoints[::-1].ravel(), 6))
        if geometry in seen_geometry or reverse in seen_geometry:
            raise VanishingCalibrationError("line geometry is duplicated")
        seen_geometry.add(geometry)
        line = normalized_line(endpoints)
        if line[0] < 0.0 or (abs(line[0]) <= 1e-12 and line[1] < 0.0):
            line = -line
        for previous in canonical_lines:
            cosine = float(np.clip(line[:2] @ previous[:2], -1.0, 1.0))
            angle = math.degrees(math.acos(abs(cosine)))
            sign = 1.0 if cosine >= 0.0 else -1.0
            offset = abs(float(line[2] - sign * previous[2]))
            if (
                angle <= MAXIMUM_DUPLICATE_LINE_ANGLE_DEG
                and offset <= MAXIMUM_DUPLICATE_LINE_OFFSET_PX
            ):
                raise VanishingCalibrationError(
                    "near-collinear fragments reuse one physical image line"
                )
        canonical_lines.append(line)
        if not isinstance(item.get("frame_id"), str) or not item["frame_id"]:
            raise VanishingCalibrationError("line frame binding is missing")
        normalized.append(item)
    return normalized


def evaluate_pair(left_lines, right_lines, principal_point, width, height):
    left_lines = validate_line_set(left_lines, width, height)
    right_lines = validate_line_set(right_lines, width, height)
    if {item["id"] for item in left_lines} & {item["id"] for item in right_lines}:
        raise VanishingCalibrationError("physical line IDs are reused across families")
    left_physical = {item["physical_line_id"] for item in left_lines}
    right_physical = {item["physical_line_id"] for item in right_lines}
    if left_physical & right_physical:
        raise VanishingCalibrationError(
            "physical line identities are reused across orthogonal families"
        )
    left_fit = [item for item in left_lines if item["split"] == "fit"]
    right_fit = [item for item in right_lines if item["split"] == "fit"]
    left_holdout = [item for item in left_lines if item["split"] == "holdout"]
    right_holdout = [item for item in right_lines if item["split"] == "holdout"]
    reasons = []
    if len(left_fit) < MINIMUM_FIT_LINES or len(right_fit) < MINIMUM_FIT_LINES:
        reasons.append("insufficient_fit_lines")
    if (
        len(left_holdout) < MINIMUM_HOLDOUT_LINES
        or len(right_holdout) < MINIMUM_HOLDOUT_LINES
    ):
        reasons.append("insufficient_holdout_lines")
    if reasons:
        return {"passed": False, "reasons": reasons}

    left = fit_vanishing_point(left_fit)
    right = fit_vanishing_point(right_fit)
    focal = focal_from_orthogonal_vanishing_points(
        left["pixel"], right["pixel"], principal_point, width
    )
    left_holdout_errors = [
        abs(float(normalized_line(item["endpoints"]) @ np.r_[left["pixel"], 1.0]))
        for item in left_holdout
    ]
    right_holdout_errors = [
        abs(float(normalized_line(item["endpoints"]) @ np.r_[right["pixel"], 1.0]))
        for item in right_holdout
    ]
    holdout = metric(left_holdout_errors + right_holdout_errors)
    holdout_p95_limit = 3.0 * width / 1280.0
    holdout_max_limit = 5.0 * width / 1280.0
    if holdout["p95_px"] > holdout_p95_limit or holdout["max_px"] > holdout_max_limit:
        reasons.append("heldout_line_error_above_gate")

    loo_focals = []
    for family, values in (("left", left_fit), ("right", right_fit)):
        for index, item in enumerate(values):
            reduced = values[:index] + values[index + 1 :]
            try:
                candidate = fit_vanishing_point(reduced)["pixel"]
                left_pixel = candidate if family == "left" else left["pixel"]
                right_pixel = candidate if family == "right" else right["pixel"]
                estimate = focal_from_orthogonal_vanishing_points(
                    left_pixel, right_pixel, principal_point, width
                )
            except VanishingCalibrationError:
                reasons.append("leave_one_line_out_has_invalid_solution")
                continue
            loo_focals.append({
                "heldout_line_id": item["id"],
                "family": family,
                **estimate,
            })
    if len(loo_focals) != len(left_fit) + len(right_fit):
        relative_loo_spread = math.inf
    else:
        values = [item["focal_px"] for item in loo_focals]
        relative_loo_spread = (max(values) - min(values)) / focal["focal_px"]
    if relative_loo_spread > MAXIMUM_RELATIVE_FOCAL_LOO_SPREAD:
        reasons.append("leave_one_line_out_focal_is_unstable")

    offsets = (-0.02, 0.0, 0.02)
    sensitivity = []
    for x_fraction in offsets:
        for y_fraction in offsets:
            point = [
                principal_point[0] + x_fraction * width,
                principal_point[1] + y_fraction * height,
            ]
            try:
                estimate = focal_from_orthogonal_vanishing_points(
                    left["pixel"], right["pixel"], point, width
                )
            except VanishingCalibrationError:
                reasons.append("principal_point_sensitivity_has_invalid_solution")
                continue
            sensitivity.append({"principal_point": point, **estimate})
    if len(sensitivity) != 9:
        principal_spread = math.inf
    else:
        values = [item["focal_px"] for item in sensitivity]
        principal_spread = (max(values) - min(values)) / focal["focal_px"]
    if principal_spread > MAXIMUM_RELATIVE_PRINCIPAL_POINT_FOCAL_SPREAD:
        reasons.append("principal_point_focal_sensitivity_is_unstable")

    fit_frames = {item["frame_id"] for item in left_fit + right_fit}
    holdout_frames = {item["frame_id"] for item in left_holdout + right_holdout}
    if not fit_frames.isdisjoint(holdout_frames):
        reasons.append("fit_and_holdout_reuse_frames")
    return {
        "passed": not reasons,
        "reasons": sorted(set(reasons)),
        "candidate": focal,
        "principal_point": list(map(float, principal_point)),
        "vanishing_points": {
            "left": left["pixel"].tolist(),
            "right": right["pixel"].tolist(),
        },
        "fit_line_metrics": {
            "left": metric(left["residuals_px"]),
            "right": metric(right["residuals_px"]),
        },
        "holdout_line_metrics": holdout,
        "leave_one_line_out": {
            "relative_focal_spread": relative_loo_spread,
            "maximum_relative_spread": MAXIMUM_RELATIVE_FOCAL_LOO_SPREAD,
            "solutions": loo_focals,
        },
        "principal_point_sensitivity": {
            "relative_focal_spread": principal_spread,
            "maximum_relative_spread": (
                MAXIMUM_RELATIVE_PRINCIPAL_POINT_FOCAL_SPREAD
            ),
            "solutions": sensitivity,
        },
        "fit_frame_ids": sorted(fit_frames),
        "holdout_frame_ids": sorted(holdout_frames),
    }


def validate_frames(observations, base_directory):
    frames = observations.get("frames")
    if not isinstance(frames, list) or not frames:
        raise VanishingCalibrationError("retained frames are missing")
    output = {}
    seen_hashes = set()
    for item in frames:
        frame_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(frame_id, str) or not frame_id or frame_id in output:
            raise VanishingCalibrationError("frame IDs are blank or duplicated")
        path = Path(item.get("path", "")).expanduser()
        if not path.is_absolute():
            path = base_directory / path
        raw = path.resolve(strict=True).read_bytes()
        digest = sha256_bytes(raw)
        if digest != item.get("sha256"):
            raise VanishingCalibrationError("retained frame hash does not match")
        if digest in seen_hashes:
            raise VanishingCalibrationError("retained frame content is duplicated")
        seen_hashes.add(digest)
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or [image.shape[1], image.shape[0]] != item.get("resolution"):
            raise VanishingCalibrationError("retained frame dimensions do not match")
        output[frame_id] = {
            "path": str(path.resolve()),
            "sha256": item["sha256"],
            "resolution": list(item["resolution"]),
        }
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    source_path = Path(args.observations).resolve()
    source_raw = source_path.read_bytes()
    observations = json.loads(source_raw)
    if (
        observations.get("schema") != SCHEMA
        or observations.get("acceptance_eligible") is not False
    ):
        raise SystemExit("observations lack the diagnostic contract")
    try:
        frames = validate_frames(observations, source_path.parent)
        results = {}
        for camera_id, camera in observations.get("cameras", {}).items():
            resolution = camera.get("resolution")
            if (
                not isinstance(camera_id, str)
                or not isinstance(resolution, list)
                or len(resolution) != 2
            ):
                raise VanishingCalibrationError("camera observation is invalid")
            width, height = map(int, resolution)
            principal = camera.get("principal_point", [width / 2.0, height / 2.0])
            families = camera.get("line_families")
            pair = camera.get("orthogonal_pair")
            if (
                not isinstance(families, dict)
                or not isinstance(pair, list)
                or len(pair) != 2
            ):
                raise VanishingCalibrationError(
                    "orthogonal line-family pair is missing"
                )
            left_lines = families.get(pair[0])
            right_lines = families.get(pair[1])
            if not isinstance(left_lines, list) or not isinstance(right_lines, list):
                raise VanishingCalibrationError("orthogonal line families are missing")
            for item in left_lines + right_lines:
                if item.get("frame_id") not in frames:
                    raise VanishingCalibrationError("line references an unknown frame")
                if frames[item["frame_id"]]["resolution"] != [width, height]:
                    raise VanishingCalibrationError(
                        "line frame resolution differs from the camera"
                    )
            results[camera_id] = evaluate_pair(
                left_lines, right_lines, principal, width, height
            )
    except (OSError, ValueError, VanishingCalibrationError) as exc:
        raise SystemExit(str(exc)) from exc
    report = {
        "schema": OUTPUT_SCHEMA,
        "acceptance_eligible": False,
        "candidate_recommendation": (
            "initialization_only"
            if results and all(item["passed"] for item in results.values())
            else "reject_or_expand_evidence"
        ),
        "observations_sha256": sha256_bytes(source_raw),
        "frames": frames,
        "cameras": results,
        "acceptance_failures": [
            "vanishing_geometry_does_not_measure_distortion_or_principal_point",
            "translation_and_georeferencing_are_unobservable",
            "checkerboard_or_charuco_intrinsics_remain_required",
        ],
    }
    output = Path(args.output).resolve()
    if output.exists():
        raise SystemExit("refusing to overwrite an existing report")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
