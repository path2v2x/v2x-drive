#!/usr/bin/env python3
"""Fit a development-only four-camera Tier-B static model.

This tool is source-only: it reads frozen development artifacts, never imports
CARLA, rejects every holdout input, and can never emit a release decision.
"""

from __future__ import annotations

import argparse
import copy
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
import tempfile
from typing import Iterable

from importlib import metadata

import numpy as np

from digital_twin_bridge.camera_projection import (
    PARAMETER_NAMES,
    ground_horizon_line,
    production_round_trip,
    project_direction,
    project_world,
)
from digital_twin_bridge.twin_camera_rig import (
    absolute_twin_model,
    heading_to_carla_yaw,
    horizontal_fov_deg,
)


SCHEMA = "v2x-tier-b-static-development-manifest/v2"
OUTPUT_SCHEMA = "v2x-tier-b-static-development-fit/v2"
CAMERAS = ("ch1", "ch2", "ch3", "ch4")
SPLITS = ("fit", "development")
CLASSES = ("road_edge", "lane_paint", "crosswalk_paint")
DEFAULT_FORBIDDEN_ROOTS = (
    "/home/path/V2XCarla/v2x-evidence/calibration/20260713T192217Z-untouched-holdout-candidate-vault",
)
SCALES = np.asarray((1.0, 1.0, 0.5, 5.0, 5.0, 2.0, 5.0))
LOWER_DELTA = np.asarray((-4.0, -4.0, -2.0, -20.0, -30.0, -12.0, -20.0))
UPPER_DELTA = -LOWER_DELTA
BASIN_NEIGHBORHOOD_Z = np.full(28, 0.25)
POINT_GATES = {"rmse_px": 10.0, "p95_px": 16.0, "max_px": 24.0}
ROAD_GATES = {"rmse_px": 6.0, "max_px": 12.0}
CONDITION_MAX = 1e8
MIN_MULTISTARTS = 8
JACOBIAN_STEP = 1e-5
EPOCH_STABILITY_MAX_SPAN_Z = 0.25
SPLIT_NEAR_DUPLICATE_IMAGE_PX = 0.05
SPLIT_NEAR_DUPLICATE_DIRECTION_RAD = 1e-6
SPLIT_POINT_WORLD_DISTANCE_M = 1e-6
SPLIT_POLYLINE_WORLD_DISTANCE_M = 0.05
MIN_HORIZON_CHORD_REFERENCE_PX = 2.0


class DevelopmentFitError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_forbidden(path: Path, roots: Iterable[Path]) -> bool:
    text = str(path).casefold()
    if "holdout" in text:
        return True
    return any(path == root or root in path.parents for root in roots)


def _absolute_path(value: object, label: str, roots: tuple[Path, ...]) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise DevelopmentFitError(f"{label} path must be absolute and normalized")
    if _is_forbidden(path, roots):
        raise DevelopmentFitError(f"{label} path is forbidden or holdout-derived")
    current = Path("/")
    for component in path.parts[1:]:
        current /= component
        if current.is_symlink():
            raise DevelopmentFitError(f"{label} path contains a symbolic link")
    return path


def _binding(value: object, label: str, roots: tuple[Path, ...]) -> dict:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise DevelopmentFitError(f"{label} binding is invalid")
    path = _absolute_path(value["path"], label, roots)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise DevelopmentFitError(f"{label} must be a single-link regular file")
    expected = value["sha256"]
    if not isinstance(expected, str) or len(expected) != 64 or _sha256(path) != expected:
        raise DevelopmentFitError(f"{label} hash mismatch")
    return {"path": str(path), "sha256": expected, "bytes": path.stat().st_size}


def _finite_vector(value: object, length: int, label: str) -> np.ndarray:
    result = np.asarray(value, dtype=float)
    if result.shape != (length,) or not np.isfinite(result).all():
        raise DevelopmentFitError(f"{label} must contain {length} finite values")
    return result


def _pixel(value: object, width: int, height: int, label: str) -> np.ndarray:
    result = _finite_vector(value, 2, label)
    if not (0 <= result[0] < width and 0 <= result[1] < height):
        raise DevelopmentFitError(f"{label} lies outside the native image")
    return result


def _split(item: dict, epochs: dict, label: str) -> str:
    split = item.get("split")
    if split not in SPLITS or "holdout" in str(split).casefold():
        raise DevelopmentFitError(f"{label} split is forbidden")
    epoch = item.get("epoch_id")
    if epoch not in epochs or epochs[epoch]["split"] != split:
        raise DevelopmentFitError(f"{label} epoch/split binding is invalid")
    return split


def _identity(item: dict, seen_ids: set[str], label: str) -> tuple[str, str]:
    item_id = item.get("id")
    feature_id = item.get("physical_feature_id")
    if not isinstance(item_id, str) or not item_id or item_id in seen_ids:
        raise DevelopmentFitError(f"{label} ID is blank or duplicated")
    if not isinstance(feature_id, str) or not feature_id:
        raise DevelopmentFitError(f"{label} physical feature ID is blank")
    if item.get("provenance") not in {
        "manually_verified_unique", "manually_traced_geometry"
    }:
        raise DevelopmentFitError(f"{label} provenance is not independently reviewed")
    seen_ids.add(item_id)
    return item_id, feature_id


def _line(value: object, label: str) -> np.ndarray:
    line = _finite_vector(value, 3, label)
    norm = float(np.linalg.norm(line[:2]))
    if norm <= 1e-12:
        raise DevelopmentFitError(f"{label} is degenerate")
    line /= norm
    if line[1] < 0 or (line[1] == 0 and line[0] < 0):
        line = -line
    return line


def _line_image_points(line: np.ndarray, width: int, height: int) -> np.ndarray:
    candidates = []
    a, b, c = line
    if abs(b) > 1e-12:
        candidates.extend(((0.0, -c / b),
                           (width - 1.0, -(a * (width - 1.0) + c) / b)))
    if abs(a) > 1e-12:
        candidates.extend(((-c / a, 0.0),
                           (-(b * (height - 1.0) + c) / a, height - 1.0)))
    valid = []
    for item in candidates:
        if (-1e-6 <= item[0] <= width - 1 + 1e-6
                and -1e-6 <= item[1] <= height - 1 + 1e-6
                and all(np.linalg.norm(np.asarray(item) - np.asarray(prior)) > 1e-6
                        for prior in valid)):
            valid.append(item)
    if len(valid) < 2:
        return np.empty((0, 2))
    pairs = [
        (float(np.linalg.norm(np.asarray(valid[left]) - np.asarray(valid[right]))),
         valid[left], valid[right])
        for left in range(len(valid)) for right in range(left + 1, len(valid))
    ]
    _native_distance, first, second = max(pairs, key=lambda item: item[0])
    midpoint = (np.asarray(first) + np.asarray(second)) / 2.0
    reference_distance = float(np.linalg.norm(
        (np.asarray(first) - np.asarray(second))
        * np.asarray((1280.0 / width, 960.0 / height))
    ))
    chord_too_short = (
        reference_distance < MIN_HORIZON_CHORD_REFERENCE_PX
        and not math.isclose(reference_distance, MIN_HORIZON_CHORD_REFERENCE_PX,
                             rel_tol=1e-12, abs_tol=1e-9)
    )
    if (chord_too_short
            or not (1e-6 < midpoint[0] < width - 1 - 1e-6
                    and 1e-6 < midpoint[1] < height - 1 - 1e-6)):
        return np.empty((0, 2))
    return np.asarray((first, second), dtype=float)


