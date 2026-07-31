#!/usr/bin/env python3
"""Register current orthophoto content to surveyed historical LiDAR intensity.

Both sources must already be exported onto the same declared north-up metric
grid.  LoFTR supplies cross-modal proposals; a partial-affine RANSAC fit,
coverage checks, and confidence-threshold stability decide whether the result
is useful as a diagnostic registration.  It is never current survey truth.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path

import cv2
import numpy as np


CHECKPOINT_NAME = "loftr_outdoor.ckpt"
CHECKPOINT_SHA256 = "21f5bec5968178e8bc8b7633441836fe5de4f47d861dd2cd7dc38e271b0479ec"


class RegistrationError(RuntimeError):
    pass


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json_exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_image(path, expected_sha256, label):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RegistrationError(f"{label} is unreadable") from exc
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RegistrationError(f"{label} hash does not match")
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise RegistrationError(f"{label} is not a decodable image")
    return path, image


def checkpoint_path():
    try:
        import torch
    except ImportError as exc:
        raise RegistrationError("PyTorch is required for LoFTR") from exc
    return Path(torch.hub.get_dir()) / "checkpoints" / CHECKPOINT_NAME


def verify_checkpoint():
    path = checkpoint_path()
    if not path.is_file() or sha256(path) != CHECKPOINT_SHA256:
        raise RegistrationError("pinned LoFTR outdoor checkpoint is unavailable")
    return path


def estimate_registration(points_lidar, points_ortho, width, height,
                          ransac_threshold_px=3.0):
    points_lidar = np.asarray(points_lidar, dtype=np.float32)
    points_ortho = np.asarray(points_ortho, dtype=np.float32)
    if (
        points_lidar.ndim != 2
        or points_lidar.shape[1:] != (2,)
        or points_ortho.shape != points_lidar.shape
        or len(points_lidar) < 3
        or not np.isfinite(points_lidar).all()
        or not np.isfinite(points_ortho).all()
    ):
        raise RegistrationError("registration point arrays are invalid")
    matrix, mask = cv2.estimateAffinePartial2D(
        points_lidar,
        points_ortho,
        method=cv2.RANSAC,
        ransacReprojThreshold=ransac_threshold_px,
        maxIters=20_000,
        confidence=0.999,
        refineIters=20,
    )
    if matrix is None or mask is None:
        raise RegistrationError("partial-affine RANSAC did not converge")
    inliers = mask.reshape(-1).astype(bool)
    if np.count_nonzero(inliers) < 3:
        raise RegistrationError("partial-affine RANSAC has too few inliers")
    predicted = cv2.transform(points_lidar[None], matrix)[0]
    errors = np.linalg.norm(predicted - points_ortho, axis=1)
    selected = errors[inliers]
    a, b = float(matrix[0, 0]), float(matrix[1, 0])
    scale = math.hypot(a, b)
    rotation_deg = math.degrees(math.atan2(b, a))
    minimum = points_lidar[inliers].min(axis=0)
    maximum = points_lidar[inliers].max(axis=0)
    coverage = [
        float((maximum[0] - minimum[0]) / width),
        float((maximum[1] - minimum[1]) / height),
    ]
    return {
        "matrix_lidar_pixel_to_orthophoto_pixel": matrix.tolist(),
        "match_count": len(points_lidar),
        "inlier_count": int(np.count_nonzero(inliers)),
        "inlier_fraction": float(np.mean(inliers)),
        "inlier_error_px": {
            "rmse": float(math.sqrt(np.mean(selected**2))),
            "median": float(np.median(selected)),
            "p95": float(np.quantile(selected, 0.95)),
            "max": float(np.max(selected)),
        },
        "scale": scale,
        "rotation_deg": rotation_deg,
        "translation_px": [float(matrix[0, 2]), float(matrix[1, 2])],
        "inlier_bbox_lidar_px": [minimum.tolist(), maximum.tolist()],
        "inlier_span_fraction_xy": coverage,
        "inlier_mask": inliers,
    }


def extract_matches(lidar, ortho):
    try:
        import torch
        from kornia.feature import LoFTR
    except ImportError as exc:
        raise RegistrationError("PyTorch and Kornia are required for LoFTR") from exc
    torch.set_num_threads(min(4, max(1, os.cpu_count() or 1)))
    matcher = LoFTR(pretrained="outdoor").eval().cpu()

    def tensor(image):
        return torch.from_numpy(image).float()[None, None] / 255.0

    with torch.inference_mode():
        values = matcher({"image0": tensor(lidar), "image1": tensor(ortho)})
    return (
        values["keypoints0"].cpu().numpy(),
        values["keypoints1"].cpu().numpy(),
        values["confidence"].cpu().numpy(),
    )


def summarize_threshold(points_lidar, points_ortho, confidence, threshold,
                        width, height, resolution_m):
    selected = confidence >= threshold
    result = estimate_registration(
        points_lidar[selected], points_ortho[selected], width, height
    )
    result.pop("inlier_mask")
    result["minimum_confidence"] = threshold
    result["inlier_error_m"] = {
        key: value * resolution_m
        for key, value in result["inlier_error_px"].items()
    }
    result["translation_m"] = [
        value * resolution_m for value in result["translation_px"]
    ]
    return result


def register(lidar_path, lidar_hash, ortho_path, ortho_hash,
             bounds, resolution_m, minimum_confidence=0.2):
    checkpoint = verify_checkpoint()
    lidar_path, lidar = load_image(lidar_path, lidar_hash, "lidar raster")
    ortho_path, ortho = load_image(ortho_path, ortho_hash, "orthophoto raster")
    if lidar.shape != ortho.shape:
        raise RegistrationError("registration images do not share one grid")
    if not 0.1 <= resolution_m <= 5.0:
        raise RegistrationError("registration resolution is unsupported")
    if len(bounds) != 4 or not all(math.isfinite(value) for value in bounds):
        raise RegistrationError("registration bounds are invalid")
    points_lidar, points_ortho, confidence = extract_matches(lidar, ortho)
    height, width = lidar.shape
    thresholds = sorted(set([minimum_confidence, 0.3, 0.4, 0.5, 0.6]))
    sweep = []
    for threshold in thresholds:
        if np.count_nonzero(confidence >= threshold) < 3:
            continue
        try:
            sweep.append(summarize_threshold(
                points_lidar, points_ortho, confidence, threshold,
                width, height, resolution_m,
            ))
        except RegistrationError:
            if threshold == minimum_confidence:
                raise
    primary = next(
        (item for item in sweep if item["minimum_confidence"] == minimum_confidence),
        None,
    )
    if primary is None:
        raise RegistrationError("minimum-confidence registration is unavailable")
    translations = np.asarray([item["translation_m"] for item in sweep])
    scales = np.asarray([item["scale"] for item in sweep])
    rotations = np.asarray([item["rotation_deg"] for item in sweep])
    stability = {
        "threshold_count": len(sweep),
        "translation_component_range_m": np.ptp(translations, axis=0).tolist(),
        "scale_range": float(np.ptp(scales)),
        "rotation_range_deg": float(np.ptp(rotations)),
    }
    diagnostic_passed = bool(
        primary["match_count"] >= 50
        and primary["inlier_count"] >= 25
        and primary["inlier_fraction"] >= 0.20
        and primary["inlier_error_m"]["rmse"] <= 1.5
        and min(primary["inlier_span_fraction_xy"]) >= 0.70
        and abs(primary["scale"] - 1.0) <= 0.01
        and abs(primary["rotation_deg"]) <= 0.5
        and max(stability["translation_component_range_m"]) <= 1.5
        and stability["scale_range"] <= 0.01
        and stability["rotation_range_deg"] <= 0.5
    )
    return {
        "schema": "v2x-orthophoto-to-lidar-registration/v1",
        "acceptance_eligible": False,
        "diagnostic_registration_passed": diagnostic_passed,
        "inputs": {
            "lidar_raster": {"path": str(lidar_path), "sha256": lidar_hash},
            "orthophoto_raster": {"path": str(ortho_path), "sha256": ortho_hash},
            "model_checkpoint": {
                "path": str(checkpoint), "sha256": CHECKPOINT_SHA256,
            },
        },
        "grid": {
            "bounds_xmin_ymin_xmax_ymax_m": list(bounds),
            "resolution_m_per_pixel": resolution_m,
            "width": width,
            "height": height,
            "north_up": True,
        },
        "match_proposals": {
            "count": len(confidence),
            "confidence_min": float(np.min(confidence)),
            "confidence_median": float(np.median(confidence)),
            "confidence_max": float(np.max(confidence)),
        },
        "primary": primary,
        "confidence_threshold_sweep": sweep,
        "threshold_stability": stability,
        "acceptance_failures": [
            "lidar_and_orthophoto_are_fifteen_years_apart",
            "registration_inlier_rmse_exceeds_final_camera_calibration_tolerance",
            "learned_feature_matches_are_not_surveyed_current_landmarks",
            "registration_is_a_map_geometry_hypothesis_not_camera_calibration",
        ],
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lidar-raster", type=Path, required=True)
    parser.add_argument("--lidar-sha256", required=True)
    parser.add_argument("--orthophoto-raster", type=Path, required=True)
    parser.add_argument("--orthophoto-sha256", required=True)
    parser.add_argument("--bounds", type=float, nargs=4, required=True)
    parser.add_argument("--resolution-m", type=float, required=True)
    parser.add_argument("--minimum-confidence", type=float, default=0.2)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    result = register(
        args.lidar_raster, args.lidar_sha256,
        args.orthophoto_raster, args.orthophoto_sha256,
        args.bounds, args.resolution_m, args.minimum_confidence,
    )
    write_json_exclusive(args.output, result)
    print(json.dumps({
        "output": str(args.output.resolve()),
        "diagnostic_registration_passed": result["diagnostic_registration_passed"],
        "match_count": result["primary"]["match_count"],
        "inlier_count": result["primary"]["inlier_count"],
        "inlier_rmse_m": result["primary"]["inlier_error_m"]["rmse"],
        "acceptance_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
