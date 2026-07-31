"""Fail-closed tests for manual calibration annotation manifests."""

import copy
import hashlib
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import build_twin_calibration_manifest as manifest_builder  # noqa: E402
from build_twin_calibration_manifest import (  # noqa: E402
    bind_survey_record_artifacts,
    build_deployment_model,
    decoded_image_size,
    depth_neighborhood_evidence,
    stable_depth_meters,
    resolve_manifest,
    retained_artifact_identity,
    validate_intrinsics_artifact,
    validate_intrinsics_calibration,
    validate_intrinsics_source_images,
    validate_annotations,
    validate_resolved_point_independence,
    validate_strict_projection_provenance,
)


def annotation_payload():
    train_real = [
        [200, 200], [800, 230], [1500, 250], [2300, 220],
        [250, 700], [900, 900], [1600, 1050], [2250, 800],
    ]
    holdout_real = [[300, 350], [2200, 420], [400, 1500], [2150, 1450]]
    train_twin = [
        [100, 100], [400, 115], [750, 125], [1150, 110],
        [125, 350], [450, 450], [800, 525], [1125, 400],
    ]
    holdout_twin = [[150, 175], [1100, 210], [200, 750], [1075, 725]]
    points = []
    for index, (image, twin) in enumerate(zip(
        train_real + holdout_real, train_twin + holdout_twin
    )):
        points.append({
            "id": f"landmark-{index}",
            "global_landmark_id": f"rfs-survey-landmark-{index}",
            "surveyed_world": [float(index * 2), float(index), 0.5],
            "survey_record_sha256": hashlib.sha256(
                f"survey-record-{index}".encode()
            ).hexdigest(),
            "survey_record_path": f"/retained/survey-record-{index}.json",
            "split": "train" if index < 8 else "holdout",
            "provenance": "manually_verified_unique",
            "category": "signal_corner",
            "description": f"Unique signal cabinet corner number {index}",
            "twin": twin,
            "image": image,
        })
    roads = []
    for index in range(5):
        roads.append({
            "id": f"road-{index}",
            "split": "train" if index < 3 else "holdout",
            "provenance": "manually_traced_geometry",
            "category": "curb_edge",
            "description": f"Unique finite curb edge segment number {index}",
            "twin_polyline": [[100, 500 + index * 10], [1100, 400 + index * 10]],
            "image_polyline": [[200, 1000 + index * 20], [2200, 800 + index * 20]],
        })
    return {
        "camera_id": "ch1",
        "real_frame_sha256": "a" * 64,
        "twin_frame_sha256": "b" * 64,
        "cameras_file_sha256": "c" * 64,
        "points": points,
        "roads": roads,
    }


def strict_projection(map_name="Carla/Maps/Richmond_Field_Station_Richmond_CA"):
    return {
        "source": "opendrive_georeference",
        "strict": True,
        "map_origin_error_m": 0.125,
        "map_name": map_name,
        "opendrive_sha256": "1" * 64,
        "georeference_sha256": "2" * 64,
    }


def test_accepts_complete_frozen_manual_evidence():
    features = validate_annotations(
        annotation_payload(), "ch1", (2560, 1920), (1280, 960)
    )
    assert len(features) == 17
    assert sum(item["type"] == "point" for item in features) == 12
    assert sum(item["type"] == "polyline" for item in features) == 5
    assert sum(item["split"] == "holdout" for item in features) == 6


def test_survey_records_are_real_files_not_unverified_hash_strings(tmp_path):
    payload = annotation_payload()
    for index, point in enumerate(payload["points"]):
        path = tmp_path / f"survey-{index}.json"
        raw = f"survey-record-{index}".encode()
        path.write_bytes(raw)
        point["survey_record_path"] = str(path.resolve())
        point["survey_record_sha256"] = hashlib.sha256(raw).hexdigest()
    features = validate_annotations(
        payload, "ch1", (2560, 1920), (1280, 960)
    )
    bound = bind_survey_record_artifacts(features)
    assert all(
        feature.get("type") != "point"
        or feature["survey_record"]["path"] == feature["survey_record_path"]
        for feature in bound
    )
    Path(bound[0]["survey_record_path"]).unlink()
    with pytest.raises(ValueError, match="missing or unreadable"):
        bind_survey_record_artifacts(features)


