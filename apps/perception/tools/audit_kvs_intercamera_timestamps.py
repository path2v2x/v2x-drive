#!/usr/bin/env python3
"""Audit four-camera producer timestamp phase from one shared KVS window.

The command is read-only. It requests GetImages with the same producer-time
window and sampling interval for every stream, discards image payloads, and
measures nearest timestamp deltas. This can reject unsynchronised evidence but
cannot prove absolute clock accuracy or calibrate geometry.
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import numpy as np


CAMERAS = ("ch1", "ch2", "ch3", "ch4")
MINIMUM_SAMPLES_PER_CAMERA = 20
PAIR_MEDIAN_GATE_MS = 20.0
PAIR_P95_GATE_MS = 50.0
PAIR_MAX_GATE_MS = 100.0
PAIR_MINIMUM_DIRECTIONAL_MATCH_FRACTION = 0.80


class TimestampAuditError(RuntimeError):
    pass


def parse_utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TimestampAuditError("timestamp is not canonical UTC")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise TimestampAuditError("timestamp is invalid") from exc
    return result.astimezone(timezone.utc)


def canonical_utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


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


def usable_timestamps(images, start, end):
    values = []
    for item in images:
        value = item.get("TimeStamp") if isinstance(item, dict) else None
        if not isinstance(value, datetime) or item.get("Error"):
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        if start <= value <= end:
            values.append(value)
    values.sort()
    if len(set(values)) != len(values):
        raise TimestampAuditError("KVS returned duplicate producer timestamps")
    return values


def nearest_pair_deltas(left, right):
    if not left or not right:
        return []
    right_epoch = np.asarray([value.timestamp() for value in right], dtype=float)
    deltas = []
    for value in left:
        epoch = value.timestamp()
        index = int(np.searchsorted(right_epoch, epoch))
        candidates = []
        if index < len(right_epoch):
            candidates.append(right_epoch[index])
        if index:
            candidates.append(right_epoch[index - 1])
        deltas.append(min(abs(candidate - epoch) for candidate in candidates) * 1000.0)
    return deltas


def pair_metrics(left, right, sampling_interval_ms=200):
    forward = nearest_pair_deltas(left, right)
    reverse = nearest_pair_deltas(right, left)
    tolerance = sampling_interval_ms / 2.0
    matched_forward = [value for value in forward if value <= tolerance]
    matched_reverse = [value for value in reverse if value <= tolerance]
    values = np.asarray(matched_forward + matched_reverse, dtype=float)
    if not len(values):
        return None
    forward_fraction = len(matched_forward) / len(forward)
    reverse_fraction = len(matched_reverse) / len(reverse)
    result = {
        "bidirectional_sample_count": len(values),
        "unmatched_sample_count": (
            len(forward) + len(reverse) - len(values)
        ),
        "match_tolerance_ms": tolerance,
        "directional_match_fraction": [forward_fraction, reverse_fraction],
        "median_absolute_delta_ms": float(np.median(values)),
        "p95_absolute_delta_ms": float(np.quantile(values, 0.95)),
        "max_absolute_delta_ms": float(np.max(values)),
        "exact_timestamp_count": len(set(left) & set(right)),
    }
    result["phase_gate_passed"] = bool(
        result["median_absolute_delta_ms"] <= PAIR_MEDIAN_GATE_MS
        and result["p95_absolute_delta_ms"] <= PAIR_P95_GATE_MS
        and result["max_absolute_delta_ms"] <= PAIR_MAX_GATE_MS
        and min(forward_fraction, reverse_fraction)
        >= PAIR_MINIMUM_DIRECTIONAL_MATCH_FRACTION
    )
    return result


def audit(profile, region, start, end, sampling_interval_ms=200):
    if not 200 <= sampling_interval_ms <= 10_000:
        raise TimestampAuditError("sampling interval must be in [200, 10000] ms")
    if start.tzinfo is None or end.tzinfo is None or not start < end:
        raise TimestampAuditError("audit window is invalid")
    if end - start > timedelta(seconds=20):
        raise TimestampAuditError("audit window exceeds 20 seconds")
    import boto3

    session = boto3.Session(profile_name=profile, region_name=region)
    kvs = session.client("kinesisvideo")
    timestamps, stream_descriptors = {}, {}
    for camera in CAMERAS:
        stream = f"v2x-backend-cam-{camera}"
        endpoint = kvs.get_data_endpoint(
            StreamName=stream, APIName="GET_IMAGES"
        )["DataEndpoint"]
        archived = session.client(
            "kinesis-video-archived-media", endpoint_url=endpoint
        )
        response = archived.get_images(
            StreamName=stream,
            ImageSelectorType="PRODUCER_TIMESTAMP",
            StartTimestamp=start,
            EndTimestamp=end,
            SamplingInterval=sampling_interval_ms,
            Format="JPEG",
            MaxResults=100,
        )
        timestamps[camera] = usable_timestamps(response.get("Images", []), start, end)
        info = kvs.describe_stream(StreamName=stream)["StreamInfo"]
        stream_descriptors[camera] = {
            "stream_name": stream,
            "stream_arn": info["StreamARN"],
            "version": info["Version"],
            "retention_hours": info["DataRetentionInHours"],
        }
    pairs = {}
    for index, left in enumerate(CAMERAS):
        for right in CAMERAS[index + 1:]:
            pairs[f"{left}-{right}"] = pair_metrics(
                timestamps[left], timestamps[right], sampling_interval_ms
            )
    sample_gate = all(
        len(values) >= MINIMUM_SAMPLES_PER_CAMERA for values in timestamps.values()
    )
    pair_gate = all(
        value is not None and value["phase_gate_passed"] for value in pairs.values()
    )
    return {
        "schema": "v2x-kvs-intercamera-timestamp-audit/v1",
        "acceptance_eligible": False,
        "region": region,
        "window": {
            "start_utc": canonical_utc(start),
            "end_utc": canonical_utc(end),
            "duration_seconds": (end - start).total_seconds(),
            "sampling_interval_ms": sampling_interval_ms,
            "selector": "PRODUCER_TIMESTAMP",
        },
        "streams": stream_descriptors,
        "timestamps": {
            camera: [canonical_utc(value) for value in values]
            for camera, values in timestamps.items()
        },
        "sample_counts": {
            camera: len(values) for camera, values in timestamps.items()
        },
        "camera_pairs": pairs,
        "producer_timestamp_phase_diagnostic_passed": bool(
            sample_gate and pair_gate
        ),
        "gates": {
            "minimum_samples_per_camera": MINIMUM_SAMPLES_PER_CAMERA,
            "pair_median_absolute_delta_ms": PAIR_MEDIAN_GATE_MS,
            "pair_p95_absolute_delta_ms": PAIR_P95_GATE_MS,
            "pair_max_absolute_delta_ms": PAIR_MAX_GATE_MS,
            "pair_minimum_directional_match_fraction": (
                PAIR_MINIMUM_DIRECTIONAL_MATCH_FRACTION
            ),
            "sample_count_gate_passed": sample_gate,
            "all_pair_phase_gates_passed": pair_gate,
        },
        "acceptance_failures": [
            "producer_timestamp_phase_does_not_prove_absolute_clock_accuracy",
            "one_short_window_does_not_prove_later_day_stability",
            "getimages_sampling_can_hide_subframe_decode_or_exposure_offsets",
            "timestamp_audit_does_not_calibrate_camera_geometry",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aws-profile", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--end-utc", required=True)
    parser.add_argument("--sampling-interval-ms", type=int, default=200)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(
        args.aws_profile,
        args.region,
        parse_utc(args.start_utc),
        parse_utc(args.end_utc),
        args.sampling_interval_ms,
    )
    write_json_exclusive(args.output, result)
    print(json.dumps({
        "output": str(args.output.resolve()),
        "sample_counts": result["sample_counts"],
        "phase_diagnostic_passed": result[
            "producer_timestamp_phase_diagnostic_passed"
        ],
        "acceptance_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
