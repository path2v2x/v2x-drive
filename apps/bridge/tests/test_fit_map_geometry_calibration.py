import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fit_map_geometry_calibration import (  # noqa: E402
    canonical_pixels,
    canonical_world_ref,
    delta_bounds,
    point_to_polyline_distances,
    fitted_image_line,
    fold_consistency,
    leave_one_polyline_annotations,
    validate_vanishing_pixel,
    resample_path,
    resolve_polyline,
)

import numpy as np
import pytest


def test_crosswalk_edge_identity_is_direction_independent():
    forward = {
        "kind": "crosswalk_edge", "crosswalk_id": "crosswalk-1",
        "start_vertex": 0, "end_vertex": 3,
    }
    reverse = {**forward, "start_vertex": 3, "end_vertex": 0}
    assert canonical_world_ref(forward) == canonical_world_ref(reverse)


def test_polyline_pixel_identity_is_direction_independent():
    assert canonical_pixels([[1.0, 2.0], [3.0, 4.0]]) == canonical_pixels(
        [[3.0, 4.0], [1.0, 2.0]]
    )


def test_lane_boundary_identity_is_direction_independent():
    forward = {
        "kind": "lane_boundary_segment",
        "lane_id": "road-45-section-0-lane--1",
        "boundary": "right",
        "start_index": 3,
        "end_index": 9,
    }
    reverse = {**forward, "start_index": 9, "end_index": 3}
    assert canonical_world_ref(forward) == canonical_world_ref(reverse)


def test_resolves_lane_boundary_segment_in_requested_direction():
    lanes = {
        "lane": {
            "left_boundary_world": [[0, 0, 0], [1, 0, 0], [2, 0, 0]],
            "center_world": [[0, 1, 0], [1, 1, 0], [2, 1, 0]],
            "right_boundary_world": [[0, 2, 0], [1, 2, 0], [2, 2, 0]],
        }
    }
    value = resolve_polyline(
        {
            "kind": "lane_boundary_segment",
            "lane_id": "lane",
            "boundary": "right",
            "start_index": 2,
            "end_index": 0,
        },
        {},
        lanes,
    )
    assert value.tolist() == [[2, 2, 0], [1, 2, 0], [0, 2, 0]]


def test_resample_path_uses_arc_length_not_vertex_index():
    value = resample_path(np.asarray([[0, 0], [9, 0], [10, 0]], dtype=float), 3)
    assert value.tolist() == [[0, 0], [5, 0], [10, 0]]
    with pytest.raises(ValueError, match="zero-length"):
        resample_path(np.asarray([[0, 0], [0, 0], [1, 0]], dtype=float), 3)


def test_delta_bounds_can_tighten_but_not_expand_safety_envelope():
    lower, upper = delta_bounds({"delta_bounds": {"location_x": [-0.5, 0.5]}})
    assert lower[0] == -0.5
    assert upper[0] == 0.5
    with pytest.raises(ValueError, match="unsafe"):
        delta_bounds({"delta_bounds": {"location_x": [-9.0, 0.5]}})


def test_point_to_polyline_distance_uses_finite_segments():
    values = point_to_polyline_distances(
        np.asarray([[5.0, 2.0], [12.0, 0.0]]),
        np.asarray([[0.0, 0.0], [10.0, 0.0]]),
    )
    assert values.tolist() == pytest.approx([2.0, 2.0])


def test_fitted_image_line_contains_collinear_points():
    points = np.asarray([[0.0, 1.0], [2.0, 3.0], [4.0, 5.0]])
    line = fitted_image_line(points)
    residuals = np.column_stack((points, np.ones(3))) @ line
    assert residuals.tolist() == pytest.approx([0.0, 0.0, 0.0], abs=1e-10)


def test_leave_one_polyline_out_does_not_leak_vanishing_constraint():
    annotation = {
        "polylines": [
            {"id": "left", "split": "fit"},
            {"id": "right", "split": "fit"},
            {"id": "center", "split": "holdout"},
        ],
        "vanishing_points": [
            {"id": "vp", "polyline_ids": ["left", "right"], "split": "fit"}
        ],
    }
    folds = dict(leave_one_polyline_annotations(annotation))
    assert folds["left"]["vanishing_points"][0]["split"] == "holdout"
    assert folds["center"]["vanishing_points"][0]["split"] == "fit"


def test_fold_consistency_fails_pose_spread_without_weakening_thresholds():
    base = {
        "forward_offset_m": 0.0,
        "right_offset_m": 0.0,
        "height_offset_m": 0.0,
        "pitch_offset_deg": 0.0,
        "yaw_offset_deg": 0.0,
        "roll_offset_deg": 0.0,
        "fov_offset_deg": 0.0,
    }
    shifted = {**base, "forward_offset_m": 0.11, "pitch_offset_deg": 0.21}
    result = fold_consistency({
        "one": {"candidate_twin_pose": base},
        "two": {"candidate_twin_pose": shifted},
    })
    assert result["passed"] is False
    assert "unstable_forward_offset_m" in result["reasons"]
    assert "unstable_pitch_offset_deg" in result["reasons"]


def test_vanishing_point_can_be_outside_image_but_not_unbounded():
    validate_vanishing_pixel([-150.0, 8.0], 640, 480, "vp")
    with pytest.raises(ValueError, match="vanishing point"):
        validate_vanishing_pixel([-7000.0, 8.0], 640, 480, "vp")
