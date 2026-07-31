import importlib.util
from pathlib import Path

import numpy as np


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "fit_diagnostic_road_marking_registration.py"
)
SPEC = importlib.util.spec_from_file_location(
    "fit_diagnostic_road_marking_registration", TOOL_PATH
)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_white_paint_mask_selects_low_saturation_bright_pixels():
    image = np.zeros((100, 100, 3), dtype=np.uint8)
    image[50:80, 30:60] = (240, 240, 240)
    image[50:80, 70:90] = (0, 0, 240)
    mask = tool.white_paint_mask(image)
    assert mask[60, 40] == 1
    assert mask[60, 80] == 0


def test_spatial_regions_partition_image_exactly():
    regions = tool.spatial_regions(320, 240)
    total = np.sum(np.stack(list(regions.values())), axis=0)
    assert np.all(total == 1)


def test_paint_metrics_prefers_aligned_masks():
    observed = np.zeros((100, 100), dtype=np.uint8)
    observed[40:60, 20:80] = 1
    aligned = tool.paint_metrics(observed.copy(), observed)
    shifted = np.zeros_like(observed)
    shifted[60:80, 20:80] = 1
    shifted_metrics = tool.paint_metrics(shifted, observed)
    assert aligned["score"] < shifted_metrics["score"]


def test_paint_metrics_region_masks_distance_transform_with_buffer():
    observed = np.zeros((100, 100), dtype=np.uint8)
    rendered = np.zeros_like(observed)
    observed[10:90, 40:44] = 1
    rendered[10:90, 0:5] = 1
    rendered[10:90, 50:55] = 1
    left = np.zeros_like(observed)
    left[:, :50] = 1
    # The tempting rendered pixels across the held-out boundary must not
    # influence distances inside the fit region; only the far-left block can.
    metrics = tool.paint_metrics(rendered, observed, left)
    assert metrics["observed_to_model_untrimmed_mean_px"] == 15.0


def test_render_markings_projects_triangle():
    vertices = np.asarray([[10, -1, -1], [10, 1, -1], [10, 0, 1]], dtype=float)
    triangles = np.asarray([[0, 1, 2]], dtype=np.int64)
    params = np.asarray([0, 0, 0, 0, 0, 0, 90], dtype=float)
    mask = tool.render_markings(vertices, triangles, params, 100, 100)
    assert np.count_nonzero(mask) > 0
    assert mask[50, 50] == 1


def test_signal_metrics_reports_exact_projection():
    world = np.asarray([[10, 0, 0], [10, 1, 0]], dtype=float)
    params = np.asarray([0, 0, 0, 0, 0, 0, 90], dtype=float)
    observed, _ = tool.project(world, params, 100, 100)
    metrics = tool.signal_metrics(world, observed, params, 100, 100)
    assert metrics["valid"] is True
    assert metrics["rmse_px"] == 0.0