def test_retained_artifact_identity_rejects_nonexistent_and_tampered_hash(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="missing or unreadable"):
        retained_artifact_identity(missing, "test")
    retained = tmp_path / "retained.json"
    retained.write_bytes(b"real-evidence")
    with pytest.raises(ValueError, match="hash does not match"):
        retained_artifact_identity(
            retained, "test", expected_sha256="0" * 64
        )


def test_staged_artifact_pair_publishes_durably_without_partial_files(tmp_path):
    depth_output = tmp_path / "depth.bgra"
    manifest_output = tmp_path / "manifest.json"
    depth_stage = manifest_builder.stage_artifact_bytes(
        depth_output, b"depth", "depth"
    )
    manifest_stage = manifest_builder.stage_artifact_bytes(
        manifest_output, b'{"manifest": true}\n', "manifest"
    )

    manifest_builder.publish_staged_artifacts_no_replace((
        (depth_stage, depth_output),
        (manifest_stage, manifest_output),
    ))

    assert depth_output.read_bytes() == b"depth"
    assert manifest_output.read_bytes() == b'{"manifest": true}\n'
    assert not depth_stage.exists()
    assert not manifest_stage.exists()


def test_staged_artifact_pair_rolls_back_if_no_replace_publish_fails(tmp_path):
    depth_output = tmp_path / "depth.bgra"
    manifest_output = tmp_path / "manifest.json"
    manifest_output.write_bytes(b"preexisting")
    depth_stage = manifest_builder.stage_artifact_bytes(
        depth_output, b"depth", "depth"
    )
    manifest_stage = manifest_builder.stage_artifact_bytes(
        tmp_path / "unused-manifest.json", b"new manifest", "manifest"
    )

    with pytest.raises(FileExistsError):
        manifest_builder.publish_staged_artifacts_no_replace((
            (depth_stage, depth_output),
            (manifest_stage, manifest_output),
        ))

    assert not depth_output.exists()
    assert manifest_output.read_bytes() == b"preexisting"
    assert not depth_stage.exists()
    assert not manifest_stage.exists()


def test_staging_fsync_failure_removes_temporary_file(tmp_path, monkeypatch):
    output = tmp_path / "manifest.json"

    def fail_fsync(_descriptor):
        raise OSError("injected fsync failure")

    monkeypatch.setattr(manifest_builder.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="injected fsync failure"):
        manifest_builder.stage_artifact_bytes(output, b"partial", "manifest")

    assert not output.exists()
    assert list(tmp_path.glob("*.staged")) == []


def test_offline_depth_projection_uses_carla_forward_right_up_axes():
    baseline = {
        "location": [1.2, -3.4, 8.5],
        "pitch_deg": 0.0,
        "yaw_deg": 90.0,
        "roll_deg": 0.0,
    }
    offline = manifest_builder.offline_depth_pixel_to_world(
        baseline, 640.0, 480.0, 23.4, 90.0, 1280, 960
    )

    assert offline == pytest.approx([1.2, 20.0, 8.5], abs=1e-12)


@pytest.mark.parametrize("failure_site", ["identity", "resolve"])
def test_post_spawn_body_failure_still_stops_and_destroys_sensor(failure_site):
    calls = []

    class Actor:
        def stop(self):
            calls.append("stop")

        def destroy(self):
            calls.append("destroy")

    with pytest.raises(ValueError, match=failure_site):
        with manifest_builder.managed_sensor_actor(Actor) as _actor:
            raise ValueError(f"injected {failure_site} failure")

    assert calls == ["stop", "destroy"]


@pytest.mark.parametrize("failed_operations", [("stop",), ("destroy",), ("stop", "destroy")])
def test_cleanup_failure_attempts_both_sensor_operations(failed_operations):
    calls = []

    class Actor:
        def stop(self):
            calls.append("stop")
            if "stop" in failed_operations:
                raise RuntimeError("injected stop failure")

        def destroy(self):
            calls.append("destroy")
            if "destroy" in failed_operations:
                raise RuntimeError("injected destroy failure")

    with pytest.raises(RuntimeError, match="sensor cleanup failed") as error:
        with manifest_builder.managed_sensor_actor(Actor):
            pass

    assert calls == ["stop", "destroy"]
    for operation in failed_operations:
        assert f"{operation} failed" in str(error.value)


