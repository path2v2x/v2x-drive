"""Tests for the Drive Server — TDD: tests first."""

import json
import asyncio
import os
import pytest
from unittest.mock import MagicMock, patch

from tests.conftest import (
    MockWorld,
    MockClient,
    MockWebSocket,
    MockVehicleControl,
    FakeV2XApi,
    SAMPLE_DETECTIONS,
)


@pytest.mark.unit
class TestDriveServerSession:
    """Unit tests for drive session lifecycle with mocked CARLA."""

    @pytest.mark.asyncio
    async def test_start_session_spawns_vehicle(self, mock_world, fake_v2x_api):
        """start_session should spawn a vehicle and return session_ready."""
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        result = await session.start(
            start="2026-03-22T17:00:00Z",
            end="2026-03-22T17:30:00Z",
        )

        assert result["type"] == "session_ready"
        assert result["vehicle_id"] is not None
        assert result["objects_count"] >= 0
        assert session.vehicle is not None

    @pytest.mark.asyncio
    async def test_apply_control(self, mock_world, fake_v2x_api):
        """Control messages should apply to the vehicle and return telemetry."""
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        telemetry = session.apply_control(steer=-0.5, throttle=0.8, brake=0.0)

        assert telemetry["type"] == "telemetry"
        assert "speed" in telemetry
        assert "pos" in telemetry
        assert "rot" in telemetry

        # Verify control was actually applied to the vehicle
        ctrl = session.vehicle.get_control()
        assert ctrl.steer == pytest.approx(-0.5, abs=1e-6)
        assert ctrl.throttle == pytest.approx(0.8, abs=1e-6)
        assert ctrl.brake == pytest.approx(0.0, abs=1e-6)

    @pytest.mark.asyncio
    async def test_end_session_cleans_up(self, mock_world, fake_v2x_api):
        """end_session should destroy vehicle, cleanup scene, restore settings."""
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        ready = await session.start(
            "2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z"
        )
        vehicle_id = session.vehicle.id
        owned_actor_ids = set(ready["owned_actor_ids"])
        assert vehicle_id in owned_actor_ids
        assert set(ready["scene_actor_ids"]).issubset(owned_actor_ids)
        assert set(ready["sensor_actor_ids"]).issubset(owned_actor_ids)

        session.end()

        # Every actor in the acceptance manifest should be destroyed.
        vehicle = mock_world.get_actor(vehicle_id)
        assert vehicle.is_destroyed
        assert all(mock_world.get_actor(actor_id).is_destroyed for actor_id in owned_actor_ids)
        assert session.vehicle is None
        assert mock_world.weather.sun_altitude_angle == 75.0

    @pytest.mark.asyncio
    async def test_set_weather_clamps_black_or_opaque_values(self, mock_world, fake_v2x_api):
        """Weather controls should not be able to make the drive feed unusable."""
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        response = session.set_weather({
            "sun_altitude_angle": -90,
            "fog_density": 100,
            "fog_distance": 0,
            "dust_storm": 100,
            "precipitation": float("inf"),
        })

        assert response["type"] == "weather_set"
        assert response["params"]["sun_altitude_angle"] == 10.0
        assert response["params"]["fog_density"] == 25.0
        assert response["params"]["fog_distance"] == 25.0
        assert response["params"]["dust_storm"] == 30.0
        assert response["params"]["precipitation"] == 0.0
        assert mock_world.weather.sun_altitude_angle == 10.0

    @pytest.mark.asyncio
    async def test_set_camera_settings_clamps_black_screen_values(self, mock_world, fake_v2x_api):
        """Camera settings should preserve postprocessing and bounded exposure."""
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        response = session.set_camera_settings({
            "image_size_x": 99999,
            "image_size_y": 1,
            "fov": 999,
            "enable_postprocess_effects": "false",
            "exposure_mode": ["bad"],
            "exposure_compensation": -99,
            "black_clip": 1,
            "white_clip": 1,
            "gamma": 10,
        })

        assert response["type"] == "camera_settings_set"
        assert response["width"] == 1280
        assert response["height"] == 480
        assert response["fov"] == 110.0
        assert session._camera_extra_attrs["enable_postprocess_effects"] == "true"
        assert session._camera_extra_attrs["exposure_mode"] == "histogram"
        assert session._camera_extra_attrs["exposure_compensation"] == "0.0"
        assert session._camera_extra_attrs["black_clip"] == "0.02"
        assert session._camera_extra_attrs["white_clip"] == "0.06"
        assert session._camera_extra_attrs["gamma"] == "2.4"

    @pytest.mark.asyncio
    async def test_control_before_start_raises(self, mock_world, fake_v2x_api):
        """Sending control before session starts should raise an error."""
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )

        with pytest.raises(RuntimeError, match="No active session"):
            session.apply_control(steer=0, throttle=0, brake=0)

    @pytest.mark.asyncio
    async def test_double_start_raises(self, mock_world, fake_v2x_api):
        """Starting a session while one is active should raise an error."""
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        with pytest.raises(RuntimeError, match="Session already active"):
            await session.start("2026-03-22T18:00:00Z", "2026-03-22T18:30:00Z")

    @pytest.mark.asyncio
    async def test_camera_switch(self, mock_world, fake_v2x_api):
        """Camera switch should update the active camera view."""
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        session.switch_camera("hood")
        assert session.active_camera == "hood"

        session.switch_camera("bird")
        assert session.active_camera == "bird"

    @pytest.mark.asyncio
    async def test_invalid_camera_view_raises(self, mock_world, fake_v2x_api):
        """Switching to an invalid camera view should raise."""
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        with pytest.raises(ValueError, match="Invalid camera view"):
            session.switch_camera("invalid_view")