def validate_document(document: object, manifest_sha256: str, extra_forbidden=()) -> dict:
    if not isinstance(document, dict):
        raise DevelopmentFitError("manifest must be an object")
    if document.get("schema") != SCHEMA or document.get("acceptance_eligible") is not False:
        raise DevelopmentFitError("manifest lacks the development-only schema")
    if document.get("coordinate_gauge") != "carla_map_exact_no_global_se2":
        raise DevelopmentFitError("manifest must freeze the CARLA map gauge")
    if any("holdout" in str(value).casefold() for value in document.get("splits", [])):
        raise DevelopmentFitError("holdout splits are forbidden")
    if tuple(document.get("splits") or ()) != SPLITS:
        raise DevelopmentFitError("manifest split contract is invalid")
    declared_roots = document.get("forbidden_roots")
    if not isinstance(declared_roots, list) or not declared_roots or any(
        not isinstance(value, str) or not Path(value).is_absolute() for value in declared_roots
    ):
        raise DevelopmentFitError("forbidden roots must be a nonempty absolute-path list")
    root_values = [*DEFAULT_FORBIDDEN_ROOTS, *declared_roots, *extra_forbidden]
    roots = tuple(Path(value).resolve(strict=False) for value in root_values)
    if not roots:
        raise DevelopmentFitError("forbidden roots are required")
    map_value = document.get("map")
    if not isinstance(map_value, dict) or set(map_value) != {
        "candidate_id", "opendrive", "topology"
    }:
        raise DevelopmentFitError("map binding is invalid")
    if not isinstance(map_value["candidate_id"], str) or not map_value["candidate_id"]:
        raise DevelopmentFitError("map candidate ID is missing")
    artifacts = {
        "opendrive": _binding(map_value["opendrive"], "OpenDRIVE", roots),
        "topology": _binding(map_value["topology"], "topology summary", roots),
        "cameras_json": _binding(document.get("cameras_json"), "cameras JSON", roots),
    }
    config = json.loads(Path(artifacts["cameras_json"]["path"]).read_bytes())
    configured_cameras = {item.get("id"): item for item in config.get("cameras", [])}
    cameras_value = document.get("cameras")
    if not isinstance(cameras_value, dict) or tuple(sorted(cameras_value)) != CAMERAS:
        raise DevelopmentFitError("manifest must contain exactly ch1/ch2/ch3/ch4")
    cameras = {}
    global_feature_splits: dict[str, set[str]] = defaultdict(set)
    global_frame_splits: dict[str, set[str]] = defaultdict(set)
    for camera_id in CAMERAS:
        value = cameras_value[camera_id]
        if not isinstance(value, dict):
            raise DevelopmentFitError(f"{camera_id} record is invalid")
        width, height = int(value.get("width", 0)), int(value.get("height", 0))
        if width < 64 or height < 64:
            raise DevelopmentFitError(f"{camera_id} native dimensions are invalid")
        baseline = _finite_vector(value.get("baseline"), 7, f"{camera_id} baseline")
        if not 1 < baseline[6] < 179:
            raise DevelopmentFitError(f"{camera_id} baseline FOV is invalid")
        anchor = _finite_vector(value.get("anchor_location"), 3, f"{camera_id} anchor")
        base = value.get("production_base")
        if not isinstance(base, dict) or set(base) != {"pitch_deg", "yaw_deg", "roll_deg", "fov_deg"}:
            raise DevelopmentFitError(f"{camera_id} production base is invalid")
        _finite_vector(list(base.values()), 4, f"{camera_id} production base")
        configured = configured_cameras.get(camera_id)
        if not isinstance(configured, dict):
            raise DevelopmentFitError(f"{camera_id} is absent from cameras JSON")
        intrinsics = configured.get("intrinsics") or {}
        if int(intrinsics.get("width", 0)) != width or int(intrinsics.get("height", 0)) != height:
            raise DevelopmentFitError(f"{camera_id} dimensions disagree with cameras JSON")
        expected_base = {
            "pitch_deg": float(configured.get("pitch_deg")),
            "yaw_deg": heading_to_carla_yaw(
                float(configured.get("heading_deg")), float(configured.get("yaw_deg"))
            ),
            "roll_deg": float(configured.get("roll_deg", 0.0)),
            "fov_deg": horizontal_fov_deg(intrinsics),
        }
        if any(not math.isclose(float(base[key]), expected_base[key], abs_tol=1e-9, rel_tol=0)
               for key in expected_base):
            raise DevelopmentFitError(f"{camera_id} production base disagrees with cameras JSON")
        expected_absolute = absolute_twin_model(
            anchor, expected_base, configured.get("twin_pose") or {}
        )
        expected_baseline = np.asarray([
            *expected_absolute["location"], expected_absolute["pitch_deg"],
            expected_absolute["yaw_deg"], expected_absolute["roll_deg"],
            expected_absolute["fov_deg"],
        ])
        if not np.allclose(baseline, expected_baseline, atol=1e-9, rtol=0):
            raise DevelopmentFitError(f"{camera_id} baseline disagrees with cameras JSON and anchor")
        epochs_value = value.get("epochs")
        if not isinstance(epochs_value, list):
            raise DevelopmentFitError(f"{camera_id} epochs are missing")
        epochs = {}
        raw_hashes_by_split = {split: set() for split in SPLITS}
        for epoch in epochs_value:
            if not isinstance(epoch, dict) or set(epoch) != {"id", "split", "frame", "median_members"}:
                raise DevelopmentFitError(f"{camera_id} epoch is malformed")
            epoch_id, split = epoch["id"], epoch["split"]
            if not isinstance(epoch_id, str) or not epoch_id or epoch_id in epochs or split not in SPLITS:
                raise DevelopmentFitError(f"{camera_id} epoch ID or split is invalid")
            frame = _binding(epoch["frame"], f"{camera_id} {epoch_id} frame", roots)
            members_value = epoch["median_members"]
            if not isinstance(members_value, list) or not members_value:
                raise DevelopmentFitError(f"{camera_id} temporal median members are invalid")
            members = [
                _binding(item, f"{camera_id} {epoch_id} median member {index}", roots)
                for index, item in enumerate(members_value)
            ]
            member_hashes = [item["sha256"] for item in members]
            if len(set(member_hashes)) != len(member_hashes):
                raise DevelopmentFitError(f"{camera_id} temporal median members are duplicated")
            hashes = {frame["sha256"], *member_hashes}
            if raw_hashes_by_split[split] & hashes:
                raise DevelopmentFitError(f"{camera_id} duplicates a frame within {split}")
            raw_hashes_by_split[split].update(hashes)
            for digest in hashes:
                global_frame_splits[digest].add(split)
            epochs[epoch_id] = {
                "split": split,
                "frame": frame,
                "median_members": sorted(members, key=lambda item: (item["sha256"], item["path"])),
            }
        if raw_hashes_by_split["fit"] & raw_hashes_by_split["development"]:
            raise DevelopmentFitError(f"{camera_id} temporal median crosses splits")
        seen_ids: set[str] = set()
        observations = {name: [] for name in ("points", "polylines", "horizons", "vanishing")}
        for item in value.get("points", []):
            item_id, feature_id = _identity(item, seen_ids, f"{camera_id} point")
            split = _split(item, epochs, f"{camera_id} point {item_id}")
            global_feature_splits[feature_id].add(split)
            uncertainty = float(item.get("uncertainty_px", 0))
            if not math.isfinite(uncertainty) or uncertainty <= 0:
                raise DevelopmentFitError(f"{camera_id} point uncertainty is invalid")
            observations["points"].append({
                "id": item_id, "physical_feature_id": feature_id, "epoch_id": item["epoch_id"],
                "split": split, "real_uv": _pixel(item.get("real_uv"), width, height, item_id),
                "world_xyz": _finite_vector(item.get("world_xyz"), 3, item_id),
                "uncertainty_px": uncertainty,
            })
        for item in value.get("polylines", []):
            item_id, feature_id = _identity(item, seen_ids, f"{camera_id} polyline")
            split = _split(item, epochs, f"{camera_id} polyline {item_id}")
            global_feature_splits[feature_id].add(split)
            feature_class = item.get("class")
            if feature_class not in CLASSES:
                raise DevelopmentFitError(f"{camera_id} polyline class is invalid")
            real = np.asarray(item.get("real_vertices"), dtype=float)
            world = np.asarray(item.get("world_vertices"), dtype=float)
            if real.ndim != 2 or real.shape[1:] != (2,) or len(real) < 2 or not np.isfinite(real).all():
                raise DevelopmentFitError(f"{camera_id} real polyline is invalid")
            if world.shape != (len(real), 3) or not np.isfinite(world).all():
                raise DevelopmentFitError(f"{camera_id} world polyline is invalid")
            if np.any(real[:, 0] < 0) or np.any(real[:, 0] >= width) or np.any(real[:, 1] < 0) or np.any(real[:, 1] >= height):
                raise DevelopmentFitError(f"{camera_id} polyline is outside the image")
            if np.linalg.norm(np.diff(real, axis=0), axis=1).sum() < 2:
                raise DevelopmentFitError(f"{camera_id} polyline is degenerate")
            uncertainty = float(item.get("uncertainty_px", 0))
            if not math.isfinite(uncertainty) or uncertainty <= 0:
                raise DevelopmentFitError(f"{camera_id} polyline uncertainty is invalid")
            observations["polylines"].append({
                "id": item_id, "physical_feature_id": feature_id, "epoch_id": item["epoch_id"],
                "split": split, "class": feature_class, "real_vertices": real,
                "world_vertices": world, "uncertainty_px": uncertainty,
            })
        for item in value.get("horizons", []):
            item_id, feature_id = _identity(item, seen_ids, f"{camera_id} horizon")
            split = _split(item, epochs, f"{camera_id} horizon {item_id}")
            global_feature_splits[feature_id].add(split)
            uncertainty = float(item.get("uncertainty_px", 0))
            if not math.isfinite(uncertainty) or uncertainty <= 0:
                raise DevelopmentFitError(f"{camera_id} horizon uncertainty is invalid")
            real_line = _line(item.get("real_line"), item_id)
            if len(_line_image_points(real_line, width, height)) != 2:
                raise DevelopmentFitError(f"{camera_id} horizon does not cross the native image")
            observations["horizons"].append({
                "id": item_id, "physical_feature_id": feature_id, "epoch_id": item["epoch_id"],
                "split": split, "real_line": real_line,
                "uncertainty_px": uncertainty,
            })
        for item in value.get("vanishing", []):
            item_id, feature_id = _identity(item, seen_ids, f"{camera_id} vanishing")
            split = _split(item, epochs, f"{camera_id} vanishing {item_id}")
            global_feature_splits[feature_id].add(split)
            uncertainty = float(item.get("uncertainty_px", 0))
            if not math.isfinite(uncertainty) or uncertainty <= 0:
                raise DevelopmentFitError(f"{camera_id} vanishing uncertainty is invalid")
            world_direction = _finite_vector(item.get("world_direction"), 3, item_id)
            if np.linalg.norm(world_direction) <= 1e-12:
                raise DevelopmentFitError(f"{camera_id} vanishing direction is degenerate")
            observations["vanishing"].append({
                "id": item_id, "physical_feature_id": feature_id, "epoch_id": item["epoch_id"],
                "split": split, "world_direction": world_direction,
                "real_uv": _pixel(item.get("real_uv"), width, height, item_id),
                "uncertainty_px": uncertainty,
            })
        _validate_denominators(camera_id, width, height, epochs, observations)
        cameras[camera_id] = {
            "width": width, "height": height, "baseline": baseline, "anchor_location": anchor,
            "production_base": {key: float(item) for key, item in base.items()},
            "epochs": epochs, **observations,
        }
    conflicts = sorted(key for key, splits in global_feature_splits.items() if len(splits) != 1)
    if conflicts:
        raise DevelopmentFitError(f"physical features cross fit/development splits: {conflicts}")
    if any(len(splits) != 1 for splits in global_frame_splits.values()):
        raise DevelopmentFitError("raw frame hashes cross fit/development splits")
    _validate_geometry_split_isolation(cameras)
    _validate_directional_split_isolation(cameras)
    return {
        "manifest_sha256": manifest_sha256,
        "map": {"candidate_id": map_value["candidate_id"], "opendrive": artifacts["opendrive"],
                "topology": artifacts["topology"]},
        "cameras_json": artifacts["cameras_json"], "cameras": cameras,
    }