def test_destroy_false_fails_closed_after_attempting_stop_and_destroy():
    calls = []

    class Actor:
        def stop(self):
            calls.append("stop")

        def destroy(self):
            calls.append("destroy")
            return False

    with pytest.raises(RuntimeError, match="destroy failed: returned False"):
        with manifest_builder.managed_sensor_actor(Actor):
            pass

    assert calls == ["stop", "destroy"]


def test_destroyed_actor_that_remains_alive_fails_closed():
    calls = []

    class Actor:
        is_alive = True

        def stop(self):
            calls.append("stop")

        def destroy(self):
            calls.append("destroy")
            return True

    with pytest.raises(RuntimeError, match="is_alive failed: remained True"):
        with manifest_builder.managed_sensor_actor(Actor):
            pass

    assert calls == ["stop", "destroy"]


def test_destroyed_actor_with_false_is_alive_state_can_publish():
    calls = []

    class Actor:
        is_alive = False

        def stop(self):
            calls.append("stop")

        def destroy(self):
            calls.append("destroy")
            return True

    with manifest_builder.managed_sensor_actor(Actor):
        pass

    assert calls == ["stop", "destroy"]


@pytest.mark.parametrize("kind", ["payload", "point", "road"])
def test_malformed_annotation_objects_fail_controlled(kind):
    payload = copy.deepcopy(annotation_payload())
    if kind == "payload":
        payload = []
    elif kind == "point":
        payload["points"][0] = None
    else:
        payload["roads"][0] = None

    with pytest.raises(ValueError, match="object"):
        validate_annotations(payload, "ch1", (2560, 1920), (1280, 960))


def test_deployment_model_reverses_existing_offsets_exactly():
    camera = {
        "pitch_deg": -30.0,
        "yaw_deg": 12.0,
        "heading_deg": 200.0,
        "roll_deg": 1.0,
        "intrinsics": {
            "fx": 640.0,
            "fy": 640.0,
            "cx": 640.0,
            "cy": 480.0,
            "width": 1280,
            "height": 960,
        },
        "twin_pose": {
            "forward_offset_m": 1.5,
            "right_offset_m": -0.4,
            "height_offset_m": 0.7,
            "yaw_offset_deg": 3.0,
        },
    }
    yaw = 200.0 + 12.0 + 3.0 - 90.0
    yaw_radians = math.radians(yaw)
    anchor = [10.0, 20.0, 8.0]
    transform = SimpleNamespace(
        location=SimpleNamespace(
            x=anchor[0] + 1.5 * math.cos(yaw_radians) - (-0.4) * math.sin(yaw_radians),
            y=anchor[1] + 1.5 * math.sin(yaw_radians) + (-0.4) * math.cos(yaw_radians),
            z=anchor[2] + 0.7,
        ),
        rotation=SimpleNamespace(yaw=yaw),
    )
    model = build_deployment_model(camera, transform)
    assert model["anchor_location"] == pytest.approx(anchor)
    assert model["base"]["yaw_deg"] == pytest.approx(122.0)
    assert model["base"]["pitch_deg"] == -30.0
    assert model["base"]["roll_deg"] == 1.0
    assert model["base"]["fov_deg"] == pytest.approx(90.0)
    assert model["lens"]["lens_k"] == -1.0

    camera["twin_lens"] = {"lens_k": -1.0}
    with pytest.raises(ValueError, match="lens overrides are held"):
        build_deployment_model(camera, transform)


def measured_camera():
    camera = {
        "intrinsics": {
            "fx": 640.0,
            "fy": 639.5,
            "cx": 638.0,
            "cy": 481.0,
            "width": 1280,
            "height": 960,
        },
        "intrinsics_calibration": {
            "method": "charuco",
            "artifact_sha256": "c" * 64,
            "image_count": 24,
            "source_images_sha256": [
                hashlib.sha256(f"calibration-{index}".encode()).hexdigest()
                for index in range(24)
            ],
            "rms_reprojection_error_px": 0.4,
            "resolution": [1280, 960],
            "camera_matrix": [
                [640.0, 0.0, 638.0],
                [0.0, 639.5, 481.0],
                [0.0, 0.0, 1.0],
            ],
            "distortion": {
                "k1": -0.04,
                "k2": 0.01,
                "p1": 0.001,
                "p2": -0.001,
                "k3": 0.0,
            },
        },
    }
    payload = {
        key: value
        for key, value in camera["intrinsics_calibration"].items()
        if key != "artifact_sha256"
    }
    artifact = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    camera["intrinsics_calibration"]["artifact_sha256"] = hashlib.sha256(artifact).hexdigest()
    return camera


