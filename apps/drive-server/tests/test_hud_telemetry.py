"""The driving HUD block of the telemetry: sensors on the ego, fallbacks, collisions."""

import time
import types

import carla
import pytest

from digital_twin_bridge.drive_server import DriveSession

START, END = "2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z"


def make_session(mock_world, fake_v2x_api, hud_sensors=True):
    return DriveSession(world=mock_world, carla_map=mock_world.get_map(),
                        api_fetcher=fake_v2x_api.get_detections_range, hud_sensors=hud_sensors)


def sensors_of(mock_world, type_id):
    return [a for a in mock_world.spawned_actors if a.type_id == type_id]


@pytest.mark.asyncio
async def test_hud_block_comes_from_the_imu_and_collision_sensors(mock_world, fake_v2x_api):
    session = make_session(mock_world, fake_v2x_api)
    ready = await session.start(START, END)
    imu = sensors_of(mock_world, "sensor.other.imu")
    col = sensors_of(mock_world, "sensor.other.collision")
    assert len(imu) == 1 and len(col) == 1
    assert sorted(ready["sensor_actor_ids"]) == sorted(session.sensor_actor_ids()) and len(ready["sensor_actor_ids"]) == 3

    # the sensors report through their callbacks, like CARLA does from its own thread
    imu[0]._listener(types.SimpleNamespace(accelerometer=carla.Vector3D(0.5, -0.25, 9.81),
                                            gyroscope=carla.Vector3D(0.01, 0.02, -0.3)))
    col[0]._listener(types.SimpleNamespace(normal_impulse=carla.Vector3D(3.0, 4.0, 0.0),
                                            other_actor=types.SimpleNamespace(type_id="static.prop.trafficcone01")))
    col[0]._listener(types.SimpleNamespace(normal_impulse=carla.Vector3D(1.0, 0.0, 0.0),
                                            other_actor=types.SimpleNamespace(type_id="vehicle.lincoln.mkz")))
    session.vehicle.set_transform(carla.Transform(carla.Location(x=10.0, y=20.0, z=6.5), carla.Rotation(yaw=-90.0)))
    mock_world.tick()
    mock_world.tick()

    hud = session.apply_control(steer=0.0, throttle=0.5, brake=0.0)["hud"]
    assert hud["imu"] is True and hud["accel"] == [0.5, -0.25, 9.81] and hud["gyro"] == [0.01, 0.02, -0.3]
    assert hud["compass"] == 0.0            # yaw -90 is north
    assert hud["frame"] == 2 and hud["sim_time"] == 0.1
    assert hud["height"] == 6.5 and hud["gnss"] is not None and len(hud["gnss"]) == 2
    assert hud["collision"] == {"intensity": 5.0, "with": "Prop Trafficcone01", "count": 2}  # the hardest hit
    assert hud["vehicle"] == "Tesla Model3" and hud["map"] == "TestMap"
    assert hud["vehicles"] == 1 and hud["hand_brake"] is False and hud["manual"] is False
    assert isinstance(hud["server_fps"], float)

    # collisions are reported once; the sensors go away with the session
    assert session.apply_control(steer=0.0, throttle=0.5, brake=0.0)["hud"]["collision"] is None
    session.end()
    assert imu[0].is_destroyed and col[0].is_destroyed and session.sensor_actor_ids() == []


@pytest.mark.asyncio
async def test_hud_block_without_sensors_falls_back_to_vehicle_state(mock_world, fake_v2x_api):
    session = make_session(mock_world, fake_v2x_api, hud_sensors=False)
    ready = await session.start(START, END)
    assert len(ready["sensor_actor_ids"]) == 1 and sensors_of(mock_world, "sensor.other.imu") == []
    session.vehicle.set_transform(carla.Transform(carla.Location(x=0.0, y=0.0, z=1.0), carla.Rotation(yaw=0.0)))
    hud = session.apply_control(steer=0.0, throttle=0.0, brake=0.0)["hud"]
    assert hud["imu"] is False and hud["accel"] == [0.0, 0.0, 0.0] and hud["gyro"] == [0.0, 0.0, 0.0]
    assert hud["compass"] == 90.0 and hud["collision"] is None   # yaw 0 is east
    # a velocity change between two frames shows up as acceleration
    session.vehicle.set_target_velocity(carla.Vector3D(2.0, 0.0, 0.0))
    time.sleep(0.01)  # telemetry frames are 50 ms apart in use
    hud = session.apply_control(steer=0.0, throttle=0.0, brake=0.0)["hud"]
    assert hud["accel"][0] > 0.0 and hud["accel"][1] == 0.0
    nearby = session.apply_control(steer=0.0, throttle=0.0, brake=0.0)["nearby_actors"]
    assert nearby == []  # only the ego is in the world
    session.end()
