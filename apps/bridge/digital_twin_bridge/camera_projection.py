"""Shared nominal-pinhole projection for the production twin camera model.

The module is deliberately CARLA-free.  Coordinates use CARLA's x-forward,
y-right, z-up convention and the centered-principal-point, zero-distortion
Tier-B optical gauge.
"""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np

from .twin_camera_rig import absolute_twin_model, twin_pose_from_absolute


PARAMETER_NAMES = (
    "location_x", "location_y", "location_z",
    "pitch_deg", "yaw_deg", "roll_deg", "fov_deg",
)


def as_parameters(values: Iterable[float]) -> np.ndarray:
    result = np.asarray(tuple(values), dtype=float)
    if result.shape != (7,) or not np.isfinite(result).all():
        raise ValueError("camera parameters must contain seven finite values")
    if not 1.0 < result[6] < 179.0:
        raise ValueError("camera horizontal FOV must be in (1, 179) degrees")
    return result


def rotation_matrix(pitch_deg: float, yaw_deg: float, roll_deg: float) -> np.ndarray:
    pitch, yaw, roll = np.radians([pitch_deg, yaw_deg, roll_deg])
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    return np.asarray([
        [cp * cy, cy * sp * sr - sy * cr, -cy * sp * cr - sy * sr],
        [cp * sy, sy * sp * sr + cy * cr, -sy * sp * cr + cy * sr],
        [sp, -cp * sr, cp * cr],
    ])


def focal_pixels(fov_deg: float, width: int) -> float:
    if int(width) <= 0 or not 1.0 < float(fov_deg) < 179.0:
        raise ValueError("projection width or FOV is invalid")
    return (float(width) / 2.0) / math.tan(math.radians(float(fov_deg)) / 2.0)


def project_world(
    world_xyz: np.ndarray, parameters: Iterable[float], width: int, height: int
) -> tuple[np.ndarray, np.ndarray]:
    params = as_parameters(parameters)
    points = np.asarray(world_xyz, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
        raise ValueError("world points must be a finite Nx3 array")
    if int(width) <= 0 or int(height) <= 0:
        raise ValueError("projection dimensions must be positive")
    local = (rotation_matrix(*params[3:6]).T @ (points - params[:3]).T).T
    depth = local[:, 0]
    focal = focal_pixels(params[6], width)
    with np.errstate(divide="ignore", invalid="ignore"):
        pixels = np.column_stack((
            float(width) / 2.0 + focal * local[:, 1] / depth,
            float(height) / 2.0 - focal * local[:, 2] / depth,
        ))
    return pixels, depth


def project_direction(
    world_direction: Iterable[float], parameters: Iterable[float], width: int, height: int
) -> np.ndarray:
    params = as_parameters(parameters)
    direction = np.asarray(tuple(world_direction), dtype=float)
    if direction.shape != (3,) or not np.isfinite(direction).all():
        raise ValueError("world direction must contain three finite values")
    norm = float(np.linalg.norm(direction))
    if norm <= 1e-12:
        raise ValueError("world direction is degenerate")
    local = rotation_matrix(*params[3:6]).T @ (direction / norm)
    if local[0] <= 1e-9:
        return np.asarray((math.nan, math.nan))
    focal = focal_pixels(params[6], width)
    return np.asarray((
        float(width) / 2.0 + focal * local[1] / local[0],
        float(height) / 2.0 - focal * local[2] / local[0],
    ))


def ground_horizon_line(parameters: Iterable[float], width: int, height: int) -> np.ndarray:
    """Return normalized ``a,b,c`` for the z=constant world-plane horizon."""
    x_vanish = project_direction((1.0, 0.0, 0.0), parameters, width, height)
    y_positive = project_direction((0.0, 1.0, 0.0), parameters, width, height)
    y_negative = project_direction((0.0, -1.0, 0.0), parameters, width, height)
    y_vanish = y_positive if np.isfinite(y_positive).all() else y_negative
    if not np.isfinite(x_vanish).all() or not np.isfinite(y_vanish).all():
        # Homogeneous vanishing directions avoid a false finite line when one
        # axis lies behind the camera.  Use local direction vectors directly.
        params = as_parameters(parameters)
        rotation = rotation_matrix(*params[3:6]).T
        focal = focal_pixels(params[6], width)
        calibration = np.asarray([
            [focal, 0.0, float(width) / 2.0],
            [0.0, -focal, float(height) / 2.0],
            [0.0, 0.0, 1.0],
        ])
        first_local = rotation @ np.asarray((1.0, 0.0, 0.0))
        second_local = rotation @ np.asarray((0.0, 1.0, 0.0))
        first = calibration @ first_local[[1, 2, 0]]
        second = calibration @ second_local[[1, 2, 0]]
        line = np.cross(first, second)
    else:
        line = np.cross(np.r_[x_vanish, 1.0], np.r_[y_vanish, 1.0])
    norm = float(np.linalg.norm(line[:2]))
    if norm <= 1e-12 or not np.isfinite(line).all():
        raise ValueError("camera horizon is degenerate")
    line = line / norm
    if line[1] < 0 or (line[1] == 0 and line[0] < 0):
        line = -line
    return line


def production_round_trip(
    anchor_location: Iterable[float], base: dict, absolute_parameters: Iterable[float]
) -> tuple[dict, np.ndarray]:
    """Convert absolute parameters to ``twin_pose`` and back exactly."""
    params = as_parameters(absolute_parameters)
    pose = twin_pose_from_absolute(
        anchor_location, base, params[:3], *params[3:]
    )
    recovered = absolute_twin_model(anchor_location, base, pose)
    values = np.asarray([
        *recovered["location"], recovered["pitch_deg"], recovered["yaw_deg"],
        recovered["roll_deg"], recovered["fov_deg"],
    ])
    return pose, values
