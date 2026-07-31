#!/usr/bin/env python3
"""Generate a measured checkerboard target or calibrate one physical camera."""

import argparse
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np


class CalibrationError(RuntimeError):
    pass


def checkerboard_svg(inner_columns, inner_rows, square_mm, margin_mm=10.0):
    """Return a dimensioned printable checkerboard with the requested inner corners."""
    if inner_columns < 3 or inner_rows < 3:
        raise CalibrationError("checkerboard requires at least 3x3 inner corners")
    if not math.isfinite(square_mm) or square_mm <= 0.0:
        raise CalibrationError("checkerboard square size must be positive")
    columns, rows = inner_columns + 1, inner_rows + 1
    width = columns * square_mm + 2.0 * margin_mm
    height = rows * square_mm + 2.0 * margin_mm
    rectangles = []
    for row in range(rows):
        for column in range(columns):
            if (row + column) % 2 == 0:
                rectangles.append(
                    f'<rect x="{margin_mm + column * square_mm:g}" '
                    f'y="{margin_mm + row * square_mm:g}" '
                    f'width="{square_mm:g}" height="{square_mm:g}" fill="black"/>'
                )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:g}mm" '
        f'height="{height:g}mm" viewBox="0 0 {width:g} {height:g}">\n'
        f'<rect width="{width:g}" height="{height:g}" fill="white"/>\n'
        + "\n".join(rectangles)
        + "\n</svg>\n"
    )


def build_artifact(*, resolution, matrix, distortion, source_hashes, rms):
    """Normalize OpenCV output into the exact fail-closed config artifact schema."""
    if len(source_hashes) < 10 or len(set(source_hashes)) != len(source_hashes):
        raise CalibrationError("at least 10 unique accepted source images are required")
    if not math.isfinite(float(rms)) or not 0.0 <= float(rms) <= 2.0:
        raise CalibrationError("calibration RMS must be finite and no worse than 2 px")
    matrix = np.asarray(matrix, dtype=float)
    distortion = np.asarray(distortion, dtype=float).reshape(-1)
    if matrix.shape != (3, 3) or distortion.size < 5:
        raise CalibrationError("calibration matrix or distortion vector is incomplete")
    numeric = [*matrix.reshape(-1), *distortion[:5]]
    if not all(math.isfinite(float(value)) for value in numeric):
        raise CalibrationError("calibration output contains non-finite values")
    return {
        "method": "checkerboard",
        "image_count": len(source_hashes),
        "source_images_sha256": list(source_hashes),
        "rms_reprojection_error_px": float(rms),
        "resolution": [int(resolution[0]), int(resolution[1])],
        "camera_matrix": matrix.tolist(),
        "distortion": dict(zip(
            ("k1", "k2", "p1", "p2", "k3"),
            (float(value) for value in distortion[:5]),
        )),
    }


def validate_board_coverage(image_points, resolution):
    """Require observations at the optical periphery, not center-only fits."""
    width, height = resolution
    pixels = np.concatenate(
        [np.asarray(points, dtype=float).reshape(-1, 2) for points in image_points]
    )
    centers = np.asarray([
        np.asarray(points, dtype=float).reshape(-1, 2).mean(axis=0)
        for points in image_points
    ])
    if (
        pixels[:, 0].min() > 0.15 * width
        or pixels[:, 0].max() < 0.85 * width
        or pixels[:, 1].min() > 0.15 * height
        or pixels[:, 1].max() < 0.85 * height
        or np.ptp(centers[:, 0]) < 0.4 * width
        or np.ptp(centers[:, 1]) < 0.4 * height
    ):
        raise CalibrationError("checkerboard observations lack edge/corner coverage")


def validate_pose_diversity(rotations, translations):
    """Reject fits with little plane tilt or distance-baseline diversity."""
    tilts = []
    distances = []
    for rotation, translation in zip(rotations, translations):
        rotation_matrix, _ = cv2.Rodrigues(rotation)
        normal = rotation_matrix[:, 2]
        tilts.append(math.degrees(math.acos(min(1.0, abs(float(normal[2]))))))
        distances.append(float(np.linalg.norm(translation)))
    if max(tilts) - min(tilts) < 15.0:
        raise CalibrationError("checkerboard poses lack 15 degree tilt spread")
    if min(distances) <= 0.0 or max(distances) / min(distances) < 1.3:
        raise CalibrationError("checkerboard poses lack 1.3x distance spread")
    return tilts, distances


def detect_checkerboards(
    image_paths, inner_columns, inner_rows, object_template, expected_resolution=None
):
    """Decode and detect a fixed board while retaining exact source identities."""
    object_points, image_points = [], []
    accepted, rejected = [], []
    resolution = expected_resolution
    flags = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
    for raw_path in image_paths:
        path = Path(raw_path)
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            rejected.append({"path": str(path), "sha256": digest, "reason": "decode_failed"})
            continue
        current_resolution = (int(image.shape[1]), int(image.shape[0]))
        if resolution is None:
            resolution = current_resolution
        if current_resolution != resolution:
            rejected.append({"path": str(path), "sha256": digest, "reason": "resolution_mismatch"})
            continue
        found, corners = cv2.findChessboardCornersSB(
            image, (inner_columns, inner_rows), flags=flags
        )
        if not found:
            rejected.append({"path": str(path), "sha256": digest, "reason": "corners_not_found"})
            continue
        object_points.append(object_template.copy())
        image_points.append(corners.astype(np.float32))
        accepted.append({"path": str(path), "sha256": digest})
    return object_points, image_points, accepted, rejected, resolution


