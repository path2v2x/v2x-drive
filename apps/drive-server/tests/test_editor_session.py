"""Editor sessions: a free camera, fly moves, focus and follow, the outliner, the guards."""

import types

import carla
import pytest

from digital_twin_bridge import drive_server
from digital_twin_bridge.drive_server import DriveSession, handle_message

START, END = "2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z"


def make_session(mock_world, fake_v2x_api):
    return DriveSession(world=mock_world, carla_map=mock_world.get_map(), api_fetcher=fake_v2x_api.get_detections_range)


def cameras(mock_world):
    return [a for a in mock_world.spawned_actors if a.type_id == "sensor.camera.rgb" and not a.is_destroyed]


def spawn_car(world, role, x=50.0):
    bp = types.SimpleNamespace(id="vehicle.lincoln.mkz", _attributes={"role_name": role})
    return world.try_spawn_actor(bp, carla.Transform(carla.Location(x=x, y=0.0, z=0.5), carla.Rotation(yaw=90.0)))


@pytest.mark.asyncio
async def test_editor_session_has_a_free_camera_and_no_car(mock_world, fake_v2x_api):
    session = make_session(mock_world, fake_v2x_api)
    ready = await handle_message(session, {"type": "start_session", "mode": "editor"})
    assert ready["type"] == "session_ready" and ready["mode"] == "editor" and ready["vehicle_id"] is None
    assert session.is_active and session.mode == "editor" and session.vehicle is None
    cams = cameras(mock_world)
    assert len(cams) == 1 and cams[0].attributes["role_name"] == session._editor_role
    assert ready["sensor_actor_ids"] == [cams[0].id] and ready["camera"]["type"] == "camera_pose"
    # above the first spawn point, looking down
    sp = mock_world.get_map().get_spawn_points()[0]
    assert ready["camera"]["z"] == pytest.approx(sp.location.z + 30.0) and ready["camera"]["pitch"] == -35.0
    assert cams[0].get_transform().location.z == pytest.approx(sp.location.z + 30.0)
    # frames from the free camera are accepted like the driving camera's
    assert session._accepting_frames is True

    # car-only actions are refused with a code the client can show
    for msg in ({"type": "control", "s": 0, "t": 0.5, "b": 0}, {"type": "respawn"}, {"type": "camera_switch", "view": "bird"},
                {"type": "camera_zoom", "factor": 1.2}, {"type": "teleport", "x": 0, "y": 0}):
        reply = await handle_message(session, msg)
        assert reply["type"] == "error" and reply["code"] == "needs_vehicle", msg

    status = await handle_message(session, {"type": "server_status"})
    assert status["mode"] == "editor" and status["editor_supported"] is True
    session.end()
    assert cams[0].is_destroyed and session.mode == "driving" and not session.is_active


