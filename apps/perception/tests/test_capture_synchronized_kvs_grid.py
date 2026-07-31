from datetime import datetime, timezone
import base64
import importlib.util
from pathlib import Path


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "capture_synchronized_kvs_grid.py"
)
SPEC = importlib.util.spec_from_file_location("synchronized_kvs_grid", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_nearest_image_accepts_base64_and_ignores_errors():
    target = datetime(2026, 1, 1, tzinfo=timezone.utc)
    images = [
        {"TimeStamp": target, "ImageContent": b"bad", "Error": "failure"},
        {"TimeStamp": target, "ImageContent": base64.b64encode(b"exact").decode()},
    ]
    offset, selected, content = tool.nearest_image(images, target)
    assert offset == 0.0
    assert selected == target
    assert content == b"exact"


def test_review_sheet_has_four_columns():
    import cv2
    import numpy as np

    image = np.zeros((100, 200, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    row = {"_timestamp": "time"}
    row.update({camera: encoded.tobytes() for camera in tool.CAMERAS})
    sheet = tool.make_review_sheet([row])
    decoded = cv2.imdecode(np.frombuffer(sheet, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape[:2] == (390, 1920)


def test_exclusive_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "grid.json"
    tool.write_json_exclusive(output, {"first": True})
    try:
        tool.write_json_exclusive(output, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("synchronized capture evidence was overwritten")
