import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest

from digital_twin_bridge.camera_projection import (
    ground_horizon_line,
    production_round_trip,
    project_direction,
    project_world,
    rotation_matrix,
)
from tools import fit_tier_b_static_development as fitter


def binding(path: Path, payload: bytes) -> dict:
    path.write_bytes(payload)
    return {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}


def synthetic_document(tmp_path: Path):
    opendrive = binding(tmp_path / "map.xodr", b"<OpenDRIVE/>")
    topology = binding(tmp_path / "topology.json", b'{"roads":222,"junctions":29}')
    cameras_path = tmp_path / "cameras.json"
    cameras_json = binding(cameras_path, b'{"cameras":[]}')
    document = {
        "schema": fitter.SCHEMA,
        "acceptance_eligible": False,
        "coordinate_gauge": "carla_map_exact_no_global_se2",
        "splits": ["fit", "development"],
        "forbidden_roots": [str(tmp_path / "sealed-holdout")],
        "map": {"candidate_id": "richmond-test", "opendrive": opendrive,
                "topology": topology},
        "cameras_json": cameras_json,
        "cameras": {},
    }
    truths = {}
    configured_cameras = []
    for camera_index, camera_id in enumerate(fitter.CAMERAS):
        width, height = 1280, 960
        baseline = np.asarray((camera_index * 20.0, camera_index * 3.0, 7.0,
                               -12.0, camera_index * 18.0, 1.0, 88.0))
        delta = np.asarray((0.12 * (camera_index + 1), -0.08 * camera_index,
                            0.05, 0.25, -0.18, 0.08, 0.35))
        truth = baseline + delta
        truths[camera_id] = truth
        focal = (width / 2.0) / np.tan(np.radians(baseline[6]) / 2.0)
        configured_cameras.append({
            "id": camera_id, "pitch_deg": baseline[3], "heading_deg": baseline[4] + 90.0,
            "yaw_deg": 0.0, "roll_deg": baseline[5],
            "intrinsics": {"width": width, "height": height, "fx": focal, "fy": focal,
                           "cx": width / 2.0, "cy": height / 2.0},
        })
        epochs = []
        for split, count in (("fit", 3), ("development", 1)):
            for index in range(count):
                name = f"{camera_id}-{split}-{index}"
                frame = binding(tmp_path / f"{name}.bin", name.encode())
                members = [binding(tmp_path / f"{name}-raw.bin", f"{name}-raw".encode())]
                epochs.append({"id": name, "split": split, "frame": frame,
                               "median_members": members})
        points, polylines, horizons, vanishing = [], [], [], []
        rotation = rotation_matrix(*truth[3:6])
        for split, count in (("fit", 8), ("development", 4)):
            epoch_ids = [item["id"] for item in epochs if item["split"] == split]
            split_shift = 0.0 if split == "fit" else 2.0
            local_points = []
            for index in range(count):
                depth = 18.0 + 5.0 * (index % 4) + index + split_shift
                side = (-1 if index % 2 == 0 else 1) * (11.0 + 2.0 * (index % 3))
                vertical = (-6.0 if (index // 2) % 2 == 0 else 5.5)
                local_points.append((depth, side, vertical))
            world = (rotation @ np.asarray(local_points).T).T + truth[:3]
            uv, depth = project_world(world, truth, width, height)
            assert np.all(depth > 0)
            for index, (xyz, pixel) in enumerate(zip(world, uv)):
                points.append({
                    "id": f"{camera_id}-{split}-point-{index}",
                    "physical_feature_id": f"{camera_id}-{split}-landmark-{index}",
                    "epoch_id": epoch_ids[index % len(epoch_ids)], "split": split,
                    "provenance": "manually_verified_unique", "real_uv": pixel.tolist(),
                    "world_xyz": xyz.tolist(), "uncertainty_px": 1.0,
                })
            for class_index, feature_class in enumerate(fitter.CLASSES):
                local = np.asarray([
                    (20 + 4 * class_index + split_shift, -5 + 4 * class_index, -2.0),
                    (27 + 4 * class_index + split_shift, -1 + 4 * class_index, -1.7),
                    (34 + 4 * class_index + split_shift, 3 + 4 * class_index, -1.4),
                ])
                line_world = (rotation @ local.T).T + truth[:3]
                line_uv, _ = project_world(line_world, truth, width, height)
                polylines.append({
                    "id": f"{camera_id}-{split}-line-{class_index}",
                    "physical_feature_id": f"{camera_id}-{split}-line-feature-{class_index}",
                    "epoch_id": epoch_ids[class_index % len(epoch_ids)], "split": split,
                    "provenance": "manually_traced_geometry", "class": feature_class,
                    "real_vertices": line_uv.tolist(), "world_vertices": line_world.tolist(),
                    "uncertainty_px": 1.0,
                })
            horizon = ground_horizon_line(truth, width, height)
            if split == "development":
                horizon = horizon.copy(); horizon[2] += 0.25
            horizons.append({
                "id": f"{camera_id}-{split}-horizon", "physical_feature_id": f"{camera_id}-{split}-horizon-feature",
                "epoch_id": epoch_ids[0], "split": split,
                "provenance": "manually_traced_geometry", "real_line": horizon.tolist(),
                "uncertainty_px": 1.0,
            })
            for index, local_direction in enumerate(((1.0, .3, .05), (1.0, -.25, .1))):
                direction = rotation @ np.asarray(local_direction)
                pixel = project_direction(direction, truth, width, height)
                if split == "development":
                    pixel = pixel + np.asarray((0.25, 0.0))
                vanishing.append({
                    "id": f"{camera_id}-{split}-vanish-{index}",
                    "physical_feature_id": f"{camera_id}-{split}-vanish-feature-{index}",
                    "epoch_id": epoch_ids[index % len(epoch_ids)], "split": split,
                    "provenance": "manually_verified_unique", "world_direction": direction.tolist(),
                    "real_uv": pixel.tolist(), "uncertainty_px": 1.0,
                })
        document["cameras"][camera_id] = {
            "width": width, "height": height, "baseline": baseline.tolist(),
            "anchor_location": baseline[:3].tolist(),
            "production_base": {"pitch_deg": baseline[3], "yaw_deg": baseline[4],
                                "roll_deg": baseline[5], "fov_deg": baseline[6]},
            "epochs": epochs, "points": points, "polylines": polylines,
            "horizons": horizons, "vanishing": vanishing,
        }
    cameras_payload = json.dumps({"cameras": configured_cameras}, sort_keys=True).encode()
    document["cameras_json"] = binding(cameras_path, cameras_payload)
    raw = json.dumps(document, sort_keys=True).encode()
    return document, truths, hashlib.sha256(raw).hexdigest()


def test_projection_and_production_pose_round_trip():
    absolute = np.asarray((10.4, -2.3, 8.1, -17.0, 42.0, 1.5, 87.5))
    base = {"pitch_deg": -15.0, "yaw_deg": 40.0, "roll_deg": 1.0, "fov_deg": 88.0}
    pose, recovered = production_round_trip((10.0, -2.0, 8.0), base, absolute)
    assert recovered == pytest.approx(absolute, abs=1e-10)
    assert set(pose) == {"forward_offset_m", "right_offset_m", "height_offset_m",
                         "pitch_offset_deg", "yaw_offset_deg", "roll_offset_deg", "fov_offset_deg"}
    near_sideways = np.asarray((0.0, 0.0, 7.0, -10.0, 90.0, 2.0, 88.0))
    horizon = ground_horizon_line(near_sideways, 1280, 960)
    assert np.isfinite(horizon).all()
    assert np.linalg.norm(horizon[:2]) == pytest.approx(1.0)


def test_schema_and_synthetic_independent_translation_recovery(tmp_path):
    document, truths, digest = synthetic_document(tmp_path)
    model = fitter.validate_document(document, digest)
    report = fitter.solve(model, starts=8, max_nfev=40)
    assert report["parameterization"]["dimension"] == 28
    assert report["parameterization"]["global_site_se2_parameter"] is False
    assert report["data_jacobian"]["rank"] == 28
    assert report["data_jacobian"]["condition"] <= 1e8
    assert report["holdout_consumed"] is False
    assert report["release_eligible"] is False
    assert report["basin_evidence_sufficient"] is True
    assert report["epoch_stability"]["status"] == "PASS"
    assert report["optimizer"]["max_nfev"] == 40
    assert len(report["optimizer"]["initial_normalized_starts"]) == 9
    assert set(report["runtime_identity"]) == {
        "python", "implementation", "python_executable", "python_executable_sha256",
        "platform", "numpy", "scipy"
    }
    assert report["reproducibility_complete"] is False
    assert report["development_gate_passed"] is False
    for epochs in report["input_bindings"]["epochs"].values():
        for epoch in epochs.values():
            assert epoch["median_members"]
            assert set(epoch["median_members"][0]) == {"path", "sha256", "bytes"}
    for camera_id, truth in truths.items():
        fitted = np.asarray(list(report["cameras"][camera_id]["absolute_parameters"].values()))
        assert fitted == pytest.approx(truth, abs=2e-3)
    translations = [tuple(report["cameras"][key]["absolute_parameters"][name]
                          for name in fitter.PARAMETER_NAMES[:3]) for key in fitter.CAMERAS]
    assert len(set(translations)) == 4


@pytest.mark.parametrize("mutation,error", [
    ("gauge", "CARLA map gauge"),
    ("split", "holdout"),
    ("cross_split", "cross fit/development"),
    ("degenerate", "point coverage"),
])
def test_fail_closed_schema_split_and_degeneracy(tmp_path, mutation, error):
    document, _truths, digest = synthetic_document(tmp_path)
    if mutation == "gauge":
        document["coordinate_gauge"] = "fitted_global_se2"
    elif mutation == "split":
        document["splits"] = ["fit", "holdout"]
    elif mutation == "cross_split":
        camera = document["cameras"]["ch1"]
        camera["points"][-1]["physical_feature_id"] = camera["points"][0]["physical_feature_id"]
    else:
        for point in document["cameras"]["ch1"]["points"]:
            if point["split"] == "fit":
                point["real_uv"] = [640.0, 480.0]
    with pytest.raises(fitter.DevelopmentFitError, match=error):
        fitter.validate_document(document, digest)


def test_forbidden_root_rejected_before_artifact_read(tmp_path):
    document, _truths, digest = synthetic_document(tmp_path)
    sealed = tmp_path / "sealed-holdout"
    sealed.mkdir()
    forbidden = sealed / "never-read.xodr"
    document["map"]["opendrive"] = {"path": str(forbidden), "sha256": "0" * 64}
    with pytest.raises(fitter.DevelopmentFitError, match="forbidden"):
        fitter.validate_document(document, digest)
    assert not forbidden.exists()


def test_rank_and_bound_and_competing_basin_fail_closed(tmp_path):
    document, _truths, digest = synthetic_document(tmp_path)
    model = fitter.validate_document(document, digest)
    z = np.zeros(28)
    assert fitter.competing_basin([
        {"z": z, "fit_loss": 1.0, "development_loss": 100.0},
        {"z": z + 0.5, "fit_loss": 1.1, "development_loss": 101.9},
    ]) is True
    assert fitter.competing_basin([
        {"z": z, "fit_loss": 1.0, "development_loss": 100.0},
        {"z": z + 0.5, "fit_loss": 1.1, "development_loss": 102.1},
    ]) is False
    lower = np.tile(fitter.LOWER_DELTA / fitter.SCALES, 4)
    upper = np.tile(fitter.UPPER_DELTA / fitter.SCALES, 4)
    assert fitter._boundary_hits(lower, lower, upper) == [
        f"{camera}:{name}" for camera in fitter.CAMERAS for name in fitter.PARAMETER_NAMES
    ]
    # Removing camera-specific directional factors makes the normalized data
    # Jacobian incapable of certifying the complete 28-parameter model.
    broken = copy.deepcopy(model)
    broken["cameras"]["ch4"]["points"] = broken["cameras"]["ch4"]["points"][:1]
    broken["cameras"]["ch4"]["polylines"] = []
    broken["cameras"]["ch4"]["horizons"] = []
    broken["cameras"]["ch4"]["vanishing"] = []
    jacobian = fitter._jacobian(lambda value: fitter.residual_vector(value, broken, "fit"), z)
    assert np.linalg.matrix_rank(jacobian) < 28
    lower = np.tile(fitter.LOWER_DELTA / fitter.SCALES, 4)
    upper = np.tile(fitter.UPPER_DELTA / fitter.SCALES, 4)
    _values, success, reason, iterations = fitter._refine(model, z, lower, upper, 0)
    assert (success, reason, iterations) == (False, "iteration_limit", 0)


def test_invalid_polyline_has_constant_residual_dimension(tmp_path):
    document, _truths, digest = synthetic_document(tmp_path)
    model = fitter.validate_document(document, digest)
    item = model["cameras"]["ch1"]["polylines"][0]
    valid = fitter._polyline_residual(item, model["cameras"]["ch1"]["baseline"], model["cameras"]["ch1"])
    behind = model["cameras"]["ch1"]["baseline"].copy()
    behind[4] += 180.0
    invalid = fitter._polyline_residual(item, behind, model["cameras"]["ch1"])
    assert valid.shape == invalid.shape == (32,)


def test_invalid_polyline_is_fail_closed_even_with_tiny_uncertainty(tmp_path):
    document, _truths, digest = synthetic_document(tmp_path)
    model = fitter.validate_document(document, digest)
    camera = model["cameras"]["ch1"]
    for item in camera["polylines"]:
        if item["split"] == "development":
            item["uncertainty_px"] = 0.001
    z = np.zeros(28)
    z[4] = 180.0 / fitter.SCALES[4]
    report = fitter._errors(model, z, "development")["ch1"]
    assert report["roads"]["rmse_px"] == math.inf
    assert report["gates_passed"] is False


def test_multistart_is_deterministic_and_axis_stratified():
    lower, upper = np.zeros(28), np.ones(28)
    first = np.asarray(fitter._low_discrepancy_starts(lower, upper, 8, 42))
    second = np.asarray(fitter._low_discrepancy_starts(lower, upper, 8, 42))
    assert first == pytest.approx(second)
    for axis in range(28):
        assert sorted(np.floor(first[:, axis] * 8).astype(int)) == list(range(8))
    selected = fitter._select_fit_candidate([
        {"fit_loss": 2.0, "development_loss": 0.0},
        {"fit_loss": 1.0, "development_loss": 100.0},
    ])
    assert selected["fit_loss"] == 1.0


def test_fov_bounds_intersect_physical_interval(tmp_path):
    document, _truths, digest = synthetic_document(tmp_path)
    model = fitter.validate_document(document, digest)
    model["cameras"]["ch1"]["baseline"][6] = 2.0
    lower, upper = fitter._normalized_bounds(model)
    assert 2.0 + lower[6] * fitter.SCALES[6] > 1.0
    assert 2.0 + upper[6] * fitter.SCALES[6] < 179.0
    values, success, reason, iterations = fitter._refine(model, lower.copy(), lower, upper, 1)
    assert np.isfinite(values).all()
    assert isinstance(success, bool) and reason in {
        "normalized_step_converged", "iteration_limit", "no_objective_progress",
        "singular_normal_equations",
    }
    assert iterations == 1


def test_multistart_minimum_is_mandatory(tmp_path):
    document, _truths, digest = synthetic_document(tmp_path)
    model = fitter.validate_document(document, digest)
    for starts in (0, -1, 7):
        with pytest.raises(fitter.DevelopmentFitError, match="at least 8"):
            fitter.solve(model, starts=starts, max_nfev=1)


def test_cross_camera_geometry_and_config_drift_are_rejected(tmp_path):
    document, _truths, digest = synthetic_document(tmp_path)
    document["cameras"]["ch2"]["points"][-1]["world_xyz"] = copy.deepcopy(
        document["cameras"]["ch1"]["points"][0]["world_xyz"]
    )
    with pytest.raises(fitter.DevelopmentFitError, match="crosses camera"):
        fitter.validate_document(document, digest)

    config_root = tmp_path / "config"
    config_root.mkdir()
    document, _truths, digest = synthetic_document(config_root)
    document["cameras"]["ch1"]["production_base"]["yaw_deg"] += 0.1
    with pytest.raises(fitter.DevelopmentFitError, match="disagrees with cameras JSON"):
        fitter.validate_document(document, digest)


def test_cross_camera_cross_kind_and_polyline_containment_are_rejected(tmp_path):
    document, _truths, digest = synthetic_document(tmp_path)
    fit_line = document["cameras"]["ch1"]["polylines"][0]
    development_point = next(
        item for item in document["cameras"]["ch2"]["points"]
        if item["split"] == "development"
    )
    endpoints = np.asarray(fit_line["world_vertices"])
    development_point["world_xyz"] = ((endpoints[0] + endpoints[1]) / 2.0).tolist()
    with pytest.raises(fitter.DevelopmentFitError, match="polyline geometry crosses camera"):
        fitter.validate_document(document, digest)

    adjacent = tmp_path / "adjacent"
    adjacent.mkdir()
    document, _truths, digest = synthetic_document(adjacent)
    fit_line = next(item for item in document["cameras"]["ch1"]["polylines"]
                    if item["split"] == "fit")
    development_line = next(item for item in document["cameras"]["ch2"]["polylines"]
                            if item["split"] == "development")
    vertices = np.asarray(fit_line["world_vertices"])
    delta = vertices[-1] - vertices[-2]
    development_line["world_vertices"] = [
        vertices[-1].tolist(), (vertices[-1] + delta).tolist(),
        (vertices[-1] + 2 * delta).tolist(),
    ]
    with pytest.raises(fitter.DevelopmentFitError, match="adjacent polyline"):
        fitter.validate_document(document, digest)


def test_directional_fingerprints_and_global_feature_clusters(tmp_path):
    document, _truths, digest = synthetic_document(tmp_path)
    fit_horizon = next(item for item in document["cameras"]["ch1"]["horizons"]
                       if item["split"] == "fit")
    development_horizon = next(item for item in document["cameras"]["ch2"]["horizons"]
                               if item["split"] == "development")
    development_horizon["real_line"] = copy.deepcopy(fit_horizon["real_line"])
    with pytest.raises(fitter.DevelopmentFitError, match="horizon fingerprint"):
        fitter.validate_document(document, digest)

    direction_root = tmp_path / "direction"
    direction_root.mkdir()
    document, _truths, digest = synthetic_document(direction_root)
    fit = next(item for item in document["cameras"]["ch1"]["vanishing"]
               if item["split"] == "fit")
    development = next(item for item in document["cameras"]["ch2"]["vanishing"]
                       if item["split"] == "development")
    development["world_direction"] = copy.deepcopy(fit["world_direction"])
    development["real_uv"] = copy.deepcopy(fit["real_uv"])
    with pytest.raises(fitter.DevelopmentFitError, match="vanishing fingerprint"):
        fitter.validate_document(document, digest)

    cluster_root = tmp_path / "cluster"
    cluster_root.mkdir()
    document, _truths, digest = synthetic_document(cluster_root)
    model = fitter.validate_document(document, digest)
    camera = model["cameras"]["ch1"]
    point = next(item for item in camera["points"] if item["split"] == "fit")
    line = next(item for item in camera["polylines"] if item["split"] == "fit")
    line["physical_feature_id"] = point["physical_feature_id"]
    _residual, weights = fitter.residual_vector(np.zeros(28), model, "fit", return_weights=True)
    unique_features = {
        item["physical_feature_id"] for value in model["cameras"].values()
        for kind in ("points", "polylines", "horizons", "vanishing")
        for item in value[kind] if item["split"] == "fit"
    }
    assert weights.sum() == pytest.approx(len(unique_features))

    contained = tmp_path / "contained"
    contained.mkdir()
    document, _truths, digest = synthetic_document(contained)
    fit_line = next(item for item in document["cameras"]["ch1"]["polylines"]
                    if item["split"] == "fit")
    development_line = next(item for item in document["cameras"]["ch2"]["polylines"]
                            if item["split"] == "development")
    vertices = np.asarray(fit_line["world_vertices"])
    development_line["world_vertices"] = [
        (vertices[0] * .75 + vertices[1] * .25).tolist(),
        ((vertices[0] + vertices[1]) / 2.0).tolist(),
        (vertices[0] * .25 + vertices[1] * .75).tolist(),
    ]
    with pytest.raises(fitter.DevelopmentFitError, match="polyline geometry crosses camera"):
        fitter.validate_document(document, digest)


@pytest.mark.parametrize("offset,rejected", [(1e-8, True), (0.049, True), (0.051, False)])
def test_horizon_near_duplicate_reference_pixel_boundary(tmp_path, offset, rejected):
    document, _truths, digest = synthetic_document(tmp_path)
    fit = next(item for item in document["cameras"]["ch1"]["horizons"]
               if item["split"] == "fit")
    development = next(item for item in document["cameras"]["ch2"]["horizons"]
                       if item["split"] == "development")
    development["real_line"] = copy.deepcopy(fit["real_line"])
    development["real_line"][2] += offset
    if rejected:
        with pytest.raises(fitter.DevelopmentFitError, match="horizon fingerprint"):
            fitter.validate_document(document, digest)
    else:
        fitter.validate_document(document, digest)


@pytest.mark.parametrize("reverse", [False, True])
def test_global_polyline_endpoint_to_interior_contact_is_rejected(tmp_path, reverse):
    document, _truths, digest = synthetic_document(tmp_path)
    fit = next(item for item in document["cameras"]["ch1"]["polylines"]
               if item["split"] == "fit")
    development = next(item for item in document["cameras"]["ch2"]["polylines"]
                       if item["split"] == "development")
    fit_vertices = np.asarray(fit["world_vertices"], dtype=float)
    point = fit_vertices[-1]
    tangent = fit_vertices[-1] - fit_vertices[-2]
    perpendicular = np.cross(tangent, np.asarray((0.0, 0.0, 1.0)))
    perpendicular /= np.linalg.norm(perpendicular)
    if reverse:
        fit["world_vertices"] = [
            (point - perpendicular).tolist(), point.tolist(),
            (point + perpendicular).tolist(),
        ]
        development["world_vertices"] = [
            point.tolist(), (point + tangent).tolist(),
            (point + 2 * tangent).tolist(),
        ]
    else:
        development["world_vertices"] = [
            (point - perpendicular).tolist(), point.tolist(),
            (point + perpendicular).tolist(),
        ]
    with pytest.raises(fitter.DevelopmentFitError, match="adjacent polyline"):
        fitter.validate_document(document, digest)


def test_segment_distance_interior_crossing_is_exact():
    assert fitter._segment_segment_distance(
        np.asarray((-8.0, 0.0, 0.0)), np.asarray((8.0, 0.0, 0.0)),
        np.asarray((0.0, -8.0, 0.0)), np.asarray((0.0, 8.0, 0.0)),
    ) == pytest.approx(0.0, abs=1e-12)


def test_global_polyline_interior_crossing_is_rejected(tmp_path):
    document, _truths, digest = synthetic_document(tmp_path)
    fit = next(item for item in document["cameras"]["ch1"]["polylines"]
               if item["split"] == "fit")
    development = next(item for item in document["cameras"]["ch2"]["polylines"]
                       if item["split"] == "development")
    vertices = np.asarray(fit["world_vertices"], dtype=float)
    midpoint = (vertices[-2] + vertices[-1]) / 2.0
    perpendicular = np.cross(vertices[-1] - vertices[-2], np.asarray((0.0, 0.0, 1.0)))
    perpendicular /= np.linalg.norm(perpendicular)
    development["world_vertices"] = [
        (midpoint - 2.0 * perpendicular).tolist(),
        (midpoint + 2.0 * perpendicular).tolist(),
        (midpoint + 4.0 * perpendicular).tolist(),
    ]
    with pytest.raises(fitter.DevelopmentFitError, match="adjacent polyline"):
        fitter.validate_document(document, digest)


def test_out_of_frame_horizon_and_zero_vanishing_direction_are_rejected(tmp_path):
    document, _truths, digest = synthetic_document(tmp_path)
    document["cameras"]["ch1"]["horizons"][0]["real_line"] = [0.0, 1.0, 1.0]
    with pytest.raises(fitter.DevelopmentFitError, match="horizon does not cross"):
        fitter.validate_document(document, digest)

    direction_root = tmp_path / "invalid-direction"
    direction_root.mkdir()
    document, _truths, digest = synthetic_document(direction_root)
    document["cameras"]["ch1"]["vanishing"][0]["world_direction"] = [0.0, 0.0, 0.0]
    with pytest.raises(fitter.DevelopmentFitError, match="vanishing direction is degenerate"):
        fitter.validate_document(document, digest)


@pytest.mark.parametrize("line", ([1.0, 1.0, 0.0], [1.0, 0.0, 0.0]))
def test_corner_only_and_edge_coincident_horizons_are_rejected(tmp_path, line):
    document, _truths, digest = synthetic_document(tmp_path)
    document["cameras"]["ch1"]["horizons"][0]["real_line"] = list(line)
    with pytest.raises(fitter.DevelopmentFitError, match="horizon does not cross"):
        fitter.validate_document(document, digest)


def test_line_image_points_are_distinct_and_cross_the_interior():
    assert fitter._line_image_points(np.asarray((1.0, 1.0, 0.0)), 1280, 960).shape == (0, 2)
    assert fitter._line_image_points(np.asarray((1.0, 0.0, 0.0)), 1280, 960).shape == (0, 2)
    points = fitter._line_image_points(np.asarray((0.0, 1.0, -480.0)), 1280, 960)
    assert points.shape == (2, 2)
    assert np.linalg.norm(points[0] - points[1]) >= fitter.MIN_HORIZON_CHORD_REFERENCE_PX


def test_horizon_chord_exact_reference_boundary_is_accepted():
    # x+y=sqrt(2) has endpoints (0,sqrt(2)), (sqrt(2),0): an exact 2 px chord.
    points = fitter._line_image_points(
        np.asarray((1.0, 1.0, -math.sqrt(2.0))), 1280, 960
    )
    assert points.shape == (2, 2)
    assert np.linalg.norm(points[0] - points[1]) == pytest.approx(2.0)


@pytest.mark.parametrize("native_chord,accepted", [(3.0, False), (4.0, True), (4.002, True)])
def test_horizon_chord_uses_reference_gauge_at_2560x1920(native_chord, accepted):
    # Uniform 2x native resolution: a native chord is half as long on 1280x960.
    line = np.asarray((1.0, 1.0, -native_chord / math.sqrt(2.0)))
    points = fitter._line_image_points(line, 2560, 1920)
    assert (points.shape == (2, 2)) is accepted


def _rotate_direction(value, radians):
    value = np.asarray(value, dtype=float)
    value /= np.linalg.norm(value)
    trial = np.asarray((0.0, 0.0, 1.0))
    if abs(np.dot(value, trial)) > 0.9:
        trial = np.asarray((0.0, 1.0, 0.0))
    orthogonal = trial - np.dot(value, trial) * value
    orthogonal /= np.linalg.norm(orthogonal)
    return (math.cos(radians) * value + math.sin(radians) * orthogonal).tolist()


@pytest.mark.parametrize(
    "pixel_offset,angle_rad,rejected",
    [(1e-8, 1e-8, True), (0.049, 0.9e-6, True),
     (0.051, 0.9e-6, False), (0.049, 1.1e-6, False)],
)
def test_vanishing_near_duplicate_pixel_and_angular_boundaries(
    tmp_path, pixel_offset, angle_rad, rejected
):
    document, _truths, digest = synthetic_document(tmp_path)
    fit = next(item for item in document["cameras"]["ch1"]["vanishing"]
               if item["split"] == "fit")
    development = next(item for item in document["cameras"]["ch2"]["vanishing"]
                       if item["split"] == "development")
    development["world_direction"] = _rotate_direction(fit["world_direction"], angle_rad)
    development["real_uv"] = copy.deepcopy(fit["real_uv"])
    development["real_uv"][0] += pixel_offset
    if rejected:
        with pytest.raises(fitter.DevelopmentFitError, match="vanishing fingerprint"):
            fitter.validate_document(document, digest)
    else:
        fitter.validate_document(document, digest)
