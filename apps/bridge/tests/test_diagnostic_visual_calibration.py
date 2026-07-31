"""Unit tests for proposal-only visual self-calibration geometry."""

import sys
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import collect_twin_roadline_cloud as cloud_tool  # noqa: E402
from collect_twin_roadline_cloud import (  # noqa: E402
    decode_depth,
    rgb_road_marking_mask,
)
from fit_diagnostic_visual_calibration import (  # noqa: E402
    CAMERA_FREE_PARAMETERS,
    LOWER_DELTAS,
    UPPER_DELTAS,
    candidate_twin_pose,
    line_residual_values,
    nearest_world_points,
    project,
)


def test_diagnostic_bounds_cover_observed_rotation_and_fov_failures():
    assert LOWER_DELTAS[3] == -45.0 and UPPER_DELTAS[3] == 45.0
    assert LOWER_DELTAS[5] == -25.0 and UPPER_DELTAS[5] == 25.0
    assert LOWER_DELTAS[6] == -35.0 and UPPER_DELTAS[6] == 35.0
    assert all(tuple(indices) == (3, 4, 5, 6) for indices in CAMERA_FREE_PARAMETERS.values())


def test_decode_depth_uses_carla_bgra_order():
    normalized = 0.125
    packed = int(round(normalized * 16777215.0))
    red = packed & 0xFF
    green = (packed >> 8) & 0xFF
    blue = (packed >> 16) & 0xFF
    depth = decode_depth(bytes((blue, green, red, 255)), 1, 1)
    assert depth[0, 0] == pytest.approx(125.0, abs=0.001)


def test_rgb_fallback_is_conservative_and_hashable(tmp_path):
    pixels = np.asarray([[
        [240, 240, 240],  # white paint
        [170, 165, 160],  # pavement
        [180, 145, 60],   # yellow paint
        [80, 160, 70],    # vegetation
    ]], dtype=np.uint8)
    path = tmp_path / "frame.png"
    Image.fromarray(pixels).save(path)
    assert rgb_road_marking_mask(path, 4, 1).tolist() == [[True, False, True, False]]


def test_projection_uses_carla_forward_right_up_axes():
    params = np.asarray([0, 0, 0, 0, 0, 0, 90], dtype=float)
    points = np.asarray([[10, 0, 0], [10, 5, 0], [10, 0, 5]], dtype=float)
    pixels, depth = project(points, params, 640, 480)
    assert depth.tolist() == [10, 10, 10]
    assert pixels[0] == pytest.approx([320, 240])
    assert pixels[1] == pytest.approx([480, 240])
    assert pixels[2] == pytest.approx([320, 80])


def test_nearest_depth_grid_lookup_returns_bound_world_point():
    cloud = {
        "grid_uv": np.asarray([[0, 0], [2, 0], [0, 2], [2, 2]], dtype=float),
        "grid_world_xyz": np.asarray([[1, 0, 0], [2, 0, 0], [3, 0, 0], [4, 0, 0]], dtype=float),
    }
    world, distance = nearest_world_points(cloud, np.asarray([[1.8, 2.1]]))
    assert world.tolist() == [[4, 0, 0]]
    assert distance[0] == pytest.approx((0.2**2 + 0.1**2) ** 0.5)


def test_candidate_pose_roundtrips_unchanged_baseline():
    camera = {"twin_pose": {
        "forward_offset_m": 0.5,
        "right_offset_m": 0.2,
        "height_offset_m": -0.4,
        "pitch_offset_deg": 1.0,
        "yaw_offset_deg": -2.0,
        "roll_offset_deg": 0.5,
        "fov_offset_deg": 3.0,
    }}
    baseline = np.asarray([10, 20, 7, -30, 45, 0.5, 91], dtype=float)
    candidate = candidate_twin_pose(camera, baseline, baseline)
    for key, expected in camera["twin_pose"].items():
        assert candidate[key] == pytest.approx(expected)


def test_bounded_line_penalizes_along_segment_displacement():
    params = np.asarray([0, 0, 0, 0, 0, 0, 90], dtype=float)
    group = {
        "annotation": {"id": "edge", "split": "fit", "bounded": True},
        "world_xyz": np.asarray([[10, 20, 0]], dtype=float),
        "real_origin": np.asarray([300, 200], dtype=float),
        "real_end": np.asarray([340, 200], dtype=float),
        "real_normal": np.asarray([0, -1], dtype=float),
    }
    # Projection is (960, 240): only 40 px from the infinite y=200 line, but
    # hundreds of pixels beyond the finite segment endpoint.
    residual = line_residual_values([group], params, 640, 480, "fit")
    assert residual[0] > 600


def test_zero_session_gate_fails_closed(monkeypatch):
    def fake_run(coroutine):
        coroutine.close()
        return {"active_sessions": 1}

    monkeypatch.setattr(cloud_tool.asyncio, "run", fake_run)
    with pytest.raises(RuntimeError, match="active_sessions=1"):
        cloud_tool.verify_zero_active_sessions("ws://example.invalid")
