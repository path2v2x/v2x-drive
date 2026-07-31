"""Tests for the twin camera rig: pose conversion + rig lifecycle."""

import math

import pytest

from digital_twin_bridge import twin_camera_rig
from digital_twin_bridge.twin_camera_rig import (
    TwinCameraRig,
    absolute_twin_model,
    camera_with_twin_pose,
    configure_twin_camera_blueprint,
    compute_twin_camera_transform,
    heading_to_carla_yaw,
    horizontal_fov_deg,
    is_twin_supported_map,
    load_cameras_config,
    twin_horizontal_fov_deg,
    twin_pose_from_absolute,
)

from tests.conftest import MockLocation, MockTransform


SITE = {"lat": 37.91560117034595, "lon": -122.33478756387032}

CAMERA = {
    "id": "ch1",
    "height_m": 7.0,
    "pitch_deg": -39.2,
    "yaw_deg": -46.06,
    "heading_deg": 200.0,
    "intrinsics": {"fx": 1325.4, "fy": 1325.4, "cx": 1280.0, "cy": 960.0, "width": 2560, "height": 1920},
}


def make_config(cameras=None):
    return {"site": dict(SITE), "cameras": cameras or [dict(CAMERA)]}


class TestHeadingToCarlaYaw:
    """CARLA x = easting / y = -northing, so bearing H -> yaw H - 90."""

    @pytest.mark.parametrize(
        "heading,expected",
        [(0.0, -90.0), (90.0, 0.0), (180.0, 90.0), (270.0, 180.0)],
    )
    def test_cardinal_bearings(self, heading, expected):
        assert heading_to_carla_yaw(heading) == pytest.approx(expected)

    def test_mounting_yaw_composes_with_heading(self):
        assert heading_to_carla_yaw(200.0, -46.06) == pytest.approx(63.94)

    def test_normalised_to_half_open_range(self):
        yaw = heading_to_carla_yaw(350.0, 50.0)
        assert -180.0 <= yaw <= 180.0
        assert yaw == pytest.approx(-50.0)


class TestIntrinsics:
    def test_horizontal_fov_matches_real_camera(self):
        fov = horizontal_fov_deg(CAMERA["intrinsics"])
        assert fov == pytest.approx(2 * math.degrees(math.atan(1280 / 1325.4)))
        assert 87.0 < fov < 89.0

    def test_twin_fov_applies_explicit_calibration_offset(self):
        camera = {**CAMERA, "twin_pose": {"fov_offset_deg": -1.25}}
        assert twin_horizontal_fov_deg(camera) == pytest.approx(
            horizontal_fov_deg(CAMERA["intrinsics"]) - 1.25
        )

    def test_blueprint_preserves_actor_default_lens_and_rejects_overrides(self, mock_world):
        blueprint = mock_world.get_blueprint_library().find("sensor.camera.rgb")
        configure_twin_camera_blueprint(blueprint, CAMERA, 1280, 960, 12.0)
        assert not any(
            key.startswith("lens_") for key, _value in blueprint.set_attribute_calls
        )
        assert float(str(blueprint.get_attribute("sensor_tick"))) == pytest.approx(
            1 / 12, abs=1e-6
        )

        measured = {**CAMERA, "twin_lens": {"lens_k": -0.2, "lens_kcube": 0.03}}
        with pytest.raises(ValueError, match="lens overrides are held"):
            configure_twin_camera_blueprint(blueprint, measured, 1280, 960)


class TestMapGate:
    def test_rfs_map_supported(self):
        assert is_twin_supported_map("Carla/Maps/Richmond_Field_Station_Richmond_CA")
        assert is_twin_supported_map("Richmond_Field_Station_Richmond_CA")

    def test_other_maps_rejected(self):
        assert not is_twin_supported_map("San_Ramon_P1_Roads")
        assert not is_twin_supported_map("Carla/Maps/Town10HD")


