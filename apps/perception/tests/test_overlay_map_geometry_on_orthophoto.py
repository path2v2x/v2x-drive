import importlib.util
from pathlib import Path

import numpy as np
import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "overlay_map_geometry_on_orthophoto.py"
)
SPEC = importlib.util.spec_from_file_location("overlay_map_geometry", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_carla_projection_preserves_explicit_handedness():
    projection = module.TransverseMercator.from_proj_string(
        "+proj=tmerc +lat_0=37.9150891287087 +lon_0=-122.333308830857 "
        "+k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    )
    pixel, utm, geodetic = module.carla_xy_to_orthophoto_pixel(
        -130.02947035,
        -56.83502878,
        projection,
        [558300.0, 4196500.0, 558650.0, 4196850.0],
        1.0,
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    )
    assert pixel == pytest.approx([171.0706, 190.5540], abs=0.002)
    assert utm == pytest.approx([558471.0706, 4196659.4460], abs=0.002)
    assert geodetic == pytest.approx([37.91560117, -122.33478756], abs=1e-8)


def test_registration_matrix_is_applied_after_metric_grid_projection():
    projection = module.TransverseMercator.from_proj_string(
        "+proj=tmerc +lat_0=0 +lon_0=-123 +k=.9996 +x_0=500000 "
        "+y_0=0 +datum=WGS84 +units=m +no_defs"
    )
    pixel, _utm, _geodetic = module.carla_xy_to_orthophoto_pixel(
        500100.0,
        -200.0,
        projection,
        [500000.0, 0.0, 501000.0, 1000.0],
        1.0,
        [[1.0, 0.0, 3.0], [0.0, 1.0, -4.0]],
    )
    assert pixel == pytest.approx([103.0, 796.0], abs=0.01)


def test_draw_polyline_handles_valid_points():
    image = np.zeros((20, 20, 3), dtype=np.uint8)
    module.draw_polyline(image, [[1, 1], [18, 18]], (255, 0, 0), 1)
    assert np.count_nonzero(image) > 0
