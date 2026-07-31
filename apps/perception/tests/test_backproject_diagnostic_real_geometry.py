import importlib.util
from pathlib import Path

import pytest


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "backproject_diagnostic_real_geometry.py"
)
SPEC = importlib.util.spec_from_file_location(
    "backproject_diagnostic_real_geometry", TOOL_PATH
)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def inputs():
    frame_hash = "frame"
    annotations = {
        "schema": "v2x-crosswalk-hypothesis-observations/v1",
        "acceptance_eligible": False,
        "camera": "ch4",
        "real_frame_sha256": frame_hash,
        "crosswalks": [{
            "id": "crossing",
            "real_vertices": [[1, 1], [8, 1], [8, 8], [1, 8]],
        }],
    }
    search = {
        "schema": "v2x-signal-hypothesis-search/v1",
        "acceptance_eligible": False,
        "camera": "ch4",
        "real_frame_sha256": frame_hash,
        "results": [{
            "fitted_absolute": [1, 2, 3, -40, 120, 0, 88],
            "optimizer_success": True,
            "boundary_hits": [],
            "identity_underconstrained": False,
        }],
    }
    signal_observations = {
        "schema": "v2x-signal-hypothesis-observations/v1",
        "acceptance_eligible": False,
        "camera": "ch4",
        "real_frame_sha256": frame_hash,
    }
    geometry = {
        "schema": "v2x-map-calibration-geometry/v1",
        "acceptance_eligible": False,
        "cameras": {"ch4": {"real": {
            "frame_sha256": frame_hash,
            "width": 10,
            "height": 10,
        }}},
    }
    return annotations, signal_observations, search, geometry


def test_validate_inputs_accepts_bound_frame_and_polygons():
    annotations, signals, search, geometry = inputs()
    checks = tool.validate_inputs(
        annotations, signals, search, geometry, "ch4", "frame", (10, 10)
    )
    assert all(checks.values())


@pytest.mark.parametrize(
    ("camera", "frame_hash", "frame_size"),
    [("ch3", "frame", (10, 10)), ("ch4", "wrong", (10, 10)), ("ch4", "frame", (9, 10))],
)
def test_validate_inputs_rejects_binding_mismatches(camera, frame_hash, frame_size):
    annotations, signals, search, geometry = inputs()
    with pytest.raises(ValueError):
        tool.validate_inputs(
            annotations, signals, search, geometry, camera, frame_hash, frame_size
        )


def test_validate_inputs_rejects_out_of_frame_polygon():
    annotations, signals, search, geometry = inputs()
    annotations["crosswalks"][0]["real_vertices"][0] = [-1, 1]
    with pytest.raises(ValueError, match="outside"):
        tool.validate_inputs(
            annotations, signals, search, geometry, "ch4", "frame", (10, 10)
        )


def test_candidate_params_is_bounded_and_finite():
    _, _, search, _ = inputs()
    assert tool.candidate_params(search, 1).tolist()[-1] == 88
    search["results"][0]["fitted_absolute"][-1] = 170
    with pytest.raises(ValueError, match="FOV"):
        tool.candidate_params(search, 1)


def test_carla_to_opendrive_flips_y_only():
    assert tool.carla_to_opendrive([1.5, -2.25, 7.0]) == [1.5, 2.25]


def test_validate_inputs_rejects_degenerate_and_self_intersecting_polygons():
    annotations, signals, search, geometry = inputs()
    annotations["crosswalks"][0]["real_vertices"] = [[1, 1], [2, 2], [3, 3]]
    with pytest.raises(ValueError, match="degenerate"):
        tool.validate_inputs(
            annotations, signals, search, geometry, "ch4", "frame", (10, 10)
        )
    annotations["crosswalks"][0]["real_vertices"] = [[1, 1], [8, 8], [1, 8], [8, 1]]
    with pytest.raises(ValueError, match="self-intersecting"):
        tool.validate_inputs(
            annotations, signals, search, geometry, "ch4", "frame", (10, 10)
        )


def test_candidate_params_rejects_failed_or_boundary_candidate():
    _, _, search, _ = inputs()
    search["results"][0]["optimizer_success"] = False
    with pytest.raises(ValueError, match="succeed"):
        tool.candidate_params(search, 1)
    search["results"][0]["optimizer_success"] = True
    search["results"][0]["boundary_hits"] = ["fov"]
    with pytest.raises(ValueError, match="boundary"):
        tool.candidate_params(search, 1)


def test_ray_intersection_at_elevation_uses_bound_ground_plane():
    point = tool.ray_intersection_at_elevation(
        [0, 0, 10, -45, 0, 0, 90], [5, 5], (10, 10), 0
    )
    assert point[0] == pytest.approx(10.0)
    assert point[1] == pytest.approx(0.0)
    assert point[2] == pytest.approx(0.0)


def test_exclusive_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "report.json"
    tool.write_json_exclusive(output, {"first": True})
    with pytest.raises(FileExistsError):
        tool.write_json_exclusive(output, {"second": True})
