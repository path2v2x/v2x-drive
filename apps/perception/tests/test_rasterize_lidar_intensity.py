import importlib.util
from pathlib import Path

import numpy as np


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "rasterize_lidar_intensity.py"
)
SPEC = importlib.util.spec_from_file_location("lidar_intensity", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_accumulate_maximum_uses_north_up_rows():
    grid = np.zeros((2, 2), dtype=float)
    counts = np.zeros((2, 2), dtype=np.uint32)
    selected = tool.accumulate_maximum(
        grid, counts,
        np.asarray([0.25, 0.25, 1.25]),
        np.asarray([0.25, 0.25, 1.25]),
        np.asarray([4, 9, 7]),
        [0.0, 0.0, 2.0, 2.0], 1.0,
    )
    assert selected == 3
    assert grid.tolist() == [[0.0, 7.0], [9.0, 0.0]]
    assert counts.tolist() == [[0, 1], [2, 0]]


def test_raster_shape_rejects_unsafe_values():
    assert tool.raster_shape([0.0, 0.0, 2.0, 1.0], 0.5) == (2, 4)
    try:
        tool.raster_shape([0.0, 0.0, 2.0, 1.0], 0.001)
    except tool.RasterError as error:
        assert "between" in str(error)
    else:
        raise AssertionError("unsafe resolution was accepted")


def test_exclusive_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "value.json"
    tool.write_json_exclusive(output, {"first": True})
    try:
        tool.write_json_exclusive(output, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("evidence was overwritten")
