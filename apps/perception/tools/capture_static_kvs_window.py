#!/usr/bin/env python3
"""Capture a bounded, hash-bound KVS window for static-scene proposals.

This command performs read-only Kinesis Video Streams API calls.  Its explicit
UTC window is independent of detections and object IDs.  The retained JPEGs
are proposal inputs only: temporal stability does not prove that a scene is
vehicle-free and does not establish calibration truth.
"""

import argparse
import base64
import binascii
import ctypes
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import uuid

import cv2
import numpy as np


OUTPUT_SCHEMA = "v2x-static-kvs-window-proposal/v1"
CAMERAS = {"ch1", "ch2", "ch3", "ch4"}
MIN_DURATION_SECONDS = 1.0
MAX_DURATION_SECONDS = 20.0
MIN_SAMPLING_MS = 200
MAX_IMAGES = 100
MAX_IMAGES_PER_CALL = 25
MAX_CALLS = int(math.ceil((MAX_IMAGES - 1) / (MAX_IMAGES_PER_CALL - 1)))
DISCARDABLE_IMAGE_ERRORS = {"NO_MEDIA", "MEDIA_ERROR"}


class StaticCaptureError(RuntimeError):
    pass


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def canonical_utc(value):
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def parse_canonical_utc(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise StaticCaptureError(f"{label} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise StaticCaptureError(f"{label} is invalid") from exc
    if parsed.utcoffset() != timedelta(0) or canonical_utc(parsed) != value:
        raise StaticCaptureError(
            f"{label} must use canonical UTC with millisecond precision"
        )
    return parsed


def validate_parameters(camera_id, start_utc, end_utc, sampling_ms):
    if camera_id not in CAMERAS:
        raise StaticCaptureError("camera must be ch1 through ch4")
    start = parse_canonical_utc(start_utc, "start UTC")
    end = parse_canonical_utc(end_utc, "end UTC")
    duration_seconds = (end - start).total_seconds()
    if not MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS:
        raise StaticCaptureError("duration must be between 1 and 20 seconds")
    if (
        not isinstance(sampling_ms, int)
        or isinstance(sampling_ms, bool)
        or sampling_ms < MIN_SAMPLING_MS
        or sampling_ms > int(MAX_DURATION_SECONDS * 1000)
    ):
        raise StaticCaptureError("sampling interval must be 200 through 20000 ms")
    expected_count = int(math.floor(duration_seconds * 1000 / sampling_ms)) + 1
    if expected_count < 3:
        raise StaticCaptureError(
            "requested window must contain at least three expected samples"
        )
    if expected_count > MAX_IMAGES:
        raise StaticCaptureError(
            "requested static window exceeds the 100-image API bound"
        )
    return start, end, duration_seconds, expected_count


def build_request_segments(start, end, sampling_ms, expected_count):
    """Split the grid into <=25-sample calls sharing one boundary sample."""
    if expected_count < 2:
        raise StaticCaptureError(
            "each static request segment requires at least two expected samples"
        )
    sampling_delta = timedelta(milliseconds=sampling_ms)
    segments = []
    start_index = 0
    final_index = expected_count - 1
    while True:
        end_index = min(
            start_index + MAX_IMAGES_PER_CALL - 1,
            final_index,
        )
        segment_start = start + sampling_delta * start_index
        segment_end = end if end_index == final_index else start + sampling_delta * end_index
        segments.append({
            "index": len(segments),
            "start": segment_start,
            "end": segment_end,
            "expected_image_count": end_index - start_index + 1,
            "global_start_sample_index": start_index,
            "global_end_sample_index": end_index,
            "boundary_overlap_sample_count": 0 if not segments else 1,
        })
        if end_index == final_index:
            break
        start_index = end_index
    if (
        len(segments) > MAX_CALLS
        or any(
            not 2 <= value["expected_image_count"] <= MAX_IMAGES_PER_CALL
            or value["start"] >= value["end"]
            for value in segments
        )
        or segments[0]["start"] != start
        or segments[-1]["end"] != end
        or any(
            right["start"] != left["end"]
            for left, right in zip(segments, segments[1:])
        )
    ):
        raise StaticCaptureError("static request segmentation is inconsistent")
    return segments


def decode_image(item):
    if not isinstance(item, dict):
        raise StaticCaptureError("KVS returned a malformed image item")
    if item.get("Error") is not None:
        raise StaticCaptureError("KVS image errors must be classified before decode")
    timestamp = item.get("TimeStamp")
    content = item.get("ImageContent")
    if isinstance(content, str):
        try:
            content = base64.b64decode(content, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise StaticCaptureError("KVS returned invalid base64 image content") from exc
    if not isinstance(timestamp, datetime) or not isinstance(
        content, (bytes, bytearray)
    ) or not content:
        raise StaticCaptureError("KVS returned incomplete image content")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    content = bytes(content)
    if not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
        raise StaticCaptureError("KVS returned content that is not a complete JPEG")
    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise StaticCaptureError("KVS returned an undecodable JPEG")
    return timestamp, content, image


def classify_discardable_error(item, requested_start, requested_end, segment_index):
    if not isinstance(item, dict):
        raise StaticCaptureError("KVS returned a malformed image item")
    code = item.get("Error")
    if not isinstance(code, str) or code not in DISCARDABLE_IMAGE_ERRORS:
        raise StaticCaptureError("KVS returned an unknown image error code")
    timestamp = item.get("TimeStamp")
    if timestamp is not None:
        if not isinstance(timestamp, datetime):
            raise StaticCaptureError("KVS image error timestamp is malformed")
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp = timestamp.astimezone(timezone.utc)
        outside_requested_window = not requested_start <= timestamp <= requested_end
        timestamp = canonical_utc(timestamp)
    else:
        outside_requested_window = None
    return {
        "code": code,
        "producer_timestamp_utc": timestamp,
        "segment_index": segment_index,
        "outside_requested_window": outside_requested_window,
    }


def normalize_stream_info(info, stream_name):
    if not isinstance(info, dict) or info.get("StreamName") != stream_name:
        raise StaticCaptureError("KVS stream metadata does not match the request")
    retention = info.get("DataRetentionInHours")
    if not isinstance(retention, int) or isinstance(retention, bool) or retention < 0:
        raise StaticCaptureError("KVS stream retention metadata is invalid")
    created = info.get("CreationTime")
    if created is not None:
        if not isinstance(created, datetime):
            raise StaticCaptureError("KVS stream creation time is invalid")
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created = canonical_utc(created)
    result = {
        "name": stream_name,
        "arn": info.get("StreamARN"),
        "status": info.get("Status"),
        "version": info.get("Version"),
        "creation_time_utc": created,
        "retention_hours": retention,
    }
    for label in ("arn", "status"):
        if not isinstance(result[label], str) or not result[label]:
            raise StaticCaptureError(f"KVS stream {label} metadata is invalid")
    return result


def frame_change_diagnostics(decoded):
    changes = []
    for (left_time, _left_raw, left), (right_time, _right_raw, right) in zip(
        decoded, decoded[1:]
    ):
        difference = cv2.absdiff(left, right)
        gray = cv2.cvtColor(difference, cv2.COLOR_BGR2GRAY)
        changes.append({
            "left_producer_timestamp_utc": canonical_utc(left_time),
            "right_producer_timestamp_utc": canonical_utc(right_time),
            "mean_absolute_luma_difference": float(np.mean(gray)),
            "changed_pixel_fraction_at_16_luma": float(np.mean(gray >= 16)),
        })
    means = [row["mean_absolute_luma_difference"] for row in changes]
    fractions = [row["changed_pixel_fraction_at_16_luma"] for row in changes]
    return {
        "method": "adjacent_decoded_jpeg_absolute_difference/v1",
        "proposal_only": True,
        "does_not_prove_vehicle_free": True,
        "does_not_establish_calibration_truth": True,
        "pair_count": len(changes),
        "maximum_mean_absolute_luma_difference": max(means) if means else None,
        "maximum_changed_pixel_fraction_at_16_luma": (
            max(fractions) if fractions else None
        ),
        "pairs": changes,
    }


def verify_staged_frames(root, rows):
    for row in rows:
        path = root / row["path"]
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise StaticCaptureError("staged static frame is unreadable") from exc
        if len(raw) != row["byte_count"] or sha256_bytes(raw) != row["sha256"]:
            raise StaticCaptureError("staged static frame hash binding failed")
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or [image.shape[1], image.shape[0]] != [
            row["width"],
            row["height"],
        ]:
            raise StaticCaptureError("staged static frame dimensions changed")


def atomic_publish_directory(source, destination):
    """Publish a directory atomically without replacing an existing path."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise StaticCaptureError("atomic no-overwrite publication is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(destination),
        rename_noreplace,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise StaticCaptureError("static capture output already exists")
    raise StaticCaptureError(
        f"atomic static capture publication failed: {os.strerror(error)}"
    )


def capture(
    camera_id,
    start_utc,
    end_utc,
    output_dir,
    profile_name,
    region_name="us-west-2",
    sampling_ms=200,
    *,
    session_factory=None,
):
    start, end, duration_seconds, expected_count = validate_parameters(
        camera_id, start_utc, end_utc, sampling_ms
    )
    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir.exists():
        raise StaticCaptureError("static capture output already exists")

    if session_factory is None:
        import boto3

        session_factory = boto3.Session
    session = session_factory(profile_name=profile_name, region_name=region_name)
    kvs = session.client("kinesisvideo")
    stream_name = f"v2x-backend-cam-{camera_id}"
    stream_info = normalize_stream_info(
        kvs.describe_stream(StreamName=stream_name).get("StreamInfo"), stream_name
    )
    endpoint_response = kvs.get_data_endpoint(
        StreamName=stream_name, APIName="GET_IMAGES"
    )
    endpoint = (
        endpoint_response.get("DataEndpoint")
        if isinstance(endpoint_response, dict)
        else None
    )
    if not isinstance(endpoint, str) or not endpoint:
        raise StaticCaptureError("KVS returned no GetImages endpoint")
    archived = session.client(
        "kinesis-video-archived-media", endpoint_url=endpoint
    )
    request_base = {
        "StreamName": stream_name,
        "ImageSelectorType": "PRODUCER_TIMESTAMP",
        "SamplingInterval": sampling_ms,
        "Format": "JPEG",
        "MaxResults": MAX_IMAGES_PER_CALL,
    }
    segments = build_request_segments(start, end, sampling_ms, expected_count)
    candidates = []
    response_segment_counts = []
    candidate_segment_counts = []
    discarded_segment_counts = []
    outside_global_segment_counts = []
    discarded_errors = []
    discarded_outside_global = []
    for segment in segments:
        response = archived.get_images(
            **request_base,
            StartTimestamp=segment["start"],
            EndTimestamp=segment["end"],
        )
        if not isinstance(response, dict) or not isinstance(
            response.get("Images"), list
        ):
            raise StaticCaptureError("KVS returned a malformed GetImages response")
        if response.get("NextToken"):
            raise StaticCaptureError(
                "KVS returned an unexpected pagination token for a bounded segment"
        )
        items = response["Images"]
        if len(items) > MAX_IMAGES_PER_CALL:
            raise StaticCaptureError(
                "KVS returned more than 25 rows for a bounded request call"
            )
        response_segment_counts.append(len(items))
        segment_valid = []
        segment_errors = []
        segment_outside_global = []
        for item in items:
            if isinstance(item, dict) and item.get("Error") is not None:
                segment_errors.append(
                    classify_discardable_error(
                        item,
                        start,
                        end,
                        segment["index"],
                    )
                )
            else:
                timestamp, content, image = decode_image(item)
                if not start <= timestamp <= end:
                    segment_outside_global.append({
                        "call_index": segment["index"],
                        "producer_timestamp_utc": canonical_utc(timestamp),
                        "sha256": sha256_bytes(content),
                        "byte_count": len(content),
                    })
                else:
                    segment_valid.append(
                        (timestamp, content, image, segment["index"])
                    )
        candidate_segment_counts.append(len(segment_valid))
        discarded_segment_counts.append(len(segment_errors))
        outside_global_segment_counts.append(len(segment_outside_global))
        candidates.extend(segment_valid)
        discarded_errors.extend(segment_errors)
        discarded_outside_global.extend(segment_outside_global)

    retained_candidates = []
    duplicate_records = []
    duplicate_segment_counts = [0 for _segment in segments]
    seen_timestamps = {}
    seen_hashes = {}
    for timestamp, content, image, call_index in sorted(
        candidates, key=lambda value: (value[0], value[3])
    ):
        timestamp_utc = canonical_utc(timestamp)
        digest = sha256_bytes(content)
        reasons = []
        if timestamp_utc in seen_timestamps:
            reasons.append("duplicate_producer_timestamp")
        if digest in seen_hashes:
            reasons.append("duplicate_jpeg_sha256")
        if reasons:
            duplicate_segment_counts[call_index] += 1
            duplicate_records.append({
                "call_index": call_index,
                "producer_timestamp_utc": timestamp_utc,
                "sha256": digest,
                "reasons": reasons,
            })
            continue
        retained_index = len(retained_candidates)
        seen_timestamps[timestamp_utc] = retained_index
        seen_hashes[digest] = retained_index
        retained_candidates.append((timestamp, content, image, call_index))
    if len(retained_candidates) > MAX_IMAGES:
        raise StaticCaptureError("KVS returned more than 100 unique retained frames")
    decoded = [value[:3] for value in retained_candidates]
    if len(decoded) < 3:
        raise StaticCaptureError("KVS returned fewer than three usable static frames")
    timestamps = [value[0] for value in decoded]
    if len({sha256_bytes(value[1]) for value in decoded}) < 3:
        raise StaticCaptureError(
            "KVS returned fewer than three unique usable static JPEGs"
        )
    dimensions = {(image.shape[1], image.shape[0]) for _, _, image in decoded}
    if len(dimensions) != 1:
        raise StaticCaptureError("static KVS frames have mixed resolutions")
    width, height = next(iter(dimensions))

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
        verify_staged_frames(temporary, rows)
        gaps_ms = [
            (right - left).total_seconds() * 1000.0
            for left, right in zip(timestamps, timestamps[1:])
        ]
        observed_at = datetime.now(timezone.utc)
        earliest_retained = observed_at - timedelta(
            hours=stream_info["retention_hours"]
        )
        returned_count_complete = (
            len(decoded) == expected_count
            and not discarded_errors
            and not discarded_outside_global
        )
        retained_segment_counts = [
            sum(1 for value in retained_candidates if value[3] == segment["index"])
            for segment in segments
        ]
        discarded_errors_by_code = {
            code: sum(1 for error in discarded_errors if error["code"] == code)
            for code in sorted(DISCARDABLE_IMAGE_ERRORS)
            if any(error["code"] == code for error in discarded_errors)
        }
        acceptance_failures = [
            "static_window_is_an_unreviewed_proposal_input",
            "temporal_stability_does_not_prove_vehicle_free",
            "window_does_not_establish_calibration_truth",
            "camera_intrinsics_are_not_measured_by_this_capture",
        ]
        if discarded_errors:
            acceptance_failures.append("kvs_documented_image_gaps_were_discarded")
        if discarded_outside_global:
            acceptance_failures.append("out_of_requested_window_media_were_discarded")
        if not returned_count_complete:
            acceptance_failures.append("requested_sampling_count_is_incomplete")
        report = {
            "schema": OUTPUT_SCHEMA,
            "acceptance_eligible": False,
            "observed_at_utc": canonical_utc(observed_at),
            "camera_id": camera_id,
            "region": region_name,
            "request": {
                "api": "GetImages",
                "stream_name": stream_name,
                "image_selector_type": "PRODUCER_TIMESTAMP",
                "start_utc": start_utc,
                "end_utc": end_utc,
                "duration_seconds": duration_seconds,
                "sampling_interval_ms": sampling_ms,
                "format": "JPEG",
                "maximum_total_images": MAX_IMAGES,
                "max_results_per_call": MAX_IMAGES_PER_CALL,
                "bounded_expected_image_count": expected_count,
                "segmentation": {
                    "strategy": "one_boundary_sample_overlap/v1",
                    "max_expected_images_per_segment": MAX_IMAGES_PER_CALL,
                    "segment_count": len(segments),
                    "boundary_overlap_sample_count": 1,
                    "ideal_call_grid_sample_count_sum_including_overlaps": sum(
                        segment["expected_image_count"] for segment in segments
                    ),
                    "response_item_count_sum": sum(response_segment_counts),
                    "decoded_in_global_candidate_count_sum": sum(
                        candidate_segment_counts
                    ),
                    "unique_retained_frame_count": len(decoded),
                    "returned_count_matches_bounded_expectation": (
                        returned_count_complete
                    ),
                    "segments": [
                        {
                            "index": segment["index"],
                            "start_utc": canonical_utc(segment["start"]),
                            "end_utc": canonical_utc(segment["end"]),
                            "expected_image_count": segment[
                                "expected_image_count"
                            ],
                            "global_start_sample_index": segment[
                                "global_start_sample_index"
                            ],
                            "global_end_sample_index": segment[
                                "global_end_sample_index"
                            ],
                            "boundary_overlap_sample_count": segment[
                                "boundary_overlap_sample_count"
                            ],
                            "response_item_count": response_segment_counts[
                                segment["index"]
                            ],
                            "decoded_in_global_candidate_count": candidate_segment_counts[
                                segment["index"]
                            ],
                            "retained_unique_frame_count": retained_segment_counts[
                                segment["index"]
                            ],
                            "discarded_error_count": discarded_segment_counts[
                                segment["index"]
                            ],
                            "discarded_outside_global_window_count": outside_global_segment_counts[
                                segment["index"]
                            ],
                            "discarded_duplicate_count": duplicate_segment_counts[
                                segment["index"]
                            ],
                        }
                        for segment in segments
                    ],
                },
            },
            "stream": stream_info,
            "retention": {
                "data_retention_hours": stream_info["retention_hours"],
                "reported_earliest_retained_utc_at_observation": canonical_utc(
                    earliest_retained
                ),
                "requested_window_within_reported_retention_at_observation": (
                    earliest_retained <= start <= end <= observed_at
                ),
                "observational_only": True,
            },
            "resolution": [width, height],
            "frames": rows,
            "frame_count": len(rows),
            "unique_jpeg_sha256_count": len({row["sha256"] for row in rows}),
            "response_call_count": len(segments),
            "response_pagination_token_count": 0,
            "discarded_error_count": len(discarded_errors),
            "discarded_errors_by_code": discarded_errors_by_code,
            "discarded_errors": discarded_errors,
            "discarded_outside_global_window_count": len(
                discarded_outside_global
            ),
            "discarded_outside_global_window": discarded_outside_global,
            "discarded_duplicate_count": len(duplicate_records),
            "discarded_duplicates": duplicate_records,
            "coverage": {
                "requested_count_complete": returned_count_complete,
                "requested_window_coverage_established": False,
                "reason": (
                    "GetImages samples and temporal differences are proposal-only; "
                    "they do not establish continuous window coverage"
                ),
            },
            "maximum_interframe_gap_ms": max(gaps_ms) if gaps_ms else None,
            "frame_change_diagnostics": frame_change_diagnostics(decoded),
            "acceptance_failures": acceptance_failures,
            "safety": {
                "read_only_kinesis_calls": True,
                "detection_or_object_id_dependency": False,
                "signed_endpoints_persisted": False,
                "pagination_tokens_persisted": False,
                "pagination_tokens_followed": False,
                "maximum_get_images_calls": MAX_CALLS,
                "credentials_persisted": False,
                "atomic_no_overwrite_publication": True,
            },
        }
        (temporary / "capture-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        atomic_publish_directory(temporary, output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output_dir / "capture-report.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", required=True, choices=sorted(CAMERAS))
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--end-utc", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--aws-profile", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--sampling-ms", type=int, default=200)
    args = parser.parse_args(argv)
    try:
        output = capture(
            args.camera,
            args.start_utc,
            args.end_utc,
            args.output_dir,
            args.aws_profile,
            args.region,
            args.sampling_ms,
        )
    except StaticCaptureError as exc:
        parser.error(str(exc))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
