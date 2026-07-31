#!/usr/bin/env python3
"""Capture exact same-producer-timestamp frames from all four KVS streams.

The read-only command requires a passing hash-bound timestamp phase audit. It
retains encoded JPEGs only when every stream returns the requested producer
timestamp within one millisecond, eliminating trajectory interpolation from
subsequent cross-camera identity and geometry review.
"""

import argparse
import base64
import binascii
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import cv2
import numpy as np


CAMERAS = ("ch1", "ch2", "ch3", "ch4")
EXACT_TIMESTAMP_TOLERANCE_MS = 1.0


class SynchronizedCaptureError(RuntimeError):
    pass


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256(path):
    return sha256_bytes(Path(path).read_bytes())


def parse_utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SynchronizedCaptureError("timestamp is not canonical UTC")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SynchronizedCaptureError("timestamp is invalid") from exc
    return result.astimezone(timezone.utc)


def canonical_utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def write_bytes_exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_exclusive(path, value):
    write_bytes_exclusive(
        path, json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
    )


def read_audit(path, expected_hash):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise SynchronizedCaptureError("timestamp audit is unreadable or invalid") from exc
    if sha256_bytes(raw) != expected_hash:
        raise SynchronizedCaptureError("timestamp audit hash does not match")
    if value.get("schema") != "v2x-kvs-intercamera-timestamp-audit/v1":
        raise SynchronizedCaptureError("timestamp audit schema is unsupported")
    if value.get("producer_timestamp_phase_diagnostic_passed") is not True:
        raise SynchronizedCaptureError("timestamp audit did not pass fixed phase gates")
    return path, raw, value


def nearest_image(images, target):
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
            candidates.append((
                abs((timestamp - target).total_seconds()), timestamp, bytes(content)
            ))
    if not candidates:
        raise SynchronizedCaptureError("KVS returned no usable image")
    return min(candidates, key=lambda item: (item[0], item[1]))


