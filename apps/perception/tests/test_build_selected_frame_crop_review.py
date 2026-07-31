import importlib.util
from pathlib import Path


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "build_selected_frame_crop_review.py"
)
SPEC = importlib.util.spec_from_file_location("selected_crop_review", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_exclusive_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "review.json"
    tool.write_json_exclusive(output, {"first": True})
    try:
        tool.write_json_exclusive(output, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("crop review was overwritten")


def test_build_rejects_boundary_crop(tmp_path):
    redetection = {
        "schema": "v2x-selected-frame-redetection/v1",
        "capture_report": {"sha256": "capture"},
        "review_sheet": {"path": "sheet", "sha256": "sheet"},
        "events": [{
            "event_id": "event",
            "camera_id": "ch1",
            "selected_frame_timestamp_utc": "2026-01-01T00:00:00.000Z",
            "frame": {"encoded_jpeg_sha256": "frame"},
            "detections": [{
                "bbox_xyxy": [0, 1, 2, 3],
                "label": "car",
                "confidence": 0.9,
                "touches_frame_boundary": True,
            }],
        }],
    }
    redetection_path = tmp_path / "redetection.json"
    redetection_path.write_text(__import__("json").dumps(redetection))
    decisions = {
        "schema": "v2x-selected-frame-crop-decisions/v1",
        "redetection_report_sha256": tool.sha256_bytes(redetection_path.read_bytes()),
        "reviewer_kind": "codex_visual_review",
        "decisions": [{
            "event_id": "event",
            "detection_index": 0,
            "vehicle_fully_visible": True,
        }],
    }
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(__import__("json").dumps(decisions))
    try:
        tool.build(redetection_path, decisions_path)
    except tool.CropReviewError as error:
        assert "boundary" in str(error)
    else:
        raise AssertionError("boundary crop was accepted")