def _validate_geometry_split_isolation(cameras: dict) -> None:
    """Reject copied geometry even when a caller relabels its feature ID."""
    for camera_id, camera in cameras.items():
        fit_points = [item for item in camera["points"] if item["split"] == "fit"]
        dev_points = [item for item in camera["points"] if item["split"] == "development"]
        for left in fit_points:
            if any(np.linalg.norm(left["world_xyz"] - right["world_xyz"])
                   < SPLIT_POINT_WORLD_DISTANCE_M
                   for right in dev_points):
                raise DevelopmentFitError(f"{camera_id} copies a point across fit/development")
        fit_lines = [item for item in camera["polylines"] if item["split"] == "fit"]
        dev_lines = [item for item in camera["polylines"] if item["split"] == "development"]
        for left in fit_lines:
            for right in dev_lines:
                if _polylines_overlap(left["world_vertices"], right["world_vertices"]):
                    raise DevelopmentFitError(f"{camera_id} copies/resamples a polyline across fit/development")
        for point in dev_points:
            if any(_point_polyline_distance(point["world_xyz"], line["world_vertices"])
                   < SPLIT_POLYLINE_WORLD_DISTANCE_M
                   for line in fit_lines):
                raise DevelopmentFitError(f"{camera_id} reuses fit polyline geometry as a development point")
        for point in fit_points:
            if any(_point_polyline_distance(point["world_xyz"], line["world_vertices"])
                   < SPLIT_POLYLINE_WORLD_DISTANCE_M
                   for line in dev_lines):
                raise DevelopmentFitError(f"{camera_id} reuses fit point geometry as a development polyline")
    fit_points = [(camera_id, item) for camera_id, camera in cameras.items()
                  for item in camera["points"] if item["split"] == "fit"]
    dev_points = [(camera_id, item) for camera_id, camera in cameras.items()
                  for item in camera["points"] if item["split"] == "development"]
    fit_lines = [(camera_id, item) for camera_id, camera in cameras.items()
                 for item in camera["polylines"] if item["split"] == "fit"]
    dev_lines = [(camera_id, item) for camera_id, camera in cameras.items()
                 for item in camera["polylines"] if item["split"] == "development"]
    if any(np.linalg.norm(left[1]["world_xyz"] - right[1]["world_xyz"])
           < SPLIT_POINT_WORLD_DISTANCE_M
           for left in fit_points for right in dev_points):
        raise DevelopmentFitError("point geometry crosses camera fit/development splits")
    if any(_polylines_overlap(left["world_vertices"], right["world_vertices"])
           for _left_camera, left in fit_lines for _right_camera, right in dev_lines):
        raise DevelopmentFitError("polyline geometry crosses camera fit/development splits")
    if any(_polylines_adjacent(left["world_vertices"], right["world_vertices"])
           for _left_camera, left in fit_lines for _right_camera, right in dev_lines):
        raise DevelopmentFitError("adjacent polyline geometry crosses camera fit/development splits")
    if any(_point_polyline_distance(point["world_xyz"], line["world_vertices"])
           < SPLIT_POLYLINE_WORLD_DISTANCE_M
           for _camera, point in dev_points for _line_camera, line in fit_lines):
        raise DevelopmentFitError("fit polyline geometry crosses camera into a development point")
    if any(_point_polyline_distance(point["world_xyz"], line["world_vertices"])
           < SPLIT_POLYLINE_WORLD_DISTANCE_M
           for _camera, point in fit_points for _line_camera, line in dev_lines):
        raise DevelopmentFitError("fit point geometry crosses camera into a development polyline")


