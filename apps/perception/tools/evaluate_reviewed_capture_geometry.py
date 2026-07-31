#!/usr/bin/env python3
"""Evaluate current camera geometry on reviewed archived vehicle tracks.

The evaluator never trusts persisted GPS/local-XZ.  It verifies capture and
review hashes, projects hash-bound reviewed contacts through the current camera model,
and compares cross-camera positions only when one camera trajectory brackets
the other observation closely enough to interpolate to the same instant.
Results remain diagnostic because contact labels, optics, and poses are unsurveyed.
"""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path

import cv2
import numpy as np


class GeometryError(RuntimeError):
    pass


STRICT_DISTANCE_THRESHOLD_M = 1.0
STRICT_MAXIMUM_BRACKET_GAP_SECONDS = 0.5
STRICT_MINIMUM_TRACKS = 3
STRICT_MINIMUM_RESIDUALS = 12
STRICT_REQUIRED_CAMERAS = {"ch1", "ch2", "ch3", "ch4"}
STRICT_REQUIRED_CAMERA_PAIRS = {
    tuple(sorted((left, right)))
    for index, left in enumerate(sorted(STRICT_REQUIRED_CAMERAS))
    for right in sorted(STRICT_REQUIRED_CAMERAS)[index + 1:]
}


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def parse_utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise GeometryError("event timestamp is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise GeometryError("event timestamp is invalid") from exc
    return parsed.astimezone(timezone.utc)


def read_bound_json(path, expected_hash, label):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise GeometryError(f"{label} is unreadable or invalid") from exc
    if sha256_bytes(raw) != expected_hash:
        raise GeometryError(f"{label} hash does not match")
    return path, raw, value


def verify_bound_file(path, expected_hash, label):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GeometryError(f"{label} is unreadable") from exc
    if sha256_bytes(raw) != expected_hash:
        raise GeometryError(f"{label} hash does not match")
    return path


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


def load_camera_config(path):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise GeometryError("camera config is unreadable or invalid") from exc
    cameras = value.get("cameras") if isinstance(value, dict) else None
    if not isinstance(cameras, list):
        raise GeometryError("camera config has no camera list")
    indexed = {}
    for camera in cameras:
        camera_id = camera.get("id") if isinstance(camera, dict) else None
        intrinsics = camera.get("intrinsics") if isinstance(camera, dict) else None
        required = ("height_m", "pitch_deg", "yaw_deg", "heading_deg")
        if (
            camera_id not in {"ch1", "ch2", "ch3", "ch4"}
            or not isinstance(intrinsics, dict)
            or any(not isinstance(camera.get(key), (int, float)) for key in required)
            or any(
                not isinstance(intrinsics.get(key), (int, float))
                for key in ("fx", "fy", "cx", "cy", "width", "height")
            )
        ):
            raise GeometryError("camera config contains an invalid camera")
        indexed[camera_id] = camera
    if set(indexed) != {"ch1", "ch2", "ch3", "ch4"}:
        raise GeometryError("camera config must contain exactly ch1 through ch4")
    return path, raw, indexed


def ground_intersection(camera, pixel):
    intrinsics = camera["intrinsics"]
    matrix = np.asarray([
        [intrinsics["fx"], 0.0, intrinsics["cx"]],
        [0.0, intrinsics["fy"], intrinsics["cy"]],
        [0.0, 0.0, 1.0],
    ], dtype=float)
    point = np.asarray(pixel, dtype=np.float32).reshape(1, 1, 2)
    undistorted = cv2.undistortPoints(
        point, matrix, np.zeros(5, dtype=float), P=matrix
    ).reshape(2)
    ray = np.asarray([
        (undistorted[0] - intrinsics["cx"]) / intrinsics["fx"],
        (undistorted[1] - intrinsics["cy"]) / intrinsics["fy"],
        1.0,
    ])
    pitch, yaw = np.radians([camera["pitch_deg"], camera["yaw_deg"]])
    rx = np.asarray([
        [1.0, 0.0, 0.0],
        [0.0, math.cos(pitch), -math.sin(pitch)],
        [0.0, math.sin(pitch), math.cos(pitch)],
    ])
    ry = np.asarray([
        [math.cos(yaw), 0.0, math.sin(yaw)],
        [0.0, 1.0, 0.0],
        [-math.sin(yaw), 0.0, math.cos(yaw)],
    ])
    dx, dy, dz = (ry @ rx) @ ray
    if dy <= 1e-6:
        raise GeometryError("contact ray does not intersect the ground in front")
    scale = float(camera["height_m"]) / float(dy)
    x_right, z_forward = scale * dx, scale * dz
    heading = math.radians(float(camera["heading_deg"]))
    east = z_forward * math.sin(heading) + x_right * math.cos(heading)
    north = z_forward * math.cos(heading) - x_right * math.sin(heading)
    if not np.isfinite([east, north]).all():
        raise GeometryError("contact projection is non-finite")
    return np.asarray([east, north], dtype=float)


def camera_origin_offset_enu(camera):
    """Current unsurveyed twin translation hypothesis in east/north metres."""
    pose = camera.get("twin_pose") or {}
    forward = float(pose.get("forward_offset_m", 0.0))
    right = float(pose.get("right_offset_m", 0.0))
    bearing = (
        float(camera["heading_deg"])
        + float(camera["yaw_deg"])
        + float(pose.get("yaw_offset_deg", 0.0))
    )
    carla_yaw = math.radians(bearing - 90.0)
    delta_east = forward * math.cos(carla_yaw) - right * math.sin(carla_yaw)
    delta_carla_y = forward * math.sin(carla_yaw) + right * math.cos(carla_yaw)
    return np.asarray([delta_east, -delta_carla_y], dtype=float)


def interpolate_bracket(records, target_epoch, maximum_gap_seconds):
    records = sorted(records, key=lambda item: item["epoch"])
    for left, right in zip(records, records[1:]):
        if not left["epoch"] <= target_epoch <= right["epoch"]:
            continue
        gap = right["epoch"] - left["epoch"]
        if gap <= 0.0 or gap > maximum_gap_seconds:
            return None
        fraction = (target_epoch - left["epoch"]) / gap
        position = left["position_enu_m"] + fraction * (
            right["position_enu_m"] - left["position_enu_m"]
        )
        return {
            "position_enu_m": position,
            "bracket_event_ids": [left["event_id"], right["event_id"]],
            "bracket_gap_seconds": gap,
            "interpolation_fraction": fraction,
        }
    return None


def cross_camera_residuals(records, maximum_bracket_gap_seconds=2.0):
    by_camera = defaultdict(list)
    for record in records:
        by_camera[record["camera_id"]].append(record)
    residuals = []
    seen = set()
    for target in records:
        for source_camera, source_records in by_camera.items():
            if source_camera == target["camera_id"]:
                continue
            interpolated = interpolate_bracket(
                source_records, target["epoch"], maximum_bracket_gap_seconds
            )
            if interpolated is None:
                continue
            key = (
                target["event_id"], source_camera,
                tuple(interpolated["bracket_event_ids"]),
            )
            if key in seen:
                continue
            seen.add(key)
            delta = target["position_enu_m"] - interpolated["position_enu_m"]
            distance = float(np.linalg.norm(delta))
            residuals.append({
                "target_event_id": target["event_id"],
                "target_camera_id": target["camera_id"],
                "source_camera_id": source_camera,
                "source_bracket_event_ids": interpolated["bracket_event_ids"],
                "source_bracket_gap_seconds": interpolated["bracket_gap_seconds"],
                "source_interpolation_fraction": interpolated["interpolation_fraction"],
                "delta_east_north_m": delta.tolist(),
                "distance_m": distance,
                "strict_diagnostic_threshold_m": STRICT_DISTANCE_THRESHOLD_M,
                "strict_diagnostic_passed": distance <= STRICT_DISTANCE_THRESHOLD_M,
            })
    return sorted(residuals, key=lambda item: (
        item["target_event_id"], item["source_camera_id"]
    ))


def evaluate(review_path, camera_config_path, maximum_bracket_gap_seconds=0.5):
    if not 0.0 < maximum_bracket_gap_seconds <= STRICT_MAXIMUM_BRACKET_GAP_SECONDS:
        raise GeometryError("maximum bracket gap must be in (0, 0.5] seconds")
    review_path = Path(review_path).resolve()
    try:
        review_raw = review_path.read_bytes()
        review = json.loads(review_raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise GeometryError("identity review is unreadable or invalid") from exc
    if review.get("schema") != "v2x-codex-visual-identity-review/v1":
        raise GeometryError("identity review schema is unsupported")
    config_path, config_raw, cameras = load_camera_config(camera_config_path)
    contact_reviews = {}
    for item in review.get("reviewed_contacts", []):
        event_id = item.get("event_id") if isinstance(item, dict) else None
        if not isinstance(event_id, str) or event_id in contact_reviews:
            raise GeometryError("reviewed contacts have missing or duplicate event IDs")
        contact_reviews[event_id] = item
    events = {}
    capture_hashes = []
    for descriptor in review.get("inputs", []):
        report_path, report_raw, report = read_bound_json(
            descriptor.get("capture_report"),
            descriptor.get("capture_report_sha256"),
            "capture report",
        )
        if report.get("schema") not in {
            "v2x-detection-event-frame-capture/v1",
            "v2x-detection-event-frame-capture/v2",
        }:
            raise GeometryError("capture report schema is unsupported")
        verify_bound_file(
            descriptor.get("review_sheet"),
            descriptor.get("review_sheet_sha256"),
            "review sheet",
        )
        capture_hashes.append({
            "path": str(report_path), "sha256": sha256_bytes(report_raw)
        })
        for event in report.get("events", []):
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or event_id in events:
                raise GeometryError("capture event IDs are missing or duplicated")
            events[event_id] = event

    used = set()
    tracks = []
    for reviewed in review.get("reviewed_tracks", []):
        event_ids = reviewed.get("event_ids")
        if not isinstance(event_ids, list) or len(event_ids) < 2:
            raise GeometryError("reviewed track has too few events")
        records = []
        for event_id in event_ids:
            if event_id not in events or event_id in used:
                raise GeometryError("reviewed event is absent or reused")
            used.add(event_id)
            event = events[event_id]
            camera_id = event["camera_id"]
            frame = event.get("frame") or {}
            review_contact = contact_reviews.get(event_id)
            geometry_failures = []
            contact = None
            if review_contact is None:
                geometry_failures.append("reviewed_selected_frame_contact_missing")
            else:
                contact = review_contact.get("contact_px")
                if (
                    not isinstance(contact, list)
                    or len(contact) != 2
                    or not all(isinstance(value, (int, float)) for value in contact)
                    or not np.isfinite(contact).all()
                ):
                    raise GeometryError("reviewed contact is invalid")
                if review_contact.get("frame_sha256") != frame.get("sha256"):
                    raise GeometryError("reviewed contact is not bound to selected frame")
                width, height = frame.get("width"), frame.get("height")
                if not (
                    isinstance(width, int)
                    and isinstance(height, int)
                    and 0.0 < float(contact[0]) < width - 1.0
                    and 0.0 < float(contact[1]) < height - 1.0
                ):
                    geometry_failures.append("reviewed_contact_touches_or_exceeds_frame")
                if review_contact.get("vehicle_fully_visible") is not True:
                    geometry_failures.append("vehicle_not_reviewed_as_fully_visible")
            timestamp_value = event.get("selected_frame_timestamp_utc")
            timestamp = parse_utc(timestamp_value)
            temporal_passed = event.get("temporal_gate_passed")
            if temporal_passed is None:
                temporal_passed = float(event["absolute_time_offset_ms"]) <= 150.0
            if not temporal_passed:
                geometry_failures.append("selected_frame_exceeds_identity_time_gate")
            position = None
            origin_offset = camera_origin_offset_enu(cameras[camera_id])
            if not geometry_failures:
                position = ground_intersection(cameras[camera_id], contact) + origin_offset
            records.append({
                "event_id": event_id,
                "camera_id": camera_id,
                "event_timestamp_utc": event["media_timestamp_utc"],
                "timestamp_utc": timestamp_value,
                "epoch": timestamp.timestamp(),
                "contact_px": contact,
                "position_enu_m": position,
                "frame_temporal_gate_passed": bool(temporal_passed),
                "camera_origin_offset_enu_m": origin_offset,
                "geometry_eligible": not geometry_failures,
                "geometry_failures": geometry_failures,
            })
        records.sort(key=lambda item: (item["epoch"], item["event_id"]))
        eligible_records = [
            record for record in records if record["geometry_eligible"]
        ]
        residuals = cross_camera_residuals(
            eligible_records, maximum_bracket_gap_seconds
        )
        distances = [item["distance_m"] for item in residuals]
        tracks.append({
            "reviewed_track_id": reviewed["reviewed_track_id"],
            "source_object_id": reviewed["source_object_id"],
            "corrected_object_type": reviewed["corrected_object_type"],
            "event_count": len(records),
            "timing_eligible_event_count": sum(
                record["frame_temporal_gate_passed"] for record in records
            ),
            "geometry_eligible_event_count": len(eligible_records),
            "cameras": sorted({record["camera_id"] for record in records}),
            "positions": [{
                **{
                    key: (
                        value.tolist() if isinstance(value, np.ndarray) else value
                    )
                    for key, value in record.items()
                    if key not in {"epoch", "position_enu_m"}
                },
                "position_enu_m": (
                    None
                    if record["position_enu_m"] is None
                    else record["position_enu_m"].tolist()
                ),
            } for record in records],
            "interpolated_cross_camera_residuals": residuals,
            "residual_summary_m": None if not distances else {
                "count": len(distances),
                "rmse": float(math.sqrt(np.mean(np.square(distances)))),
                "median": float(np.median(distances)),
                "max": float(np.max(distances)),
                "strict_pass_count": sum(
                    value <= STRICT_DISTANCE_THRESHOLD_M for value in distances
                ),
            },
        })
    all_residuals = [
        residual
        for track in tracks
        for residual in track["interpolated_cross_camera_residuals"]
    ]
    covered_tracks = sum(
        bool(track["interpolated_cross_camera_residuals"]) for track in tracks
    )
    covered_cameras = {
        camera
        for track in tracks
        if track["interpolated_cross_camera_residuals"]
        for camera in track["cameras"]
    }
    covered_pairs = {
        tuple(sorted((item["target_camera_id"], item["source_camera_id"])))
        for item in all_residuals
    }
    strict_coverage_passed = bool(
        covered_tracks >= STRICT_MINIMUM_TRACKS
        and len(all_residuals) >= STRICT_MINIMUM_RESIDUALS
        and covered_cameras == STRICT_REQUIRED_CAMERAS
        and covered_pairs == STRICT_REQUIRED_CAMERA_PAIRS
    )
    return {
        "schema": "v2x-reviewed-capture-geometry-evaluation/v2",
        "acceptance_eligible": False,
        "current_geometry_strict_diagnostic_passed": strict_coverage_passed and all(
            residual["strict_diagnostic_passed"] for residual in all_residuals
        ),
        "inputs": {
            "identity_review": {
                "path": str(review_path), "sha256": sha256_bytes(review_raw)
            },
            "camera_config": {
                "path": str(config_path), "sha256": sha256_bytes(config_raw)
            },
            "capture_reports": capture_hashes,
        },
        "projection_model": {
            "intrinsics": "current_nominal_pinhole",
            "extrinsics": "current_pitch_yaw_heading_height",
            "contact": "selected_frame_hash_bound_reviewed_contact_required",
            "site_frame": "camera_specific_unsurveyed_twin_translation_hypothesis",
            "persisted_gps_or_local_xz_parsed": False,
        },
        "maximum_bracket_gap_seconds": maximum_bracket_gap_seconds,
        "tracks": tracks,
        "aggregate": {
            "reviewed_track_count": len(tracks),
            "interpolated_cross_camera_residual_count": len(all_residuals),
            "strict_pass_count": sum(
                residual["strict_diagnostic_passed"] for residual in all_residuals
            ),
            "strict_fail_count": sum(
                not residual["strict_diagnostic_passed"] for residual in all_residuals
            ),
            "distance_rmse_m": None if not all_residuals else float(math.sqrt(
                np.mean(np.square([item["distance_m"] for item in all_residuals]))
            )),
            "distance_max_m": None if not all_residuals else float(max(
                item["distance_m"] for item in all_residuals
            )),
            "covered_track_count": covered_tracks,
            "covered_cameras": sorted(covered_cameras),
            "covered_camera_pairs": [list(pair) for pair in sorted(covered_pairs)],
            "strict_coverage_passed": strict_coverage_passed,
            "strict_coverage_requirements": {
                "minimum_tracks": STRICT_MINIMUM_TRACKS,
                "minimum_residuals": STRICT_MINIMUM_RESIDUALS,
                "required_cameras": sorted(STRICT_REQUIRED_CAMERAS),
                "required_camera_pairs": [
                    list(pair) for pair in sorted(STRICT_REQUIRED_CAMERA_PAIRS)
                ],
                "maximum_residual_m": STRICT_DISTANCE_THRESHOLD_M,
            },
        },
        "acceptance_failures": [
            "current_review_has_no_hash_bound_selected_frame_contact_labels"
            if not contact_reviews
            else "contact_reviews_are_not_surveyed_ground_truth",
            "identity_review_is_not_independent_human_review",
            "intrinsics_are_nominal_not_measured",
            "camera_specific_origins_are_unsurveyed_twin_pose_hypotheses",
            "interpolated_vehicle_consistency_is_not_global_position_truth",
            "strict_later_day_heldout_partition_is_missing",
        ],
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-review", type=Path, required=True)
    parser.add_argument("--camera-config", type=Path, required=True)
    parser.add_argument("--maximum-bracket-gap-seconds", type=float, default=0.5)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    result = evaluate(
        args.identity_review, args.camera_config, args.maximum_bracket_gap_seconds
    )
    write_json_exclusive(args.output, result)
    print(json.dumps({
        "output": str(args.output.resolve()),
        "residual_count": result["aggregate"]["interpolated_cross_camera_residual_count"],
        "strict_pass_count": result["aggregate"]["strict_pass_count"],
        "strict_fail_count": result["aggregate"]["strict_fail_count"],
        "acceptance_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
