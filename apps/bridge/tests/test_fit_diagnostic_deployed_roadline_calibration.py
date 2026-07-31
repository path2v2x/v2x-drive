import importlib.util
from pathlib import Path

import numpy as np


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "fit_diagnostic_deployed_roadline_calibration.py"
)
SPEC = importlib.util.spec_from_file_location(
    "fit_diagnostic_deployed_roadline_calibration", TOOL_PATH
)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_render_roadline_points_projects_and_dilates():
    points = np.asarray([[10, 0, 0], [10, 1, 0]], dtype=float)
    params = np.asarray([0, 0, 0, 0, 0, 0, 90], dtype=float)
    mask = tool.render_roadline_points(points, None, params, 100, 100)
    assert mask[50, 50] == 1
    assert np.count_nonzero(mask) >= 4


def test_filter_points_to_deployed_lanes_removes_offroad_and_high_points():
    geometry = {"geometry": {"lanes": [{
        "lane_width_m": 4.0,
        "center_world": [[value / 10, 0, 6.5] for value in range(101)],
    }]}}
    points = np.asarray([
        [0, 1, 6.5],
        [0, 8, 6.5],
        [0, 1, 9.0],
    ], dtype=float)
    filtered, report = tool.filter_points_to_deployed_lanes(points, geometry)
    assert filtered.tolist() == [[0.0, 1.0, 6.5]]
    assert report["retained_count"] == 1


def test_validate_bindings_rejects_nondefault_lens(tmp_path):
    paths = {}
    for name in (
        "geometry", "signal_observations", "candidate_search", "pair_manifest",
        "real_frame", "twin_frame",
    ):
        path = tmp_path / name
        path.write_text(name)
        paths[name] = path
    geometry_hash = tool.sha256(paths["geometry"])
    frame_hash = tool.sha256(paths["real_frame"])
    twin_hash = tool.sha256(paths["twin_frame"])
    pair_hash = tool.sha256(paths["pair_manifest"])
    values = {
        "geometry": {
            "schema": "v2x-map-calibration-geometry/v1",
            "acceptance_eligible": False,
            "pair_manifest_sha256": pair_hash,
            "cameras_file_sha256": "config",
            "map": "map",
            "cameras": {"ch4": {
                "camera_config_sha256": "camera",
                "real": {"frame_sha256": frame_hash},
            }},
        },
        "signal_observations": {
            "schema": "v2x-signal-hypothesis-observations/v1",
            "acceptance_eligible": False,
            "camera": "ch4",
            "map_geometry_sha256": geometry_hash,
        },
        "candidate_search": {
            "schema": "v2x-signal-hypothesis-search/v1",
            "acceptance_eligible": False,
            "camera": "ch4",
            "geometry_sha256": geometry_hash,
            "observations_sha256": tool.sha256(paths["signal_observations"]),
            "real_frame_sha256": frame_hash,
            "results": [{
                "optimizer_success": True,
                "boundary_hits": [],
                "identity_underconstrained": False,
            }],
        },
        "pair_manifest": {
            "schema": "v2x-observational-calibration-pairs/v1",
            "cameras": {"ch4": {
                "real": {"sha256": frame_hash},
                "twin": {
                    "sha256": twin_hash,
                    "camera_model": {"lens": {"lens_k": 0.0}},
                },
            }},
        },
    }
    cloud = {
        "schema": "v2x-diagnostic-roadline-cloud/v1",
        "acceptance_eligible": False,
        "camera": "ch4",
        "twin_frame_sha256": twin_hash,
        "pair_manifest_sha256": pair_hash,
        "camera_config_sha256": "camera",
        "cameras_json_sha256": "config",
        "carla_map": "map",
    }
    try:
        tool.validate_bindings(paths, values, cloud, "ch4")
    except ValueError as error:
        assert "default_pinhole_lens" in str(error)
    else:
        raise AssertionError("nondefault lens was accepted")
