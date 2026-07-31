#!/usr/bin/env python3
"""Recover hash-bound KVS frames for retained V2 detection events.

The command is read-only against Kinesis Video Streams.  It verifies the
sanitized detection snapshot, requests a narrow producer-timestamp image
window, and retains the closest native-resolution JPEG plus a bbox overlay.
Signed endpoints and credentials are never written to the report.
"""

import argparse
import base64
import binascii
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path

import cv2
import numpy as np


VEHICLE_TYPES = {"car", "truck", "bus", "motorcycle"}
TEMPORAL_GATE_MS = 150.0
MAXIMUM_DIAGNOSTIC_CAPTURE_OFFSET_MS = 500.0
SOURCE_FRAME_IDENTITY_TOLERANCE_MS = 1.0


class CaptureError(RuntimeError):
    pass


def canonical_json_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256(path):
    return sha256_bytes(Path(path).read_bytes())


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CaptureError("media timestamp is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CaptureError("media timestamp is invalid") from exc
    if parsed.utcoffset() != timedelta(0):
        raise CaptureError("media timestamp is not UTC")
    return parsed


def write_bytes_exclusive(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_exclusive(path, value):
    write_bytes_exclusive(path, json.dumps(
        value, indent=2, sort_keys=True, ensure_ascii=False
    ).encode() + b"\n")


def load_snapshot(directory):
    directory = Path(directory).resolve()
    manifest_path = directory / "manifest.json"
    detections_path = directory / "detections.ndjson"
    try:
        manifest_raw = manifest_path.read_bytes()
        manifest = json.loads(manifest_raw)
        detections_raw = detections_path.read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureError("snapshot is unreadable or invalid") from exc
    if manifest.get("schema") != "v2x-detection-corpus-snapshot/v1":
        raise CaptureError("snapshot schema is unsupported")
    if (
        (manifest.get("artifacts") or {}).get("detections.ndjson")
        != sha256_bytes(detections_raw)
    ):
        raise CaptureError("snapshot detections hash does not match manifest")
    rows = []
    for line_number, line in enumerate(detections_raw.splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaptureError(
                f"snapshot line {line_number} is invalid"
            ) from exc
        if not isinstance(row, dict):
            raise CaptureError(f"snapshot line {line_number} is not an object")
        rows.append(row)
    if len(rows) != (manifest.get("counts") or {}).get("items"):
        raise CaptureError("snapshot item count does not match manifest")
    return {
        "directory": directory,
        "manifest_path": manifest_path,
        "manifest_sha256": sha256_bytes(manifest_raw),
        "detections_path": detections_path,
        "detections_sha256": sha256_bytes(detections_raw),
        "rows": rows,
    }


def load_camera_config(path):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureError("camera config is unreadable or invalid") from exc
    cameras = value.get("cameras") if isinstance(value, dict) else None
    if not isinstance(cameras, list):
        raise CaptureError("camera config has no camera list")
    indexed = {}
    for camera in cameras:
        camera_id = camera.get("id") if isinstance(camera, dict) else None
        intrinsics = camera.get("intrinsics") if isinstance(camera, dict) else None
        if (
            camera_id not in {"ch1", "ch2", "ch3", "ch4"}
            or not isinstance(intrinsics, dict)
            or not isinstance(intrinsics.get("width"), int)
            or not isinstance(intrinsics.get("height"), int)
        ):
            raise CaptureError("camera config contains an invalid camera")
        indexed[camera_id] = camera
    if set(indexed) != {"ch1", "ch2", "ch3", "ch4"}:
        raise CaptureError("camera config must contain exactly ch1 through ch4")
    return path, sha256_bytes(raw), indexed


def camera_id_for(row):
    device = row.get("device_id")
    if not isinstance(device, str) or "-" not in device:
        raise CaptureError("detection has no supported camera device ID")
    camera_id = device.rsplit("-", 1)[-1]
    if camera_id not in {"ch1", "ch2", "ch3", "ch4"}:
        raise CaptureError("detection camera is unsupported")
    return camera_id


def select_events(rows, event_ids, object_ids):
    requested_events = set(event_ids)
    requested_objects = set(object_ids)
    if not requested_events and not requested_objects:
        raise CaptureError("at least one event ID or object ID is required")
    selected = [
        row for row in rows
        if row.get("event_id") in requested_events
        or row.get("object_id") in requested_objects
    ]
    found_events = {row.get("event_id") for row in selected}
    found_objects = {row.get("object_id") for row in selected}
    if requested_events - found_events:
        raise CaptureError("one or more requested event IDs are absent")
    if requested_objects - found_objects:
        raise CaptureError("one or more requested object IDs are absent")
    if len(selected) > 100:
        raise CaptureError("refusing to capture more than 100 event frames")
    seen = set()
    for row in selected:
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or event_id in seen:
            raise CaptureError("selected event IDs are missing or duplicated")
        seen.add(event_id)
        if row.get("object_type") not in VEHICLE_TYPES:
            raise CaptureError("selected event is not a supported vehicle")
        parse_utc(row.get("media_timestamp_utc"))
        camera_id_for(row)
    return sorted(selected, key=lambda row: (row["media_timestamp_utc"], row["event_id"]))


def choose_nearest_image(images, target):
    candidates = []
    for item in images:
        timestamp = item.get("TimeStamp") if isinstance(item, dict) else None
        content = item.get("ImageContent") if isinstance(item, dict) else None
        if isinstance(content, str):
            try:
                content = base64.b64decode(content, validate=True)
            except (ValueError, binascii.Error):
                content = None
        if (
            isinstance(timestamp, datetime)
            and isinstance(content, (bytes, bytearray))
            and content
            and not item.get("Error")
        ):
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)
            candidates.append((abs((timestamp - target).total_seconds()), timestamp, bytes(content)))
    if not candidates:
        raise CaptureError("KVS returned no usable image near the event")
    return min(candidates, key=lambda item: (item[0], item[1]))


def bbox_for(row):
    value = (((row.get("camera_data") or {}).get("bifocal_metadata") or {}).get("bbox"))
    if not isinstance(value, dict):
        raise CaptureError("detection has no bbox")
    try:
        bbox = [float(value[key]) for key in ("x1", "y1", "x2", "y2")]
    except (KeyError, TypeError, ValueError) as exc:
        raise CaptureError("detection bbox is invalid") from exc
    if not all(math.isfinite(item) for item in bbox) or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise CaptureError("detection bbox is invalid")
    return bbox


def bbox_diagnostics(bbox, width, height):
    x1, y1, x2, y2 = bbox
    outside = x1 < 0.0 or y1 < 0.0 or x2 > width or y2 > height
    touches_boundary = x1 <= 0.0 or y1 <= 0.0 or x2 >= width or y2 >= height
    return {
        "within_frame": not outside,
        "touches_frame_boundary": touches_boundary,
        "untruncated_contact_candidate": not touches_boundary and not outside,
    }


def overlay_bbox(image, bbox, label, bbox_applies_to_frame):
    output = image.copy()
    x1, y1, x2, y2 = (int(round(value)) for value in bbox)
    color = (30, 220, 30) if bbox_applies_to_frame else (20, 80, 240)
    cv2.rectangle(output, (x1, y1), (x2, y2), color, 5)
    suffix = " exact" if bbox_applies_to_frame else " EVENT BBOX / NEARBY FRAME"
    label = label + suffix
    cv2.putText(
        output, label, (max(10, x1), max(35, y1 - 12)),
        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 6, cv2.LINE_AA,
    )
    cv2.putText(
        output, label, (max(10, x1), max(35, y1 - 12)),
        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2, cv2.LINE_AA,
    )
    success, encoded = cv2.imencode(".jpg", output, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not success:
        raise CaptureError("failed to encode bbox overlay")
    return encoded.tobytes()


def capture(repository_snapshot, camera_config, selected, output_directory,
            profile_name, region_name="us-west-2"):
    import boto3

    output_directory = Path(output_directory).resolve()
    config_path, config_hash, cameras = camera_config
    session = boto3.Session(profile_name=profile_name, region_name=region_name)
    kvs = session.client("kinesisvideo")
    archived_clients = {}
    reports = []
    for row in selected:
        camera_id = camera_id_for(row)
        stream_name = f"v2x-backend-cam-{camera_id}"
        if camera_id not in archived_clients:
            endpoint = kvs.get_data_endpoint(
                StreamName=stream_name, APIName="GET_IMAGES"
            )["DataEndpoint"]
            archived_clients[camera_id] = session.client(
                "kinesis-video-archived-media", endpoint_url=endpoint
            )
        target = parse_utc(row["media_timestamp_utc"])
        response = archived_clients[camera_id].get_images(
            StreamName=stream_name,
            ImageSelectorType="PRODUCER_TIMESTAMP",
            StartTimestamp=target - timedelta(seconds=1),
            EndTimestamp=target + timedelta(seconds=1),
            SamplingInterval=200,
            Format="JPEG",
            MaxResults=25,
        )
        offset_seconds, selected_time, content = choose_nearest_image(
            response.get("Images", []), target
        )
        event_id = row["event_id"]
        image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise CaptureError("KVS image is not a decodable JPEG")
        expected = cameras[camera_id]["intrinsics"]
        height, width = image.shape[:2]
        if [width, height] != [expected["width"], expected["height"]]:
            raise CaptureError("KVS image resolution disagrees with camera config")
        offset_ms = offset_seconds * 1000.0
        if offset_ms > MAXIMUM_DIAGNOSTIC_CAPTURE_OFFSET_MS:
            raise CaptureError(
                f"{event_id} "
                f"({camera_id}) nearest KVS frame is {offset_ms:.1f} ms from the event; "
                "outside the 500 ms diagnostic retention bound"
            )
        bbox = bbox_for(row)
        bbox_state = bbox_diagnostics(bbox, width, height)
        bbox_applies = offset_ms <= SOURCE_FRAME_IDENTITY_TOLERANCE_MS
        frame_path = output_directory / "frames" / f"{event_id}.jpg"
        overlay_path = output_directory / "overlays" / f"{event_id}.jpg"
        write_bytes_exclusive(frame_path, content)
        write_bytes_exclusive(
            overlay_path,
            overlay_bbox(
                image,
                bbox,
                f"{camera_id} {row['object_type']} {event_id[:8]}",
                bbox_applies,
            ),
        )
        reports.append({
            "event_id": event_id,
            "object_id": row.get("object_id"),
            "object_type": row["object_type"],
            "camera_id": camera_id,
            "stream_name": stream_name,
            "media_timestamp_utc": row["media_timestamp_utc"],
            "selected_frame_timestamp_utc": selected_time.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "absolute_time_offset_ms": offset_ms,
            "temporal_gate_ms": TEMPORAL_GATE_MS,
            "temporal_gate_passed": offset_ms <= TEMPORAL_GATE_MS,
            "bbox_xyxy": bbox,
            "bbox_bottom_center_px": [(bbox[0] + bbox[2]) / 2.0, bbox[3]],
            "bbox_frame_binding": {
                "source": "detection_event_frame_only",
                "selected_frame_identity_tolerance_ms": (
                    SOURCE_FRAME_IDENTITY_TOLERANCE_MS
                ),
                "applies_to_selected_frame": bbox_applies,
                **bbox_state,
            },
            "detection_record_sha256": sha256_bytes(canonical_json_bytes(row)),
            "frame": {
                "path": str(frame_path),
                "sha256": sha256(frame_path),
                "width": width,
                "height": height,
            },
            "overlay": {
                "path": str(overlay_path),
                "sha256": sha256(overlay_path),
            },
            "ground_contact_reviewed": False,
            "selected_frame_geometry_eligible": bool(
                bbox_applies and bbox_state["untruncated_contact_candidate"]
            ),
            "acceptance_eligible": False,
        })
    acceptance_failures = [
        "model_object_id_is_not_reviewed_identity_truth",
        "bbox_bottom_center_is_not_reviewed_wheel_road_contact",
        "event_bbox_does_not_apply_to_a_nearby_recovered_frame",
        "frames_are_evidence_inputs_not_calibration_targets",
        "camera_intrinsics_are_not_measured",
    ]
    if any(not event["temporal_gate_passed"] for event in reports):
        acceptance_failures.append("one_or_more_event_frames_exceed_150ms")
    return {
        "schema": "v2x-detection-event-frame-capture/v2",
        "generated_at": utc_now(),
        "acceptance_eligible": False,
        "source_snapshot": {
            "path": str(repository_snapshot["directory"]),
            "manifest_sha256": repository_snapshot["manifest_sha256"],
            "detections_sha256": repository_snapshot["detections_sha256"],
        },
        "camera_config": {
            "path": str(config_path),
            "sha256": config_hash,
        },
        "region": region_name,
        "events": reports,
        "temporal_gate": {
            "maximum_offset_ms": TEMPORAL_GATE_MS,
            "passed_event_count": sum(
                event["temporal_gate_passed"] for event in reports
            ),
            "failed_event_count": sum(
                not event["temporal_gate_passed"] for event in reports
            ),
        },
        "acceptance_failures": acceptance_failures,
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--camera-config", type=Path, required=True)
    parser.add_argument("--event-id", action="append", default=[])
    parser.add_argument("--object-id", action="append", default=[])
    parser.add_argument("--aws-profile", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    snapshot = load_snapshot(args.snapshot)
    config = load_camera_config(args.camera_config)
    selected = select_events(snapshot["rows"], args.event_id, args.object_id)
    output_directory = args.output_directory.resolve()
    output = args.output.resolve()
    if output.parent != output_directory:
        raise CaptureError("--output must be directly inside --output-directory")
    report = capture(
        snapshot, config, selected, output_directory, args.aws_profile, args.region
    )
    write_json_exclusive(output, report)
    print(json.dumps({
        "output": str(output),
        "event_count": len(report["events"]),
        "cameras": sorted({event["camera_id"] for event in report["events"]}),
        "acceptance_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