class TestCamerasConfig:
    def test_repo_config_loads_all_channels(self):
        config = load_cameras_config()
        assert config is not None
        assert [c["id"] for c in config["cameras"]] == ["ch1", "ch2", "ch3", "ch4"]
        assert config["site"]["lat"] == pytest.approx(37.9156, abs=1e-3)

    def test_missing_file_returns_none(self):
        assert load_cameras_config("/nonexistent/cameras.json") is None


class TestComputeTransform:
    def test_candidate_pose_is_isolated_from_source(self):
        source = {**CAMERA, "twin_pose": {"forward_offset_m": 0.5}}
        candidate = camera_with_twin_pose(source, {"yaw_offset_deg": 2.0})
        assert candidate["twin_pose"]["yaw_offset_deg"] == 2.0
        assert "yaw_offset_deg" not in source["twin_pose"]

    def test_pole_height_and_rotation(self, mock_world, monkeypatch):
        monkeypatch.setattr(
            twin_camera_rig, "gps_to_carla", lambda m, lat, lon: MockLocation(10.0, 20.0, 1.5)
        )
        transform = compute_twin_camera_transform(mock_world.get_map(), SITE, CAMERA)
        assert transform.location.z == pytest.approx(1.5 + 7.0)
        assert transform.rotation.pitch == pytest.approx(-39.2)
        assert transform.rotation.yaw == pytest.approx(200.0 - 46.06 - 90.0)
        assert transform.rotation.roll == 0.0

    def test_full_pose_offsets_include_right_translation_and_roll(self, mock_world, monkeypatch):
        monkeypatch.setattr(
            twin_camera_rig, "gps_to_carla", lambda m, lat, lon: MockLocation(10.0, 20.0, 1.5)
        )
        camera = {
            **CAMERA,
            "heading_deg": 90.0,
            "yaw_deg": 0.0,
            "twin_pose": {
                "forward_offset_m": 2.0,
                "right_offset_m": 1.0,
                "height_offset_m": 0.5,
                "roll_offset_deg": 3.0,
            },
        }
        transform = compute_twin_camera_transform(mock_world.get_map(), SITE, camera)
        assert transform.location.x == pytest.approx(12.0)
        assert transform.location.y == pytest.approx(21.0)
        assert transform.location.z == pytest.approx(9.0)
        assert transform.rotation.roll == pytest.approx(3.0)

    def test_missing_forward_offset_has_no_hidden_translation(self, mock_world, monkeypatch):
        monkeypatch.setattr(
            twin_camera_rig,
            "gps_to_carla",
            lambda m, lat, lon: MockLocation(10.0, 20.0, 1.5),
        )
        camera = {**CAMERA, "twin_pose": {}}
        transform = compute_twin_camera_transform(mock_world.get_map(), SITE, camera)
        assert transform.location.x == pytest.approx(10.0)
        assert transform.location.y == pytest.approx(20.0)

    def test_absolute_fit_roundtrips_through_shared_production_math(self):
        base = {
            "pitch_deg": -39.2,
            "yaw_deg": 63.94,
            "roll_deg": 0.0,
            "fov_deg": 88.0,
        }
        anchor = [10.0, 20.0, 8.5]
        target = [11.3, 19.2, 9.1]
        pose = twin_pose_from_absolute(
            anchor, base, target, -35.0, 71.0, 2.0, 91.0
        )
        absolute = absolute_twin_model(anchor, base, pose)
        assert absolute["location"] == pytest.approx(target)
        assert absolute["pitch_deg"] == pytest.approx(-35.0)
        assert absolute["yaw_deg"] == pytest.approx(71.0)
        assert absolute["roll_deg"] == pytest.approx(2.0)
        assert absolute["fov_deg"] == pytest.approx(91.0)