def test_validates_measured_intrinsics_and_distortion_evidence():
    evidence = validate_intrinsics_calibration(measured_camera())
    assert evidence["method"] == "charuco"
    assert evidence["image_count"] == 24
    assert evidence["distortion"]["k1"] == -0.04


def test_artifact_hash_and_contents_are_bound_to_camera_config():
    camera = measured_camera()
    payload = {
        key: value
        for key, value in camera["intrinsics_calibration"].items()
        if key != "artifact_sha256"
    }
    artifact = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    evidence = validate_intrinsics_artifact(camera, artifact)
    assert evidence["artifact_sha256"] == hashlib.sha256(artifact).hexdigest()
    with pytest.raises(ValueError, match="hash does not match"):
        validate_intrinsics_artifact(camera, artifact + b" ")


def test_retained_image_dimensions_are_decoded_not_trusted_from_cli(tmp_path):
    from PIL import Image

    image_path = tmp_path / "frame.png"
    Image.new("RGB", (32, 24)).save(image_path)
    assert decoded_image_size(image_path.read_bytes(), "test") == (32, 24)
    with pytest.raises(ValueError, match="not a valid retained image"):
        decoded_image_size(b"not-an-image", "test")


def depth_buffer(width, height, default_depth, overrides=None):
    overrides = overrides or {}
    raw = bytearray(width * height * 4)
    for v in range(height):
        for u in range(width):
            depth = overrides.get((u, v), default_depth)
            encoded = round((depth / 1000.0) * 16777215.0)
            offset = (v * width + u) * 4
            raw[offset:offset + 4] = bytes([
                (encoded >> 16) & 0xFF,
                (encoded >> 8) & 0xFF,
                encoded & 0xFF,
                255,
            ])
    return bytes(raw)


def test_depth_neighborhood_rejects_geometry_edges():
    stable = depth_buffer(5, 5, 10.0)
    assert stable_depth_meters(stable, 5, 5, 2, 2) == pytest.approx(10.0, abs=0.001)
    evidence = depth_neighborhood_evidence(stable, 5, 5, 2, 2)
    assert evidence["center_depth_m"] == pytest.approx(10.0, abs=0.001)
    assert evidence["minimum_depth_m"] == pytest.approx(10.0, abs=0.001)
    assert evidence["maximum_deviation_m"] == pytest.approx(0.0)
    assert evidence["allowed_deviation_m"] == pytest.approx(0.25)
    discontinuous = depth_buffer(5, 5, 10.0, {(2, 2): 40.0})
    with pytest.raises(ValueError, match="depth discontinuity"):
        stable_depth_meters(discontinuous, 5, 5, 2, 2)


def test_resolve_manifest_rejects_wrong_depth_resolution_before_backprojection():
    depth = SimpleNamespace(width=4, height=4, raw_data=b"\0" * 64, frame=1, timestamp=1.0)
    with pytest.raises(ValueError, match="depth resolution"):
        resolve_manifest(
            [], camera_id="ch1", camera={}, transform=SimpleNamespace(),
            depth_image=depth, depth_raw=depth.raw_data, expected_twin_size=(5, 5),
            real_frame_sha256="a" * 64, twin_frame_sha256="b" * 64,
            annotation_sha256="c" * 64, cameras_file_sha256="d" * 64,
            camera_config_sha256="e" * 64, depth_raw_sha256="f" * 64,
            depth_frame_artifact={"path": "/retained/depth.bgra"},
            source_artifacts={},
            projection_provenance=strict_projection(),
            ue5_map="Carla/Maps/Richmond_Field_Station_Richmond_CA",
            ue5_map_opendrive_sha256="1" * 64,
        )


@pytest.mark.parametrize(
    "projection",
    [
        None,
        {"source": "origin_centered_fallback", "strict": False},
        {
            **strict_projection(),
            "georeference_sha256": "not-a-sha256",
        },
    ],
)
def test_manifest_builder_rejects_missing_malformed_or_fallback_projection(
    projection,
):
    with pytest.raises(ValueError, match="strict OpenDRIVE projection"):
        validate_strict_projection_provenance(
            projection,
            map_name="Carla/Maps/Richmond_Field_Station_Richmond_CA",
            opendrive_sha256="1" * 64,
        )


