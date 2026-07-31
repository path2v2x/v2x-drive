import sys
from pathlib import Path

import cv2
import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from evaluate_static_inverse_render import (  # noqa: E402
    StaticAlignmentError,
    contour_edges,
    extract_paint_mask,
    robust_edge_metrics,
    validate_polygons,
    validate_thresholds,
)


THRESHOLDS = {
    "white_value_min": 180,
    "white_saturation_max": 60,
    "yellow_hue_min": 15,
    "yellow_hue_max": 40,
    "yellow_saturation_min": 60,
    "yellow_value_min": 120,
    "local_contrast_min": 20,
    "road_value_max": 100,
    "road_saturation_max": 100,
}


def test_thresholded_paint_extraction_respects_region():
    rgb = np.zeros((80, 100, 3), dtype=np.uint8)
    rgb[20:30, 10:90] = 255
    region = np.zeros((80, 100), dtype=np.uint8)
    region[:, :50] = 255
    mask = extract_paint_mask(rgb, THRESHOLDS, region)
    assert np.count_nonzero(mask[:, :50]) > 0
    assert np.count_nonzero(mask[:, 50:]) == 0


def test_robust_edge_metrics_rank_aligned_ahead_of_shifted():
    left = np.zeros((120, 160), dtype=np.uint8)
    cv2.rectangle(left, (30, 40), (120, 70), 255, thickness=-1)
    aligned = contour_edges(left)
    shifted_mask = np.zeros_like(left)
    cv2.rectangle(shifted_mask, (42, 40), (132, 70), 255, thickness=-1)
    shifted = contour_edges(shifted_mask)
    exact_metrics = robust_edge_metrics(aligned, aligned)
    shifted_metrics = robust_edge_metrics(aligned, shifted)
    assert exact_metrics["optimization_loss"] == pytest.approx(0.0)
    assert shifted_metrics["optimization_loss"] > 1.0
    assert shifted_metrics["symmetric_p95_px"] >= 10.0


def test_annotation_inputs_fail_closed():
    with pytest.raises(StaticAlignmentError):
        validate_polygons([[[0, 0], [1, 1]]], 100, 100, "test")
    invalid = dict(THRESHOLDS)
    invalid["yellow_hue_min"] = 50
    with pytest.raises(StaticAlignmentError):
        validate_thresholds(invalid, "test")