def _point_polyline_distance(point: np.ndarray, vertices: np.ndarray) -> float:
    """Exact Euclidean distance from a point to a piecewise-linear polyline."""
    point = np.asarray(point, dtype=float)
    vertices = np.asarray(vertices, dtype=float)
    starts, vectors = vertices[:-1], np.diff(vertices, axis=0)
    lengths_squared = np.sum(vectors * vectors, axis=1)
    if np.any(lengths_squared <= 1e-18):
        raise DevelopmentFitError("world polyline contains a degenerate segment")
    fractions = np.sum((point - starts) * vectors, axis=1) / lengths_squared
    closest = starts + np.clip(fractions, 0.0, 1.0)[:, None] * vectors
    return float(np.min(np.linalg.norm(closest - point, axis=1)))


def _polylines_overlap(left: np.ndarray, right: np.ndarray,
                       threshold: float = SPLIT_POLYLINE_WORLD_DISTANCE_M) -> bool:
    """Reject containment or copying in either directed polyline distance."""
    left_samples = _resample_world_polyline(left)
    right_samples = _resample_world_polyline(right)
    left_to_right = max(_point_polyline_distance(point, right) for point in left_samples)
    right_to_left = max(_point_polyline_distance(point, left) for point in right_samples)
    return left_to_right < threshold or right_to_left < threshold


def _polylines_adjacent(left: np.ndarray, right: np.ndarray,
                        threshold: float = SPLIT_POLYLINE_WORLD_DISTANCE_M) -> bool:
    """Reject any split contact, including endpoint-to-interior and segment crossings."""
    left = np.asarray(left, dtype=float)
    right = np.asarray(right, dtype=float)
    return any(
        _segment_segment_distance(left[index], left[index + 1], right[other], right[other + 1])
        < threshold
        for index in range(len(left) - 1) for other in range(len(right) - 1)
    )


def _segment_segment_distance(p0: np.ndarray, p1: np.ndarray,
                              q0: np.ndarray, q1: np.ndarray) -> float:
    """Exact minimum Euclidean distance between two finite nondegenerate 3-D segments."""
    u, v = p1 - p0, q1 - q0
    w = p0 - q0
    a, b, c = float(u @ u), float(u @ v), float(v @ v)
    d, e = float(u @ w), float(v @ w)
    if a <= 1e-18 or c <= 1e-18:
        raise DevelopmentFitError("world polyline contains a degenerate segment")
    candidates = [
        (0.0, float(np.clip(e / c, 0.0, 1.0))),
        (1.0, float(np.clip((e + b) / c, 0.0, 1.0))),
        (float(np.clip(-d / a, 0.0, 1.0)), 0.0),
        (float(np.clip((b - d) / a, 0.0, 1.0)), 1.0),
    ]
    determinant = a * c - b * b
    if determinant > 1e-18:
        s = (b * e - c * d) / determinant
        t = (a * e - b * d) / determinant
        if 0.0 <= s <= 1.0 and 0.0 <= t <= 1.0:
            candidates.append((s, t))
    return min(float(np.linalg.norm(w + s * u - t * v)) for s, t in candidates)


def _canonical_direction(value: np.ndarray) -> np.ndarray:
    value = np.asarray(value, dtype=float)
    value = value / np.linalg.norm(value)
    first = next((item for item in value if abs(item) > 1e-12), 1.0)
    return value if first > 0 else -value


def _reference_line(line: np.ndarray, width: int, height: int) -> np.ndarray:
    """Transform a native-image homogeneous line onto the 1280x960 gauge."""
    result = np.asarray((line[0] * width / 1280.0,
                         line[1] * height / 960.0, line[2]), dtype=float)
    result /= np.linalg.norm(result[:2])
    if result[1] < 0 or (result[1] == 0 and result[0] < 0):
        result = -result
    return result


def _reference_line_distance(left: tuple[str, dict], right: tuple[str, dict],
                             cameras: dict) -> float:
    left_camera, left_item = left
    right_camera, right_item = right
    first = _reference_line(left_item["real_line"], cameras[left_camera]["width"],
                            cameras[left_camera]["height"])
    second = _reference_line(right_item["real_line"], cameras[right_camera]["width"],
                             cameras[right_camera]["height"])
    residual = _horizon_residual(first, second, {"width": 1280, "height": 960})
    return float(np.max(np.abs(residual)))


def _direction_angle(left: np.ndarray, right: np.ndarray) -> float:
    first = _canonical_direction(left)
    second = _canonical_direction(right)
    return float(math.acos(float(np.clip(np.dot(first, second), -1.0, 1.0))))


def _validate_directional_split_isolation(cameras: dict) -> None:
    """Reject renamed directional/image-line copies across the frozen split."""
    fit_horizons = [(camera_id, item) for camera_id, camera in cameras.items()
                    for item in camera["horizons"] if item["split"] == "fit"]
    dev_horizons = [(camera_id, item) for camera_id, camera in cameras.items()
                    for item in camera["horizons"] if item["split"] == "development"]
    if any(_reference_line_distance(left, right, cameras)
           <= SPLIT_NEAR_DUPLICATE_IMAGE_PX
           for left in fit_horizons for right in dev_horizons):
        raise DevelopmentFitError("horizon fingerprint crosses camera fit/development splits")
    fit_vanishing = [(camera_id, item) for camera_id, camera in cameras.items()
                     for item in camera["vanishing"] if item["split"] == "fit"]
    dev_vanishing = [(camera_id, item) for camera_id, camera in cameras.items()
                     for item in camera["vanishing"] if item["split"] == "development"]
    if any(
        _direction_angle(left[1]["world_direction"], right[1]["world_direction"])
        <= SPLIT_NEAR_DUPLICATE_DIRECTION_RAD
        and np.linalg.norm(
            left[1]["real_uv"] * _reference_scale(cameras[left[0]])
            - right[1]["real_uv"] * _reference_scale(cameras[right[0]])
        ) <= SPLIT_NEAR_DUPLICATE_IMAGE_PX
        for left in fit_vanishing for right in dev_vanishing
    ):
        raise DevelopmentFitError("vanishing fingerprint crosses camera fit/development splits")