class TestTwinCameraRig:
    def test_carla010_runtime_rig_requires_strict_opendrive_projection(
        self, mock_world, monkeypatch
    ):
        strict_map = object()
        calls = []

        def compute(carla_map, site, camera, **kwargs):
            calls.append(kwargs)
            return MockTransform(), {
                "source": "opendrive_georeference",
                "strict": True,
                "map_origin_error_m": 0.1,
                "map_name": "Carla/Maps/Richmond_Field_Station_Richmond_CA",
                "opendrive_sha256": "a" * 64,
                "georeference_sha256": "b" * 64,
            }

        monkeypatch.setattr(
            twin_camera_rig, "compute_twin_camera_transform", compute
        )
        rig = TwinCameraRig(mock_world, strict_map, make_config())

        assert rig.spawn() == 1
        assert calls == [{
            "require_opendrive_georeference": True,
            "return_projection_provenance": True,
        }]
        assert rig.status()["projection_provenance"]["ch1"]["source"] == (
            "opendrive_georeference"
        )

    def test_carla010_runtime_rig_fails_closed_on_projection_error(
        self, mock_world, monkeypatch
    ):
        monkeypatch.setattr(
            twin_camera_rig,
            "compute_twin_camera_transform",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("fallback projection")
            ),
        )
        rig = TwinCameraRig(mock_world, object(), make_config())

        assert rig.spawn() == 0
        assert rig.status()["spawn_failures"] == {
            "ch1": "projection_gate_failed"
        }

    def test_invalid_lens_configuration_fails_closed(self, mock_world):
        config = make_config()
        config["cameras"][0]["twin_lens"] = {"lens_k": "not-a-number"}
        rig = TwinCameraRig(mock_world, mock_world.get_map(), config)

        assert rig.spawn() == 0
        assert rig.camera_ids == []
        assert rig.status()["refused_cameras"] == {
            "ch1": "lens_override_safety_hold"
        }

    def test_even_baseline_matching_lens_override_is_refused(self, mock_world):
        config = make_config()
        config["cameras"][0]["twin_lens"] = {
            "lens_k": -1.0,
            "lens_kcube": 0.0,
            "lens_circle_falloff": 5.0,
            "lens_circle_multiplier": 0.0,
            "lens_x_size": 0.08,
            "lens_y_size": 0.08,
        }
        rig = TwinCameraRig(mock_world, mock_world.get_map(), config)

        assert rig.spawn() == 0
        assert rig.status()["refused_cameras"]["ch1"] == (
            "lens_override_safety_hold"
        )

    def test_mixed_config_refuses_only_unsafe_channel(
        self, mock_world, monkeypatch
    ):
        unsafe = {**CAMERA, "id": "ch1", "twin_lens": {"lens_k": -1.0}}
        safe = {**CAMERA, "id": "ch2"}
        monkeypatch.setattr(
            twin_camera_rig,
            "gps_to_carla",
            lambda *_args: MockLocation(0.0, 0.0, 0.0),
        )
        rig = TwinCameraRig(
            mock_world, mock_world.get_map(), make_config([unsafe, safe])
        )

        assert rig.spawn() == 1
        assert rig.camera_ids == ["ch2"]
        assert rig.status()["refused_cameras"] == {
            "ch1": "lens_override_safety_hold"
        }

    def test_nonpositive_fps_is_rejected(self, mock_world):
        with pytest.raises(ValueError, match="finite and positive"):
            TwinCameraRig(mock_world, mock_world.get_map(), make_config(), fps=0)

    def test_spawn_failure_is_machine_readable(
        self, mock_world, monkeypatch
    ):
        monkeypatch.setattr(
            twin_camera_rig,
            "gps_to_carla",
            lambda *_args: MockLocation(0.0, 0.0, 0.0),
        )
        monkeypatch.setattr(
            mock_world,
            "spawn_actor",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("injected spawn failure")
            ),
        )
        rig = TwinCameraRig(mock_world, mock_world.get_map(), make_config())

        assert rig.spawn() == 0
        assert rig.status()["spawn_failures"] == {
            "ch1": "spawn_actor_failed"
        }

    def test_spawn_frames_destroy(self, mock_world, monkeypatch):
        monkeypatch.setattr(
            twin_camera_rig, "gps_to_carla", lambda m, lat, lon: MockLocation(0.0, 0.0, 0.0)
        )
        rig = TwinCameraRig(
            mock_world,
            mock_world.get_map(),
            make_config(),
            frame_context_provider=lambda: {
                "mode": "replay",
                "replay_clock": "2026-07-10T18:23:19.735Z",
            },
        )
        assert rig.spawn() == 1
        assert rig.camera_ids == ["ch1"]
        assert rig.has_camera("ch1")
        assert not rig.has_camera("ch9")
        assert len(rig.actor_ids()) == 1
        camera_bp = mock_world.get_blueprint_library().find("sensor.camera.rgb")
        assert not any(
            key.startswith("lens_")
            for key, _value in camera_bp.set_attribute_calls
        )

        # No frame yet
        assert rig.get_latest_frame("ch1") is None

        # Push a frame through the listener path (bypassing JPEG encoding)
        monkeypatch.setattr(twin_camera_rig, "encode_jpeg", lambda image, quality=70: b"jpeg")
        listener = rig._cameras["ch1"]._listener
        image = type("Image", (), {"frame": 123, "timestamp": 45.25})()
        listener(image)
        assert rig.get_latest_frame("ch1") == b"jpeg"
        frame, metadata = rig.get_latest_frame_packet("ch1")
        assert frame == b"jpeg"
        assert metadata == {
            "camera_id": "ch1",
            "frame_count": 1,
            "carla_frame": 123,
            "sensor_timestamp": 45.25,
            "jpeg_sha256": "41e5787e9f28562d07b891b1816b492309d646c0f2829743fa4963a9f9cc1d61",
            "mode": "replay",
            "replay_clock": "2026-07-10T18:23:19.735Z",
        }
        assert rig.status()["frame_counts"]["ch1"] == 1
        model = rig.camera_model("ch1")
        assert model["camera_id"] == "ch1"
        assert model["actor_id"] in rig.actor_ids()
        assert len(model["config_sha256"]) == 64
        assert len(model["cameras_config_sha256"]) == 64
        assert model["image"]["width"] == 1280
        assert model["image"]["height"] == 960
        assert model["image"]["horizontal_fov_deg"] == pytest.approx(
            horizontal_fov_deg(CAMERA["intrinsics"]), abs=1e-6
        )
        assert model["lens"] == {
            "lens_k": -1.0,
            "lens_kcube": 0.0,
            "lens_circle_falloff": 5.0,
            "lens_circle_multiplier": 0.0,
            "lens_x_size": 0.08,
            "lens_y_size": 0.08,
        }
        assert model["transform"]["location"]["z"] == pytest.approx(7.0)
        assert rig.camera_model("ch9") is None

        rig._cameras["ch1"].attributes["fov"] = "87.5"
        assert rig.camera_model("ch1")["image"]["horizontal_fov_deg"] == 87.5

        rig._cameras["ch1"].attributes.pop("lens_k")
        assert rig.camera_model("ch1") is None
        assert rig.status()["camera_model_errors"]["ch1"] == (
            "observed_camera_model_incomplete"
        )

        rig._cameras["ch1"].attributes["lens_k"] = "nan"
        assert rig.camera_model("ch1") is None
        assert rig.status()["camera_model_errors"]["ch1"] == (
            "observed_camera_model_invalid"
        )

        rig.destroy()
        assert rig.camera_ids == []
        assert rig.get_latest_frame("ch1") is None

    def test_frames_ignored_after_destroy(self, mock_world, monkeypatch):
        monkeypatch.setattr(
            twin_camera_rig, "gps_to_carla", lambda m, lat, lon: MockLocation(0.0, 0.0, 0.0)
        )
        monkeypatch.setattr(twin_camera_rig, "encode_jpeg", lambda image, quality=70: b"jpeg")
        rig = TwinCameraRig(mock_world, mock_world.get_map(), make_config())
        rig.spawn()
        listener = rig._cameras["ch1"]._listener
        rig.destroy()
        listener(type("Image", (), {"frame": 124, "timestamp": 46.25})())
        assert rig.get_latest_frame("ch1") is None