def calibration_errors(sources, object_points, image_points, rotations, translations, matrix, distortion):
    output = []
    for source, world, observed, rotation, translation in zip(
        sources, object_points, image_points, rotations, translations
    ):
        projected, _ = cv2.projectPoints(world, rotation, translation, matrix, distortion)
        errors = np.linalg.norm(
            observed.reshape(-1, 2) - projected.reshape(-1, 2), axis=1
        )
        output.append({
            **source,
            "rmse_px": float(np.sqrt(np.mean(errors ** 2))),
            "max_error_px": float(np.max(errors)),
        })
    return output


def calibrate_checkerboard(
    image_paths, holdout_paths, inner_columns, inner_rows, square_mm
):
    """Fit one fixed physical camera and score untouched board holdouts."""
    object_template = np.zeros((inner_columns * inner_rows, 3), np.float32)
    object_template[:, :2] = np.mgrid[
        0:inner_columns, 0:inner_rows
    ].T.reshape(-1, 2)
    object_template *= float(square_mm) / 1000.0
    object_points, image_points, accepted, rejected, resolution = detect_checkerboards(
        image_paths, inner_columns, inner_rows, object_template
    )
    fit_hashes = [item["sha256"] for item in accepted]
    if len(fit_hashes) < 10 or len(set(fit_hashes)) != len(fit_hashes):
        raise CalibrationError("fewer than 10 unique checkerboard images were accepted")
    validate_board_coverage(image_points, resolution)
    rms, matrix, distortion, rotations, translations = cv2.calibrateCamera(
        object_points, image_points, resolution, None, None
    )
    tilts, distances = validate_pose_diversity(rotations, translations)
    per_view = calibration_errors(
        accepted, object_points, image_points, rotations, translations, matrix, distortion
    )
    holdout_objects, holdout_points, holdouts, holdout_rejected, _ = detect_checkerboards(
        holdout_paths, inner_columns, inner_rows, object_template, resolution
    )
    holdout_hashes = [item["sha256"] for item in holdouts]
    if len(holdout_hashes) < 2 or len(set(holdout_hashes)) != len(holdout_hashes):
        raise CalibrationError("fewer than 2 unique checkerboard holdouts were accepted")
    if set(fit_hashes) & set(holdout_hashes):
        raise CalibrationError("checkerboard fit and holdout images must be disjoint")
    holdout_rotations, holdout_translations = [], []
    for world, observed in zip(holdout_objects, holdout_points):
        solved, rotation, translation = cv2.solvePnP(
            world, observed, matrix, distortion, flags=cv2.SOLVEPNP_ITERATIVE
        )
        if not solved:
            raise CalibrationError("checkerboard holdout pose could not be solved")
        holdout_rotations.append(rotation)
        holdout_translations.append(translation)
    holdout_errors = calibration_errors(
        holdouts, holdout_objects, holdout_points, holdout_rotations,
        holdout_translations, matrix, distortion,
    )
    holdout_rmse = float(np.sqrt(np.mean([
        item["rmse_px"] ** 2 for item in holdout_errors
    ])))
    holdout_max = max(item["max_error_px"] for item in holdout_errors)
    if holdout_rmse > 2.0 or holdout_max > 5.0:
        raise CalibrationError("checkerboard holdout reprojection exceeds 2/5 px RMS/max")
    hashes = fit_hashes + holdout_hashes
    artifact = build_artifact(
        resolution=resolution,
        matrix=matrix,
        distortion=distortion,
        source_hashes=hashes,
        rms=rms,
    )
    return artifact, {
        "schema": "v2x-checkerboard-calibration-report/v1",
        "board": {
            "inner_columns": inner_columns,
            "inner_rows": inner_rows,
            "square_mm": float(square_mm),
        },
        "accepted": per_view,
        "rejected": rejected,
        "holdouts": holdout_errors,
        "holdout_rejected": holdout_rejected,
        "holdout_metrics": {"rmse_px": holdout_rmse, "max_error_px": holdout_max},
        "pose_diversity": {
            "tilt_degrees": tilts,
            "distance_meters": distances,
            "tilt_spread_degrees": max(tilts) - min(tilts),
            "distance_ratio": max(distances) / min(distances),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate-board")
    generate.add_argument("--output", required=True)
    generate.add_argument("--inner-columns", type=int, default=9)
    generate.add_argument("--inner-rows", type=int, default=6)
    generate.add_argument("--square-mm", type=float, default=25.0)
    calibrate = subparsers.add_parser("calibrate")
    calibrate.add_argument("--image", action="append", required=True)
    calibrate.add_argument("--holdout-image", action="append", required=True)
    calibrate.add_argument("--output", required=True)
    calibrate.add_argument("--report", required=True)
    calibrate.add_argument("--inner-columns", type=int, default=9)
    calibrate.add_argument("--inner-rows", type=int, default=6)
    calibrate.add_argument("--square-mm", type=float, default=25.0)
    args = parser.parse_args()
    if args.command == "generate-board":
        svg = checkerboard_svg(
            args.inner_columns, args.inner_rows, args.square_mm
        )
        Path(args.output).write_text(svg)
        print(hashlib.sha256(svg.encode()).hexdigest())
        return 0
    artifact, report = calibrate_checkerboard(
        args.image, args.holdout_image,
        args.inner_columns, args.inner_rows, args.square_mm
    )
    encoded = (json.dumps(artifact, indent=2, sort_keys=True) + "\n").encode()
    Path(args.output).write_bytes(encoded)
    report["artifact"] = {
        "path": str(args.output),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
    Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report["artifact"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
