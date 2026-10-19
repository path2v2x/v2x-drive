"""Fly camera maths for the Editor mode."""

import math
import types

import pytest

from digital_twin_bridge.editor_camera import (
    PITCH_LIMIT,
    SPEED_MAX,
    SPEED_MIN,
    CameraPose,
    Envelope,
    FlyCamera,
    look_at,
    pose_behind,
    pose_from_dict,
    wrap_yaw,
)


def point(x, y, z=0.0):
    return types.SimpleNamespace(location=types.SimpleNamespace(x=x, y=y, z=z))


def test_envelope_from_spawn_points_and_the_empty_fallback():
    env = Envelope.from_points([point(0, 0), point(100, -50)], margin_xy=10.0)
    assert (env.min_x, env.max_x, env.min_y, env.max_y) == (-10.0, 110.0, -60.0, 10.0)
    assert env.clamp(500.0, -500.0, 1000.0) == (110.0, -60.0, 600.0)
    wide = Envelope.from_points([])
    assert wide.clamp(0.0, 0.0, -100.0) == (0.0, 0.0, -50.0)


def test_moves_along_the_view_and_turns_with_limits():
    cam = FlyCamera(CameraPose(0.0, 0.0, 10.0, 0.0, 0.0), speed=10.0)
    cam.move(forward=1.0, dt=0.5)                 # 5 m east
    assert (cam.pose.x, cam.pose.y, cam.pose.z) == pytest.approx((5.0, 0.0, 10.0))
    cam.move(right=1.0, dt=0.5)                   # 5 m to the right of east is south (+y)
    assert (cam.pose.x, cam.pose.y) == pytest.approx((5.0, 5.0))
    cam.move(up=-1.0, dt=0.5)                     # Q: down the world axis
    assert cam.pose.z == pytest.approx(5.0)
    cam.move(dyaw=90.0)                           # now facing south
    cam.move(forward=1.0, dt=1.0)                 # a frame longer than 0.5 s counts as 0.5 s
    assert (cam.pose.x, cam.pose.y) == pytest.approx((5.0, 10.0))
    cam.move(dpitch=-200.0)                       # pitch stays inside the limit
    assert cam.pose.pitch == -PITCH_LIMIT
    cam.move(forward=1.0, dt=0.5, boost=True)     # looking almost straight down, boosted: mostly descends
    assert cam.pose.z < 5.0 - 15.0
    cam.move(dyaw=300.0)
    assert -180.0 <= cam.pose.yaw < 180.0 and wrap_yaw(390.0) == 30.0
    assert cam.set_speed(0.1) == SPEED_MIN and cam.set_speed(10_000) == SPEED_MAX and cam.set_speed("x") == SPEED_MAX
    cam.move(forward=5.0, dt=5.0)                 # inputs and dt are clamped
    assert math.isfinite(cam.pose.x)


def test_envelope_clamps_the_position():
    env = Envelope(-10.0, 10.0, -10.0, 10.0, 0.0, 50.0)
    cam = FlyCamera(CameraPose(0.0, 0.0, 10.0, 0.0, 0.0), speed=100.0, envelope=env)
    cam.move(forward=1.0, dt=0.5)
    assert cam.pose.x == 10.0
    cam.set_pose(CameraPose(-99.0, 99.0, -5.0, 720.0, 100.0))
    assert (cam.pose.x, cam.pose.y, cam.pose.z, cam.pose.yaw, cam.pose.pitch) == (-10.0, 10.0, 0.0, 0.0, PITCH_LIMIT)


def test_focus_pose_looks_at_the_actor_from_behind_and_above():
    pose = pose_behind(100.0, 0.0, 2.0, yaw_deg=0.0, distance=12.0, height=5.0)
    assert (pose.x, pose.y, pose.z) == pytest.approx((88.0, 0.0, 7.0))
    assert pose.yaw == pytest.approx(0.0)
    assert -30.0 < pose.pitch < 0.0            # looking slightly down at it
    yaw, pitch = look_at((0.0, 0.0, 0.0), (0.0, 10.0, 10.0))
    assert (yaw, pitch) == pytest.approx((90.0, 45.0))
    d = pose.to_dict()
    assert set(d) == {"x", "y", "z", "yaw", "pitch"}


def test_pose_from_client_dict_falls_back_per_field():
    default = CameraPose(1.0, 2.0, 3.0, 4.0, 5.0)
    assert pose_from_dict(None, default) == default
    pose = pose_from_dict({"x": 10, "yaw": 370.0, "pitch": 500, "z": "no", "nan": 1.0}, default)
    assert (pose.x, pose.y, pose.z, pose.yaw, pose.pitch) == (10.0, 2.0, 3.0, 10.0, PITCH_LIMIT)
    assert pose_from_dict({"x": float("nan")}, default).x == 1.0