def test_intrinsics_sources_are_bound_to_retained_images(tmp_path):
    camera = measured_camera()
    images = []
    hashes = []
    for index in range(10):
        path = tmp_path / f"source-{index}.png"
        image = Image.new("RGB", (1280, 960), (index, 0, 0))
        image.save(path)
        images.append(path)
        hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
    camera["intrinsics_calibration"]["source_images_sha256"] = hashes
    camera["intrinsics_calibration"]["image_count"] = len(hashes)
    assert validate_intrinsics_source_images(camera, images) == sorted(hashes)
    images[-1] = images[0]
    with pytest.raises(ValueError, match="count or uniqueness"):
        validate_intrinsics_source_images(camera, images)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda camera: camera.pop("intrinsics_calibration"), "lacks measured"),
        (
            lambda camera: camera["intrinsics_calibration"].update(image_count=3),
            "at least 10",
        ),
        (
            lambda camera: camera["intrinsics_calibration"].update(
                rms_reprojection_error_px=4.0
            ),
            "no worse than 2",
        ),
        (
            lambda camera: camera["intrinsics_calibration"]["camera_matrix"][0].__setitem__(0, 700.0),
            "does not match",
        ),
    ],
)
def test_rejects_missing_or_untrusted_intrinsics_evidence(mutate, message):
    camera = measured_camera()
    mutate(camera)
    with pytest.raises(ValueError, match=message):
        validate_intrinsics_calibration(camera)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["points"][0].update(
                provenance="manual_verified_static"
            ),
            "not independently verified",
        ),
        (
            lambda payload: payload["roads"][0].update(
                provenance="matcher_proposal"
            ),
            "not manually traced",
        ),
        (lambda payload: payload["points"].pop(), "8 train and 4 holdout"),
        (
            lambda payload: payload["points"][0].update(twin=[2000, 20]),
            "outside",
        ),
        (
            lambda payload: payload["roads"][0].update(
                id=payload["points"][0]["id"]
            ),
            "unique",
        ),
        (
            lambda payload: payload["points"][0].update(description=""),
            "semantic description",
        ),
        (
            lambda payload: payload["points"][0].pop("global_landmark_id"),
            "global landmark IDs",
        ),
        (
            lambda payload: payload["points"][8].update(
                global_landmark_id=payload["points"][0]["global_landmark_id"]
            ),
            "globally unique",
        ),
        (
            lambda payload: payload["points"][8].update(
                image=payload["points"][0]["image"]
            ),
            "distinct across train and holdout",
        ),
        (
            lambda payload: payload["roads"][0].update(
                image_polyline=[[200, 1000], [200, 1000]]
            ),
            "zero-length segment",
        ),
        (
            lambda payload: payload["roads"][3].update(
                image_polyline=payload["roads"][0]["image_polyline"]
            ),
            "geometrically unique",
        ),
    ],
)
def test_rejects_unverified_sparse_or_malformed_evidence(mutate, message):
    payload = copy.deepcopy(annotation_payload())
    mutate(payload)
    with pytest.raises(ValueError, match=message):
        validate_annotations(payload, "ch1", (2560, 1920), (1280, 960))


def test_rejects_collinear_or_fit_line_copied_holdouts():
    payload = annotation_payload()
    train_line = [
        [100, 200], [400, 400], [700, 600], [1000, 800],
        [1300, 1000], [1600, 1200], [1900, 1400], [2350, 1700],
    ]
    holdout_line = [[100, 200], [850, 700], [1600, 1200], [2350, 1700]]
    for point, pixel in zip(payload["points"], train_line + holdout_line):
        point["image"] = pixel
    with pytest.raises(ValueError, match="collinear or clustered"):
        validate_annotations(payload, "ch1", (2560, 1920), (1280, 960))

    payload = annotation_payload()
    payload["points"][8]["image"] = [300, 350]
    payload["points"][9]["image"] = [2200, 350]
    payload["points"][10]["image"] = [400, 1500]
    payload["points"][11]["image"] = [2150, 1500]
    payload["roads"][0]["image_polyline"] = [[100, 350], [2400, 350]]
    payload["roads"][1]["image_polyline"] = [[100, 1500], [2400, 1500]]
    with pytest.raises(ValueError, match="not independent of fit roads"):
        validate_annotations(payload, "ch1", (2560, 1920), (1280, 960))