def _resample_world_polyline(vertices: np.ndarray, count: int = 16) -> np.ndarray:
    vertices = np.asarray(vertices, dtype=float)
    segment = np.linalg.norm(np.diff(vertices, axis=0), axis=1)
    cumulative = np.r_[0.0, np.cumsum(segment)]
    if cumulative[-1] <= 1e-9:
        raise DevelopmentFitError("world polyline is degenerate")
    targets = np.linspace(0.0, cumulative[-1], count)
    return np.column_stack([
        np.interp(targets, cumulative, vertices[:, axis]) for axis in range(3)
    ])


def _validate_denominators(camera_id, width, height, epochs, observations):
    for split in SPLITS:
        points = [item for item in observations["points"] if item["split"] == split]
        lines = [item for item in observations["polylines"] if item["split"] == split]
        min_points, min_lines = (8, 3) if split == "fit" else (4, 2)
        if len({item["physical_feature_id"] for item in points}) < min_points:
            raise DevelopmentFitError(f"{camera_id} {split} point denominator is insufficient")
        if len({item["physical_feature_id"] for item in lines}) < min_lines:
            raise DevelopmentFitError(f"{camera_id} {split} polyline denominator is insufficient")
        if {item["class"] for item in lines} != set(CLASSES):
            raise DevelopmentFitError(f"{camera_id} {split} semantic classes are incomplete")
        split_epochs = {key for key, item in epochs.items() if item["split"] == split}
        if split == "fit" and len(split_epochs) < 3:
            raise DevelopmentFitError(f"{camera_id} fit requires three disjoint epochs")
        used_epochs = {
            item["epoch_id"] for kind in ("points", "polylines")
            for item in observations[kind] if item["split"] == split
        }
        if used_epochs != split_epochs:
            raise DevelopmentFitError(f"{camera_id} {split} contains an unused capture epoch")
        values = np.asarray([item["real_uv"] for item in points])
        if np.ptp(values[:, 0]) < 0.5 * width or np.ptp(values[:, 1]) < 0.3 * height:
            raise DevelopmentFitError(f"{camera_id} {split} point coverage is insufficient")
        quadrants = {(int(x >= width / 2), int(y >= height / 2)) for x, y in values}
        if len(quadrants) != 4:
            raise DevelopmentFitError(f"{camera_id} {split} does not cover four quadrants")
        if not any(item["split"] == split for item in observations["horizons"]):
            raise DevelopmentFitError(f"{camera_id} {split} horizon is missing")
        vanishing = [item for item in observations["vanishing"] if item["split"] == split]
        if len(vanishing) < 2 or np.linalg.matrix_rank(
            np.asarray([item["world_direction"] for item in vanishing])
        ) < 2:
            raise DevelopmentFitError(f"{camera_id} {split} vanishing directions are insufficient")


def _absolute(parameters_z: np.ndarray, model: dict) -> dict[str, np.ndarray]:
    return {
        camera_id: model["cameras"][camera_id]["baseline"]
        + parameters_z[index * 7:(index + 1) * 7] * SCALES
        for index, camera_id in enumerate(CAMERAS)
    }


def _reference_scale(camera: dict) -> np.ndarray:
    return np.asarray((1280.0 / camera["width"], 960.0 / camera["height"]))


def _horizon_residual(predicted: np.ndarray, observed: np.ndarray, camera: dict) -> np.ndarray:
    """Symmetric homogeneous point-to-line distances without slope division."""
    width, height = camera["width"], camera["height"]
    predicted_points = _line_image_points(predicted, width, height)
    observed_points = _line_image_points(observed, width, height)
    if len(predicted_points) != 2 or len(observed_points) != 2:
        return np.full(4, 100.0)
    scale = _reference_scale(camera)
    forward = np.abs(np.c_[observed_points, np.ones(2)] @ predicted) * np.mean(scale)
    reverse = np.abs(np.c_[predicted_points, np.ones(2)] @ observed) * np.mean(scale)
    result = np.r_[forward, reverse]
    return result if np.isfinite(result).all() else np.full(4, 100.0)


def _polyline_residual(item: dict, params: np.ndarray, camera: dict) -> np.ndarray:
    projected, depth = project_world(item["world_vertices"], params, camera["width"], camera["height"])
    if np.any(depth <= 0.1) or not np.isfinite(projected).all():
        return np.full(32, 100.0 / item["uncertainty_px"])
    scale = _reference_scale(camera)
    predicted = _resample_polyline(projected * scale)
    observed = _resample_polyline(item["real_vertices"] * scale)
    forward = np.min(np.linalg.norm(predicted[:, None] - observed[None, :], axis=2), axis=1)
    reverse = np.min(np.linalg.norm(observed[:, None] - predicted[None, :], axis=2), axis=1)
    return np.r_[forward, reverse] / item["uncertainty_px"]


def _resample_polyline(vertices: np.ndarray, count: int = 16) -> np.ndarray:
    vertices = np.asarray(vertices, dtype=float)
    segment = np.linalg.norm(np.diff(vertices, axis=0), axis=1)
    cumulative = np.r_[0.0, np.cumsum(segment)]
    if cumulative[-1] <= 1e-9:
        raise DevelopmentFitError("polyline is degenerate after projection")
    targets = np.linspace(0.0, cumulative[-1], count)
    return np.column_stack([
        np.interp(targets, cumulative, vertices[:, axis]) for axis in range(2)
    ])


def residual_vector(parameters_z: np.ndarray, model: dict, split: str, *, return_weights=False):
    absolute = _absolute(np.asarray(parameters_z, dtype=float), model)
    values, cluster_weights = [], []
    cluster_counts = Counter()
    for camera_id in CAMERAS:
        camera = model["cameras"][camera_id]
        for kind in ("points", "polylines", "horizons", "vanishing"):
            for item in camera[kind]:
                if item["split"] == split:
                    cluster_counts[item["physical_feature_id"]] += 1
    for camera_id in CAMERAS:
        camera, params = model["cameras"][camera_id], absolute[camera_id]
        scale = _reference_scale(camera)
        for kind in ("points", "polylines", "horizons", "vanishing"):
            for item in camera[kind]:
                if item["split"] != split:
                    continue
                if kind == "points":
                    predicted, depth = project_world(item["world_xyz"][None, :], params, camera["width"], camera["height"])
                    residual = (predicted[0] - item["real_uv"]) * scale / item["uncertainty_px"]
                    if depth[0] <= 0.1 or not np.isfinite(residual).all():
                        residual = np.full(2, 100.0 / item["uncertainty_px"])
                elif kind == "polylines":
                    residual = _polyline_residual(item, params, camera)
                elif kind == "horizons":
                    predicted = ground_horizon_line(params, camera["width"], camera["height"])
                    residual = _horizon_residual(predicted, item["real_line"], camera) / item["uncertainty_px"]
                else:
                    predicted = project_direction(item["world_direction"], params, camera["width"], camera["height"])
                    residual = (predicted - item["real_uv"]) * scale / item["uncertainty_px"]
                    if not np.isfinite(residual).all():
                        residual = np.full(2, 100.0 / item["uncertainty_px"])
                count = cluster_counts[item["physical_feature_id"]]
                values.extend(residual.tolist())
                cluster_weights.extend([1.0 / (count * max(1, len(residual)))] * len(residual))
    raw = np.asarray(values, dtype=float)
    weights = np.asarray(cluster_weights, dtype=float)
    if return_weights:
        return raw, weights
    return raw * np.sqrt(weights)


