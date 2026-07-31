#!/usr/bin/env python3
"""Score real/UE5 static road-paint alignment with robust contour distances."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

import cv2
import numpy as np
from PIL import Image


ANNOTATION_SCHEMA = "v2x-static-inverse-render-annotations/v1"
RENDER_SCHEMA = "v2x-semantic-calibration-render/v1"
OUTPUT_SCHEMA = "v2x-static-inverse-render-score/v1"


class StaticAlignmentError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_bound_image(binding, label):
    if not isinstance(binding, dict):
        raise StaticAlignmentError(f"{label} image binding is missing")
    path = Path(str(binding.get("path") or "")).resolve()
    if not path.is_file() or sha256_file(path) != binding.get("sha256"):
        raise StaticAlignmentError(f"{label} image hash binding is invalid")
    with Image.open(path) as image:
        rgb = np.asarray(image.convert("RGB"))
    if list(rgb.shape[1::-1]) != binding.get("resolution"):
        raise StaticAlignmentError(f"{label} image resolution binding is invalid")
    return path, rgb


def validate_polygons(values, width, height, label):
    if values is None:
        return []
    if not isinstance(values, list):
        raise StaticAlignmentError(f"{label} polygons are invalid")
    output = []
    for value in values:
        points = np.asarray(value, dtype=float)
        if (
            points.ndim != 2
            or points.shape[1] != 2
            or len(points) < 3
            or not np.isfinite(points).all()
            or np.any(points[:, 0] < 0)
            or np.any(points[:, 0] >= width)
            or np.any(points[:, 1] < 0)
            or np.any(points[:, 1] >= height)
        ):
            raise StaticAlignmentError(f"{label} polygon is malformed")
        if abs(float(cv2.contourArea(points.astype(np.float32)))) < 25.0:
            raise StaticAlignmentError(f"{label} polygon is too small")
        output.append(points)
    return output


def scaled_polygons(values, source_size, target_size):
    scale = np.asarray(target_size, dtype=float) / np.asarray(
        source_size, dtype=float
    )
    return [np.rint(points * scale).astype(np.int32) for points in values]


def polygon_mask(size, include, exclude):
    width, height = size
    mask = np.zeros((height, width), dtype=np.uint8)
    if include:
        cv2.fillPoly(mask, include, 255)
    else:
        mask.fill(255)
    if exclude:
        cv2.fillPoly(mask, exclude, 0)
    return mask


def validate_thresholds(value, label):
    required = {
        "white_value_min",
        "white_saturation_max",
        "yellow_hue_min",
        "yellow_hue_max",
        "yellow_saturation_min",
        "yellow_value_min",
        "local_contrast_min",
        "road_value_max",
        "road_saturation_max",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise StaticAlignmentError(f"{label} paint thresholds are invalid")
    output = {key: int(item) for key, item in value.items()}
    if any(item < 0 or item > 255 for item in output.values()):
        raise StaticAlignmentError(f"{label} paint threshold is out of range")
    if output["yellow_hue_min"] > output["yellow_hue_max"]:
        raise StaticAlignmentError(f"{label} yellow hue interval is invalid")
    return output


def retain_components(mask, minimum_area, maximum_fraction):
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask)
    output = np.zeros_like(mask)
    maximum_area = float(mask.size) * float(maximum_fraction)
    for component in range(1, count):
        area = int(stats[component, cv2.CC_STAT_AREA])
        if int(minimum_area) <= area <= maximum_area:
            output[labels == component] = 255
    return output


def retain_directional_paint(mask, length=11):
    kernels = [
        np.ones((1, length), dtype=np.uint8),
        np.ones((length, 1), dtype=np.uint8),
        np.eye(length, dtype=np.uint8),
        np.fliplr(np.eye(length, dtype=np.uint8)),
    ]
    output = np.zeros_like(mask)
    for kernel in kernels:
        output = cv2.bitwise_or(
            output, cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        )
    return output


def extract_paint_mask(
    rgb,
    thresholds,
    region,
    minimum_component_area=8,
    maximum_component_fraction=0.2,
):
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, saturation, value = cv2.split(hsv)
    local_background = cv2.morphologyEx(
        value,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31)),
    )
    locally_bright = (
        value.astype(np.int16) - local_background.astype(np.int16)
        >= thresholds["local_contrast_min"]
    )
    local_saturation = cv2.blur(saturation, (21, 21))
    road_context = (
        (local_background <= thresholds["road_value_max"])
        & (local_saturation <= thresholds["road_saturation_max"])
    )
    white = (
        (value >= thresholds["white_value_min"])
        & (saturation <= thresholds["white_saturation_max"])
        & locally_bright
        & road_context
    )
    yellow = (
        (hue >= thresholds["yellow_hue_min"])
        & (hue <= thresholds["yellow_hue_max"])
        & (saturation >= thresholds["yellow_saturation_min"])
        & (value >= thresholds["yellow_value_min"])
        & locally_bright
        & road_context
    )
    mask = ((white | yellow).astype(np.uint8) * 255) & region
    kernel = np.ones((3, 3), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = retain_directional_paint(mask)
    return retain_components(
        mask, minimum_component_area, maximum_component_fraction
    )


def contour_edges(mask):
    kernel = np.ones((3, 3), dtype=np.uint8)
    return cv2.morphologyEx(mask, cv2.MORPH_GRADIENT, kernel)


def robust_edge_metrics(real_edges, twin_edges, clip_distance_px=24.0):
    real_count = int(np.count_nonzero(real_edges))
    twin_count = int(np.count_nonzero(twin_edges))
    if real_count < 100 or twin_count < 100:
        raise StaticAlignmentError("paint contour coverage is insufficient")
    real_distance = cv2.distanceTransform(
        255 - real_edges, cv2.DIST_L2, cv2.DIST_MASK_PRECISE
    )
    twin_distance = cv2.distanceTransform(
        255 - twin_edges, cv2.DIST_L2, cv2.DIST_MASK_PRECISE
    )
    distances = np.concatenate(
        (twin_distance[real_edges > 0], real_distance[twin_edges > 0])
    )
    clipped = np.minimum(distances, float(clip_distance_px))
    tolerance_kernel = np.ones((7, 7), dtype=np.uint8)
    real_tolerance = cv2.dilate(real_edges, tolerance_kernel)
    twin_tolerance = cv2.dilate(twin_edges, tolerance_kernel)
    matched_real = np.count_nonzero((real_edges > 0) & (twin_tolerance > 0))
    matched_twin = np.count_nonzero((twin_edges > 0) & (real_tolerance > 0))
    precision = float(matched_twin / twin_count)
    recall = float(matched_real / real_count)
    f1 = 2.0 * precision * recall / max(precision + recall, 1e-12)
    return {
        "real_edge_pixels": real_count,
        "twin_edge_pixels": twin_count,
        "symmetric_mean_px": float(np.mean(distances)),
        "symmetric_rmse_px": float(math.sqrt(np.mean(distances**2))),
        "symmetric_p95_px": float(np.quantile(distances, 0.95)),
        "symmetric_max_px": float(np.max(distances)),
        "robust_clipped_mean_px": float(np.mean(clipped)),
        "tolerance_px": 3.0,
        "tolerance_precision": precision,
        "tolerance_recall": recall,
        "tolerance_f1": f1,
        "optimization_loss": float(np.mean(clipped) + 12.0 * (1.0 - f1)),
    }


def write_visuals(output, real_rgb, twin_rgb, real_mask, twin_mask):
    real_edges = contour_edges(real_mask)
    twin_edges = contour_edges(twin_mask)
    blend = (
        0.5 * real_rgb.astype(np.float32) + 0.5 * twin_rgb.astype(np.float32)
    ).astype(np.uint8)
    overlay = blend.copy()
    overlay[real_edges > 0] = [255, 48, 48]
    overlap = (real_edges > 0) & (twin_edges > 0)
    overlay[twin_edges > 0] = [48, 255, 48]
    overlay[overlap] = [255, 255, 0]
    Image.fromarray(real_mask).save(output / "real-paint-mask.png")
    Image.fromarray(twin_mask).save(output / "twin-paint-mask.png")
    Image.fromarray(overlay).save(output / "paint-contour-overlay.png")
    return real_edges, twin_edges


def evaluate(annotations, render_path, output):
    if (
        annotations.get("schema") != ANNOTATION_SCHEMA
        or annotations.get("acceptance_eligible") is not False
    ):
        raise StaticAlignmentError("annotations lack the diagnostic contract")
    camera_id = annotations.get("camera_id")
    if camera_id not in {"ch1", "ch2", "ch3", "ch4"}:
        raise StaticAlignmentError("annotation camera ID is invalid")
    real_path, real_rgb = validate_bound_image(
        annotations.get("real_frame"), "real"
    )
    real_height, real_width = real_rgb.shape[:2]
    include = validate_polygons(
        annotations.get("real_include_polygons"),
        real_width,
        real_height,
        "real include",
    )
    exclude = validate_polygons(
        annotations.get("real_exclude_polygons"),
        real_width,
        real_height,
        "real exclude",
    )
    real_thresholds = validate_thresholds(
        annotations.get("real_paint_thresholds"), "real"
    )
    twin_thresholds = validate_thresholds(
        annotations.get("twin_paint_thresholds"), "twin"
    )

    render_path = Path(render_path).resolve()
    render_bytes = render_path.read_bytes()
    render = json.loads(render_bytes)
    if (
        render.get("schema") != RENDER_SCHEMA
        or render.get("acceptance_eligible") is not False
        or render.get("camera_id") != camera_id
    ):
        raise StaticAlignmentError("render metadata does not match annotations")
    render_root = render_path.parent
    rgb_binding = (render.get("files") or {}).get("rgb.png") or {}
    twin_path = render_root / str(rgb_binding.get("path") or "")
    if not twin_path.is_file() or sha256_file(twin_path) != rgb_binding.get("sha256"):
        raise StaticAlignmentError("render RGB hash binding is invalid")
    with Image.open(twin_path) as image:
        twin_rgb = np.asarray(image.convert("RGB"))
    target_height, target_width = twin_rgb.shape[:2]
    if [target_width, target_height] != render.get("resolution"):
        raise StaticAlignmentError("render RGB dimensions are invalid")
    real_resized = cv2.resize(
        real_rgb, (target_width, target_height), interpolation=cv2.INTER_AREA
    )
    include_scaled = scaled_polygons(
        include, (real_width, real_height), (target_width, target_height)
    )
    exclude_scaled = scaled_polygons(
        exclude, (real_width, real_height), (target_width, target_height)
    )
    real_region = polygon_mask(
        (target_width, target_height), include_scaled, exclude_scaled
    )
    twin_region = np.full((target_height, target_width), 255, dtype=np.uint8)
    real_mask = extract_paint_mask(
        real_resized, real_thresholds, real_region
    )
    twin_mask = extract_paint_mask(
        twin_rgb, twin_thresholds, twin_region
    )

    output = Path(output).resolve()
    if output.exists():
        raise StaticAlignmentError("refusing to overwrite static alignment output")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent)
    )
    try:
        real_edges, twin_edges = write_visuals(
            temporary, real_resized, twin_rgb, real_mask, twin_mask
        )
        metrics = robust_edge_metrics(real_edges, twin_edges)
        files = {
            name: {"path": name, "sha256": sha256_file(temporary / name)}
            for name in (
                "real-paint-mask.png",
                "twin-paint-mask.png",
                "paint-contour-overlay.png",
            )
        }
        report = {
            "schema": OUTPUT_SCHEMA,
            "acceptance_eligible": False,
            "created_at_utc": utc_now(),
            "camera_id": camera_id,
            "candidate_id": render.get("candidate_id"),
            "twin_pose": render.get("twin_pose"),
            "fov_deg": render.get("fov_deg"),
            "buffer_statistics": render.get("buffer_statistics"),
            "annotations_sha256": annotations["_sha256"],
            "render_sha256": hashlib.sha256(render_bytes).hexdigest(),
            "real_frame": {
                "path": str(real_path),
                "sha256": sha256_file(real_path),
                "source_resolution": [real_width, real_height],
                "scored_resolution": [target_width, target_height],
            },
            "metrics": metrics,
            "files": files,
            "limitations": [
                "thresholded_paint_is_a_diagnostic_class_proposal",
                "ue5_semantic_static_labels_are_degenerate_on_this_map_build",
                "static_image_alignment_does_not_prove_metric_world_scale",
                "measured_intrinsics_and_surveyed_anchors_remain_required",
            ],
        }
        (temporary / "score.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output / "score.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("annotations")
    parser.add_argument("render_json")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    annotation_path = Path(args.annotations).resolve()
    annotation_bytes = annotation_path.read_bytes()
    annotations = json.loads(annotation_bytes)
    annotations["_sha256"] = hashlib.sha256(annotation_bytes).hexdigest()
    try:
        result = evaluate(annotations, args.render_json, args.output_dir)
    except (OSError, ValueError, StaticAlignmentError) as exc:
        raise SystemExit(str(exc)) from exc
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
