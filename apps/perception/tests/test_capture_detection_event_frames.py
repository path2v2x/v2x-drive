from datetime import datetime, timezone
import base64
import importlib.util
import json
from pathlib import Path


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "capture_detection_event_frames.py"
)
SPEC = importlib.util.spec_from_file_location("capture_detection_frames", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_choose_nearest_image_ignores_errors_and_empty_content():
    target = datetime(2026, 1, 1, tzinfo=timezone.utc)
    images = [
        {"TimeStamp": target, "ImageContent": b"", "Error": None},
        {"TimeStamp": target, "ImageContent": b"bad", "Error": "failed"},
        {
            "TimeStamp": target.replace(microsecond=100_000),
            "ImageContent": b"later",
        },
        {
            "TimeStamp": target.replace(microsecond=50_000),
            "ImageContent": base64.b64encode(b"closest").decode(),
        },
    ]
    offset, timestamp, content = tool.choose_nearest_image(images, target)
    assert offset == 0.05
    assert timestamp.microsecond == 50_000
    assert content == b"closest"


def test_select_events_requires_vehicles_and_exact_ids():
    rows = [{
        "event_id": "event-1",
        "object_id": "global_car_1",
        "object_type": "car",
        "device_id": "cam-001-ch2",
        "media_timestamp_utc": "2026-01-01T00:00:00.000Z",
    }]
    assert tool.select_events(rows, ["event-1"], [])[0]["device_id"].endswith("ch2")
    try:
        tool.select_events(rows, ["missing"], [])
    except tool.CaptureError as error:
        assert "absent" in str(error)
    else:
        raise AssertionError("missing event was accepted")


def test_snapshot_hash_mismatch_fails_closed(tmp_path):
    detections = b'{"event_id":"one"}\n'
    (tmp_path / "detections.ndjson").write_bytes(detections)
    (tmp_path / "manifest.json").write_text(json.dumps({
        "schema": "v2x-detection-corpus-snapshot/v1",
        "artifacts": {"detections.ndjson": "0" * 64},
        "counts": {"items": 1},
    }))
    try:
        tool.load_snapshot(tmp_path)
    except tool.CaptureError as error:
        assert "hash does not match" in str(error)
    else:
        raise AssertionError("tampered snapshot was accepted")


def test_exclusive_writer_refuses_overwrite(tmp_path):
    path = tmp_path / "evidence.json"
    tool.write_json_exclusive(path, {"first": True})
    try:
        tool.write_json_exclusive(path, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("immutable evidence was overwritten")


def test_bbox_diagnostics_rejects_boundary_and_outside_contacts():
    assert tool.bbox_diagnostics([1, 2, 99, 98], 100, 100) == {
        "within_frame": True,
        "touches_frame_boundary": False,
        "untruncated_contact_candidate": True,
    }
    boundary = tool.bbox_diagnostics([0, 2, 100, 98], 100, 100)
    assert boundary["touches_frame_boundary"] is True
    assert boundary["untruncated_contact_candidate"] is False
    outside = tool.bbox_diagnostics([-1, 2, 99, 98], 100, 100)
    assert outside["within_frame"] is False
    assert outside["untruncated_contact_candidate"] is False