def _jacobian(function, values: np.ndarray, step=JACOBIAN_STEP,
              lower: np.ndarray | None = None,
              upper: np.ndarray | None = None) -> np.ndarray:
    base = function(values)
    result = np.empty((len(base), len(values)))
    for index in range(len(values)):
        can_left = lower is None or values[index] - step >= lower[index]
        can_right = upper is None or values[index] + step <= upper[index]
        if can_left and can_right:
            left, right = values.copy(), values.copy()
            left[index] -= step
            right[index] += step
            result[:, index] = (function(right) - function(left)) / (2 * step)
        elif can_right:
            right = values.copy(); right[index] += step
            result[:, index] = (function(right) - base) / step
        elif can_left:
            left = values.copy(); left[index] -= step
            result[:, index] = (base - function(left)) / step
        else:
            raise DevelopmentFitError(f"parameter {index} has no finite-difference interval")
    return result


def competing_basin(solutions: list[dict], relative=0.02) -> bool:
    if len(solutions) < 2:
        return False
    ranked = sorted(solutions, key=lambda item: item["fit_loss"])
    best = ranked[0]
    ceiling = best["development_loss"] + relative * max(abs(best["development_loss"]), 1e-12)
    return any(
        item["development_loss"] <= ceiling
        and np.any(np.abs(item["z"] - best["z"]) > BASIN_NEIGHBORHOOD_Z)
        for item in ranked[1:]
    )


def _select_fit_candidate(candidates: list[dict]) -> dict:
    """Select parameters without consulting development observations."""
    return min(candidates, key=lambda item: item["fit_loss"])


def _errors(model: dict, z: np.ndarray, split: str) -> dict:
    absolute = _absolute(z, model)
    report = {}
    for camera_id in CAMERAS:
        camera, params = model["cameras"][camera_id], absolute[camera_id]
        scale = _reference_scale(camera)
        point_rows, road_rows, horizon_rows, vanishing_rows = [], [], [], []
        by_epoch = defaultdict(lambda: {"points": [], "roads": [], "horizons": [], "vanishing": []})
        by_class = defaultdict(list)
        by_quadrant = defaultdict(list)
        for item in camera["points"]:
            if item["split"] != split:
                continue
            uv, depth = project_world(item["world_xyz"][None, :], params, camera["width"], camera["height"])
            error = float(np.linalg.norm((uv[0] - item["real_uv"]) * scale)) if depth[0] > 0 else math.inf
            point_rows.append(error); by_epoch[item["epoch_id"]]["points"].append(error)
            quadrant = f"{int(item['real_uv'][0] >= camera['width']/2)}{int(item['real_uv'][1] >= camera['height']/2)}"
            by_quadrant[quadrant].append(error)
        for item in camera["polylines"]:
            if item["split"] != split:
                continue
            projected, depth = project_world(
                item["world_vertices"], params, camera["width"], camera["height"]
            )
            if np.any(depth <= 0.1) or not np.isfinite(projected).all():
                row = np.full(32, math.inf)
            else:
                row = np.abs(_polyline_residual(item, params, camera) * item["uncertainty_px"])
            road_rows.extend(row); by_epoch[item["epoch_id"]]["roads"].extend(row); by_class[item["class"]].extend(row)
        for item in camera["horizons"]:
            if item["split"] != split:
                continue
            predicted = ground_horizon_line(params, camera["width"], camera["height"])
            row = np.abs(_horizon_residual(predicted, item["real_line"], camera))
            horizon_rows.extend(row); by_epoch[item["epoch_id"]]["horizons"].extend(row)
        for item in camera["vanishing"]:
            if item["split"] != split:
                continue
            predicted = project_direction(item["world_direction"], params, camera["width"], camera["height"])
            row = [float(np.linalg.norm((predicted - item["real_uv"]) * scale))]
            vanishing_rows.extend(row); by_epoch[item["epoch_id"]]["vanishing"].extend(row)
        def metrics(rows):
            values = np.asarray(rows, dtype=float)
            if not np.isfinite(values).all():
                return {"count": len(values), "rmse_px": math.inf,
                        "p95_px": math.inf, "max_px": math.inf}
            return {"count": len(values), "rmse_px": float(np.sqrt(np.mean(values**2))),
                    "p95_px": float(np.quantile(values, .95)), "max_px": float(np.max(values))}
        points, roads = metrics(point_rows), metrics(road_rows)
        horizons, vanish = metrics(horizon_rows), metrics(vanishing_rows)
        class_metrics = {key: metrics(value) for key, value in sorted(by_class.items())}
        quadrant_metrics = {key: metrics(value) for key, value in sorted(by_quadrant.items())}
        epoch_metrics = {key: {name: metrics(rows) for name, rows in value.items() if rows}
                         for key, value in sorted(by_epoch.items())}
        def point_pass(row):
            return (row["rmse_px"] <= POINT_GATES["rmse_px"]
                    and row["p95_px"] <= POINT_GATES["p95_px"]
                    and row["max_px"] <= POINT_GATES["max_px"])
        def road_pass(row):
            return row["rmse_px"] <= ROAD_GATES["rmse_px"] and row["max_px"] <= ROAD_GATES["max_px"]
        report[camera_id] = {
            "points": points, "roads": roads, "horizons": horizons, "vanishing": vanish,
            "epochs": epoch_metrics, "classes": class_metrics, "quadrants": quadrant_metrics,
            "gates_passed": (
                point_pass(points) and road_pass(roads) and point_pass(horizons) and point_pass(vanish)
                and set(class_metrics) == set(CLASSES) and all(road_pass(row) for row in class_metrics.values())
                and len(quadrant_metrics) == 4 and all(point_pass(row) for row in quadrant_metrics.values())
                and all("points" in value and "roads" in value and point_pass(value["points"])
                        and road_pass(value["roads"])
                        and all(point_pass(row) for name, row in value.items() if name in {"horizons", "vanishing"})
                        for value in epoch_metrics.values())
            ),
        }
    return report


def _huber_objective(residual: np.ndarray) -> float:
    absolute = np.abs(residual)
    return float(np.mean(np.where(absolute <= 1.0, 0.5 * residual**2, absolute - 0.5)))


def _clustered_huber_objective(residual: np.ndarray, weights: np.ndarray) -> float:
    absolute = np.abs(residual)
    loss = np.where(absolute <= 1.0, 0.5 * residual**2, absolute - 0.5)
    return float(np.sum(weights * loss) / np.sum(weights))


def _low_discrepancy_starts(lower: np.ndarray, upper: np.ndarray, count: int, seed: int):
    if count <= 0:
        return []
    rng = np.random.default_rng(seed)
    unit = np.empty((count, len(lower)))
    for axis in range(len(lower)):
        unit[:, axis] = (rng.permutation(count) + rng.random(count)) / count
    return [lower + row * (upper - lower) for row in unit]


