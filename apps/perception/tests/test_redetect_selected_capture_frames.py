import importlib.util
from pathlib import Path


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "redetect_selected_capture_frames.py"
)
SPEC = importlib.util.spec_from_file_location("selected_frame_redetection", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_bbox_iou_has_expected_identity_and_disjoint_values():
    assert tool.bbox_iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert tool.bbox_iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0


def test_choose_event_match_is_independent_of_source_object_id():
    detections = [
        {"label": "car", "bbox_xyxy": [100, 100, 200, 200]},
        {"label": "truck", "bbox_xyxy": [12, 12, 48, 48]},
        {"label": "person", "bbox_xyxy": [10, 10, 50, 50]},
    ]
    result = tool.choose_event_match(detections, [10, 10, 50, 50], 400, 300)
    assert result["detection_index"] == 1
    assert result["uses_event_bbox_as_geometry"] is False


def test_boundary_detection_is_strict():
    assert tool.touches_boundary([0, 2, 20, 20], 100, 100) is True
    assert tool.touches_boundary([1, 2, 20, 99], 100, 100) is False


def test_exclusive_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "redetection.json"
    tool.write_json_exclusive(output, {"first": True})
    try:
        tool.write_json_exclusive(output, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("redetection evidence was overwritten")


def test_review_sheet_has_deterministic_grid_dimensions():
    import cv2
    import numpy as np

    image = np.zeros((100, 200, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    sheet = tool.make_review_sheet([("first", encoded.tobytes())], columns=2)
    decoded = cv2.imdecode(np.frombuffer(sheet, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape[:2] == (390, 960)
