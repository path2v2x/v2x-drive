from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path

import pytest


TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "evaluate_kvs_timestamp_drift.py"
SPEC = importlib.util.spec_from_file_location("kvs_timestamp_drift", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def canonical(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def report(tmp_path, index, *, phase_ms=0.0, hours=0, identical_grid=False):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=hours)
    timestamps = {}
    for camera_index, camera in enumerate(tool.CAMERAS):
        phase = phase_ms if camera_index == 1 else 0.0
        # A producer that emits exactly the same timestamp grid for every
        # camera may be stamping at shared ingest rather than exposure. Normal
        # passing fixtures therefore retain tiny, deterministic camera jitter.
        jitter = 0.0 if identical_grid else camera_index * 0.8
        timestamps[camera] = [
            canonical(
                start
                + timedelta(
                    milliseconds=200 * sample + phase + jitter + (sample % 3) * camera_index * 0.1
                )
            )
            for sample in range(25)
        ]
    value = {
        "schema": tool.INPUT_SCHEMA,
        "acceptance_eligible": False,
        "window": {
            "start_utc": canonical(start),
            "end_utc": canonical(start + timedelta(seconds=6)),
        },
        "timestamps": timestamps,
    }
    path = tmp_path / f"report-{index}.json"
    path.write_text(json.dumps(value))
    return path


def test_drift_passes_four_well_spaced_aligned_windows(tmp_path):
    paths = [report(tmp_path, i, hours=i * 4) for i in range(4)]
    result = tool.evaluate(paths)
    assert result["gates"]["coverage_gate_passed"] is True
    assert result["gates"]["producer_timestamp_drift_diagnostic_passed"] is True
    assert result["pairs"]["ch1-ch2"]["absolute_drift_ms_per_hour"] == pytest.approx(0)
    assert result["acceptance_eligible"] is False


def test_rejects_identical_ingest_timestamp_grids(tmp_path):
    paths = [report(tmp_path, i, hours=i * 4, identical_grid=True) for i in range(4)]
    result = tool.evaluate(paths)
    pair = result["pairs"]["ch1-ch2"]
    assert pair["phase_gate_passed"] is True
    assert pair["timestamp_grid_independence_gate_passed"] is False
    assert result["gates"]["producer_timestamp_drift_diagnostic_passed"] is False


def test_injected_offset_is_recovered_and_sparse_matches_fail_coverage():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    left = [start + timedelta(milliseconds=200 * sample) for sample in range(20)]
    right = [value + timedelta(milliseconds=60) for value in left]
    metrics = tool.pair_window_metrics(left, right)
    assert metrics["signed_median_phase_ms"] == pytest.approx(60, abs=0.01)
    assert metrics["minimum_matched_fraction"] == 1.0

    sparse_right = right[::4]
    sparse = tool.pair_window_metrics(left, sparse_right)
    assert sparse["minimum_matched_fraction"] < tool.MINIMUM_MATCHED_FRACTION


def test_drift_rejects_phase_growth_and_insufficient_coverage(tmp_path):
    # Keep the synthetic phase below half of the 200 ms sampling period; larger
    # offsets are intentionally ambiguous to a nearest-neighbour phase audit.
    drifting = [report(tmp_path, i, phase_ms=i * 30, hours=i * 2) for i in range(4)]
    result = tool.evaluate(drifting)
    pair = result["pairs"]["ch1-ch2"]
    assert pair["absolute_drift_ms_per_hour"] > 10
    assert pair["drift_gate_passed"] is False
    assert result["gates"]["producer_timestamp_drift_diagnostic_passed"] is False

    short = tool.evaluate(drifting[:2])
    assert short["gates"]["coverage_gate_passed"] is False


def test_rejects_overlapping_or_duplicate_reports(tmp_path):
    first = report(tmp_path, 0)
    second = report(tmp_path, 1, phase_ms=1, hours=0)
    with pytest.raises(tool.DriftAuditError, match="overlap"):
        tool.evaluate([first, second])
    with pytest.raises(tool.DriftAuditError, match="duplicated"):
        tool.evaluate([first, first])