def _refine(model: dict, start: np.ndarray, lower: np.ndarray, upper: np.ndarray,
            max_iterations: int) -> tuple[np.ndarray, bool, str, int]:
    values = np.clip(np.asarray(start, dtype=float), lower, upper)
    damping = 1e-3
    for _iteration in range(max_iterations):
        residual, cluster = residual_vector(values, model, "fit", return_weights=True)
        jacobian = _jacobian(
            lambda z: residual_vector(z, model, "fit", return_weights=True)[0], values,
            lower=lower, upper=upper,
        )
        robust = np.minimum(1.0, 1.0 / np.maximum(np.abs(residual), 1e-12))
        weights = cluster * robust
        weighted_jacobian = jacobian * np.sqrt(weights)[:, None]
        weighted_residual = residual * np.sqrt(weights)
        system = weighted_jacobian.T @ weighted_jacobian + damping * np.eye(len(values))
        try:
            delta = np.linalg.solve(system, -(weighted_jacobian.T @ weighted_residual))
        except np.linalg.LinAlgError:
            return values, False, "singular_normal_equations", _iteration + 1
        if float(np.linalg.norm(delta)) < 1e-8:
            return values, True, "normalized_step_converged", _iteration + 1
        objective = _clustered_huber_objective(residual, cluster)
        accepted = False
        for fraction in (1.0, 0.5, 0.25, 0.125, 0.0625):
            candidate = np.clip(values + fraction * delta, lower, upper)
            candidate_residual, candidate_cluster = residual_vector(
                candidate, model, "fit", return_weights=True
            )
            if _clustered_huber_objective(candidate_residual, candidate_cluster) < objective:
                values = candidate; damping = max(1e-9, damping * 0.5); accepted = True; break
        if not accepted:
            damping = min(1e9, damping * 10.0)
            if damping >= 1e8:
                return values, False, "no_objective_progress", _iteration + 1
    return values, False, "iteration_limit", max_iterations


def _boundary_hits(values: np.ndarray, lower: np.ndarray, upper: np.ndarray) -> list[str]:
    return [
        f"{CAMERAS[index // 7]}:{PARAMETER_NAMES[index % 7]}"
        for index, value in enumerate(values)
        if min(value - lower[index], upper[index] - value) <= 1e-5
    ]


def _normalized_bounds(model: dict) -> tuple[np.ndarray, np.ndarray]:
    lower = np.tile(LOWER_DELTA / SCALES, 4)
    upper = np.tile(UPPER_DELTA / SCALES, 4)
    fov_index = PARAMETER_NAMES.index("fov_deg")
    for camera_index, camera_id in enumerate(CAMERAS):
        baseline_fov = model["cameras"][camera_id]["baseline"][fov_index]
        index = camera_index * 7 + fov_index
        lower[index] = max(lower[index], (1.0 + 1e-6 - baseline_fov) / SCALES[fov_index])
        upper[index] = min(upper[index], (179.0 - 1e-6 - baseline_fov) / SCALES[fov_index])
        if lower[index] >= upper[index]:
            raise DevelopmentFitError(f"{camera_id} has no physically valid FOV search interval")
    return lower, upper


def _epoch_row_pass(row: dict) -> bool:
    def point_pass(value):
        return (value["rmse_px"] <= POINT_GATES["rmse_px"]
                and value["p95_px"] <= POINT_GATES["p95_px"]
                and value["max_px"] <= POINT_GATES["max_px"])
    def road_pass(value):
        return (value["rmse_px"] <= ROAD_GATES["rmse_px"]
                and value["max_px"] <= ROAD_GATES["max_px"])
    return ("points" in row and "roads" in row and point_pass(row["points"])
            and road_pass(row["roads"])
            and all(point_pass(value) for key, value in row.items()
                    if key in {"horizons", "vanishing"}))


def _epoch_stability(model: dict, best_z: np.ndarray, lower: np.ndarray,
                     upper: np.ndarray, max_nfev: int) -> dict:
    """Leave out each fit epoch and require stable, predictive refits."""
    rows = []
    estimates = {camera_id: [] for camera_id in CAMERAS}
    for camera_index, camera_id in enumerate(CAMERAS):
        fit_epochs = sorted(
            epoch_id for epoch_id, epoch in model["cameras"][camera_id]["epochs"].items()
            if epoch["split"] == "fit"
        )
        if len(fit_epochs) < 3:
            return {"status": "INSUFFICIENT", "passed": False,
                    "reason": f"{camera_id} has fewer than three fit epochs", "refits": rows}
        for epoch_id in fit_epochs:
            reduced = copy.deepcopy(model)
            camera = reduced["cameras"][camera_id]
            for kind in ("points", "polylines", "horizons", "vanishing"):
                camera[kind] = [item for item in camera[kind]
                                if not (item["split"] == "fit" and item["epoch_id"] == epoch_id)]
            values, success, reason, iterations = _refine(
                reduced, best_z, lower, upper, max_nfev
            )
            omitted = _errors(model, values, "fit")[camera_id]["epochs"].get(epoch_id, {})
            predictive_passed = _epoch_row_pass(omitted)
            estimates[camera_id].append(values[camera_index * 7:(camera_index + 1) * 7].copy())
            rows.append({
                "camera_id": camera_id, "omitted_epoch_id": epoch_id,
                "success": success, "convergence_reason": reason,
                "iterations": iterations, "predictive_gate_passed": predictive_passed,
                "normalized_parameters": values.tolist(),
            })
    intervals = {}
    stable = all(row["success"] and row["predictive_gate_passed"] for row in rows)
    for camera_index, camera_id in enumerate(CAMERAS):
        values = np.asarray(estimates[camera_id])
        low = np.quantile(values, 0.025, axis=0)
        high = np.quantile(values, 0.975, axis=0)
        full = best_z[camera_index * 7:(camera_index + 1) * 7]
        span = high - low
        contains = bool(np.all(full >= low - 1e-8) and np.all(full <= high + 1e-8))
        span_passed = bool(np.all(span <= EPOCH_STABILITY_MAX_SPAN_Z))
        stable = stable and contains and span_passed
        intervals[camera_id] = {
            "parameter_names": list(PARAMETER_NAMES),
            "normalized_p2_5": low.tolist(), "normalized_p97_5": high.tolist(),
            "normalized_span": span.tolist(),
            "max_normalized_span": EPOCH_STABILITY_MAX_SPAN_Z,
            "full_fit_inside_interval": contains, "span_gate_passed": span_passed,
        }
    return {"status": "PASS" if stable else "FAIL", "passed": bool(stable),
            "method": "leave_one_fit_epoch_out_empirical_95_percent_interval",
            "refits": rows, "intervals": intervals}


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not-installed"


