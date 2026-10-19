"""Fly camera for the Editor mode: pure pose maths, no CARLA objects.

CARLA's frame is left-handed: x forward (east), y right (south), z up; yaw turns
clockwise seen from above, pitch positive is nose up, both in degrees. The camera
moves along its own view direction (W / S), sideways (A / D) and along the world
z axis (E / Q), like the viewport of the Unreal editor; the mouse turns it. The
position is clamped to an envelope around the map's spawn points so a runaway
camera cannot leave the world.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

SPEED_MIN = 1.0
SPEED_MAX = 200.0
SPEED_DEFAULT = 20.0
PITCH_LIMIT = 89.0
BOOST = 4.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def wrap_yaw(deg: float) -> float:
    """Yaw folded into [-180, 180)."""
    return ((float(deg) + 180.0) % 360.0) - 180.0


@dataclass
class Envelope:
    min_x: float
    max_x: float
    min_y: float
    max_y: float
    min_z: float = -50.0
    max_z: float = 600.0

    @classmethod
    def from_points(cls, points: Iterable, margin_xy: float = 300.0, min_z: float = -50.0,
                    max_z: float = 600.0) -> "Envelope":
        xs, ys = [], []
        for p in points:
            loc = getattr(p, "location", p)
            xs.append(float(loc.x))
            ys.append(float(loc.y))
        if not xs:
            return cls(-5000.0, 5000.0, -5000.0, 5000.0, min_z, max_z)
        return cls(min(xs) - margin_xy, max(xs) + margin_xy, min(ys) - margin_xy, max(ys) + margin_xy, min_z, max_z)

    def clamp(self, x: float, y: float, z: float) -> Tuple[float, float, float]:
        return (_clamp(x, self.min_x, self.max_x), _clamp(y, self.min_y, self.max_y), _clamp(z, self.min_z, self.max_z))


@dataclass
class CameraPose:
    x: float = 0.0
    y: float = 0.0
    z: float = 30.0
    yaw: float = 0.0
    pitch: float = -30.0

    def to_dict(self) -> dict:
        return {"x": round(self.x, 2), "y": round(self.y, 2), "z": round(self.z, 2),
                "yaw": round(wrap_yaw(self.yaw), 1), "pitch": round(self.pitch, 1)}

    def forward(self) -> Tuple[float, float, float]:
        yaw, pitch = math.radians(self.yaw), math.radians(self.pitch)
        return (math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch))

    def right(self) -> Tuple[float, float, float]:
        yaw = math.radians(self.yaw)
        return (-math.sin(yaw), math.cos(yaw), 0.0)


def pose_from_dict(data: Optional[dict], default: CameraPose) -> CameraPose:
    """A pose from a client dict (``x y z yaw pitch``), missing or bad fields from ``default``."""
    pose = CameraPose(default.x, default.y, default.z, default.yaw, default.pitch)
    if not isinstance(data, dict):
        return pose
    for key in ("x", "y", "z", "yaw", "pitch"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            setattr(pose, key, float(value))
    pose.yaw = wrap_yaw(pose.yaw)
    pose.pitch = _clamp(pose.pitch, -PITCH_LIMIT, PITCH_LIMIT)
    return pose


class FlyCamera:
    """Integrates fly inputs into a pose; the session copies the pose onto the sensor."""

    def __init__(self, pose: CameraPose, speed: float = SPEED_DEFAULT, envelope: Optional[Envelope] = None):
        self.pose = pose
        self.speed = _clamp(float(speed), SPEED_MIN, SPEED_MAX)
        self.envelope = envelope
        self.set_pose(pose)

    def set_speed(self, speed) -> float:
        try:
            self.speed = _clamp(float(speed), SPEED_MIN, SPEED_MAX)
        except (TypeError, ValueError):
            pass
        return self.speed

    def set_pose(self, pose: CameraPose) -> CameraPose:
        x, y, z = pose.x, pose.y, pose.z
        if self.envelope is not None:
            x, y, z = self.envelope.clamp(x, y, z)
        self.pose = CameraPose(x, y, z, wrap_yaw(pose.yaw), _clamp(pose.pitch, -PITCH_LIMIT, PITCH_LIMIT))
        return self.pose

    def move(self, forward: float = 0.0, right: float = 0.0, up: float = 0.0, dyaw: float = 0.0,
             dpitch: float = 0.0, dt: float = 0.05, boost: bool = False) -> CameraPose:
        """One input frame: axes in [-1, 1] (fractions of the speed), turns in degrees, ``dt`` seconds."""
        f = _clamp(float(forward), -1.0, 1.0)
        r = _clamp(float(right), -1.0, 1.0)
        u = _clamp(float(up), -1.0, 1.0)
        dt = _clamp(float(dt), 0.0, 0.5)
        self.pose.yaw = wrap_yaw(self.pose.yaw + _clamp(float(dyaw), -180.0, 180.0))
        self.pose.pitch = _clamp(self.pose.pitch + _clamp(float(dpitch), -180.0, 180.0), -PITCH_LIMIT, PITCH_LIMIT)
        distance = self.speed * (BOOST if boost else 1.0) * dt
        fx, fy, fz = self.pose.forward()
        rx, ry, _ = self.pose.right()
        x = self.pose.x + (fx * f + rx * r) * distance
        y = self.pose.y + (fy * f + ry * r) * distance
        z = self.pose.z + (fz * f + u) * distance
        if self.envelope is not None:
            x, y, z = self.envelope.clamp(x, y, z)
        self.pose.x, self.pose.y, self.pose.z = x, y, z
        return self.pose


def look_at(from_xyz: Tuple[float, float, float], to_xyz: Tuple[float, float, float]) -> Tuple[float, float]:
    """Yaw and pitch (degrees) that point from one location at another."""
    dx, dy, dz = (to_xyz[0] - from_xyz[0], to_xyz[1] - from_xyz[1], to_xyz[2] - from_xyz[2])
    yaw = math.degrees(math.atan2(dy, dx))
    pitch = math.degrees(math.atan2(dz, math.hypot(dx, dy)))
    return wrap_yaw(yaw), _clamp(pitch, -PITCH_LIMIT, PITCH_LIMIT)


def pose_behind(x: float, y: float, z: float, yaw_deg: float, distance: float = 12.0,
                height: float = 5.0) -> CameraPose:
    """A viewpoint behind and above a point that faces ``yaw``, looking at it (the Focus pose)."""
    rad = math.radians(yaw_deg)
    cx, cy, cz = x - math.cos(rad) * distance, y - math.sin(rad) * distance, z + height
    look_yaw, look_pitch = look_at((cx, cy, cz), (x, y, z + 1.0))
    return CameraPose(cx, cy, cz, look_yaw, look_pitch)