@pytest.mark.unit
class TestDriveServerMessageHandling:
    """Test WebSocket message parsing and routing."""

    @pytest.mark.asyncio
    async def test_handle_start_session_message(self, mock_world, fake_v2x_api):
        """A start_session message should trigger session start."""
        from digital_twin_bridge.drive_server import DriveSession, handle_message

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        msg = {"type": "start_session", "start": "2026-03-22T17:00:00Z", "end": "2026-03-22T17:30:00Z"}
        response = await handle_message(session, msg)

        assert response["type"] == "session_ready"

    @pytest.mark.asyncio
    async def test_handle_control_message(self, mock_world, fake_v2x_api):
        """A control message should return telemetry."""
        from digital_twin_bridge.drive_server import DriveSession, handle_message

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await handle_message(session, {
            "type": "start_session",
            "start": "2026-03-22T17:00:00Z",
            "end": "2026-03-22T17:30:00Z",
        })

        response = await handle_message(session, {
            "type": "control",
            "s": 0.3,
            "t": 0.7,
            "b": 0.0,
        })

        assert response["type"] == "telemetry"

    @pytest.mark.asyncio
    async def test_handle_end_session_message(self, mock_world, fake_v2x_api):
        """An end_session message should clean up and confirm."""
        from digital_twin_bridge.drive_server import DriveSession, handle_message

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await handle_message(session, {
            "type": "start_session",
            "start": "2026-03-22T17:00:00Z",
            "end": "2026-03-22T17:30:00Z",
        })

        response = await handle_message(session, {"type": "end_session"})
        assert response["type"] == "session_ended"
        assert session.vehicle is None

    @pytest.mark.asyncio
    async def test_handle_unknown_message_type(self, mock_world, fake_v2x_api):
        """Unknown message types should return an error."""
        from digital_twin_bridge.drive_server import DriveSession, handle_message

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        response = await handle_message(session, {"type": "bogus"})
        assert response["type"] == "error"
        assert "unknown" in response["message"].lower() or "Unknown" in response["message"]

    @pytest.mark.asyncio
    async def test_handle_server_status_reports_active_sessions(self, mock_world, fake_v2x_api):
        from digital_twin_bridge import drive_server
        from digital_twin_bridge.drive_server import DriveSession, handle_message

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")
        drive_server._active_sessions.clear()
        drive_server._active_sessions.append(session)

        response = await handle_message(session, {"type": "server_status"})

        assert response == {
            "type": "server_status",
            "active_sessions": 1,
            "this_session_active": True,
            "mode": "driving",
            "editor_supported": True,
            "editor_sessions": 0,
            "editor_max_sessions": 2,
        }
        drive_server._active_sessions.clear()

    @pytest.mark.asyncio
    async def test_set_map_handler_uses_controller_only_when_idle(self, mock_world, fake_v2x_api):
        from digital_twin_bridge import drive_server
        from digital_twin_bridge.drive_server import DriveSession, handle_message

        class FakeMapController:
            world = mock_world
            carla_map = mock_world.get_map()
            trajectory_player = None
            openscenario_runner = None

            def status_payload(self):
                return {
                    "current_map": "richmond",
                    "current_map_name": "Richmond_Field_Station_Richmond_CA",
                    "maps": [
                        {"id": "richmond", "label": "Richmond", "map_name": "Richmond_Field_Station_Richmond_CA"},
                        {"id": "san_ramon", "label": "San Ramon", "map_name": "San_Ramon_P1_Roads"},
                    ],
                }

            async def switch_map(self, map_id):
                if drive_server.active_session_count() > 0:
                    raise RuntimeError("End the active drive session before switching maps")
                return {"type": "map_set", "current_map": map_id, "maps": self.status_payload()["maps"]}

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        controller = FakeMapController()
        drive_server._active_sessions.clear()

        idle_response = await handle_message(
            session,
            {"type": "set_map", "map": "san_ramon"},
            map_controller=controller,
        )
        assert idle_response["type"] == "map_set"
        assert idle_response["current_map"] == "san_ramon"

        drive_server._active_sessions.clear()
        await session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")
        drive_server._active_sessions.append(session)
        active_response = await handle_message(
            session,
            {"type": "set_map", "map": "richmond"},
            map_controller=controller,
        )
        assert active_response["type"] == "error"
        assert "active drive session" in active_response["message"]
        drive_server._active_sessions.clear()


