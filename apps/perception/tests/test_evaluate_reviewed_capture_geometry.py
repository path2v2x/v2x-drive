import importlib.util
from pathlib import Path

import numpy as np


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "evaluate_reviewed_capture_geometry.py"
)
SPEC = importlib.util.spec_from_file_location("capture_geometry", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_interpolate_bracket_rejects_large_gap():
    records = [
        {"epoch": 0.0, "event_id": "a", "position_enu_m": np.asarray([0.0, 0.0])},
        {"epoch": 1.0, "event_id": "b", "position_enu_m": np.asarray([2.0, 4.0])},
    ]
    result = tool.interpolate_bracket(records, 0.25, 2.0)
    assert result["bracket_event_ids"] == ["a", "b"]
    assert np.allclose(result["position_enu_m"], [0.5, 1.0])
    assert tool.interpolate_bracket(records, 0.25, 0.5) is None


def test_cross_camera_residual_at_common_time():
    records = [
        {
            "event_id": "a0", "camera_id": "ch1", "epoch": 0.0,
            "position_enu_m": np.asarray([0.0, 0.0]),
        },
        {
            "event_id": "a1", "camera_id": "ch1", "epoch": 1.0,
            "position_enu_m": np.asarray([10.0, 0.0]),
        },
        {
            "event_id": "b", "camera_id": "ch2", "epoch": 0.5,
            "position_enu_m": np.asarray([5.5, 0.0]),
        },
    ]
    result = tool.cross_camera_residuals(records, 2.0)
    assert len(result) == 1
    assert result[0]["source_bracket_event_ids"] == ["a0", "a1"]
    assert result[0]["distance_m"] == 0.5
    assert result[0]["strict_diagnostic_passed"] is True


def test_ground_projection_is_finite():
    camera = {
        "height_m": 7.0,
        "pitch_deg": -45.0,
        "yaw_deg": 0.0,
        "heading_deg": 0.0,
        "intrinsics": {
            "fx": 1000.0, "fy": 1000.0, "cx": 1280.0, "cy": 960.0,
        },
    }
    value = tool.ground_intersection(camera, [1280.0, 960.0])
    assert value.shape == (2,)
    assert np.isfinite(value).all()


def test_exclusive_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "geometry.json"
    tool.write_json_exclusive(output, {"first": True})
    try:
        tool.write_json_exclusive(output, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("geometry evidence was overwritten")


def test_camera_origin_offset_uses_per_camera_translation():
    camera = {
        "heading_deg": 90.0,
        "yaw_deg": 0.0,
        "twin_pose": {
            "yaw_offset_deg": 0.0,
            "forward_offset_m": 2.0,
            "right_offset_m": 0.5,
        },
    }
    assert np.allclose(tool.camera_origin_offset_enu(camera), [2.0, -0.5])