def solve(model: dict, seed=20260714, starts=8, max_nfev=80) -> dict:
    if not isinstance(starts, int) or starts < MIN_MULTISTARTS:
        raise DevelopmentFitError(
            f"at least {MIN_MULTISTARTS} pre-registered multistarts plus the zero seed are required"
        )
    if not isinstance(max_nfev, int) or max_nfev < 1:
        raise DevelopmentFitError("max_nfev must be a positive integer")
    lower, upper = _normalized_bounds(model)
    seeds = [np.zeros(28), *_low_discrepancy_starts(lower, upper, starts, seed)]
    candidates = []
    for start in seeds:
        values, success, reason, iterations = _refine(model, start, lower, upper, max_nfev)
        fit, fit_weights = residual_vector(values, model, "fit", return_weights=True)
        development, development_weights = residual_vector(
            values, model, "development", return_weights=True
        )
        candidates.append({
            "z": values, "success": success, "convergence_reason": reason,
            "iterations": iterations,
            "fit_loss": _clustered_huber_objective(fit, fit_weights),
            "development_loss": _clustered_huber_objective(development, development_weights),
        })
    candidates.sort(key=lambda item: item["fit_loss"])
    best = _select_fit_candidate(candidates)
    jacobian = _jacobian(lambda z: residual_vector(z, model, "fit"), best["z"],
                         lower=lower, upper=upper)
    singular = np.linalg.svd(jacobian, compute_uv=False)
    tolerance = max(jacobian.shape) * np.finfo(float).eps * singular[0]
    rank = int(np.count_nonzero(singular > tolerance))
    condition = math.inf if rank < 28 else float(singular[0] / singular[-1])
    boundary_hits = _boundary_hits(best["z"], lower, upper)

    fit_metrics = _errors(model, best["z"], "fit")
    development_metrics = _errors(model, best["z"], "development")
    basin_failed = competing_basin(candidates)
    basin_evidence_sufficient = len(candidates) >= MIN_MULTISTARTS + 1
    development_gate = all(item["gates_passed"] for item in development_metrics.values())
    fit_gate = all(item["gates_passed"] for item in fit_metrics.values())
    epoch_stability = _epoch_stability(model, best["z"], lower, upper, max_nfev)
    reproducibility_complete = isinstance(model.get("manifest"), dict)
    passed = (
        best["success"] and rank == 28 and condition <= CONDITION_MAX and not boundary_hits
        and basin_evidence_sufficient and not basin_failed and fit_gate and development_gate
        and epoch_stability["passed"] and reproducibility_complete
    )
    absolute = _absolute(best["z"], model)
    cameras = {}
    for camera_id in CAMERAS:
        camera = model["cameras"][camera_id]
        pose, recovered = production_round_trip(
            camera["anchor_location"], camera["production_base"], absolute[camera_id]
        )
        if not np.allclose(recovered, absolute[camera_id], atol=1e-9, rtol=0):
            raise DevelopmentFitError(f"{camera_id} production round trip failed")
        cameras[camera_id] = {
            "absolute_parameters": dict(zip(PARAMETER_NAMES, map(float, absolute[camera_id]))),
            "candidate_twin_pose": pose,
            "fit_metrics": fit_metrics[camera_id],
            "development_metrics": development_metrics[camera_id],
        }
    return {
        "schema": OUTPUT_SCHEMA, "acceptance_eligible": False,
        "holdout_consumed": False, "release_eligible": False,
        "created_at_utc": _utc_now(), "manifest_sha256": model["manifest_sha256"],
        "coordinate_gauge": "carla_map_exact_no_global_se2",
        "map": model["map"], "cameras_json": model["cameras_json"],
        "parameterization": {"per_camera": list(PARAMETER_NAMES), "dimension": 28,
                             "global_site_se2_parameter": False, "scales": SCALES.tolist()},
        "split_isolation_thresholds": {
            "reference_image_width": 1280, "reference_image_height": 960,
            "near_duplicate_image_px": SPLIT_NEAR_DUPLICATE_IMAGE_PX,
            "near_duplicate_direction_rad": SPLIT_NEAR_DUPLICATE_DIRECTION_RAD,
            "point_world_distance_m": SPLIT_POINT_WORLD_DISTANCE_M,
            "polyline_world_distance_m": SPLIT_POLYLINE_WORLD_DISTANCE_M,
            "minimum_horizon_chord_reference_px": MIN_HORIZON_CHORD_REFERENCE_PX,
        },
        "optimizer": {
            "seed": seed, "requested_multistarts": starts, "starts": len(seeds),
            "minimum_multistarts": MIN_MULTISTARTS, "initial_normalized_starts": [
                np.asarray(item).tolist() for item in seeds
            ],
            "max_nfev": max_nfev, "loss": "clustered_huber", "f_scale": 1.0,
            "finite_difference_step": JACOBIAN_STEP,
            "finite_difference_method": "bound_aware_central_or_one_sided",
        },
        "runtime_identity": {
            "python": sys.version, "implementation": platform.python_implementation(),
            "python_executable": sys.executable,
            "python_executable_sha256": _sha256(Path(sys.executable).resolve()),
            "platform": platform.platform(), "numpy": np.__version__,
            "scipy": _package_version("scipy"),
        },
        "input_bindings": {
            "manifest": model.get("manifest"),
            "map": model["map"], "cameras_json": model["cameras_json"],
            "epochs": {
                camera_id: {
                    epoch_id: {
                        "split": epoch["split"], "frame": epoch["frame"],
                        "median_members": epoch["median_members"],
                    }
                    for epoch_id, epoch in sorted(model["cameras"][camera_id]["epochs"].items())
                }
                for camera_id in CAMERAS
            },
        },
        "reproducibility_complete": reproducibility_complete,
        "code_identity": {
            "fitter_sha256": _sha256(Path(__file__).resolve()),
            "projection_sha256": _sha256(Path(project_world.__code__.co_filename).resolve()),
            "rig_sha256": _sha256(Path(absolute_twin_model.__code__.co_filename).resolve()),
        },
        "data_jacobian": {"rank": rank, "required_rank": 28, "condition": condition,
                          "condition_max": CONDITION_MAX, "singular_values": singular.tolist()},
        "boundary_hits": boundary_hits, "competing_basin": basin_failed,
        "basin_evidence_sufficient": basin_evidence_sufficient,
        "epoch_stability": epoch_stability,
        "development_gate_passed": passed,
        "candidate_losses": [{key: (value.tolist() if key == "z" else value) for key, value in item.items()} for item in candidates],
        "cameras": cameras,
        "limitations": ["development_only_no_sealed_holdout", "map_relative_not_surveyed_world_accuracy",
                        "world_geometry_is_manifest_asserted_not_derived_from_hashed_opendrive",
                        "candidate_requires_isolated_ue5_rerender_and_visual_veto"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--output", required=True)
    parser.add_argument("--forbidden-root", action="append", default=[])
    parser.add_argument("--seed", type=int, default=20260714)
    parser.add_argument("--starts", type=int, default=8)
    args = parser.parse_args(argv)
    if args.starts < MIN_MULTISTARTS:
        parser.error(f"--starts must be at least {MIN_MULTISTARTS}")
    roots = tuple(Path(value).resolve(strict=False) for value in (*DEFAULT_FORBIDDEN_ROOTS, *args.forbidden_root))
    manifest_path = _absolute_path(args.manifest, "manifest", roots)
    raw = manifest_path.read_bytes()
    document = json.loads(raw)
    model = validate_document(document, hashlib.sha256(raw).hexdigest(), args.forbidden_root)
    model["manifest"] = {
        "path": str(manifest_path), "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }
    report = solve(model, seed=args.seed, starts=args.starts)
    output = _absolute_path(args.output, "output", roots)
    if output.exists():
        raise SystemExit("refusing to overwrite development fit output")
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, output)
        os.unlink(temporary)
    except Exception:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
