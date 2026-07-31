"""Pure geometry tests for global calibration landmark construction."""

import math
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from build_twin_camera_landmarks import encoded_depth_meters  # noqa: E402


def test_encoded_depth_uses_carla_bgra_channel_order():
    normalized = 0.125
    packed = int(round(normalized * 16777215.0))
    red = packed & 0xFF
    green = (packed >> 8) & 0xFF
    blue = (packed >> 16) & 0xFF
    raw = bytes((blue, green, red, 255))
    assert encoded_depth_meters(raw, 1, 0, 0) == pytest.approx(125.0, abs=0.001)


def test_encoded_depth_rejects_pixels_outside_buffer():
    with pytest.raises(ValueError):
        encoded_depth_meters(bytes(4), 1, 1, 0)
