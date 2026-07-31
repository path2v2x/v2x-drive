#!/usr/bin/env python3
"""Evaluate multi-window KVS producer timestamp phase and drift.

This consumes retained reports from ``audit_kvs_intercamera_timestamps.py``.
It is deliberately diagnostic: aligned KVS producer timestamps can reject an
unsynchronised capture pipeline, but they cannot prove the camera exposure
instant or absolute UTC accuracy without an independent timing target.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np


INPUT_SCHEMA = "v2x-kvs-intercamera-timestamp-audit/v1"
OUTPUT_SCHEMA = "v2x-kvs-intercamera-timestamp-drift/v1"
CAMERAS = ("ch1", "ch2", "ch3", "ch4")
MINIMUM_WINDOWS = 4
MINIMUM_SPAN_HOURS = 12.0
MAXIMUM_P95_PHASE_MS = 75.0
MAXIMUM_PHASE_MS = 125.0
MAXIMUM_ABSOLUTE_DRIFT_MS_PER_HOUR = 10.0
MINIMUM_MATCHED_FRACTION = 0.80


class DriftAuditError(RuntimeError):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DriftAuditError("timestamp is not canonical UTC")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except ValueError as exc:
        raise DriftAuditError("timestamp is invalid") from exc


def load_report(path):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        report = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise DriftAuditError("timestamp audit report is unreadable") from exc
    if report.get("schema") != INPUT_SCHEMA:
        raise DriftAuditError("timestamp audit schema is unsupported")
    timestamps = report.get("timestamps")
    if not isinstance(timestamps, dict) or set(timestamps) != set(CAMERAS):
        raise DriftAuditError("timestamp audit does not contain exactly four cameras")
    parsed = {}
    for camera in CAMERAS:
        values = [parse_utc(value) for value in timestamps[camera]]
        if len(values) < 2 or values != sorted(values) or len(set(values)) != len(values):
            raise DriftAuditError("camera timestamps are sparse, unordered, or duplicated")
        parsed[camera] = values
    window = report.get("window") or {}
    start, end = parse_utc(window.get("start_utc")), parse_utc(window.get("end_utc"))
    if not start < end:
        raise DriftAuditError("timestamp audit window is invalid")
    if any(value < start or value > end for values in parsed.values() for value in values):
        raise DriftAuditError("camera timestamp is outside its audit window")
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "start": start,
        "end": end,
        "midpoint": start + (end - start) / 2,
        "timestamps": parsed,
    }


def _nearest_indices(source, target):
    """Return the nearest target index for each source timestamp."""
    target_epoch = np.asarray([value.timestamp() for value in target], dtype=float)
    result = []
    for value in source:
        epoch = value.timestamp()
        index = int(np.searchsorted(target_epoch, epoch))
        candidates = []
        if index < len(target_epoch):
            candidates.append(index)
        if index:
            candidates.append(index - 1)
        result.append(min(candidates, key=lambda item: abs(target_epoch[item] - epoch)))
    return result


def mutual_nearest_phase(left, right, tolerance_ms=125.0):
    """Return one-to-one, reciprocal nearest-neighbour ``right-left`` pairs.

    Reciprocal matching prevents the old symmetric implementation from counting
    one timestamp several times and reporting high apparent coverage after most
    frames have actually fallen outside the tolerance.
    """
    left_epoch = np.asarray([value.timestamp() for value in left], dtype=float)
    right_epoch = np.asarray([value.timestamp() for value in right], dtype=float)
    left_to_right = _nearest_indices(left, right)
    right_to_left = _nearest_indices(right, left)
    pairs = []
    for left_index, right_index in enumerate(left_to_right):
        if right_to_left[right_index] != left_index:
            continue
        residual_ms = (right_epoch[right_index] - left_epoch[left_index]) * 1000.0
        if abs(residual_ms) <= tolerance_ms:
            pairs.append((left_index, right_index, residual_ms))
    return pairs


def pair_window_metrics(left, right):
    pairs = mutual_nearest_phase(left, right)
    residuals = np.asarray([item[2] for item in pairs], dtype=float)
    if not len(residuals):
        raise DriftAuditError("camera pair has no phase matches")
    absolute = np.abs(residuals)
    left_fraction = len(pairs) / len(left)
    right_fraction = len(pairs) / len(right)
    identical_grid = len(left) == len(right) and all(
        left_value == right_value for left_value, right_value in zip(left, right)
    )
    shared_zero_residual_grid = (
        min(left_fraction, right_fraction) >= MINIMUM_MATCHED_FRACTION
        and bool(len(residuals))
        and bool(np.all(np.abs(residuals) <= 1e-6))
    )
    return {
        "matched_sample_count": int(len(residuals)),
        "left_sample_count": len(left),
        "right_sample_count": len(right),
        "left_matched_fraction": left_fraction,
        "right_matched_fraction": right_fraction,
        "minimum_matched_fraction": min(left_fraction, right_fraction),
        "identical_timestamp_grid": identical_grid,
        "shared_zero_residual_timestamp_grid": shared_zero_residual_grid,
        "signed_median_phase_ms": float(np.median(residuals)),
        "p95_absolute_phase_ms": float(np.quantile(absolute, 0.95)),
        "max_absolute_phase_ms": float(absolute.max()),
    }


def evaluate(paths):
    reports = sorted((load_report(path) for path in paths), key=lambda item: item["midpoint"])
    if len({item["sha256"] for item in reports}) != len(reports):
        raise DriftAuditError("timestamp audit reports are duplicated")
    for left, right in zip(reports, reports[1:]):
        if left["end"] >= right["start"]:
            raise DriftAuditError("timestamp audit windows overlap")

    span_hours = (
        (reports[-1]["midpoint"] - reports[0]["midpoint"]).total_seconds() / 3600.0
        if len(reports) > 1 else 0.0
    )
    origin = reports[0]["midpoint"]
    hours = np.asarray(
        [(item["midpoint"] - origin).total_seconds() / 3600.0 for item in reports],
        dtype=float,
    )
    pairs = {}
    for index, left_camera in enumerate(CAMERAS):
        for right_camera in CAMERAS[index + 1:]:
            windows = []
            for report in reports:
                metrics = pair_window_metrics(
                    report["timestamps"][left_camera], report["timestamps"][right_camera]
                )
                windows.append({
                    "report_sha256": report["sha256"],
                    "midpoint_utc": report["midpoint"].isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    **metrics,
                })
            phases = np.asarray([item["signed_median_phase_ms"] for item in windows])
            drift = float(np.polyfit(hours, phases, 1)[0]) if len(hours) >= 2 else math.inf
            phase_gate = all(
                item["p95_absolute_phase_ms"] <= MAXIMUM_P95_PHASE_MS
                and item["max_absolute_phase_ms"] <= MAXIMUM_PHASE_MS
                for item in windows
            )
            matching_gate = all(
                item["minimum_matched_fraction"] >= MINIMUM_MATCHED_FRACTION
                for item in windows
            )
            independent_grid_gate = all(
                not item["shared_zero_residual_timestamp_grid"] for item in windows
            )
            pairs[f"{left_camera}-{right_camera}"] = {
                "windows": windows,
                "absolute_drift_ms_per_hour": abs(drift),
                "phase_gate_passed": phase_gate,
                "matching_coverage_gate_passed": matching_gate,
                "timestamp_grid_independence_gate_passed": independent_grid_gate,
                "drift_gate_passed": abs(drift) <= MAXIMUM_ABSOLUTE_DRIFT_MS_PER_HOUR,
            }

    coverage_gate = len(reports) >= MINIMUM_WINDOWS and span_hours >= MINIMUM_SPAN_HOURS
    diagnostic_passed = coverage_gate and all(
        item["phase_gate_passed"]
        and item["matching_coverage_gate_passed"]
        and item["timestamp_grid_independence_gate_passed"]
        and item["drift_gate_passed"]
        for item in pairs.values()
    )
    return {
        "schema": OUTPUT_SCHEMA,
        "acceptance_eligible": False,
        "inputs": [
            {"path": item["path"], "sha256": item["sha256"]} for item in reports
        ],
        "window_count": len(reports),
        "span_hours": span_hours,
        "pairs": pairs,
        "gates": {
            "minimum_windows": MINIMUM_WINDOWS,
            "minimum_span_hours": MINIMUM_SPAN_HOURS,
            "maximum_p95_phase_ms": MAXIMUM_P95_PHASE_MS,
            "maximum_phase_ms": MAXIMUM_PHASE_MS,
            "maximum_absolute_drift_ms_per_hour": MAXIMUM_ABSOLUTE_DRIFT_MS_PER_HOUR,
            "minimum_matched_fraction": MINIMUM_MATCHED_FRACTION,
            "identical_timestamp_grids_are_rejected": True,
            "shared_zero_residual_timestamp_grids_are_rejected": True,
            "coverage_gate_passed": coverage_gate,
            "producer_timestamp_drift_diagnostic_passed": diagnostic_passed,
        },
        "acceptance_failures": [
            "producer_timestamps_do_not_measure_sensor_exposure_instant",
            "producer_timestamps_do_not_prove_absolute_utc_accuracy",
            "independent_timing_target_and_replay_clock_measurement_are_required",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.reports)
    from audit_kvs_intercamera_timestamps import write_json_exclusive
    write_json_exclusive(args.output, result)
    print(json.dumps({
        "output": str(args.output.resolve()),
        "window_count": result["window_count"],
        "span_hours": result["span_hours"],
        "diagnostic_passed": result["gates"]["producer_timestamp_drift_diagnostic_passed"],
        "acceptance_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