@pytest.mark.unit
class TestScenarioStorageCompatibility:
    """Scenario file storage should remain compatible across repo moves."""

    def test_list_scenarios_includes_legacy_directory(self, tmp_path, monkeypatch):
        """Legacy pre-reorg scenarios should still appear in the picker."""
        from digital_twin_bridge import drive_server

        current_dir = tmp_path / "bridge" / "scenes"
        legacy_dir = tmp_path / "v2x-digital-twin-bridge" / "scenes"
        current_dir.mkdir(parents=True)
        legacy_dir.mkdir(parents=True)

        (legacy_dir / "legacy_scene.json").write_text(json.dumps({
            "name": "Legacy Scene",
            "objects": [{"blueprint": "static.prop.trafficcone01"}],
        }))

        monkeypatch.setattr(drive_server, "SCENARIOS_DIR", os.fspath(current_dir))
        monkeypatch.setattr(drive_server, "LEGACY_SCENARIOS_DIR", os.fspath(legacy_dir))

        scenarios = drive_server.list_scenarios()

        assert scenarios == [{
            "name": "Legacy Scene",
            "file": "legacy_scene.json",
            "object_count": 1,
            "zone_count": 0,
        }]

    def test_load_scenario_falls_back_to_legacy_directory(self, tmp_path, monkeypatch):
        """Loading should work even when the scenario only exists at the old path."""
        from digital_twin_bridge import drive_server

        current_dir = tmp_path / "bridge" / "scenes"
        legacy_dir = tmp_path / "v2x-digital-twin-bridge" / "scenes"
        current_dir.mkdir(parents=True)
        legacy_dir.mkdir(parents=True)

        payload = {
            "name": "Legacy Scene",
            "objects": [{"blueprint": "static.prop.trafficwarning", "pos": [1, 2, 3], "yaw": 45}],
        }
        (legacy_dir / "legacy_scene.json").write_text(json.dumps(payload))

        monkeypatch.setattr(drive_server, "SCENARIOS_DIR", os.fspath(current_dir))
        monkeypatch.setattr(drive_server, "LEGACY_SCENARIOS_DIR", os.fspath(legacy_dir))

        scenario = drive_server.load_scenario("legacy_scene.json")

        assert scenario == payload

    def test_current_directory_wins_when_duplicate_filenames_exist(self, tmp_path, monkeypatch):
        """Current scenarios should take precedence over legacy copies with the same filename."""
        from digital_twin_bridge import drive_server

        current_dir = tmp_path / "bridge" / "scenes"
        legacy_dir = tmp_path / "v2x-digital-twin-bridge" / "scenes"
        current_dir.mkdir(parents=True)
        legacy_dir.mkdir(parents=True)

        (current_dir / "shared_scene.json").write_text(json.dumps({
            "name": "Current Scene",
            "objects": [{"blueprint": "vehicle.tesla.model3"}],
        }))
        (legacy_dir / "shared_scene.json").write_text(json.dumps({
            "name": "Legacy Scene",
            "objects": [],
        }))

        monkeypatch.setattr(drive_server, "SCENARIOS_DIR", os.fspath(current_dir))
        monkeypatch.setattr(drive_server, "LEGACY_SCENARIOS_DIR", os.fspath(legacy_dir))

        scenarios = drive_server.list_scenarios()
        scenario = drive_server.load_scenario("shared_scene.json")

        assert scenarios == [{
            "name": "Current Scene",
            "file": "shared_scene.json",
            "object_count": 1,
            "zone_count": 0,
        }]
        assert scenario["name"] == "Current Scene"