def make_review_sheet(rows, tile_width=480, tile_height=390):
    sheet = np.full(
        (len(rows) * tile_height, len(CAMERAS) * tile_width, 3), 25, dtype=np.uint8
    )
    for row_index, row in enumerate(rows):
        for column, camera in enumerate(CAMERAS):
            encoded = row[camera]
            image = cv2.imdecode(
                np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            if image is None:
                raise SynchronizedCaptureError("grid frame cannot be decoded")
            available_height = tile_height - 30
            scale = min(
                tile_width / image.shape[1], available_height / image.shape[0]
            )
            resized = cv2.resize(
                image,
                (
                    max(1, int(image.shape[1] * scale)),
                    max(1, int(image.shape[0] * scale)),
                ),
                interpolation=cv2.INTER_AREA,
            )
            x = column * tile_width + (tile_width - resized.shape[1]) // 2
            y = row_index * tile_height + 30 + (
                available_height - resized.shape[0]
            ) // 2
            sheet[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
            cv2.putText(
                sheet,
                f"{row['_timestamp']} {camera}",
                (column * tile_width + 5, row_index * tile_height + 21),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.46,
                (240, 240, 240),
                1,
                cv2.LINE_AA,
            )
    success, encoded = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not success:
        raise SynchronizedCaptureError("failed to encode synchronized review sheet")
    return encoded.tobytes()


def capture(profile, region, audit_path, audit_hash, targets, output_directory):
    audit_path, audit_raw, audit = read_audit(audit_path, audit_hash)
    if not targets or len(targets) > 20:
        raise SynchronizedCaptureError("target count must be in [1, 20]")
    parsed = [parse_utc(value) for value in targets]
    if len(set(parsed)) != len(parsed):
        raise SynchronizedCaptureError("target timestamps are duplicated")
    import boto3

    session = boto3.Session(profile_name=profile, region_name=region)
    kvs = session.client("kinesisvideo")
    clients = {}
    for camera in CAMERAS:
        stream = audit["streams"][camera]["stream_name"]
        endpoint = kvs.get_data_endpoint(
            StreamName=stream, APIName="GET_IMAGES"
        )["DataEndpoint"]
        clients[camera] = session.client(
            "kinesis-video-archived-media", endpoint_url=endpoint
        )
    output_directory = Path(output_directory).resolve()
    report_rows, sheet_rows = [], []
    for target in sorted(parsed):
        timestamp_text = canonical_utc(target)
        directory_name = timestamp_text.replace(":", "").replace("-", "")
        descriptors, sheet = {}, {"_timestamp": timestamp_text}
        for camera in CAMERAS:
            stream = audit["streams"][camera]["stream_name"]
            response = clients[camera].get_images(
                StreamName=stream,
                ImageSelectorType="PRODUCER_TIMESTAMP",
                # KVS rejects some sub-second windows after service-side
                # timestamp normalization. Keep a two-second query window and
                # enforce exact identity on the selected response below.
                StartTimestamp=target - timedelta(seconds=1),
                EndTimestamp=target + timedelta(seconds=1),
                SamplingInterval=200,
                Format="JPEG",
                MaxResults=10,
            )
            offset_seconds, selected, encoded = nearest_image(
                response.get("Images", []), target
            )
            offset_ms = offset_seconds * 1000.0
            if offset_ms > EXACT_TIMESTAMP_TOLERANCE_MS:
                raise SynchronizedCaptureError(
                    f"{timestamp_text} {camera} has no exact producer frame"
                )
            image = cv2.imdecode(
                np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            if image is None:
                raise SynchronizedCaptureError("KVS frame is not a decodable JPEG")
            height, width = image.shape[:2]
            frame_path = output_directory / "frames" / directory_name / f"{camera}.jpg"
            write_bytes_exclusive(frame_path, encoded)
            descriptors[camera] = {
                "stream_name": stream,
                "requested_timestamp_utc": timestamp_text,
                "selected_timestamp_utc": canonical_utc(selected),
                "absolute_offset_ms": offset_ms,
                "path": str(frame_path),
                "encoded_jpeg_sha256": sha256(frame_path),
                "width": width,
                "height": height,
            }
            sheet[camera] = encoded
        report_rows.append({
            "target_timestamp_utc": timestamp_text,
            "exact_all_camera_timestamp": True,
            "cameras": descriptors,
        })
        sheet_rows.append(sheet)
    sheet_path = output_directory / "synchronized-review-sheet.jpg"
    write_bytes_exclusive(sheet_path, make_review_sheet(sheet_rows))
    return {
        "schema": "v2x-synchronized-kvs-frame-grid/v1",
        "acceptance_eligible": False,
        "timestamp_audit": {
            "path": str(audit_path),
            "sha256": sha256_bytes(audit_raw),
        },
        "region": region,
        "exact_timestamp_tolerance_ms": EXACT_TIMESTAMP_TOLERANCE_MS,
        "rows": report_rows,
        "review_sheet": {"path": str(sheet_path), "sha256": sha256(sheet_path)},
        "acceptance_failures": [
            "synchronized_frames_do_not_establish_vehicle_identity",
            "producer_timestamp_equality_does_not_prove_exposure_midpoint_equality",
            "frames_have_no_reviewed_wheel_road_contacts",
            "synchronized_capture_does_not_calibrate_geometry",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aws-profile", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--timestamp-audit", type=Path, required=True)
    parser.add_argument("--timestamp-audit-sha256", required=True)
    parser.add_argument("--target-utc", action="append", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = capture(
        args.aws_profile,
        args.region,
        args.timestamp_audit,
        args.timestamp_audit_sha256,
        args.target_utc,
        args.output_directory,
    )
    write_json_exclusive(args.output, result)
    print(json.dumps({
        "output": str(args.output.resolve()),
        "timestamp_count": len(result["rows"]),
        "frame_count": len(result["rows"]) * len(CAMERAS),
        "acceptance_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