@pytest.mark.parametrize(
    ("field", "near_value"),
    [
        ("image", [102.0, 101.0]),
        ("twin", [101.0, 100.5]),
        ("world", [0.1, 0.1, 0.0]),
    ],
)
def test_resolved_holdouts_reject_proximity_to_train_in_every_space(
    field, near_value
):
    train = {
        "id": "train-point",
        "global_landmark_id": "rfs-train-point",
        "surveyed_world": [0.0, 0.0, 0.0],
        "survey_record_sha256": "a" * 64,
        "survey_record_path": "/retained/train-survey.json",
        "survey_record": {
            "path": "/retained/train-survey.json",
            "sha256": "a" * 64,
            "size_bytes": 1,
        },
        "type": "point",
        "split": "train",
        "image": [100.0, 100.0],
        "twin": [100.0, 100.0],
        "world": [0.0, 0.0, 0.0],
    }
    holdout = {
        "id": "holdout-point",
        "global_landmark_id": "rfs-holdout-point",
        "surveyed_world": [10.0, 10.0, 10.0],
        "survey_record_sha256": "b" * 64,
        "survey_record_path": "/retained/holdout-survey.json",
        "survey_record": {
            "path": "/retained/holdout-survey.json",
            "sha256": "b" * 64,
            "size_bytes": 1,
        },
        "type": "point",
        "split": "holdout",
        "image": [500.0, 500.0],
        "twin": [500.0, 500.0],
        "world": [10.0, 10.0, 10.0],
    }
    holdout[field] = near_value

    with pytest.raises(ValueError, match=f"{field} space"):
        validate_resolved_point_independence([train, holdout])


def test_resolve_manifest_checks_world_proximity_after_depth_resolution(
    monkeypatch
):
    annotations = [
        {
            "id": "train-point",
            "global_landmark_id": "rfs-train-point",
            "surveyed_world": [0.0, 0.0, 0.0],
            "survey_record_sha256": "a" * 64,
            "survey_record_path": "/retained/train-survey.json",
            "survey_record": {
                "path": "/retained/train-survey.json",
                "sha256": "a" * 64,
                "size_bytes": 1,
            },
            "type": "point",
            "split": "train",
            "provenance": "manually_verified_unique",
            "category": "signal_corner",
            "description": "Unique surveyed train signal corner",
            "twin": [2.0, 2.0],
            "image": [10.0, 10.0],
        },
        {
            "id": "holdout-point",
            "global_landmark_id": "rfs-holdout-point",
            "surveyed_world": [10.0, 10.0, 10.0],
            "survey_record_sha256": "b" * 64,
            "survey_record_path": "/retained/holdout-survey.json",
            "survey_record": {
                "path": "/retained/holdout-survey.json",
                "sha256": "b" * 64,
                "size_bytes": 1,
            },
            "type": "point",
            "split": "holdout",
            "provenance": "manually_verified_unique",
            "category": "signal_corner",
            "description": "Unique surveyed holdout signal corner",
            "twin": [5.0, 5.0],
            "image": [100.0, 100.0],
        },
    ]
    raw = depth_buffer(8, 8, 10.0)
    depth = SimpleNamespace(width=8, height=8, frame=1, timestamp=1.0)
    monkeypatch.setattr(
        manifest_builder,
        "depth_pixel_to_world",
        lambda *_args: SimpleNamespace(x=1.0, y=2.0, z=3.0),
    )

    with pytest.raises(ValueError, match="world space"):
        resolve_manifest(
            annotations,
            camera_id="ch1",
            camera=measured_camera(),
            transform=SimpleNamespace(),
            depth_image=depth,
            depth_raw=raw,
            expected_twin_size=(8, 8),
            real_frame_sha256="a" * 64,
            twin_frame_sha256="b" * 64,
            annotation_sha256="c" * 64,
            cameras_file_sha256="d" * 64,
            camera_config_sha256="e" * 64,
            depth_raw_sha256="f" * 64,
            depth_frame_artifact={"path": "/retained/depth.bgra"},
            source_artifacts={},
            projection_provenance=strict_projection(),
            ue5_map="Carla/Maps/Richmond_Field_Station_Richmond_CA",
            ue5_map_opendrive_sha256="1" * 64,
        )