@pytest.mark.unit
class TestScenarioZonePersistence:
    """Scenarios should round-trip V2X zones alongside placed objects."""

    def test_save_scenario_persists_zones(self, tmp_path, monkeypatch):
        from digital_twin_bridge import drive_server

        scenes = tmp_path / "scenes"
        scenes.mkdir()
        monkeypatch.setattr(drive_server, "SCENARIOS_DIR", os.fspath(scenes))
        monkeypatch.setattr(drive_server, "LEGACY_SCENARIOS_DIR", os.fspath(scenes))

        zones = [{
            "id": "z1",
            "name": "Main & 2nd",
            "message": "school zone",
            "signal_type": "warning",
            "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]],
            "color": "#ef4444",
        }]

        result = drive_server.save_scenario(
            name="School Zone Test",
            objects=[{"blueprint": "static.prop.trafficcone01", "pos": [0, 0, 0], "yaw": 0}],
            zones=zones,
        )

        assert result["zone_count"] == 1
        assert result["object_count"] == 1

        loaded = drive_server.load_scenario(result["file"])
        assert loaded["zones"] == zones
        assert loaded["objects"][0]["blueprint"] == "static.prop.trafficcone01"

    def test_save_scenario_without_zones_keeps_empty_list(self, tmp_path, monkeypatch):
        from digital_twin_bridge import drive_server

        scenes = tmp_path / "scenes"
        scenes.mkdir()
        monkeypatch.setattr(drive_server, "SCENARIOS_DIR", os.fspath(scenes))
        monkeypatch.setattr(drive_server, "LEGACY_SCENARIOS_DIR", os.fspath(scenes))

        result = drive_server.save_scenario(
            name="Objects Only",
            objects=[{"blueprint": "static.prop.trafficcone01", "pos": [0, 0, 0], "yaw": 0}],
        )

        loaded = drive_server.load_scenario(result["file"])
        assert loaded["zones"] == []
        assert result["zone_count"] == 0

    def test_load_scenario_handler_returns_zones_without_active_session(self, tmp_path, monkeypatch):
        """At idle (no session), load_scenario should return zones without spawning objects."""
        from digital_twin_bridge import drive_server

        scenes = tmp_path / "scenes"
        scenes.mkdir()
        monkeypatch.setattr(drive_server, "SCENARIOS_DIR", os.fspath(scenes))
        monkeypatch.setattr(drive_server, "LEGACY_SCENARIOS_DIR", os.fspath(scenes))

        zones = [{"id": "z1", "name": "Z", "message": "", "signal_type": "info",
                  "polygon": [[0, 0], [1, 0], [1, 1]], "color": "#3b82f6"}]
        drive_server.save_scenario(
            name="Idle Load",
            objects=[{"blueprint": "static.prop.trafficcone01", "pos": [0, 0, 0], "yaw": 0}],
            zones=zones,
        )

        session = MagicMock()
        session.is_active = False
        session._placed_objects = []

        response = asyncio.run(drive_server.handle_message(session, {
            "type": "load_scenario",
            "file": "idle_load.json",
        }))

        assert response["type"] == "scenario_loaded"
        assert response["zones"] == zones
        assert response["spawned"] == 0
        assert response["placed_count"] == 0
        # Should NOT have spawned objects when session is inactive
        session.load_scenario_objects.assert_not_called()