@pytest.mark.asyncio
async def test_fly_moves_focus_and_follow(mock_world, fake_v2x_api):
    session = make_session(mock_world, fake_v2x_api)
    await handle_message(session, {"type": "start_session", "mode": "editor",
                                   "pose": {"x": 0.0, "y": 0.0, "z": 20.0, "yaw": 0.0, "pitch": 0.0}})
    cam = cameras(mock_world)[0]
    pose = await handle_message(session, {"type": "camera_move", "f": 1.0, "dt": 0.5, "speed": 10.0})
    assert pose["type"] == "camera_pose" and pose["x"] == pytest.approx(5.0) and pose["speed"] == 10.0
    assert cam.get_transform().location.x == pytest.approx(5.0)
    pose = await handle_message(session, {"type": "camera_move", "dyaw": 90.0, "dpitch": -10.0, "dt": 0.05})
    assert pose["yaw"] == 90.0 and pose["pitch"] == -10.0 and cam.get_transform().rotation.yaw == 90.0
    idle = await handle_message(session, {"type": "camera_move"})
    assert idle["x"] == pose["x"] and idle["following"] is None

    # focus: behind and above the car, looking at it
    car = spawn_car(mock_world, "PATH-M-1", x=50.0)
    pose = await handle_message(session, {"type": "camera_focus", "actor_id": car.id})
    assert pose["x"] == pytest.approx(50.0) and pose["y"] == pytest.approx(-12.0) and pose["z"] == pytest.approx(5.5)
    assert pose["yaw"] == pytest.approx(90.0, abs=0.1) and pose["pitch"] < 0

    # follow: the camera is re-created on the car; any input takes it back as a free camera
    before = cam.id
    pose = await handle_message(session, {"type": "camera_follow", "actor_id": car.id})
    assert pose["following"] == car.id and cam.is_destroyed
    riding = cameras(mock_world)[0]
    assert riding.id != before and session.sensor_actor_ids() == [riding.id]
    listing = await handle_message(session, {"type": "list_world_actors"})
    assert listing["following"] == car.id
    pose = await handle_message(session, {"type": "camera_move", "f": 1.0, "dt": 0.1})
    assert pose["following"] is None and riding.is_destroyed and len(cameras(mock_world)) == 1
    # explicit unfollow is harmless when free
    assert (await handle_message(session, {"type": "camera_follow", "actor_id": None}))["following"] is None
    # a dead follow target drops the follow on the next input
    await handle_message(session, {"type": "camera_follow", "actor_id": car.id})
    car.is_alive = False
    pose = await handle_message(session, {"type": "camera_move"})
    assert pose["following"] is None
    missing = await handle_message(session, {"type": "camera_focus", "actor_id": 424242})
    assert missing["type"] == "error"
    session.end()


@pytest.mark.asyncio
async def test_outliner_and_details_through_the_socket(mock_world, fake_v2x_api):
    session = make_session(mock_world, fake_v2x_api)
    await handle_message(session, {"type": "start_session", "mode": "editor"})
    car = spawn_car(mock_world, "bkg", x=10.0)
    listing = await handle_message(session, {"type": "list_world_actors", "max": 50})
    assert listing["type"] == "world_actors" and listing["truncated"] is False
    groups = {a["id"]: a["group"] for a in listing["actors"]}
    assert groups[car.id] == "foreign" and groups[session._camera_sensor.id] == "sensor"
    details = await handle_message(session, {"type": "inspect_actor", "actor_id": car.id})
    assert details["type"] == "actor_details" and details["name"] == "Lincoln Mkz" and "control" in details
    assert (await handle_message(session, {"type": "inspect_actor", "actor_id": 9999}))["type"] == "error"
    session.end()


@pytest.mark.asyncio
async def test_editor_sessions_are_capped(mock_world, fake_v2x_api, monkeypatch):
    monkeypatch.setenv("DTB_EDITOR_MAX_SESSIONS", "1")
    first = make_session(mock_world, fake_v2x_api)
    second = make_session(mock_world, fake_v2x_api)
    drive_server._active_sessions.extend([first, second])
    try:
        first.start_editor()
        assert drive_server.editor_session_count() == 1
        with pytest.raises(RuntimeError, match="limit"):
            second.start_editor()
        assert not second.is_active and len(cameras(mock_world)) == 1
        first.end()
        second.start_editor()
        assert second.is_active
        second.end()
    finally:
        drive_server._active_sessions.remove(first)
        drive_server._active_sessions.remove(second)


@pytest.mark.asyncio
async def test_driving_session_can_start_at_a_requested_pose(mock_world, fake_v2x_api):
    session = make_session(mock_world, fake_v2x_api)
    ready = await handle_message(session, {"type": "start_session", "start": START, "end": END,
                                           "spawn": {"x": 123.0, "y": -45.0, "yaw": 30.0}})
    assert ready["type"] == "session_ready" and "mode" not in ready
    loc = session.vehicle.get_transform().location
    assert (loc.x, loc.y) == (123.0, -45.0) and session.vehicle.get_transform().rotation.yaw == 30.0
    session.end()
