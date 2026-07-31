#!/usr/bin/env python3
"""Capture a hash-bound dense KVS image window around retained events.

This command is read-only against Kinesis Video Streams.  It derives the
window from an existing event-frame capture report, requests producer-time
JPEGs, and writes immutable frame/hash evidence without persisting signed
endpoints or credentials.
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
import shutil
import uuid

import cv2
import numpy as np


INPUT_SCHEMAS = {
    "v2x-detection-event-frame-capture/v1",
    "v2x-detection-event-frame-capture/v2",
}
OUTPUT_SCHEMA = "v2x-dense-kvs-window/v1"
CAMERAS = {"ch1", "ch2", "ch3", "ch4"}


class DenseCaptureError(RuntimeError):
    pass


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def parse_utc(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DenseCaptureError(f"{label} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DenseCaptureError(f"{label} is invalid") from exc
    if parsed.utcoffset() != timedelta(0):
        raise DenseCaptureError(f"{label} is not UTC")
    return parsed


def canonical_utc(value):
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def load_source_report(path, camera_id, object_id):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        report = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise DenseCaptureError("source event report is unreadable or invalid") from exc
    if report.get("schema") not in INPUT_SCHEMAS:
        raise DenseCaptureError("source event report schema is unsupported")
    events = report.get("events")
    if not isinstance(events, list):
        raise DenseCaptureError("source event report has no event list")
    selected = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("camera_id") == camera_id
        and event.get("object_id") == object_id
    ]
    if len(selected) < 2:
        raise DenseCaptureError(
            "fewer than two source events bind the requested window"
        )
    event_ids = []
    timestamps = []
    for event in selected:
        event_id = event.get("event_id")
        frame = event.get("frame")
        if (
            not isinstance(event_id, str)
            or not event_id
            or event_id in event_ids
            or not isinstance(frame, dict)
            or not isinstance(frame.get("sha256"), str)
            or len(frame["sha256"]) != 64
        ):
            raise DenseCaptureError("source event identity or frame binding is invalid")
        frame_path = Path(frame.get("path", "")).expanduser().resolve()
        try:
            frame_raw = frame_path.read_bytes()
        except OSError as exc:
            raise DenseCaptureError("source event frame is unreadable") from exc
        if sha256_bytes(frame_raw) != frame["sha256"]:
            raise DenseCaptureError("source event frame hash does not match")
        event_ids.append(event_id)
        timestamps.append(
            parse_utc(event.get("selected_frame_timestamp_utc"), "event timestamp")
        )
    selected.sort(key=lambda value: value["selected_frame_timestamp_utc"])
    return path, raw, selected, min(timestamps), max(timestamps)


def validate_parameters(camera_id, object_id, padding_seconds, sampling_ms):
    if camera_id not in CAMERAS:
        raise DenseCaptureError("camera must be ch1 through ch4")
    if not isinstance(object_id, str) or not object_id.strip():
        raise DenseCaptureError("object ID is missing")
    if (
        not isinstance(padding_seconds, (int, float))
        or isinstance(padding_seconds, bool)
        or not math.isfinite(float(padding_seconds))
        or not 0.0 <= float(padding_seconds) <= 10.0
    ):
        raise DenseCaptureError("padding must be between zero and ten seconds")
    if (
        not isinstance(sampling_ms, int)
        or isinstance(sampling_ms, bool)
        or not 200 <= sampling_ms <= 20000
    ):
        raise DenseCaptureError("sampling interval must be 200 through 20000 ms")


def decode_image(item):
    timestamp = item.get("TimeStamp") if isinstance(item, dict) else None
    content = item.get("ImageContent") if isinstance(item, dict) else None
    if isinstance(content, str):
        try:
            content = base64.b64decode(content, validate=True)
        except (ValueError, binascii.Error):
            content = None
    if (
        not isinstance(timestamp, datetime)
        or not isinstance(content, (bytes, bytearray))
        or not content
        or item.get("Error")
    ):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    content = bytes(content)
    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise DenseCaptureError("KVS returned an undecodable JPEG")
    return timestamp, content, image


def capture(
    source_report,
    camera_id,
    object_id,
    output_dir,
    profile_name,
    region_name="us-west-2",
    padding_seconds=1.0,
    sampling_ms=200,
    *,
    session_factory=None,
):
    validate_parameters(camera_id, object_id, padding_seconds, sampling_ms)
    source_path, source_raw, events, first, last = load_source_report(
        source_report, camera_id, object_id
    )
    start = first - timedelta(seconds=float(padding_seconds))
    end = last + timedelta(seconds=float(padding_seconds))
    expected_count = (
        int(math.floor((end - start).total_seconds() * 1000 / sampling_ms)) + 1
    )
    if expected_count > 100:
        raise DenseCaptureError(
            "requested dense window exceeds the 100-image API bound"
        )

    if session_factory is None:
        import boto3

        session_factory = boto3.Session
    session = session_factory(profile_name=profile_name, region_name=region_name)
    kvs = session.client("kinesisvideo")
    stream_name = f"v2x-backend-cam-{camera_id}"
    endpoint = kvs.get_data_endpoint(
        StreamName=stream_name, APIName="GET_IMAGES"
    )["DataEndpoint"]
    archived = session.client(
        "kinesis-video-archived-media", endpoint_url=endpoint
    )
    request = {
        "StreamName": stream_name,
        "ImageSelectorType": "PRODUCER_TIMESTAMP",
        "StartTimestamp": start,
        "EndTimestamp": end,
        "SamplingInterval": sampling_ms,
        "Format": "JPEG",
        "MaxResults": 100,
    }
    response = archived.get_images(**request)
    image_items = list(response.get("Images", []))
    page_count = 1
    while response.get("NextToken"):
        page_count += 1
        if page_count > 10:
            raise DenseCaptureError("KVS dense capture exceeded ten response pages")
        response = archived.get_images(
            **request,
            NextToken=response["NextToken"],
        )
        image_items.extend(response.get("Images", []))
        if len(image_items) > 110:
            raise DenseCaptureError("KVS returned more images than the bounded request")
    decoded = [
        value for item in image_items if (value := decode_image(item))
    ]
    if len(decoded) < 3:
        raise DenseCaptureError("KVS returned fewer than three usable dense frames")
    decoded.sort(key=lambda value: value[0])
    timestamps = [value[0] for value in decoded]
    if len(set(timestamps)) != len(timestamps):
        raise DenseCaptureError("KVS returned duplicate producer timestamps")
    dimensions = {(image.shape[1], image.shape[0]) for _, _, image in decoded}
    if len(dimensions) != 1:
        raise DenseCaptureError("dense KVS frames have mixed resolutions")
    width, height = next(iter(dimensions))

    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir.exists():
        raise DenseCaptureError("dense capture output already exists")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_dir.parent / f".{output_dir.name}.tmp-{uuid.uuid4().hex}"
    rows = []
    try:
        (temporary / "frames").mkdir(parents=True)
        for index, (timestamp, content, _image) in enumerate(decoded):
            name = f"frame-{index:03d}.jpg"
            path = temporary / "frames" / name
            path.write_bytes(content)
            rows.append({
                "index": index,
                "producer_timestamp_utc": canonical_utc(timestamp),
                "path": f"frames/{name}",
                "sha256": sha256_bytes(content),
                "byte_count": len(content),
                "width": width,
                "height": height,
            })
        gaps_ms = [
            (right - left).total_seconds() * 1000.0
            for left, right in zip(timestamps, timestamps[1:])
        ]
        report = {
            "schema": OUTPUT_SCHEMA,
            "acceptance_eligible": False,
            "source_event_report": {
                "path": str(source_path),
                "sha256": sha256_bytes(source_raw),
            },
            "source_events": [
                {
                    "event_id": event["event_id"],
                    "selected_frame_timestamp_utc": event[
                        "selected_frame_timestamp_utc"
                    ],
                    "frame_sha256": event["frame"]["sha256"],
                }
                for event in events
            ],
            "camera_id": camera_id,
            "object_id": object_id,
            "stream_name": stream_name,
            "region": region_name,
            "requested_window": {
                "start_utc": canonical_utc(start),
                "end_utc": canonical_utc(end),
                "sampling_interval_ms": sampling_ms,
                "padding_seconds": float(padding_seconds),
            },
            "resolution": [width, height],
            "frames": rows,
            "frame_count": len(rows),
            "response_page_count": page_count,
            "discarded_error_count": len(image_items) - len(decoded),
            "maximum_interframe_gap_ms": max(gaps_ms) if gaps_ms else None,
            "acceptance_failures": [
                "dense_frames_are_unreviewed_tracking_inputs",
                "model_object_id_is_not_independent_identity_truth",
                "camera_intrinsics_are_not_measured",
            ],
            "safety": {
                "read_only_kinesis_calls": True,
                "signed_endpoints_persisted": False,
                "credentials_persisted": False,
            },
        }
        (temporary / "capture-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.rename(temporary, output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output_dir / "capture-report.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--camera", required=True, choices=sorted(CAMERAS))
    parser.add_argument("--object-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--aws-profile", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--padding-seconds", type=float, default=1.0)
    parser.add_argument("--sampling-ms", type=int, default=200)
    args = parser.parse_args(argv)
    try:
        output = capture(
            args.source_report,
            args.camera,
            args.object_id,
            args.output_dir,
            args.aws_profile,
            args.region,
            args.padding_seconds,
            args.sampling_ms,
        )
    except DenseCaptureError as exc:
        parser.error(str(exc))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