@pytest.mark.unit
class TestDynamicActorGeofences:
    def test_wheel_count_parses_real_carla_actor_attribute_string(self):
        from digital_twin_bridge.drive_server import blueprint_wheel_count
        from tests.conftest import MockBlueprintAttribute, MockBlueprint

        class RealisticActorAttribute(MockBlueprintAttribute):
            recommended_values = []

            def __init__(self):
                self.value = "4"
                self.recommended_values = []

            def as_int(self):
                return 4

            def __str__(self):
                return "ActorAttribute(id=number_of_wheels,type=int,value=4(const))"

        bp = MockBlueprint(
            "vehicle.carlamotors.firetruck",
            {"number_of_wheels": RealisticActorAttribute()},
        )

        assert blueprint_wheel_count(bp) == 4

    @pytest.mark.asyncio
    async def test_spawn_dynamic_actor_sets_autopilot_and_emits_telemetry(self, mock_world, fake_v2x_api):
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        result = session.spawn_dynamic_actor(
            blueprint_id="vehicle.carlamotors.firetruck",
            geofence_radius=42.0,
            message="Firefighter response vehicle active",
        )

        assert result["type"] == "dynamic_actor_spawned"
        actor_id = result["actor"]["actor_id"]
        actor = mock_world.get_actor(actor_id)
        assert actor is not None
        assert actor.type_id == "vehicle.carlamotors.firetruck"
        assert actor.autopilot_enabled is True
        assert actor.traffic_manager_port == 8000
        assert result["actor"]["geofence_radius"] == 42.0

        telemetry = session.apply_control(steer=0.0, throttle=0.0, brake=0.0)
        assert telemetry["dynamic_actors"][0]["actor_id"] == actor_id
        assert telemetry["dynamic_actors"][0]["message"] == "Firefighter response vehicle active"
        assert telemetry["nearby_actors"][0]["type"] == "dynamic"

    @pytest.mark.asyncio
    async def test_dynamic_actor_geofence_is_drawn_in_carla(self, mock_world, fake_v2x_api):
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")
        spawned = session.spawn_dynamic_actor(
            blueprint_id="vehicle.carlamotors.firetruck",
            geofence_radius=20.0,
            message="Firefighter response vehicle active",
        )

        actor = mock_world.get_actor(spawned["actor"]["actor_id"])
        actor.set_transform(type(actor.get_transform())(
            location=type(actor.get_transform().location)(10, 20, 0),
            rotation=actor.get_transform().rotation,
        ))

        session.apply_control(steer=0.0, throttle=0.0, brake=0.0)

        assert len(mock_world.debug.lines) == 32
        first_line = mock_world.debug.lines[0]
        assert first_line["thickness"] == 0.12
        assert first_line["life_time"] == 0.25
        assert first_line["color"].r == 255
        assert first_line["color"].g == 60
        assert first_line["color"].b == 60

    @pytest.mark.asyncio
    async def test_dynamic_actor_radius_is_clamped(self, mock_world, fake_v2x_api):
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        small = session.spawn_dynamic_actor("vehicle.tesla.model3", geofence_radius=1.0)
        large = session.spawn_dynamic_actor("vehicle.tesla.model3", geofence_radius=999.0)

        assert small["actor"]["geofence_radius"] == 5.0
        assert large["actor"]["geofence_radius"] == 250.0

    @pytest.mark.asyncio
    async def test_despawn_dynamic_actor_disables_autopilot_and_removes_snapshot(self, mock_world, fake_v2x_api):
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")
        spawned = session.spawn_dynamic_actor("vehicle.carlamotors.firetruck", geofence_radius=35.0)
        actor_id = spawned["actor"]["actor_id"]
        actor = mock_world.get_actor(actor_id)

        result = session.despawn_dynamic_actor(actor_id)

        assert result == {"type": "dynamic_actor_despawned", "actor_id": actor_id, "count": 0}
        assert actor.autopilot_enabled is False
        assert actor.is_destroyed is True
        assert session.get_dynamic_actors_snapshot() == []

    @pytest.mark.asyncio
    async def test_spawn_dynamic_actor_cleans_up_when_autopilot_fails(self, mock_world, fake_v2x_api, monkeypatch):
        from digital_twin_bridge.drive_server import DriveSession, _dynamic_actor_ids

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")

        original_try_spawn_actor = mock_world.try_spawn_actor
        spawned_actor = {}

        def spawn_with_broken_autopilot(blueprint, transform, attach_to=None):
            actor = original_try_spawn_actor(blueprint, transform, attach_to)
            if getattr(blueprint, "id", "") == "vehicle.carlamotors.firetruck":
                spawned_actor["actor"] = actor

                def fail_autopilot(enabled, tm_port=None):
                    raise RuntimeError("traffic manager unavailable")

                actor.set_autopilot = fail_autopilot
            return actor

        monkeypatch.setattr(mock_world, "try_spawn_actor", spawn_with_broken_autopilot)

        with pytest.raises(RuntimeError, match="traffic manager unavailable"):
            session.spawn_dynamic_actor("vehicle.carlamotors.firetruck", geofence_radius=35.0)

        actor = spawned_actor["actor"]
        assert actor.is_destroyed is True
        assert actor.id not in _dynamic_actor_ids
        assert session.get_dynamic_actors_snapshot() == []

    @pytest.mark.asyncio
    async def test_handle_spawn_dynamic_actor_message(self, mock_world, fake_v2x_api):
        from digital_twin_bridge.drive_server import DriveSession, handle_message

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=fake_v2x_api.get_detections_range,
        )
        await handle_message(session, {
            "type": "start_session",
            "start": "2026-03-22T17:00:00Z",
            "end": "2026-03-22T17:30:00Z",
        })

        response = await handle_message(session, {
            "type": "spawn_dynamic_actor",
            "blueprint": "vehicle.carlamotors.firetruck",
            "geofence_radius": 45,
            "message": "Firefighter route active",
        })

        assert response["type"] == "dynamic_actor_spawned"
        assert response["actor"]["blueprint"] == "vehicle.carlamotors.firetruck"
        assert response["actor"]["geofence_radius"] == 45.0
        assert response["actor"]["message"] == "Firefighter route active"
