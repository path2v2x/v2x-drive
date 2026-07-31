#!/usr/bin/env python3
"""Fit one proposal-only UE5 camera by rendering physical road-paint geometry.

The optimizer runs only against the owned, loopback-bound UE5.5 calibration
worker.  It scores actual RGB road paint rather than CARLA's exported
crosswalk polygons because the Richmond map API geometry is known not to match
the visible crosswalk mesh.  Fit and development images may guide the search;
an untouched holdout is deliberately not accepted by this command.

Every result is diagnostic.  It cannot establish measured physical intrinsics,
surveyed map accuracy, or production calibration acceptance.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import sys
import tempfile
import time
from typing import Iterable

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from digital_twin_bridge.twin_camera_rig import (  # noqa: E402
    camera_with_twin_pose,
    compute_twin_camera_transform,
    configure_twin_camera_blueprint,
    load_cameras_config,
)
from render_semantic_calibration_candidate import (  # noqa: E402
    EXPECTED_CONTAINER,
    EXPECTED_HOST,
    EXPECTED_MAP_NAME,
    EXPECTED_OPENDRIVE_SHA256,
    EXPECTED_PORT,
    RenderError,
    inspect_worker,
    validate_endpoint,
)


OUTPUT_SCHEMA = "v2x-diagnostic-inverse-render-search/v1"
ROI_SCHEMA = "v2x-diagnostic-road-rois/v1"
POSE_KEYS = (
    "forward_offset_m",
    "right_offset_m",
    "height_offset_m",
    "pitch_offset_deg",
    "yaw_offset_deg",
    "roll_offset_deg",
    "fov_offset_deg",
)
DEFAULT_HALF_RANGES = np.asarray((3.0, 3.0, 1.0, 15.0, 15.0, 5.0, 12.0))
DEFAULT_STEPS = np.asarray((0.50, 0.50, 0.25, 1.5, 1.5, 0.75, 1.5))
RANGE_ARGUMENTS = (
    "forward_half_range_m",
    "right_half_range_m",
    "height_half_range_m",
    "pitch_half_range_deg",
    "yaw_half_range_deg",
    "roll_half_range_deg",
    "fov_half_range_deg",
)
STEP_ARGUMENTS = (
    "forward_step_m",
    "right_step_m",
    "height_step_m",
    "pitch_step_deg",
    "yaw_step_deg",
    "roll_step_deg",
    "fov_step_deg",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_rgb(path: Path, width: int, height: int) -> np.ndarray:
    raw = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if raw is None or raw.ndim != 3 or raw.shape[2] != 3:
        raise ValueError(f"cannot decode RGB target: {path}")
    if raw.shape[1] != width or raw.shape[0] != height:
        raw = cv2.resize(raw, (width, height), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)


def validate_rois(document: dict, camera_id: str) -> list[list[list[float]]]:
    if (
        document.get("schema") != ROI_SCHEMA
        or document.get("acceptance_eligible") is not False
        or document.get("coordinate_space") != "normalized_image_xy"
    ):
        raise ValueError("road ROI file does not have the diagnostic contract")
    item = (document.get("cameras") or {}).get(camera_id) or {}
    polygons = item.get("polygons")
    if not isinstance(polygons, list) or not polygons:
        raise ValueError(f"{camera_id}: road ROI polygons are missing")
    normalized = []
    for polygon in polygons:
        if not isinstance(polygon, list) or len(polygon) < 3:
            raise ValueError(f"{camera_id}: road ROI polygon is degenerate")
        points = []
        for point in polygon:
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"{camera_id}: road ROI point is invalid")
            x, y = map(float, point)
            if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError(f"{camera_id}: road ROI point is outside the image")
            points.append([x, y])
        normalized.append(points)
    return normalized


def validate_target_polylines(
    document: dict, camera_id: str
) -> list[list[list[float]]]:
    item = (document.get("cameras") or {}).get(camera_id) or {}
    polylines = item.get("target_polylines")
    if not isinstance(polylines, list) or len(polylines) < 3:
        raise ValueError(f"{camera_id}: target road polylines are insufficient")
    normalized = []
    for polyline in polylines:
        if not isinstance(polyline, list) or len(polyline) < 2:
            raise ValueError(f"{camera_id}: target road polyline is degenerate")
        points = []
        for point in polyline:
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"{camera_id}: target road point is invalid")
            x, y = map(float, point)
            if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError(f"{camera_id}: target road point is outside the image")
            points.append([x, y])
        normalized.append(points)
    return normalized


def rasterize_rois(
    polygons: list[list[list[float]]], width: int, height: int
) -> np.ndarray:
    mask = np.zeros((height, width), dtype=np.uint8)
    for polygon in polygons:
        points = np.asarray(
            [
                [
                    int(round(x * (width - 1))),
                    int(round(y * (height - 1))),
                ]
                for x, y in polygon
            ],
            dtype=np.int32,
        )
        cv2.fillPoly(mask, [points], 255)
    return mask > 0


def rasterize_target_polylines(
    polylines: list[list[list[float]]], width: int, height: int, thickness: int = 3
) -> np.ndarray:
    mask = np.zeros((height, width), dtype=np.uint8)
    for polyline in polylines:
        points = np.asarray(
            [
                [int(round(x * (width - 1))), int(round(y * (height - 1)))]
                for x, y in polyline
            ],
            dtype=np.int32,
        )
        cv2.polylines(mask, [points], False, 255, thickness, cv2.LINE_AA)
    return mask > 0


def _remove_small_components(mask: np.ndarray, minimum_area: int) -> np.ndarray:
    labels, values, statistics, _ = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), connectivity=8
    )
    output = np.zeros_like(mask, dtype=bool)
    for label in range(1, labels):
        if int(statistics[label, cv2.CC_STAT_AREA]) >= minimum_area:
            output[values == label] = True
    return output


def _keep_elongated_components(
    mask: np.ndarray, minimum_area: int, minimum_elongation: float
) -> np.ndarray:
    label_count, labels, statistics, _ = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), connectivity=8
    )
    output = np.zeros_like(mask, dtype=bool)
    for label in range(1, label_count):
        area = int(statistics[label, cv2.CC_STAT_AREA])
        if area < minimum_area:
            continue
        y, x = np.nonzero(labels == label)
        if len(x) < 2:
            continue
        coordinates = np.column_stack((x, y)).astype(np.float64)
        covariance = np.cov(coordinates, rowvar=False)
        eigenvalues = np.linalg.eigvalsh(covariance)
        elongation = math.sqrt(
            float((max(eigenvalues[-1], 0.0) + 1.0) / (max(eigenvalues[0], 0.0) + 1.0))
        )
        width = int(statistics[label, cv2.CC_STAT_WIDTH])
        height = int(statistics[label, cv2.CC_STAT_HEIGHT])
        aspect = max(width, height) / max(1, min(width, height))
        if max(elongation, aspect) >= minimum_elongation:
            output[labels == label] = True
    return output


def _retain_linear_runs(mask: np.ndarray, length: int = 11) -> np.ndarray:
    size = length + 4
    if size % 2 == 0:
        size += 1
    center = size // 2
    radius = length // 2
    supported = np.zeros_like(mask, dtype=np.uint8)
    for angle_deg in range(0, 180, 15):
        angle = math.radians(angle_deg)
        dx = int(round(radius * math.cos(angle)))
        dy = int(round(radius * math.sin(angle)))
        kernel = np.zeros((size, size), dtype=np.uint8)
        cv2.line(
            kernel,
            (center - dx, center - dy),
            (center + dx, center + dy),
            1,
            1,
        )
        opened = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, kernel)
        supported |= opened
    supported = cv2.dilate(supported, np.ones((5, 5), np.uint8), iterations=1)
    return mask & (supported > 0)


def extract_road_paint_masks(
    rgb: np.ndarray, roi: np.ndarray
) -> dict[str, np.ndarray]:
    """Extract lighting-tolerant white/yellow paint proposals inside a road ROI."""

    if rgb.ndim != 3 or rgb.shape[2] != 3 or roi.shape != rgb.shape[:2]:
        raise ValueError("RGB/ROI dimensions do not match")
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    hue, saturation, value = [hsv[:, :, index].astype(np.int16) for index in range(3)]
    lightness = lab[:, :, 0].astype(np.int16)
    red = rgb[:, :, 0].astype(np.int16)
    green = rgb[:, :, 1].astype(np.int16)
    blue = rgb[:, :, 2].astype(np.int16)
    chroma = np.maximum.reduce((red, green, blue)) - np.minimum.reduce((red, green, blue))

    lightness_float = lightness.astype(np.float32)
    local_background = cv2.GaussianBlur(lightness_float, (0, 0), 7.0)
    local_squared = cv2.GaussianBlur(lightness_float ** 2, (0, 0), 7.0)
    local_std = np.sqrt(np.maximum(local_squared - local_background ** 2, 0.0))
    # Road context accepts both the dark physical asphalt and the brighter UE5
    # material, while rejecting dry grass using chroma and local texture.
    # Dilation lets the center of a wide painted stripe inherit its immediately
    # adjacent asphalt without admitting distant buildings.
    asphalt = (
        roi
        & (saturation <= 58)
        & (value >= 18)
        & (value <= 185)
        & (local_std <= 50.0)
    )
    context = cv2.dilate(
        asphalt.astype(np.uint8), np.ones((13, 13), np.uint8), iterations=1
    ) > 0

    local_contrast = lightness_float - local_background
    yellow_opponent = (red + green) / 2.0 - blue
    road_interior = cv2.erode(
        roi.astype(np.uint8), np.ones((9, 9), np.uint8), iterations=1
    ) > 0
    yellow = (
        road_interior
        & context
        & (value >= 62)
        & (red >= blue + 8)
        & (green >= blue + 8)
        & (yellow_opponent >= 11.0)
        & ((hue >= 7) | (saturation <= 34))
        & (hue <= 48)
        & (local_contrast >= 0.5)
    )
    white = (
        roi
        & context
        & (value >= 132)
        & (lightness >= 136)
        & (saturation <= 74)
        & (chroma <= 58)
        & (yellow_opponent < 16.0)
        & (local_contrast >= 4.0)
    )

    kernel = np.ones((3, 3), np.uint8)
    white = cv2.morphologyEx(white.astype(np.uint8), cv2.MORPH_CLOSE, kernel) > 0
    yellow = cv2.morphologyEx(yellow.astype(np.uint8), cv2.MORPH_CLOSE, kernel) > 0
    scale = max(1, int(round(rgb.shape[0] * rgb.shape[1] / (640 * 480))))
    white = _retain_linear_runs(white, length=7)
    white = _remove_small_components(white, 8 * scale)
    yellow = _retain_linear_runs(yellow)
    yellow = _keep_elongated_components(yellow, 5 * scale, 2.25)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    equalized = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    blurred = cv2.GaussianBlur(equalized, (5, 5), 0.0)
    median = float(np.median(blurred[roi])) if np.any(roi) else 96.0
    lower_canny = int(max(18, 0.55 * median))
    upper_canny = int(min(220, max(lower_canny + 24, 1.35 * median)))
    linear = cv2.Canny(blurred, lower_canny, upper_canny) > 0
    linear &= roi
    linear = _retain_linear_runs(linear, length=7)
    linear = _remove_small_components(linear, 5 * scale)
    return {"linear": linear, "white": white, "yellow": yellow}


def extract_candidate_road_paint_masks(
    rgb: np.ndarray, roi: np.ndarray
) -> dict[str, np.ndarray]:
    """Extract paint from the stable UE5 material palette with strict colors."""

    proposal = extract_road_paint_masks(rgb, roi)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    hue, saturation, value = [hsv[:, :, index].astype(np.int16) for index in range(3)]
    lightness = lab[:, :, 0].astype(np.float32)
    background = cv2.GaussianBlur(lightness, (0, 0), 5.0)
    red = rgb[:, :, 0].astype(np.int16)
    green = rgb[:, :, 1].astype(np.int16)
    blue = rgb[:, :, 2].astype(np.int16)
    chroma = np.maximum.reduce((red, green, blue)) - np.minimum.reduce((red, green, blue))
    opponent = (red + green) / 2.0 - blue
    asphalt = roi & (saturation <= 40) & (value >= 24) & (value <= 190)
    road_context = cv2.dilate(
        asphalt.astype(np.uint8), np.ones((17, 17), np.uint8), iterations=1
    ) > 0
    white = (
        road_context
        & (value >= 154)
        & (saturation <= 52)
        & (chroma <= 52)
        & ((lightness - background) >= 2.0)
    )
    yellow = (
        road_context
        & (hue >= 8)
        & (hue <= 31)
        & (saturation >= 42)
        & (value >= 112)
        & (opponent >= 25.0)
        & (red >= blue + 22)
        & (green >= blue + 18)
    )
    scale = max(1, int(round(rgb.shape[0] * rgb.shape[1] / (640 * 480))))
    white = _retain_linear_runs(white, length=7)
    white = _remove_small_components(white, 8 * scale)
    yellow = _retain_linear_runs(yellow, length=7)
    yellow = _keep_elongated_components(yellow, 5 * scale, 2.25)
    return {"linear": proposal["linear"], "white": white, "yellow": yellow}


def _directed_distances(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    if not np.any(source) or not np.any(target):
        return np.asarray([], dtype=np.float64)
    distance = cv2.distanceTransform((~target).astype(np.uint8), cv2.DIST_L2, 3)
    return distance[source].astype(np.float64)


def class_metrics(
    target: np.ndarray,
    candidate: np.ndarray,
    tolerance_px: float = 4.0,
    target_priority: bool = False,
) -> dict:
    target_count = int(np.count_nonzero(target))
    candidate_count = int(np.count_nonzero(candidate))
    if target_count == 0 or candidate_count == 0:
        return {
            "target_pixels": target_count,
            "candidate_pixels": candidate_count,
            "rmse_px": 64.0,
            "p95_px": 64.0,
            "max_px": 64.0,
            "tolerance_f1": 0.0,
            "area_ratio": None if target_count == 0 else candidate_count / target_count,
            "objective": 256.0,
        }
    forward = _directed_distances(target, candidate)
    reverse = _directed_distances(candidate, target)
    values = np.concatenate((forward, reverse))
    clipped = np.minimum(values, 64.0)
    precision = float(np.mean(reverse <= tolerance_px))
    recall = float(np.mean(forward <= tolerance_px))
    f1 = 0.0 if precision + recall == 0 else 2.0 * precision * recall / (precision + recall)
    area_ratio = candidate_count / target_count
    rmse = float(math.sqrt(np.mean(clipped ** 2)))
    p95 = float(np.quantile(clipped, 0.95))
    maximum = float(np.max(clipped))
    forward_clipped = np.minimum(forward, 64.0)
    reverse_clipped = np.minimum(reverse, 64.0)
    forward_rmse = float(math.sqrt(np.mean(forward_clipped ** 2)))
    reverse_rmse = float(math.sqrt(np.mean(reverse_clipped ** 2)))
    forward_p95 = float(np.quantile(forward_clipped, 0.95))
    reverse_p95 = float(np.quantile(reverse_clipped, 0.95))
    if target_priority:
        objective = (
            0.65 * forward_rmse
            + 0.20 * forward_p95
            + 12.0 * (1.0 - recall)
            + 0.10 * reverse_rmse
            + 4.0 * (1.0 - precision)
            + 2.0 * abs(math.log(max(area_ratio, 1e-6)))
        )
    else:
        objective = (
            0.55 * rmse
            + 0.20 * p95
            + 16.0 * (1.0 - f1)
            + 5.0 * abs(math.log(max(area_ratio, 1e-6)))
        )
    return {
        "target_pixels": target_count,
        "candidate_pixels": candidate_count,
        "rmse_px": rmse,
        "p95_px": p95,
        "max_px": maximum,
        "tolerance_precision": precision,
        "tolerance_recall": recall,
        "tolerance_f1": f1,
        "area_ratio": float(area_ratio),
        "target_to_candidate_rmse_px": forward_rmse,
        "target_to_candidate_p95_px": forward_p95,
        "candidate_to_target_rmse_px": reverse_rmse,
        "candidate_to_target_p95_px": reverse_p95,
        "objective": float(objective),
    }


def score_masks(
    target: dict[str, np.ndarray],
    candidate: dict[str, np.ndarray],
    weights: dict[str, float] | None = None,
) -> dict:
    if weights is None:
        defaults = {"manual": 10.0, "linear": 0.5, "white": 0.25, "yellow": 0.10}
        weights = {name: defaults[name] for name in target}
    else:
        weights = dict(weights)
    if set(weights) != set(target) or set(weights) != set(candidate):
        raise ValueError("paint-mask classes and weights do not match")
    if not all(math.isfinite(value) and value > 0 for value in weights.values()):
        raise ValueError("paint-mask weights must be finite and positive")
    metrics = {
        name: class_metrics(
            target[name], candidate[name], target_priority=(name == "manual")
        )
        for name in sorted(weights)
    }
    denominator = sum(weights.values())
    objective = sum(weights[name] * metrics[name]["objective"] for name in weights) / denominator
    return {"objective": float(objective), "classes": metrics, "weights": weights}


def overlay_masks(rgb: np.ndarray, masks: dict[str, np.ndarray]) -> np.ndarray:
    output = rgb.astype(np.float32).copy()
    colors = {
        "manual": np.asarray((255, 150, 20), dtype=np.float32),
        "linear": np.asarray((40, 255, 40), dtype=np.float32),
        "white": np.asarray((30, 255, 255), dtype=np.float32),
        "yellow": np.asarray((255, 40, 220), dtype=np.float32),
    }
    for name, color in colors.items():
        output[masks[name]] = 0.45 * output[masks[name]] + 0.55 * color
    return np.clip(output, 0, 255).astype(np.uint8)


def candidate_manual_geometry(masks: dict[str, np.ndarray]) -> np.ndarray:
    paint = masks["white"] | masks["yellow"]
    gradient = cv2.morphologyEx(
        paint.astype(np.uint8), cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)
    ) > 0
    gradient = _retain_linear_runs(gradient, length=5)
    # Yellow center paint is often only a few pixels wide, so preserve its
    # centerline in addition to the boundary of wider white paint.
    return gradient | masks["yellow"]


def candidate_road_surface_fraction(
    rgb: np.ndarray, roi: np.ndarray, masks: dict[str, np.ndarray]
) -> tuple[float, np.ndarray]:
    hsv = cv2.cvtColor(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    surface = (
        roi
        & (saturation <= 46)
        & (value >= 22)
        & (value <= 205)
    ) | masks["white"] | masks["yellow"]
    surface &= roi
    surface = cv2.morphologyEx(
        surface.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8)
    ) > 0
    surface = _remove_small_components(surface, 96)
    denominator = int(np.count_nonzero(roi))
    return (
        0.0 if denominator == 0 else float(np.count_nonzero(surface) / denominator),
        surface,
    )


def normalized_pose(camera: dict) -> np.ndarray:
    pose = camera.get("twin_pose") or {}
    return np.asarray([float(pose.get(key, 0.0)) for key in POSE_KEYS], dtype=float)


def pose_document(values: Iterable[float]) -> dict[str, float]:
    values = np.asarray(list(values), dtype=float)
    if values.shape != (len(POSE_KEYS),) or not np.all(np.isfinite(values)):
        raise ValueError("candidate pose is invalid")
    return {key: float(value) for key, value in zip(POSE_KEYS, values)}


def search_vectors(args) -> tuple[np.ndarray, np.ndarray]:
    """Validate explicit per-axis search bounds and refinement steps."""
    half_ranges = np.asarray(
        [float(getattr(args, name)) for name in RANGE_ARGUMENTS], dtype=float
    )
    steps = np.asarray(
        [float(getattr(args, name)) for name in STEP_ARGUMENTS], dtype=float
    )
    if not np.all(np.isfinite(half_ranges)) or np.any(half_ranges <= 0.0):
        raise ValueError("search half-ranges must be finite and positive")
    if not np.all(np.isfinite(steps)) or np.any(steps <= 0.0):
        raise ValueError("search steps must be finite and positive")
    if np.any(steps > 2.0 * half_ranges):
        raise ValueError("search steps cannot exceed the full per-axis range")
    if half_ranges[POSE_KEYS.index("height_offset_m")] > 5.0:
        raise ValueError("height half-range exceeds the diagnostic safety bound")
    bounded_angular = [
        POSE_KEYS.index("pitch_offset_deg"),
        POSE_KEYS.index("roll_offset_deg"),
        POSE_KEYS.index("fov_offset_deg"),
    ]
    if np.any(half_ranges[bounded_angular] > 45.0):
        raise ValueError("angular half-range exceeds the diagnostic safety bound")
    if half_ranges[POSE_KEYS.index("yaw_offset_deg")] > 180.0:
        raise ValueError("yaw half-range exceeds the diagnostic safety bound")
    if np.any(half_ranges[:2] > 15.0):
        raise ValueError("horizontal half-range exceeds the diagnostic safety bound")
    return half_ranges, steps


def candidate_key(values: np.ndarray) -> tuple[float, ...]:
    return tuple(float(round(value, 6)) for value in values)


def initial_candidates(initial: np.ndarray) -> list[np.ndarray]:
    zero_corrections = initial.copy()
    for key in (
        "right_offset_m",
        "height_offset_m",
        "pitch_offset_deg",
        "yaw_offset_deg",
        "roll_offset_deg",
        "fov_offset_deg",
    ):
        zero_corrections[POSE_KEYS.index(key)] = 0.0
    return [initial.copy(), zero_corrections]


def _radical_inverse(index: int, base: int) -> float:
    result, factor = 0.0, 1.0 / base
    while index:
        index, digit = divmod(index, base)
        result += digit * factor
        factor /= base
    return result


def low_discrepancy_candidates(
    lower: np.ndarray, upper: np.ndarray, count: int, seed: int
) -> list[np.ndarray]:
    if count <= 0:
        return []
    primes = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31)
    if len(lower) > len(primes):
        raise ValueError("low-discrepancy search dimensionality is unsupported")
    # A deterministic Cranley-Patterson rotation avoids always placing the
    # first Halton samples on the same parameter-space axes.
    shifts = np.random.default_rng(seed).random(len(lower))
    samples = []
    for index in range(1, count + 1):
        unit = np.asarray(
            [(_radical_inverse(index, base) + shifts[axis]) % 1.0 for axis, base in enumerate(primes[: len(lower)])]
        )
        samples.append(lower + unit * (upper - lower))
    return samples


def axis_sweep_candidates(
    reference: np.ndarray,
    half_ranges: np.ndarray,
    fractions: tuple[float, ...] = (-1.0, -0.5, 0.5, 1.0),
) -> list[np.ndarray]:
    """Deterministically probe every pose axis before joint random coverage."""
    reference = np.asarray(reference, dtype=float)
    half_ranges = np.asarray(half_ranges, dtype=float)
    if reference.shape != half_ranges.shape or reference.shape != (len(POSE_KEYS),):
        raise ValueError("axis sweep pose dimensions do not match")
    if not np.all(np.isfinite(reference)) or not np.all(np.isfinite(half_ranges)):
        raise ValueError("axis sweep inputs must be finite")
    candidates = []
    for parameter_index in range(len(POSE_KEYS)):
        for fraction in fractions:
            if not math.isfinite(fraction) or not -1.0 <= fraction <= 1.0 or fraction == 0.0:
                raise ValueError(
                    "axis sweep fractions must be finite nonzero values in [-1, 1]"
                )
            candidate = reference.copy()
            candidate[parameter_index] += fraction * half_ranges[parameter_index]
            candidates.append(candidate)
    return candidates


@dataclass
class RenderedCandidate:
    index: int
    values: np.ndarray
    rgb: np.ndarray
    depth_m: np.ndarray
    carla_frame: int
    sensor_timestamp: float
    sensor_transform: dict


class IsolatedRenderer:
    def __init__(
        self,
        world,
        carla_map,
        site: dict,
        camera: dict,
        width: int,
        height: int,
        timeout_seconds: float,
    ):
        self.world = world
        self.map = carla_map
        self.site = site
        self.camera = camera
        self.width = width
        self.height = height
        self.timeout_seconds = timeout_seconds
        self.original_settings = world.get_settings()
        settings = world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = 0.05
        settings.no_rendering_mode = False
        world.apply_settings(settings)

    def close(self):
        self.world.apply_settings(self.original_settings)

    def render(self, values: np.ndarray, index: int) -> RenderedCandidate:
        camera = camera_with_twin_pose(self.camera, pose_document(values))
        transform = compute_twin_camera_transform(self.map, self.site, camera)
        library = self.world.get_blueprint_library()
        blueprints = {
            "rgb": library.find("sensor.camera.rgb"),
            "depth": library.find("sensor.camera.depth"),
        }
        for name, blueprint in blueprints.items():
            configure_twin_camera_blueprint(
                blueprint, camera, self.width, self.height
            )
            if blueprint.has_attribute("role_name"):
                blueprint.set_attribute(
                    "role_name", f"v2x_inverse_render:{index}:{name}"
                )
        actors = {}
        frame_queues = {
            name: queue.Queue(maxsize=4) for name in blueprints
        }
        destroyed = {}
        try:
            for name, blueprint in blueprints.items():
                actors[name] = self.world.spawn_actor(blueprint, transform)
                actors[name].listen(frame_queues[name].put)
            target_frame = int(self.world.tick())
            frames = {}
            for name, frame_queue in frame_queues.items():
                for _ in range(8):
                    try:
                        candidate = frame_queue.get(timeout=self.timeout_seconds)
                    except queue.Empty as exc:
                        raise RenderError(
                            f"{name} candidate render timed out"
                        ) from exc
                    if int(candidate.frame) >= target_frame:
                        frames[name] = candidate
                        break
                if name not in frames:
                    raise RenderError(
                        f"{name} candidate did not reach the requested CARLA frame"
                    )
            frame_ids = {int(frame.frame) for frame in frames.values()}
            if frame_ids != {target_frame}:
                raise RenderError("RGB/depth candidate frames are not synchronized")
            image = frames["rgb"]
            raw = np.frombuffer(image.raw_data, dtype=np.uint8)
            expected = self.width * self.height * 4
            if raw.size != expected:
                raise RenderError("RGB candidate byte count is invalid")
            bgra = raw.reshape((self.height, self.width, 4))
            rgb = bgra[:, :, :3][:, :, ::-1].copy()
            depth_raw = np.frombuffer(frames["depth"].raw_data, dtype=np.uint8)
            if depth_raw.size != expected:
                raise RenderError("depth candidate byte count is invalid")
            depth_bgra = depth_raw.reshape((self.height, self.width, 4)).astype(
                np.float64
            )
            normalized_depth = (
                depth_bgra[:, :, 2]
                + depth_bgra[:, :, 1] * 256.0
                + depth_bgra[:, :, 0] * 65536.0
            ) / 16777215.0
            depth_m = (normalized_depth * 1000.0).astype(np.float32)
            sensor_transform = {
                "location": [
                    float(image.transform.location.x),
                    float(image.transform.location.y),
                    float(image.transform.location.z),
                ],
                "rotation": [
                    float(image.transform.rotation.pitch),
                    float(image.transform.rotation.yaw),
                    float(image.transform.rotation.roll),
                ],
            }
            return RenderedCandidate(
                index=index,
                values=values.copy(),
                rgb=rgb,
                depth_m=depth_m,
                carla_frame=int(image.frame),
                sensor_timestamp=float(image.timestamp),
                sensor_transform=sensor_transform,
            )
        finally:
            for name, actor in actors.items():
                try:
                    actor.stop()
                except Exception:
                    pass
                result = actor.destroy() if actor.is_alive else True
                destroyed[name] = bool(result is not False and not actor.is_alive)
            if set(destroyed) != set(blueprints) or not all(destroyed.values()):
                raise RenderError("temporary inverse-render sensor cleanup failed")


def near_occlusion_fraction(
    depth_m: np.ndarray, maximum_depth_m: float = 4.0
) -> float:
    if depth_m.ndim != 2 or depth_m.size == 0:
        raise ValueError("depth image is invalid")
    valid = np.isfinite(depth_m) & (depth_m > 0.05)
    if not np.any(valid):
        raise ValueError("depth image has no finite positive samples")
    return float(np.mean(valid & (depth_m < maximum_depth_m)))


def write_png(path: Path, rgb: np.ndarray):
    Image.fromarray(rgb).save(path)


def optimize(args) -> Path:
    validate_endpoint(args.host, args.port, args.container)
    if not args.authorized_isolated_worker:
        raise RenderError("--authorized-isolated-worker is required")
    if args.holdout_target:
        raise RenderError("holdout input is forbidden during inverse-render search")
    worker = inspect_worker(args.container)
    camera_path = Path(args.cameras_json).resolve()
    camera_bytes = camera_path.read_bytes()
    config = load_cameras_config(str(camera_path))
    camera = next((item for item in config["cameras"] if item["id"] == args.camera), None)
    if camera is None:
        raise RenderError("camera is absent from cameras JSON")
    roi_path = Path(args.road_rois).resolve()
    roi_bytes = roi_path.read_bytes()
    polygons = validate_rois(json.loads(roi_bytes), args.camera)
    roi = rasterize_rois(polygons, args.width, args.height)
    target_polylines = validate_target_polylines(json.loads(roi_bytes), args.camera)
    manual_target = rasterize_target_polylines(
        target_polylines, args.width, args.height
    )

    fit_path = Path(args.fit_target).resolve()
    dev_path = Path(args.dev_target).resolve()
    fit_bytes = fit_path.read_bytes()
    dev_bytes = dev_path.read_bytes()
    if sha256_bytes(fit_bytes) == sha256_bytes(dev_bytes):
        raise RenderError("fit and development targets must be distinct images")
    fit_rgb = read_rgb(fit_path, args.width, args.height)
    dev_rgb = read_rgb(dev_path, args.width, args.height)
    fit_masks = extract_road_paint_masks(fit_rgb, roi)
    dev_masks = extract_road_paint_masks(dev_rgb, roi)
    fit_masks["manual"] = manual_target
    dev_masks["manual"] = manual_target
    for name in ("manual", "linear", "white", "yellow"):
        if np.count_nonzero(fit_masks[name]) < args.minimum_class_pixels:
            raise RenderError(f"fit target has insufficient {name} paint pixels")
        if np.count_nonzero(dev_masks[name]) < args.minimum_class_pixels:
            raise RenderError(f"development target has insufficient {name} paint pixels")
    target_consistency = score_masks(fit_masks, dev_masks)
    base_weights = {"manual": 10.0, "linear": 0.5, "white": 0.25, "yellow": 0.10}
    search_weights = {
        name: float(
            base_weights[name]
            * max(0.10, target_consistency["classes"][name]["tolerance_f1"])
        )
        for name in base_weights
    }

    output = Path(args.output_dir).resolve()
    if output.exists():
        raise RenderError("refusing to overwrite inverse-render output")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    (temporary / "renders").mkdir()
    write_png(temporary / "fit-target-mask-overlay.png", overlay_masks(fit_rgb, fit_masks))
    write_png(temporary / "dev-target-mask-overlay.png", overlay_masks(dev_rgb, dev_masks))
    Image.fromarray((roi.astype(np.uint8) * 255)).save(temporary / "road-roi.png")

    import carla

    client = carla.Client(args.host, args.port)
    client.set_timeout(args.timeout_seconds)
    world = client.get_world()
    carla_map = world.get_map()
    opendrive_sha256 = sha256_bytes(carla_map.to_opendrive().encode("utf-8"))
    if carla_map.name != EXPECTED_MAP_NAME or opendrive_sha256 != EXPECTED_OPENDRIVE_SHA256:
        raise RenderError("isolated UE5 Richmond map fingerprint is invalid")
    if list(world.get_actors().filter("sensor.camera.*")):
        raise RenderError("isolated worker contains pre-existing camera sensors")

    renderer = IsolatedRenderer(
        world, carla_map, config["site"], camera,
        args.width, args.height, args.timeout_seconds,
    )

    half_ranges, initial_steps = search_vectors(args)
    initial = normalized_pose(camera)
    reference = initial_candidates(initial)[1]
    lower = reference - half_ranges
    upper = reference + half_ranges
    # Preserve a physically valid FOV after applying the nominal camera model.
    fov_index = POSE_KEYS.index("fov_offset_deg")
    lower[fov_index] = max(lower[fov_index], -35.0)
    upper[fov_index] = min(upper[fov_index], 35.0)
    evaluations: list[dict] = []
    evaluated: dict[tuple[float, ...], dict] = {}
    def evaluate(values: np.ndarray, phase: str) -> dict | None:
        values = np.minimum(np.maximum(np.asarray(values, dtype=float), lower), upper)
        key = candidate_key(values)
        if key in evaluated:
            return evaluated[key]
        if len(evaluations) >= args.max_renders:
            return None
        rendered = renderer.render(values, len(evaluations))
        masks = extract_candidate_road_paint_masks(rendered.rgb, roi)
        masks["manual"] = candidate_manual_geometry(masks)
        fit_score = score_masks(fit_masks, masks, search_weights)
        dev_score = score_masks(dev_masks, masks, search_weights)
        road_surface_fraction, road_surface = candidate_road_surface_fraction(
            rendered.rgb, roi, masks
        )
        road_surface_minimum = 0.72
        road_surface_gate_passed = road_surface_fraction >= road_surface_minimum
        road_surface_penalty = (
            0.0
            if road_surface_gate_passed
            else 500.0 + 500.0 * (road_surface_minimum - road_surface_fraction)
        )
        near_fraction = near_occlusion_fraction(rendered.depth_m)
        near_fraction_maximum = 0.02
        near_occlusion_gate_passed = near_fraction <= near_fraction_maximum
        near_occlusion_penalty = (
            0.0
            if near_occlusion_gate_passed
            else 500.0 + 1000.0 * (near_fraction - near_fraction_maximum)
        )
        combined = float(
            0.67 * fit_score["objective"]
            + 0.33 * dev_score["objective"]
            + road_surface_penalty
            + near_occlusion_penalty
        )
        record = {
            "index": rendered.index,
            "phase": phase,
            "twin_pose": pose_document(values),
            "combined_objective": combined,
            "fit": fit_score,
            "development": dev_score,
            "road_surface_fraction": road_surface_fraction,
            "road_surface_minimum": road_surface_minimum,
            "road_surface_penalty": road_surface_penalty,
            "road_surface_gate_passed": road_surface_gate_passed,
            "near_depth_threshold_m": 4.0,
            "near_occlusion_fraction": near_fraction,
            "near_occlusion_fraction_maximum": near_fraction_maximum,
            "near_occlusion_penalty": near_occlusion_penalty,
            "near_occlusion_gate_passed": near_occlusion_gate_passed,
            "view_gate_passed": road_surface_gate_passed and near_occlusion_gate_passed,
            "carla_frame": rendered.carla_frame,
            "sensor_timestamp": rendered.sensor_timestamp,
            "sensor_transform": rendered.sensor_transform,
        }
        evaluations.append(record)
        evaluated[key] = record
        render_dir = temporary / "renders" / f"candidate-{rendered.index:04d}"
        render_dir.mkdir()
        write_png(render_dir / "rgb.png", rendered.rgb)
        write_png(render_dir / "paint-mask-overlay.png", overlay_masks(rendered.rgb, masks))
        road_overlay = rendered.rgb.astype(np.float32).copy()
        road_overlay[road_surface] = (
            0.45 * road_overlay[road_surface]
            + 0.55 * np.asarray((40, 120, 255), dtype=np.float32)
        )
        write_png(
            render_dir / "road-surface-overlay.png",
            np.clip(road_overlay, 0, 255).astype(np.uint8),
        )
        (render_dir / "score.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return record

    try:
        seeds = initial_candidates(initial)
        seeds.extend(axis_sweep_candidates(reference, half_ranges))
        global_count = max(0, min(args.global_candidates, args.max_renders - len(seeds)))
        seeds.extend(low_discrepancy_candidates(lower, upper, global_count, args.seed))
        for values in seeds:
            if len(evaluations) >= args.max_renders:
                break
            evaluate(values, "global")

        ranked = sorted(evaluations, key=lambda item: item["combined_objective"])
        basins = [
            np.asarray([item["twin_pose"][key] for key in POSE_KEYS], dtype=float)
            for item in ranked[: args.refine_basins]
        ]
        for basin_index, values in enumerate(basins):
            best = evaluate(values, f"refine-{basin_index}")
            steps = initial_steps.copy()
            for _round in range(args.refine_rounds):
                if len(evaluations) >= args.max_renders:
                    break
                improved = False
                for parameter_index in range(len(POSE_KEYS)):
                    if len(evaluations) >= args.max_renders:
                        break
                    for direction in (-1.0, 1.0):
                        candidate = values.copy()
                        candidate[parameter_index] += direction * steps[parameter_index]
                        result = evaluate(candidate, f"refine-{basin_index}")
                        if result is not None and (
                            best is None
                            or result["combined_objective"] < best["combined_objective"]
                        ):
                            values = np.asarray(
                                [result["twin_pose"][key] for key in POSE_KEYS], dtype=float
                            )
                            best = result
                            improved = True
                steps *= 0.5 if not improved else 0.72
    finally:
        renderer.close()

    remaining_sensors = []
    for _ in range(20):
        remaining_sensors = list(
            client.get_world().get_actors().filter("sensor.camera.*")
        )
        if not remaining_sensors:
            break
        try:
            client.get_world().wait_for_tick(seconds=0.25)
        except RuntimeError:
            time.sleep(0.05)
    if remaining_sensors:
        raise RenderError("camera sensors remain after inverse-render search")
    if not evaluations:
        raise RenderError("inverse-render search produced no candidates")
    ranked = sorted(evaluations, key=lambda item: item["combined_objective"])
    best = ranked[0]
    winner_dir = temporary / "renders" / f"candidate-{best['index']:04d}"
    shutil.copy2(winner_dir / "rgb.png", temporary / "best-rgb.png")
    shutil.copy2(winner_dir / "paint-mask-overlay.png", temporary / "best-mask-overlay.png")
    shutil.copy2(
        winner_dir / "road-surface-overlay.png",
        temporary / "best-road-surface-overlay.png",
    )
    report = {
        "schema": OUTPUT_SCHEMA,
        "acceptance_eligible": False,
        "created_at_utc": utc_now(),
        "camera_id": args.camera,
        "camera_config": {
            "path": str(camera_path),
            "sha256": sha256_bytes(camera_bytes),
        },
        "road_rois": {"path": str(roi_path), "sha256": sha256_bytes(roi_bytes)},
        "fit_target": {"path": str(fit_path), "sha256": sha256_bytes(fit_bytes)},
        "development_target": {"path": str(dev_path), "sha256": sha256_bytes(dev_bytes)},
        "holdout_target_consumed": False,
        "worker": worker,
        "map_name": carla_map.name,
        "opendrive_sha256": opendrive_sha256,
        "resolution": [args.width, args.height],
        "search": {
            "seed": args.seed,
            "maximum_renders": args.max_renders,
            "render_count": len(evaluations),
            "global_candidates": global_count,
            "refine_basins": args.refine_basins,
            "refine_rounds": args.refine_rounds,
            "pose_keys": list(POSE_KEYS),
            "initial_pose": pose_document(initial),
            "physical_reference_pose": pose_document(reference),
            "configured_half_ranges": pose_document(half_ranges),
            "configured_initial_steps": pose_document(initial_steps),
            "lower": pose_document(lower),
            "upper": pose_document(upper),
            "target_fit_development_consistency": target_consistency,
            "class_weights": search_weights,
        },
        "best": best,
        "ranked_candidate_indices": [item["index"] for item in ranked],
        "evaluations": evaluations,
        "temporary_sensors_destroyed": True,
        "candidate_recommendation": (
            "evaluate_once_on_fresh_untouched_holdout"
            if best["view_gate_passed"]
            else "reject_no_candidate_passed_the_view_gate"
        ),
        "limitations": [
            "diagnostic_inverse_render_search_not_camera_calibration_acceptance",
            "physical_intrinsics_and_distortion_are_unmeasured",
            "map_geometry_is_not_independently_surveyed",
            "road_rois_and_color_masks_are_proposals_not_annotation_truth",
            "visible_map_paint_may_not_match_exported_opendrive_geometry",
        ],
    }
    (temporary / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, output)
    return output / "report.json"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", required=True, choices=("ch1", "ch2", "ch3", "ch4"))
    parser.add_argument("--fit-target", required=True)
    parser.add_argument("--dev-target", required=True)
    parser.add_argument("--holdout-target", help=argparse.SUPPRESS)
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--road-rois", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--host", default=EXPECTED_HOST)
    parser.add_argument("--port", type=int, default=EXPECTED_PORT)
    parser.add_argument("--container", default=EXPECTED_CONTAINER)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--authorized-isolated-worker", action="store_true")
    parser.add_argument("--seed", type=int, default=20260712)
    parser.add_argument("--max-renders", type=int, default=192)
    parser.add_argument("--global-candidates", type=int, default=96)
    parser.add_argument("--refine-basins", type=int, default=4)
    parser.add_argument("--refine-rounds", type=int, default=3)
    parser.add_argument("--minimum-class-pixels", type=int, default=40)
    for argument, default in zip(RANGE_ARGUMENTS, DEFAULT_HALF_RANGES):
        parser.add_argument(
            "--" + argument.replace("_", "-"),
            type=float,
            default=float(default),
        )
    for argument, default in zip(STEP_ARGUMENTS, DEFAULT_STEPS):
        parser.add_argument(
            "--" + argument.replace("_", "-"),
            type=float,
            default=float(default),
        )
    args = parser.parse_args(argv)
    if not 320 <= args.width <= 1280 or not 240 <= args.height <= 960:
        parser.error("search dimensions are outside the bounded range")
    if args.max_renders < 2 or args.global_candidates < 0:
        parser.error("render counts are invalid")
    if args.refine_basins < 1 or args.refine_rounds < 0:
        parser.error("refinement settings are invalid")
    try:
        search_vectors(args)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        result = optimize(args)
    except (OSError, ValueError, RenderError) as exc:
        raise SystemExit(str(exc)) from exc
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
