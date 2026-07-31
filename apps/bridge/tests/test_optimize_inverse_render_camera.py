import importlib.util
from types import SimpleNamespace
from pathlib import Path
import sys

import numpy as np


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "optimize_inverse_render_camera.py"
)
SPEC = importlib.util.spec_from_file_location("optimize_inverse_render_camera", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def synthetic_scene(shift=0, distractor=False):
    import cv2

    rgb = np.zeros((240, 320, 3), dtype=np.uint8)
    rgb[:] = (58, 61, 60)
    cv2.line(rgb, (25 + shift, 210), (150 + shift, 25), (245, 245, 240), 5)
    cv2.line(rgb, (270 + shift, 210), (180 + shift, 25), (245, 245, 240), 5)
    cv2.line(rgb, (140 + shift, 210), (165 + shift, 25), (230, 185, 35), 4)
    for y in range(120, 181, 12):
        cv2.line(rgb, (80 + shift, y), (235 + shift, y - 20), (245, 245, 240), 5)
    if distractor:
        rgb[0:40, 0:90] = (250, 250, 250)
    return rgb


def test_roi_validation_and_rasterization():
    document = {
        "schema": MODULE.ROI_SCHEMA,
        "acceptance_eligible": False,
        "coordinate_space": "normalized_image_xy",
        "cameras": {
            "ch1": {
                "polygons": [[[0, 0.2], [1, 0.2], [1, 1], [0, 1]]]
                ,"target_polylines": [
                    [[0, 0.2], [1, 0.2]],
                    [[0, 0.5], [1, 0.5]],
                    [[0, 0.8], [1, 0.8]]
                ]
            }
        },
    }
    polygons = MODULE.validate_rois(document, "ch1")
    mask = MODULE.rasterize_rois(polygons, 320, 240)
    assert not mask[0, 0]
    assert mask[-1, -1]
    lines = MODULE.validate_target_polylines(document, "ch1")
    line_mask = MODULE.rasterize_target_polylines(lines, 320, 240)
    assert line_mask.sum() > 500


def test_paint_extraction_rejects_bright_distractor_outside_roi():
    rgb = synthetic_scene(distractor=True)
    roi = np.zeros((240, 320), dtype=bool)
    roi[45:, :] = True
    masks = MODULE.extract_road_paint_masks(rgb, roi)
    assert masks["linear"].sum() > 50
    assert masks["white"].sum() > 50
    assert masks["yellow"].sum() > 100
    assert not masks["white"][:40].any()


def test_symmetric_score_prefers_aligned_geometry():
    roi = np.ones((240, 320), dtype=bool)
    target = MODULE.extract_road_paint_masks(synthetic_scene(), roi)
    aligned = MODULE.extract_road_paint_masks(synthetic_scene(), roi)
    shifted = MODULE.extract_road_paint_masks(synthetic_scene(shift=18), roi)
    aligned_score = MODULE.score_masks(target, aligned)
    shifted_score = MODULE.score_masks(target, shifted)
    assert aligned_score["objective"] == 0.0
    assert shifted_score["objective"] > aligned_score["objective"] + 5.0
    assert shifted_score["classes"]["white"]["p95_px"] > 4.0


def test_yellow_proposal_rejects_non_linear_grass_blob():
    import cv2

    rgb = synthetic_scene()
    cv2.circle(rgb, (280, 70), 24, (175, 145, 70), -1)
    roi = np.ones((240, 320), dtype=bool)
    masks = MODULE.extract_road_paint_masks(rgb, roi)
    assert masks["yellow"][:, 130:190].sum() > 100
    assert masks["yellow"][45:95, 255:305].sum() == 0


def test_candidate_manual_geometry_uses_paint_not_unrelated_edges():
    roi = np.ones((240, 320), dtype=bool)
    masks = MODULE.extract_candidate_road_paint_masks(synthetic_scene(), roi)
    manual = MODULE.candidate_manual_geometry(masks)
    assert manual.sum() > 100
    unrelated = masks["linear"] & ~(masks["white"] | masks["yellow"])
    assert not (manual & unrelated).all()


def test_road_surface_fraction_penalizes_non_road_roi():
    roi = np.ones((240, 320), dtype=bool)
    road = synthetic_scene()
    masks = MODULE.extract_candidate_road_paint_masks(road, roi)
    full_fraction, _ = MODULE.candidate_road_surface_fraction(road, roi, masks)
    obstructed = road.copy()
    obstructed[:, :160] = (80, 160, 80)
    obstructed_masks = MODULE.extract_candidate_road_paint_masks(obstructed, roi)
    obstructed_fraction, _ = MODULE.candidate_road_surface_fraction(
        obstructed, roi, obstructed_masks
    )
    assert full_fraction > 0.9
    assert obstructed_fraction < full_fraction - 0.3


def test_near_occlusion_fraction_counts_only_valid_near_depth():
    depth = np.full((20, 20), 12.0, dtype=np.float32)
    depth[:4, :] = 2.0
    depth[4, :10] = np.nan
    assert MODULE.near_occlusion_fraction(depth) == 0.2


def test_initial_candidates_preserve_forward_only_in_zero_correction_seed():
    initial = np.asarray((0.5, 1.0, -0.4, 8.0, -3.0, 2.0, 4.0))
    current, zero = MODULE.initial_candidates(initial)
    assert np.array_equal(current, initial)
    assert zero[0] == 0.5
    assert np.count_nonzero(zero[1:]) == 0


def test_low_discrepancy_search_is_deterministic_and_bounded():
    lower = np.full(len(MODULE.POSE_KEYS), -1.0)
    upper = np.full(len(MODULE.POSE_KEYS), 2.0)
    left = MODULE.low_discrepancy_candidates(lower, upper, 11, 7)
    right = MODULE.low_discrepancy_candidates(lower, upper, 11, 7)
    assert len(left) == 11
    assert all(np.all(row >= lower) and np.all(row <= upper) for row in left)
    assert all(np.array_equal(a, b) for a, b in zip(left, right))


def test_axis_sweep_probes_each_parameter_without_cross_axis_drift():
    reference = np.arange(len(MODULE.POSE_KEYS), dtype=float)
    ranges = np.ones(len(MODULE.POSE_KEYS), dtype=float) * 2.0
    candidates = MODULE.axis_sweep_candidates(reference, ranges)
    assert len(candidates) == len(MODULE.POSE_KEYS) * 4
    for candidate in candidates:
        changed = np.flatnonzero(candidate != reference)
        assert len(changed) == 1
        assert abs(candidate[changed[0]] - reference[changed[0]]) in {1.0, 2.0}


def test_explicit_search_vectors_are_bound_by_pose_axis():
    values = {
        name: float(index + 1) / 10.0
        for index, name in enumerate(MODULE.RANGE_ARGUMENTS)
    }
    values.update(
        {
            name: float(index + 1) / 100.0
            for index, name in enumerate(MODULE.STEP_ARGUMENTS)
        }
    )
    ranges, steps = MODULE.search_vectors(SimpleNamespace(**values))
    assert np.allclose(ranges, np.arange(1, 8) / 10.0)
    assert np.allclose(steps, np.arange(1, 8) / 100.0)


def test_search_vectors_reject_nonfinite_and_unsafe_values():
    values = {
        name: float(default)
        for name, default in zip(MODULE.RANGE_ARGUMENTS, MODULE.DEFAULT_HALF_RANGES)
    }
    values.update(
        {
            name: float(default)
            for name, default in zip(MODULE.STEP_ARGUMENTS, MODULE.DEFAULT_STEPS)
        }
    )
    invalid = dict(values, pitch_half_range_deg=46.0)
    try:
        MODULE.search_vectors(SimpleNamespace(**invalid))
    except ValueError as exc:
        assert "angular half-range" in str(exc)
    else:
        raise AssertionError("unsafe angular search must fail")

    invalid = dict(values, yaw_half_range_deg=181.0)
    try:
        MODULE.search_vectors(SimpleNamespace(**invalid))
    except ValueError as exc:
        assert "yaw half-range" in str(exc)
    else:
        raise AssertionError("unsafe yaw search must fail")

    invalid = dict(values, forward_step_m=float("nan"))
    try:
        MODULE.search_vectors(SimpleNamespace(**invalid))
    except ValueError as exc:
        assert "finite and positive" in str(exc)
    else:
        raise AssertionError("non-finite search step must fail")
