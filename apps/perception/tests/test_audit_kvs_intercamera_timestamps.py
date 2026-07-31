from datetime import datetime, timedelta, timezone
import importlib.util
import math
from pathlib import Path


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "audit_kvs_intercamera_timestamps.py"
)
SPEC = importlib.util.spec_from_file_location("kvs_timestamp_audit", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_nearest_pair_deltas_recovers_fixed_phase():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    left = [start + timedelta(milliseconds=200 * index) for index in range(5)]
    right = [value + timedelta(milliseconds=12) for value in left]
    values = tool.nearest_pair_deltas(left, right)
    assert len(values) == 5
    assert all(abs(value - 12.0) < 0.001 for value in values)


def test_pair_metrics_rejects_large_phase():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    left = [start + timedelta(milliseconds=200 * index) for index in range(5)]
    right = [value + timedelta(milliseconds=80) for value in left]
    result = tool.pair_metrics(left, right)
    assert math.isclose(result["median_absolute_delta_ms"], 80.0, abs_tol=0.001)
    assert result["phase_gate_passed"] is False


def test_pair_metrics_separates_missing_sample_from_phase():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    full = [start + timedelta(milliseconds=200 * index) for index in range(10)]
    missing_one = full[:4] + full[5:]
    result = tool.pair_metrics(missing_one, full, 200)
    assert result["unmatched_sample_count"] == 1
    assert result["median_absolute_delta_ms"] == 0.0
    assert result["phase_gate_passed"] is True


def test_usable_timestamps_rejects_duplicates():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    images = [{"TimeStamp": start}, {"TimeStamp": start}]
    try:
        tool.usable_timestamps(images, start, start + timedelta(seconds=1))
    except tool.TimestampAuditError as error:
        assert "duplicate" in str(error)
    else:
        raise AssertionError("duplicate producer timestamps were accepted")


def test_exclusive_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "audit.json"
    tool.write_json_exclusive(output, {"first": True})
    try:
        tool.write_json_exclusive(output, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("timestamp audit was overwritten")
