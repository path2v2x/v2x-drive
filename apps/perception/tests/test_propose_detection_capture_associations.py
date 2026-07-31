from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import numpy as np


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "propose_detection_capture_associations.py"
)
SPEC = importlib.util.spec_from_file_location("capture_associations", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def record(event_id, camera_id, seconds, embedding, object_id="vehicle-1"):
    return {
        "event_id": event_id,
        "object_id": object_id,
        "object_type": "car",
        "camera_id": camera_id,
        "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds),
        "embedding": np.asarray(embedding, dtype=float),
    }


def test_build_candidates_excludes_same_camera_and_distant_pairs():
    values = [
        record("a", "ch1", 0, [1, 0]),
        record("b", "ch1", 1, [1, 0]),
        record("c", "ch2", 2, [0.8, 0.6]),
        record("d", "ch3", 200, [1, 0]),
    ]
    result = tool.build_candidates(values, maximum_transit_seconds=90)
    assert len(result) == 2
    assert {tuple(item["event_ids"]) for item in result} == {("a", "c"), ("b", "c")}
    assert all(item["visual_threshold_passed"] for item in result)


def test_build_candidates_can_repair_fragmented_model_ids():
    values = [
        record("a", "ch1", 0, [1, 0], object_id="model-a"),
        record("b", "ch2", 2, [1, 0], object_id="model-b"),
    ]
    result = tool.build_candidates(values, maximum_transit_seconds=10)
    assert len(result) == 1
    assert result[0]["same_model_object_id"] is False
    assert result[0]["source_object_ids"] == ["model-a", "model-b"]


def test_vehicle_crop_clamps_to_frame():
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    crop, bounds = tool.vehicle_crop(image, [-10, 10, 50, 90])
    assert bounds[0] == 0
    assert bounds[2] > 50
    assert crop.shape[0] > 80


def test_exclusive_writer_refuses_overwrite(tmp_path):
    path = tmp_path / "associations.json"
    tool.write_json_exclusive(path, {"first": True})
    try:
        tool.write_json_exclusive(path, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("association evidence was overwritten")
