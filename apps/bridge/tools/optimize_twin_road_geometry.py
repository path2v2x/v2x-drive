#!/usr/bin/env python3
"""Bounded multi-start calibration from independent road/landmark geometry.

This optimizer is intentionally independent of CARLA runtime APIs.  A manifest
contains globally anchored CARLA XYZ points/lines, real-image observations, a
frozen train/holdout split, and the baseline camera transform captured by the
UE5-side builder.  It solves pose plus pinhole/radial intrinsics, then reports
held-out metrics without modifying cameras.json.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
from scipy.optimize import least_squares

BRIDGE_DIR = Path(__file__).resolve().parents[1]
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
from digital_twin_bridge.twin_camera_rig import (  # noqa: E402
    CARLA_DEFAULT_PINHOLE_LENS,
    absolute_twin_model,
    compute_twin_camera_transform,
    configure_twin_camera_blueprint,
    heading_to_carla_yaw,
    horizontal_fov_deg,
    load_cameras_config,
    normalize_angle_degrees,
    twin_pose_from_absolute,
    twin_horizontal_fov_deg,
)
from build_twin_calibration_manifest import (  # noqa: E402
    build_deployment_model,
    convex_hull_area,
    decoded_image_size,
    stable_depth_meters,
    validate_strict_projection_provenance,
)
from build_twin_camera_landmarks import (  # noqa: E402
    depth_pixel_to_world,
    wait_for_frame,
)
from aggregate_twin_calibration_manifests import (  # noqa: E402
    CAMERAS as SITE_CAMERAS,
    SiteManifestError,
    aggregate_site_manifests,
)


# At the 1280x960 acceptance render, errors above roughly one percent of the
# image width can put a vehicle in the wrong lane or beyond a road edge.  The
# former 75/125/175 point limits were diagnostic framing checks, not precision
# calibration gates.
POINT_RMSE_MAX_PX = 10.0
POINT_P95_MAX_PX = 16.0
POINT_MAX_ERROR_PX = 24.0
LINE_RMSE_MAX_PX = 6.0
LINE_MAX_ERROR_PX = 12.0
DEPLOYMENT_OPTICAL_ROUNDTRIP_MAX_PX = 0.25
DEPLOYMENT_TRANSFORM_ROUNDTRIP_MAX = 1e-6
DEPLOYMENT_BOUND_MARGIN_MIN_FRACTION = 0.01
DEPLOYMENT_JACOBIAN_CONDITION_MAX = 100_000.0

PARAMETER_NAMES = (
    "location_x",
    "location_y",
    "location_z",
    "pitch_deg",
    "yaw_deg",
    "roll_deg",
    "fov_deg",
    "cx",
    "cy",
    "k1",
)
DISTORTION_KEYS = ("k1", "k2", "p1", "p2", "k3")


def carla_rotation_matrix(pitch_deg, yaw_deg, roll_deg):
    pitch, yaw, roll = np.radians([pitch_deg, yaw_deg, roll_deg])
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    return np.array([
        [cp * cy, cy * sp * sr - sy * cr, -cy * sp * cr - sy * sr],
        [cp * sy, sy * sp * sr + cy * cr, -sy * sp * cr + cy * sr],
        [sp, -cp * sr, cp * cr],
    ])


def project_world_points(world_points, location, params, width, height):
    """Project CARLA XYZ with [pitch,yaw,roll,fov,cx,cy,k1]."""
    pitch, yaw, roll, fov, cx, cy, k1 = [float(value) for value in params]
    rotation = carla_rotation_matrix(pitch, yaw, roll)
    local = (rotation.T @ (np.asarray(world_points) - np.asarray(location)).T).T
    depth = local[:, 0]
    focal = (float(width) / 2.0) / math.tan(math.radians(fov) / 2.0)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        x = local[:, 1] / depth
        y = -local[:, 2] / depth
        radial = 1.0 + k1 * (x * x + y * y)
        pixels = np.column_stack((cx + focal * x * radial, cy + focal * y * radial))
    return pixels, depth


def project_calibration_points(world_points, params, width, height):
    """Project with the full fitted 6-DoF pose plus optical parameters."""
    return project_world_points(world_points, params[:3], params[3:], width, height)


def deployment_candidate(manifest, params):
    """Translate an absolute fit into the exact tracked twin_pose representation."""
    model = manifest["deployment_model"]
    base = model["base"]
    return twin_pose_from_absolute(
        model["anchor_location"], base, params[:3], *params[3:7]
    )


def deployment_roundtrip(manifest, params):
    """Prove that the fit survives conversion to the production rig contract.

    UE5's tracked rig can represent a full translated/rotated pinhole camera,
    but it cannot move the principal point.  CARLA lens_k is also not the same
    calibrated radial model used by this optimizer.  We therefore quantify the
    optical mismatch over the whole image and fail closed above a sub-pixel
    bound instead of silently copying k1 into an unrelated blueprint field.
    """
    model = manifest["deployment_model"]
    candidate = deployment_candidate(manifest, params)
    base = model["base"]
    absolute = absolute_twin_model(model["anchor_location"], base, candidate)
    roundtrip = np.array([
        *absolute["location"],
        absolute["pitch_deg"],
        absolute["yaw_deg"],
        absolute["roll_deg"],
        absolute["fov_deg"],
    ])
    expected = np.asarray(params[:7], dtype=float)
    transform_errors = np.abs(roundtrip - expected)
    transform_errors[4] = abs(
        normalize_angle_degrees(roundtrip[4] - expected[4])
    )

    width, height = float(manifest["width"]), float(manifest["height"])
    fov, cx, cy, k1 = (float(value) for value in params[6:10])
    focal = (width / 2.0) / math.tan(math.radians(fov) / 2.0)
    half_x = math.tan(math.radians(fov) / 2.0)
    half_y = half_x * height / width
    optical_errors = []
    for x in np.linspace(-half_x, half_x, 5):
        for y in np.linspace(-half_y, half_y, 5):
            radial = 1.0 + k1 * (x * x + y * y)
            fitted = np.array([cx + focal * x * radial, cy + focal * y * radial])
            deployed = np.array([width / 2.0 + focal * x, height / 2.0 + focal * y])
            optical_errors.append(float(np.linalg.norm(fitted - deployed)))
    optical_max = max(optical_errors)
    calibration = manifest["intrinsics_calibration"]
    matrix = calibration["camera_matrix"]
    distortion = calibration["distortion"]
    fx, fy = float(matrix[0][0]), float(matrix[1][1])
    measured_cx, measured_cy = float(matrix[0][2]), float(matrix[1][2])
    k1_m, k2_m, p1_m, p2_m, k3_m = (
        float(distortion[key]) for key in DISTORTION_KEYS
    )
    measured_errors = []
    for x in np.linspace(-half_x, half_x, 9):
        for y in np.linspace(-half_y, half_y, 9):
            radius2 = x * x + y * y
            radial = 1.0 + k1_m * radius2 + k2_m * radius2**2 + k3_m * radius2**3
            distorted_x = x * radial + 2.0 * p1_m * x * y + p2_m * (radius2 + 2.0 * x * x)
            distorted_y = y * radial + p1_m * (radius2 + 2.0 * y * y) + 2.0 * p2_m * x * y
            measured = np.array([
                measured_cx + fx * distorted_x,
                measured_cy + fy * distorted_y,
            ])
            deployed = np.array([width / 2.0 + focal * x, height / 2.0 + focal * y])
            measured_errors.append(float(np.linalg.norm(measured - deployed)))
    measured_optical_max = max(measured_errors)
    reasons = []
    if float(np.max(transform_errors)) > DEPLOYMENT_TRANSFORM_ROUNDTRIP_MAX:
        reasons.append("deployment_transform_roundtrip")
    if optical_max > DEPLOYMENT_OPTICAL_ROUNDTRIP_MAX_PX:
        reasons.append("unrepresentable_principal_point_or_radial_distortion")
    if measured_optical_max > DEPLOYMENT_OPTICAL_ROUNDTRIP_MAX_PX:
        reasons.append("measured_physical_optics_not_representable_in_ue5")
    lens = model.get("lens") or {}
    if set(lens) != set(CARLA_DEFAULT_PINHOLE_LENS) or any(
        not math.isclose(
            float(lens.get(key, math.nan)),
            expected,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        for key, expected in CARLA_DEFAULT_PINHOLE_LENS.items()
    ):
        reasons.append("unsupported_existing_ue5_lens_model")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "candidate_twin_pose": candidate,
        "transform_roundtrip_max": float(np.max(transform_errors)),
        "optical_roundtrip_max_px": optical_max,
        "optical_roundtrip_limit_px": DEPLOYMENT_OPTICAL_ROUNDTRIP_MAX_PX,
        "measured_optical_roundtrip_max_px": measured_optical_max,
        "unsupported_fitted_optics": {
            "principal_point_offset_px": [cx - width / 2.0, cy - height / 2.0],
            "radial_k1": k1,
        },
    }


def deployment_identifiability(manifest, params, parameter_scales):
    """Measure whether frozen training geometry constrains all 6-DoF+FOV axes."""
    params = np.asarray(params, dtype=float)
    scales = np.asarray(parameter_scales[:7], dtype=float)
    steps = np.array([0.001, 0.001, 0.001, 0.001, 0.001, 0.001, 0.001])
    columns = []
    for index, step in enumerate(steps):
        above, below = params.copy(), params.copy()
        above[index] += step
        below[index] -= step
        derivative = (
            feature_residuals(manifest, above, "train")
            - feature_residuals(manifest, below, "train")
        ) / (2.0 * step)
        columns.append(derivative * scales[index])
    jacobian = np.column_stack(columns)
    singular_values = np.linalg.svd(jacobian, compute_uv=False)
    if not len(singular_values) or singular_values[0] <= 0.0:
        rank, condition = 0, None
    else:
        tolerance = singular_values[0] * 1e-8
        rank = int(np.sum(singular_values > tolerance))
        condition = (
            None if singular_values[-1] <= tolerance
            else float(singular_values[0] / singular_values[-1])
        )
    return {
        "passed": (
            rank == 7
            and condition is not None
            and condition <= DEPLOYMENT_JACOBIAN_CONDITION_MAX
        ),
        "rank": rank,
        "required_rank": 7,
        "condition": condition,
        "condition_limit": DEPLOYMENT_JACOBIAN_CONDITION_MAX,
        "scaled_singular_values": [float(value) for value in singular_values],
    }


def normalized_line(image_line):
    x1, y1, x2, y2 = [float(value) for value in image_line]
    line = np.cross([x1, y1, 1.0], [x2, y2, 1.0])
    norm = math.hypot(line[0], line[1])
    if norm < 1e-6:
        raise ValueError("image line endpoints coincide")
    return line / norm


def point_to_polyline_distances(points, polyline):
    """Shortest Euclidean distance from each point to polyline segments."""
    points = np.asarray(points, dtype=float)
    polyline = np.asarray(polyline, dtype=float)
    if len(polyline) < 2:
        raise ValueError("polyline requires at least two vertices")
    starts, vectors = polyline[:-1], polyline[1:] - polyline[:-1]
    lengths2 = np.sum(vectors * vectors, axis=1)
    if np.any(lengths2 < 1e-9):
        raise ValueError("polyline contains duplicate adjacent vertices")
    delta = points[:, None, :] - starts[None, :, :]
    along = np.clip(
        np.sum(delta * vectors[None, :, :], axis=2) / lengths2[None, :],
        0.0, 1.0,
    )
    closest = starts[None, :, :] + along[:, :, None] * vectors[None, :, :]
    return np.sqrt(np.min(np.sum((points[:, None, :] - closest) ** 2, axis=2), axis=1))


def point_metrics(errors):
    values = np.asarray(errors, dtype=float)
    if not len(values):
        return None
    nonfinite_count = int(np.count_nonzero(~np.isfinite(values)))
    if nonfinite_count:
        values = np.where(np.isfinite(values), values, 5000.0)
    return {
        "count": int(len(values)),
        "nonfinite_count": nonfinite_count,
        "rmse_px": float(np.sqrt(np.mean(values * values))),
        "p95_px": float(np.percentile(values, 95)),
        "max_px": float(np.max(values)),
    }


def manifest_gate(manifest):
    reasons = []
    if manifest.get("schema_version") != 1:
        reasons.append("invalid_manifest_schema")
    if manifest.get("camera_id") not in {"ch1", "ch2", "ch3", "ch4"}:
        reasons.append("invalid_camera_id")
    source_hash = str(manifest.get("source_frame_sha256") or "")
    if not (len(source_hash) == 64 and all(ch in "0123456789abcdef" for ch in source_hash)):
        reasons.append("missing_source_frame_hash")
    for field in (
        "twin_frame_sha256",
        "annotation_sha256",
        "cameras_file_sha256",
        "camera_config_sha256",
    ):
        value = str(manifest.get(field) or "")
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            reasons.append(f"missing_{field}")
    if not isinstance(manifest.get("source_artifacts"), dict):
        reasons.append("missing_retained_source_artifacts")
    if not str(manifest.get("ue5_map") or "").lower().endswith(
        "richmond_field_station_richmond_ca"
    ):
        reasons.append("invalid_ue5_map")
    map_hash = str(manifest.get("ue5_map_opendrive_sha256") or "")
    if len(map_hash) != 64 or any(char not in "0123456789abcdef" for char in map_hash):
        reasons.append("missing_ue5_map_opendrive_sha256")
    try:
        validate_strict_projection_provenance(
            manifest.get("projection"),
            map_name=manifest.get("ue5_map"),
            opendrive_sha256=map_hash,
        )
    except ValueError:
        reasons.append("invalid_strict_opendrive_projection_provenance")
    deployment = manifest.get("deployment_model")
    if not isinstance(deployment, dict) or deployment.get("type") != "twin_camera_rig_v1":
        reasons.append("missing_deployment_model")
    else:
        anchor = deployment.get("anchor_location")
        base = deployment.get("base")
        numeric_base = ("pitch_deg", "yaw_deg", "roll_deg", "fov_deg")
        if (
            not isinstance(anchor, list)
            or len(anchor) != 3
            or not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in anchor)
            or not isinstance(base, dict)
            or not all(
                isinstance(base.get(key), (int, float))
                and math.isfinite(float(base[key]))
                for key in numeric_base
            )
        ):
            reasons.append("invalid_deployment_model")
    calibration = manifest.get("intrinsics_calibration")
    if not isinstance(calibration, dict):
        reasons.append("missing_measured_intrinsics_calibration")
    else:
        matrix = calibration.get("camera_matrix")
        distortion = calibration.get("distortion")
        artifact_hash = str(calibration.get("artifact_sha256") or "")
        image_count = calibration.get("image_count")
        source_hashes = calibration.get("source_images_sha256")
        rms = calibration.get("rms_reprojection_error_px")
        try:
            optical_values = [
                float(matrix[0][0]), float(matrix[0][2]),
                float(matrix[1][1]), float(matrix[1][2]),
                *(float(distortion[key]) for key in DISTORTION_KEYS),
            ]
        except (KeyError, TypeError, ValueError, IndexError):
            optical_values = []
        if (
            calibration.get("method") not in {"checkerboard", "charuco"}
            or len(artifact_hash) != 64
            or any(char not in "0123456789abcdef" for char in artifact_hash)
            or calibration.get("resolution") != [manifest.get("width"), manifest.get("height")]
            or isinstance(image_count, bool)
            or not isinstance(image_count, int)
            or image_count < 10
            or not isinstance(source_hashes, list)
            or len(source_hashes) != image_count
            or len(set(source_hashes)) != len(source_hashes)
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
                for value in source_hashes
            )
            or isinstance(rms, bool)
            or not isinstance(rms, (int, float))
            or not math.isfinite(float(rms))
            or not 0.0 <= float(rms) <= 2.0
            or len(optical_values) != 9
            or not all(math.isfinite(value) for value in optical_values)
        ):
            reasons.append("invalid_measured_intrinsics_calibration")
    depth = manifest.get("depth_frame")
    if not isinstance(depth, dict):
        reasons.append("missing_depth_frame_identity")
    else:
        carla_frame = depth.get("carla_frame")
        timestamp = depth.get("sensor_timestamp")
        depth_width, depth_height = depth.get("width"), depth.get("height")
        raw_hash = str(depth.get("raw_data_sha256") or "")
        raw_size = depth.get("raw_data_size")
        if (
            isinstance(carla_frame, bool)
            or not isinstance(carla_frame, int)
            or carla_frame <= 0
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
            or float(timestamp) < 0.0
            or depth_width != 1280
            or depth_height != 960
            or len(raw_hash) != 64
            or any(char not in "0123456789abcdef" for char in raw_hash)
            or isinstance(raw_size, bool)
            or not isinstance(raw_size, int)
            or raw_size != depth_width * depth_height * 4
            or not isinstance(depth.get("path"), str)
            or not depth.get("path")
        ):
            reasons.append("invalid_depth_frame_identity")
    features = manifest.get("features") or []
    ids = [feature.get("id") for feature in features]
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        reasons.append("invalid_or_duplicate_feature_id")
    train_points = [f for f in features if f.get("type") == "point" and f.get("split") == "train"]
    held_points = [f for f in features if f.get("type") == "point" and f.get("split") == "holdout"]
    if any(feature.get("type") == "line" for feature in features):
        reasons.append("infinite_line_evidence_not_allowed")
    train_lines = [f for f in features if f.get("type") == "polyline" and f.get("split") == "train"]
    held_lines = [f for f in features if f.get("type") == "polyline" and f.get("split") == "holdout"]
    if len(train_points) < 8:
        reasons.append("insufficient_train_points")
    if len(held_points) < 4:
        reasons.append("insufficient_heldout_points")
    if len(train_lines) < 3:
        reasons.append("insufficient_train_lines")
    if len(held_lines) < 2:
        reasons.append("insufficient_heldout_lines")
    if any(
        feature.get("provenance") != "manually_verified_unique"
        for feature in train_points + held_points
    ):
        reasons.append("unverified_unique_landmark_provenance")
    global_landmark_ids = []
    for feature in train_points + held_points:
        landmark_id = feature.get("global_landmark_id")
        surveyed_world = feature.get("surveyed_world")
        survey_record_sha256 = feature.get("survey_record_sha256")
        survey_record_path = feature.get("survey_record_path")
        survey_record = feature.get("survey_record")
        if (
            not isinstance(landmark_id, str)
            or not landmark_id
            or landmark_id.strip() != landmark_id
            or not isinstance(surveyed_world, list)
            or len(surveyed_world) != 3
            or not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                for value in surveyed_world
            )
            or not isinstance(survey_record_sha256, str)
            or len(survey_record_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in survey_record_sha256
            )
            or not isinstance(survey_record_path, str)
            or not survey_record_path
            or not isinstance(survey_record, dict)
        ):
            reasons.append("unbound_global_landmark_identity")
            break
        global_landmark_ids.append(landmark_id)
    if len(set(global_landmark_ids)) != len(global_landmark_ids):
        reasons.append("duplicate_global_landmark_identity")
    if any(
        feature.get("provenance") != "manually_traced_geometry"
        for feature in train_lines + held_lines
    ):
        reasons.append("unverified_road_geometry_provenance")
    descriptions = [str(feature.get("description") or "").strip() for feature in features]
    if (
        any(len(value) < 8 for value in descriptions)
        or len({value.casefold() for value in descriptions}) != len(descriptions)
    ):
        reasons.append("missing_or_duplicate_semantic_descriptions")
    for field in ("image", "twin"):
        pixels = [tuple(feature.get(field) or ()) for feature in train_points + held_points]
        if any(len(pixel) != 2 for pixel in pixels) or len(set(pixels)) != len(pixels):
            reasons.append(f"duplicate_or_invalid_{field}_point_pixels")
    if any(
        not isinstance(feature.get("depth_neighborhood"), dict)
        for feature in train_points + held_points
    ) or any(
        not isinstance(feature.get("depth_neighborhoods"), list)
        or len(feature["depth_neighborhoods"]) != len(feature.get("twin_polyline") or [])
        for feature in train_lines + held_lines
    ):
        reasons.append("missing_depth_neighborhood_evidence")

    width, height = float(manifest.get("width", 0)), float(manifest.get("height", 0))
    for label, points in (("train", train_points), ("heldout", held_points)):
        pixels = [feature.get("image") for feature in points]
        if pixels and width > 0 and height > 0:
            horizontal = (max(p[0] for p in pixels) - min(p[0] for p in pixels)) / width
            vertical = (max(p[1] for p in pixels) - min(p[1] for p in pixels)) / height
        else:
            horizontal = vertical = 0.0
        if horizontal < 0.5 or vertical < 0.3:
            reasons.append(f"{label}_image_coverage")
        if pixels and width > 0 and height > 0:
            if convex_hull_area(pixels) / (width * height) < 0.02:
                reasons.append(f"{label}_image_collinear_or_clustered")

    angles = []
    for feature in train_lines:
        if feature.get("type") == "polyline":
            x1, y1 = feature.get("image_polyline", [[0, 0]])[0]
            x2, y2 = feature.get("image_polyline", [[0, 0]])[-1]
        else:
            x1, y1, x2, y2 = feature.get("image_line", [0, 0, 0, 0])
        angles.append(math.atan2(y2 - y1, x2 - x1) % math.pi)
    diverse = any(
        min(abs(a - b), math.pi - abs(a - b)) >= math.radians(20)
        for index, a in enumerate(angles) for b in angles[index + 1:]
    )
    if not diverse:
        reasons.append("insufficient_line_direction_diversity")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "train_points": len(train_points),
        "heldout_points": len(held_points),
        "train_lines": len(train_lines),
        "heldout_lines": len(held_lines),
    }


def verify_retained_artifact_paths(manifest):
    """Independently re-read every filesystem artifact bound by the manifest."""
    reasons = []

    def verify(identity, label, expected_sha256=None, expected_size=None):
        if not isinstance(identity, dict):
            reasons.append(f"retained_artifact_identity_missing:{label}")
            return None
        path_value = identity.get("path")
        if not isinstance(path_value, str) or not path_value:
            reasons.append(f"retained_artifact_path_invalid:{label}")
            return None
        try:
            path = Path(path_value).expanduser().resolve(strict=True)
            if not path.is_file() or str(path) != path_value:
                raise OSError("not a canonical regular file")
            payload = path.read_bytes()
        except OSError:
            reasons.append(f"retained_artifact_missing:{label}")
            return None
        actual_hash = hashlib.sha256(payload).hexdigest()
        if (
            identity.get("sha256") != actual_hash
            or identity.get("size_bytes") != len(payload)
            or (expected_sha256 is not None and actual_hash != expected_sha256)
            or (expected_size is not None and len(payload) != expected_size)
        ):
            reasons.append(f"retained_artifact_identity_mismatch:{label}")
            return None
        return str(path)

    sources = manifest.get("source_artifacts")
    calibration = manifest.get("intrinsics_calibration") or {}
    if not isinstance(sources, dict):
        reasons.append("retained_source_artifacts_missing")
        sources = {}
    for key, expected_hash in (
        ("annotations", manifest.get("annotation_sha256")),
        ("real_frame", manifest.get("source_frame_sha256")),
        ("twin_frame", manifest.get("twin_frame_sha256")),
        ("cameras_file", manifest.get("cameras_file_sha256")),
        ("intrinsics_artifact", calibration.get("artifact_sha256")),
    ):
        verify(sources.get(key), key, expected_sha256=expected_hash)
    source_images = sources.get("intrinsics_source_images")
    expected_source_hashes = calibration.get("source_images_sha256") or []
    if not isinstance(source_images, list) or len(source_images) != len(
        expected_source_hashes
    ):
        reasons.append("retained_intrinsics_source_artifacts_incomplete")
    else:
        verified_hashes = []
        for index, identity in enumerate(source_images):
            if verify(identity, f"intrinsics_source_image:{index}") is not None:
                verified_hashes.append(identity["sha256"])
        if (
            len(verified_hashes) != len(expected_source_hashes)
            or len(set(verified_hashes)) != len(verified_hashes)
            or set(verified_hashes) != set(expected_source_hashes)
        ):
            reasons.append("retained_intrinsics_source_artifacts_mismatch")
    depth = manifest.get("depth_frame") or {}
    verify(
        {
            "path": depth.get("path"),
            "sha256": depth.get("raw_data_sha256"),
            "size_bytes": depth.get("raw_data_size"),
        },
        "depth_frame",
        expected_sha256=depth.get("raw_data_sha256"),
        expected_size=depth.get("raw_data_size"),
    )
    for feature in manifest.get("features") or []:
        if isinstance(feature, dict) and feature.get("type") == "point":
            path = verify(
                feature.get("survey_record"),
                f"survey_record:{feature.get('id')}",
                expected_sha256=feature.get("survey_record_sha256"),
            )
            if path is not None and feature.get("survey_record_path") != path:
                reasons.append(
                    f"retained_survey_record_path_mismatch:{feature.get('id')}"
                )
    return {"passed": not reasons, "reasons": reasons}


def verify_external_evidence(
    manifest,
    *,
    annotations_bytes,
    real_frame_bytes,
    twin_frame_bytes,
    cameras_bytes,
    intrinsics_artifact_bytes,
    intrinsics_source_image_bytes,
    depth_frame_bytes,
    runtime_evidence,
    external_evidence_paths=None,
):
    """Re-bind a mutable optimizer manifest to every retained source artifact."""
    reasons = []
    retained = verify_retained_artifact_paths(manifest)
    if not retained["passed"]:
        reasons.extend(retained["reasons"])
    sources = manifest.get("source_artifacts") or {}
    depth_identity = manifest.get("depth_frame") or {}
    if not isinstance(external_evidence_paths, dict):
        reasons.append("external_evidence_paths_missing")
    else:
        path_bindings = (
            ("annotations", "annotations"),
            ("real_frame", "real_frame"),
            ("twin_frame", "twin_frame"),
            ("cameras_file", "cameras_file"),
            ("intrinsics_artifact", "intrinsics_artifact"),
        )
        for cli_key, source_key in path_bindings:
            try:
                actual_path = str(
                    Path(external_evidence_paths[cli_key])
                    .expanduser()
                    .resolve(strict=True)
                )
            except (KeyError, OSError, TypeError):
                actual_path = None
            if actual_path != (sources.get(source_key) or {}).get("path"):
                reasons.append(f"external_evidence_path_mismatch:{cli_key}")
        try:
            actual_depth_path = str(
                Path(external_evidence_paths["depth_frame"])
                .expanduser()
                .resolve(strict=True)
            )
        except (KeyError, OSError, TypeError):
            actual_depth_path = None
        if actual_depth_path != depth_identity.get("path"):
            reasons.append("external_evidence_path_mismatch:depth_frame")
        try:
            source_paths = {
                str(Path(path).expanduser().resolve(strict=True))
                for path in external_evidence_paths["intrinsics_source_images"]
            }
        except (KeyError, OSError, TypeError):
            source_paths = set()
        expected_paths = {
            identity.get("path")
            for identity in sources.get("intrinsics_source_images") or []
            if isinstance(identity, dict)
        }
        if source_paths != expected_paths or len(source_paths) != len(
            sources.get("intrinsics_source_images") or []
        ):
            reasons.append("external_evidence_path_mismatch:intrinsics_source_images")
    bindings = (
        ("annotation_sha256", annotations_bytes),
        ("source_frame_sha256", real_frame_bytes),
        ("twin_frame_sha256", twin_frame_bytes),
        ("cameras_file_sha256", cameras_bytes),
    )
    for field, payload in bindings:
        if hashlib.sha256(payload).hexdigest() != manifest.get(field):
            reasons.append(f"{field}_mismatch")
    if hashlib.sha256(depth_frame_bytes).hexdigest() != depth_identity.get(
        "raw_data_sha256"
    ):
        reasons.append("depth_frame_sha256_mismatch")
    if len(depth_frame_bytes) != depth_identity.get("raw_data_size"):
        reasons.append("depth_frame_size_mismatch")
    try:
        cameras_payload = json.loads(cameras_bytes)
        camera = next(
            item for item in cameras_payload["cameras"]
            if item.get("id") == manifest.get("camera_id")
        )
    except (KeyError, TypeError, StopIteration, UnicodeDecodeError, json.JSONDecodeError):
        camera = None
        reasons.append("camera_config_unreadable")
    if camera is not None:
        canonical = json.dumps(
            camera, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != manifest.get("camera_config_sha256"):
            reasons.append("camera_config_sha256_mismatch")
        if camera.get("intrinsics_calibration") != manifest.get("intrinsics_calibration"):
            reasons.append("intrinsics_calibration_config_mismatch")
        expected_source_hashes = set(
            (manifest.get("intrinsics_calibration") or {}).get(
                "source_images_sha256"
            ) or []
        )
        actual_source_hashes = []
        try:
            for index, payload in enumerate(intrinsics_source_image_bytes):
                decoded_image_size(payload, f"intrinsics source {index}")
                actual_source_hashes.append(hashlib.sha256(payload).hexdigest())
        except ValueError:
            reasons.append("intrinsics_source_image_invalid")
        if (
            len(actual_source_hashes) != len(expected_source_hashes)
            or len(set(actual_source_hashes)) != len(actual_source_hashes)
            or set(actual_source_hashes) != expected_source_hashes
        ):
            reasons.append("intrinsics_source_images_mismatch")
        intrinsics = camera.get("intrinsics") or {}
        twin_pose = camera.get("twin_pose") or {}
        expected_base = {
            "pitch_deg": float(camera["pitch_deg"]),
            "yaw_deg": heading_to_carla_yaw(
                float(camera["heading_deg"]), float(camera["yaw_deg"])
            ),
            "roll_deg": float(camera.get("roll_deg", 0.0)),
            "fov_deg": horizontal_fov_deg(intrinsics),
        }
        deployment = manifest.get("deployment_model") or {}
        if deployment.get("base") != expected_base:
            reasons.append("deployment_base_camera_config_mismatch")
        if camera.get("twin_lens"):
            reasons.append("camera_lens_override_safety_hold")
        expected_lens = dict(CARLA_DEFAULT_PINHOLE_LENS)
        if deployment.get("lens") != expected_lens:
            reasons.append("deployment_lens_camera_config_mismatch")
        baseline = manifest.get("baseline") or {}
        expected_baseline = {
            "pitch_deg": expected_base["pitch_deg"]
            + float(twin_pose.get("pitch_offset_deg", 0.0)),
            "yaw_deg": expected_base["yaw_deg"]
            + float(twin_pose.get("yaw_offset_deg", 0.0)),
            "roll_deg": expected_base["roll_deg"]
            + float(twin_pose.get("roll_offset_deg", 0.0)),
            "fov_deg": expected_base["fov_deg"]
            + float(twin_pose.get("fov_offset_deg", 0.0)),
            "cx": float(intrinsics["cx"]),
            "cy": float(intrinsics["cy"]),
            "k1": 0.0,
        }
        if any(
            not math.isclose(
                float(baseline.get(key, math.nan)), value,
                rel_tol=0.0, abs_tol=1e-9,
            )
            for key, value in expected_baseline.items()
        ):
            reasons.append("baseline_camera_config_mismatch")
    calibration = manifest.get("intrinsics_calibration") or {}
    if hashlib.sha256(intrinsics_artifact_bytes).hexdigest() != calibration.get(
        "artifact_sha256"
    ):
        reasons.append("intrinsics_artifact_sha256_mismatch")
    try:
        artifact = json.loads(intrinsics_artifact_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        artifact = None
        reasons.append("intrinsics_artifact_unreadable")
    expected_artifact = {
        key: value for key, value in calibration.items() if key != "artifact_sha256"
    }
    if artifact is not None and artifact != expected_artifact:
        reasons.append("intrinsics_artifact_contents_mismatch")
    try:
        annotations = json.loads(annotations_bytes)
        expected_features = []
        for feature in annotations["points"]:
            expected_features.append({
                "id": str(feature["id"]).strip(),
                "global_landmark_id": feature["global_landmark_id"],
                "surveyed_world": [
                    float(value) for value in feature["surveyed_world"]
                ],
                "survey_record_sha256": feature["survey_record_sha256"],
                "survey_record_path": feature["survey_record_path"],
                "type": "point",
                "split": feature["split"],
                "provenance": feature["provenance"],
                "category": str(feature.get("category") or "").strip(),
                "description": str(feature.get("description") or "").strip(),
                "twin": [float(value) for value in feature["twin"]],
                "image": [float(value) for value in feature["image"]],
            })
        for feature in annotations["roads"]:
            expected_features.append({
                "id": str(feature["id"]).strip(),
                "type": "polyline",
                "split": feature["split"],
                "provenance": feature["provenance"],
                "category": str(feature.get("category") or "").strip(),
                "description": str(feature.get("description") or "").strip(),
                "twin_polyline": [
                    [float(value) for value in pixel]
                    for pixel in feature["twin_polyline"]
                ],
                "image_polyline": [
                    [float(value) for value in pixel]
                    for pixel in feature["image_polyline"]
                ],
            })
        actual_features = []
        for feature in manifest.get("features", []):
            keys = (
                (
                    "id", "global_landmark_id", "surveyed_world",
                    "survey_record_sha256", "survey_record_path", "type",
                    "split", "provenance",
                    "category", "description", "twin", "image",
                )
                if feature.get("type") == "point"
                else (
                    "id", "type", "split", "provenance", "category", "description",
                    "twin_polyline", "image_polyline",
                )
            )
            actual_features.append({key: feature.get(key) for key in keys})
        if actual_features != expected_features:
            reasons.append("manifest_features_annotation_mismatch")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        reasons.append("annotations_unreadable_for_feature_binding")
    if not isinstance(runtime_evidence, dict):
        reasons.append("runtime_calibration_evidence_missing")
    else:
        if runtime_evidence.get("ue5_map") != manifest.get("ue5_map"):
            reasons.append("runtime_ue5_map_mismatch")
        if runtime_evidence.get("ue5_map_opendrive_sha256") != manifest.get(
            "ue5_map_opendrive_sha256"
        ):
            reasons.append("runtime_ue5_map_content_mismatch")
        if runtime_evidence.get("projection") != manifest.get("projection"):
            reasons.append("runtime_projection_provenance_mismatch")
        if runtime_evidence.get("baseline") != manifest.get("baseline"):
            reasons.append("runtime_baseline_mismatch")
        if runtime_evidence.get("deployment_model") != manifest.get(
            "deployment_model"
        ):
            reasons.append("runtime_deployment_model_mismatch")
        runtime_worlds = runtime_evidence.get("feature_worlds") or {}
        for feature in manifest.get("features", []):
            expected_world = runtime_worlds.get(feature.get("id"))
            actual_world = feature.get("world")
            try:
                equal = np.allclose(
                    np.asarray(actual_world, dtype=float),
                    np.asarray(expected_world, dtype=float),
                    rtol=0.0,
                    atol=1e-6,
                )
            except (TypeError, ValueError):
                equal = False
            if not equal:
                reasons.append(f"runtime_feature_world_mismatch:{feature.get('id')}")
    return {"passed": not reasons, "reasons": reasons}


def collect_runtime_calibration_evidence(
    manifest, cameras_json_path, depth_frame_bytes, host="127.0.0.1", port=2000
):
    """Re-derive the absolute anchor and every depth-backed world coordinate."""
    import carla

    config = load_cameras_config(cameras_json_path)
    if config is None:
        raise RuntimeError("cameras config is unavailable for runtime revalidation")
    camera = next(
        item for item in config["cameras"]
        if item["id"] == manifest["camera_id"]
    )
    client = carla.Client(host, int(port))
    client.set_timeout(20.0)
    world = client.get_world()
    carla_map = world.get_map()
    transform, projection = compute_twin_camera_transform(
        carla_map,
        config["site"],
        camera,
        require_opendrive_georeference=True,
        return_projection_provenance=True,
    )
    opendrive = carla_map.to_opendrive()
    opendrive_sha256 = hashlib.sha256(opendrive.encode("utf-8")).hexdigest()
    projection = validate_strict_projection_provenance(
        projection,
        map_name=str(carla_map.name),
        opendrive_sha256=opendrive_sha256,
    )
    fov = twin_horizontal_fov_deg(camera)
    intrinsics = camera["intrinsics"]
    baseline = {
        "location": [
            float(transform.location.x),
            float(transform.location.y),
            float(transform.location.z),
        ],
        "pitch_deg": float(transform.rotation.pitch),
        "yaw_deg": float(transform.rotation.yaw),
        "roll_deg": float(transform.rotation.roll),
        "fov_deg": float(fov),
        "cx": float(intrinsics["cx"]),
        "cy": float(intrinsics["cy"]),
        "k1": 0.0,
    }
    depth = manifest["depth_frame"]
    width, height = int(depth["width"]), int(depth["height"])
    if len(depth_frame_bytes) != width * height * 4:
        raise RuntimeError("retained depth buffer dimensions are inconsistent")

    blueprint = world.get_blueprint_library().find("sensor.camera.depth")
    configure_twin_camera_blueprint(blueprint, camera, width, height)
    frames = []
    actor = world.spawn_actor(blueprint, transform)
    try:
        actor.listen(frames.append)
        fresh_image = wait_for_frame(world, frames)
        if fresh_image is None:
            raise RuntimeError("no fresh UE5 depth frame received for revalidation")
        fresh_depth_bytes = bytes(fresh_image.raw_data)
    finally:
        try:
            actor.stop()
        finally:
            actor.destroy()
    if len(fresh_depth_bytes) != width * height * 4:
        raise RuntimeError("fresh UE5 depth buffer dimensions are inconsistent")

    def world_at(pixel):
        retained_value = stable_depth_meters(
            depth_frame_bytes, width, height, pixel[0], pixel[1]
        )
        fresh_value = stable_depth_meters(
            fresh_depth_bytes, width, height, pixel[0], pixel[1]
        )
        tolerance = max(0.25, 0.02 * retained_value)
        if abs(fresh_value - retained_value) > tolerance:
            raise RuntimeError(
                "fresh UE5 depth disagrees with retained calibration depth"
            )
        location = depth_pixel_to_world(
            transform, pixel[0], pixel[1], fresh_value, fov, width, height
        )
        return [float(location.x), float(location.y), float(location.z)]

    feature_worlds = {}
    for feature in manifest["features"]:
        if feature["type"] == "point":
            feature_worlds[feature["id"]] = world_at(feature["twin"])
        elif feature["type"] == "polyline":
            feature_worlds[feature["id"]] = [
                world_at(pixel) for pixel in feature["twin_polyline"]
            ]
    return {
        "ue5_map": str(carla_map.name),
        "ue5_map_opendrive_sha256": opendrive_sha256,
        "projection": projection,
        "endpoint": {"host": str(host), "port": int(port)},
        "fresh_depth_frame": {
            "carla_frame": int(fresh_image.frame),
            "sensor_timestamp": float(fresh_image.timestamp),
            "raw_data_sha256": hashlib.sha256(fresh_depth_bytes).hexdigest(),
        },
        "baseline": baseline,
        "deployment_model": build_deployment_model(camera, transform),
        "feature_worlds": feature_worlds,
    }


def feature_residuals(manifest, params, split):
    width, height = manifest["width"], manifest["height"]
    residuals = []
    for feature in manifest["features"]:
        if feature.get("split") != split:
            continue
        if feature["type"] == "point":
            pixels, depth = project_calibration_points(
                [feature["world"]], params, width, height
            )
            if depth[0] <= 0.1:
                residuals.extend([5000.0, 5000.0])
            else:
                residuals.extend(pixels[0] - np.asarray(feature["image"], dtype=float))
        elif feature["type"] == "line":
            pixels, depth = project_calibration_points(
                feature["world"], params, width, height
            )
            line = normalized_line(feature["image_line"])
            for pixel, point_depth in zip(pixels, depth):
                residuals.append(
                    5000.0 if point_depth <= 0.1 else float(line @ [pixel[0], pixel[1], 1.0])
                )
        elif feature["type"] == "polyline":
            pixels, depth = project_calibration_points(
                feature["world"], params, width, height
            )
            target = np.asarray(feature["image_polyline"], dtype=float)
            invalid = np.any(depth <= 0.1) or not np.all(np.isfinite(pixels))
            if invalid:
                residuals.extend([5000.0] * (len(pixels) + len(target)))
            else:
                forward = point_to_polyline_distances(pixels, target)
                backward = point_to_polyline_distances(target, pixels)
                residuals.extend(float(error) for error in forward)
                residuals.extend(float(error) for error in backward)
        else:
            raise ValueError(f"unsupported feature type {feature['type']!r}")
    return np.asarray(residuals, dtype=float)


def evaluate_split(manifest, params, split):
    point_errors, line_errors = [], []
    width, height = manifest["width"], manifest["height"]
    for feature in manifest["features"]:
        if feature.get("split") != split:
            continue
        pixels, depth = project_calibration_points(
            [feature["world"]] if feature["type"] == "point" else feature["world"],
            params, width, height,
        )
        if feature["type"] == "point":
            point_errors.append(
                5000.0 if depth[0] <= 0.1
                else float(np.linalg.norm(pixels[0] - np.asarray(feature["image"])))
            )
        elif feature["type"] == "line":
            line = normalized_line(feature["image_line"])
            line_errors.extend(
                5000.0 if d <= 0.1 else abs(float(line @ [p[0], p[1], 1.0]))
                for p, d in zip(pixels, depth)
            )
        elif feature["type"] == "polyline":
            target = np.asarray(feature["image_polyline"], dtype=float)
            invalid = np.any(depth <= 0.1) or not np.all(np.isfinite(pixels))
            if invalid:
                line_errors.extend([5000.0] * (len(pixels) + len(target)))
            else:
                line_errors.extend(point_to_polyline_distances(pixels, target))
                line_errors.extend(point_to_polyline_distances(target, pixels))
        else:
            raise ValueError(f"unsupported feature type {feature['type']!r}")
    return {"points": point_metrics(point_errors), "lines": point_metrics(line_errors)}


def verify_site_aggregation_report(manifest, manifest_bytes, report_bytes):
    """Recompute the four-camera registry report and bind this exact manifest."""
    reasons = []
    retained = verify_retained_artifact_paths(manifest)
    if not retained["passed"]:
        reasons.extend(retained["reasons"])
    try:
        parsed_manifest = json.loads(manifest_bytes)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        parsed_manifest = None
        reasons.append("optimizer_manifest_bytes_invalid")
    if parsed_manifest != manifest:
        reasons.append("optimizer_manifest_object_bytes_mismatch")
    try:
        report = json.loads(report_bytes)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        report = None
        reasons.append("site_aggregation_report_invalid")
    manifest_sha256 = (
        hashlib.sha256(manifest_bytes).hexdigest()
        if isinstance(manifest_bytes, bytes)
        else None
    )
    report_sha256 = (
        hashlib.sha256(report_bytes).hexdigest()
        if isinstance(report_bytes, bytes)
        else None
    )
    if isinstance(report, dict):
        manifests = report.get("manifests")
        camera_id = manifest.get("camera_id")
        entry = manifests.get(camera_id) if isinstance(manifests, dict) else None
        if (
            report.get("schema") != "v2x-site-calibration-aggregation/v1"
            or report.get("gate_passed") is not True
            or report.get("acceptance_eligible") is not False
            or set(manifests or {}) != set(SITE_CAMERAS)
            or not isinstance(entry, dict)
            or entry.get("sha256") != manifest_sha256
        ):
            reasons.append("site_aggregation_current_manifest_mismatch")
        registry = report.get("site_landmark_registry")
        try:
            registry_path = registry["path"]
            manifest_paths = [
                manifests[camera]["path"] for camera in sorted(SITE_CAMERAS)
            ]
            recomputed = aggregate_site_manifests(
                registry_path,
                manifest_paths,
            )
        except (KeyError, TypeError, OSError, SiteManifestError):
            recomputed = None
            reasons.append("site_aggregation_recompute_failed")
        if recomputed is not None and recomputed != report:
            reasons.append("site_aggregation_report_content_mismatch")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "report_sha256": report_sha256,
        "manifest_sha256": manifest_sha256,
        "retained_artifacts": retained,
    }


def optimize_manifest(
    manifest,
    *,
    manifest_bytes=None,
    site_aggregation_report_bytes=None,
    allow_incomplete=False,
    external_evidence_verified=False,
):
    if not external_evidence_verified:
        return {
            "passed": False,
            "reason": "external_evidence_not_verified",
            "reasons": ["external_evidence_not_verified"],
        }
    site_aggregation = verify_site_aggregation_report(
        manifest,
        manifest_bytes,
        site_aggregation_report_bytes,
    )
    if not site_aggregation["passed"]:
        return {
            "passed": False,
            "reason": "site_aggregation_gate",
            "reasons": list(site_aggregation["reasons"]),
            "site_aggregation": site_aggregation,
        }
    gate = manifest_gate(manifest)
    if not gate["passed"] and not allow_incomplete:
        return {
            "passed": False,
            "dataset_gate": gate,
            "reason": "dataset_gate",
            "site_aggregation": site_aggregation,
        }
    baseline = manifest["baseline"]
    x0 = np.array([
        *baseline["location"],
        baseline["pitch_deg"], baseline["yaw_deg"], baseline.get("roll_deg", 0.0),
        baseline["fov_deg"], baseline.get("cx", manifest["width"] / 2.0),
        baseline.get("cy", manifest["height"] / 2.0), baseline.get("k1", 0.0),
    ], dtype=float)
    lower = x0 + np.array([-3, -3, -3, -15, -15, -8, -20, -80, -80, -0.30])
    upper = x0 + np.array([3, 3, 3, 15, 15, 8, 20, 80, 80, 0.30])
    scales = np.array([1, 1, 1, 5, 5, 3, 8, 30, 30, 0.10])

    def objective(params):
        evidence = feature_residuals(manifest, params, "train")
        priors = 0.25 * (params - x0) / scales
        return np.concatenate((evidence, priors))

    def multistart(seed0, seed_lower, seed_upper, seed_objective):
        candidates = []
        for pitch_delta in (-6.0, 0.0, 6.0):
            for yaw_delta in (-6.0, 0.0, 6.0):
                for fov_delta in (-8.0, 0.0, 8.0):
                    seed = seed0.copy()
                    seed[[3, 4, 6]] += [pitch_delta, yaw_delta, fov_delta]
                    candidates.append(least_squares(
                        seed_objective,
                        seed,
                        bounds=(seed_lower, seed_upper),
                        loss="soft_l1",
                        f_scale=50.0,
                        max_nfev=10_000,
                    ))
        return min(
            candidates,
            key=lambda result: float(np.sum(seed_objective(result.x) ** 2)),
        )

    # First retain the fully unconstrained solution as measured diagnostic
    # evidence.  Pose/principal-point/distortion are partly degenerate, so it
    # must never be treated as production-deployable merely because it has a
    # small residual.
    unconstrained = multistart(x0, lower, upper, objective)

    # Then solve the exact optical model the tracked UE5 rig and replay
    # verifier share: centered principal point and zero radial distortion.
    # A camera whose measured optics materially require cx/cy/k1 will fail its
    # held-out evidence here instead of being silently approximated.
    def deployable_params(core):
        return np.concatenate((core, [manifest["width"] / 2.0, manifest["height"] / 2.0, 0.0]))

    def deployable_objective(core):
        params = deployable_params(core)
        evidence = feature_residuals(manifest, params, "train")
        # Bounds carry the physical safety constraint.  This light prior only
        # breaks exact degeneracies; it must not bias a perfect geometric fit
        # several pixels away from its observations.
        priors = 0.25 * (core - x0[:7]) / scales[:7]
        return np.concatenate((evidence, priors))

    deployable = multistart(x0[:7], lower[:7], upper[:7], deployable_objective)
    best_params = deployable_params(deployable.x)
    train = evaluate_split(manifest, best_params, "train")
    heldout = evaluate_split(manifest, best_params, "holdout")
    unconstrained_train = evaluate_split(manifest, unconstrained.x, "train")
    unconstrained_heldout = evaluate_split(manifest, unconstrained.x, "holdout")
    reasons = [] if gate["passed"] else ["dataset_gate"]
    point = heldout["points"]
    line = heldout["lines"]
    threshold_scale = float(manifest["width"]) / 1280.0
    thresholds = {
        "point_rmse_px": POINT_RMSE_MAX_PX * threshold_scale,
        "point_p95_px": POINT_P95_MAX_PX * threshold_scale,
        "point_max_px": POINT_MAX_ERROR_PX * threshold_scale,
        "line_rmse_px": LINE_RMSE_MAX_PX * threshold_scale,
        "line_max_px": LINE_MAX_ERROR_PX * threshold_scale,
        "reference_width": 1280,
        "scale": threshold_scale,
    }
    if not point or point["rmse_px"] > thresholds["point_rmse_px"]:
        reasons.append("heldout_point_rmse")
    if not point or point["p95_px"] > thresholds["point_p95_px"]:
        reasons.append("heldout_point_p95")
    if not point or point["max_px"] > thresholds["point_max_px"]:
        reasons.append("heldout_point_max")
    if not line or line["rmse_px"] > thresholds["line_rmse_px"]:
        reasons.append("heldout_line_rmse")
    if not line or line["max_px"] > thresholds["line_max_px"]:
        reasons.append("heldout_line_max")
    deployability = deployment_roundtrip(manifest, best_params) if gate["passed"] else {
        "passed": False,
        "reasons": ["dataset_gate"],
    }
    reasons.extend(deployability["reasons"])
    identifiability = deployment_identifiability(manifest, best_params, scales)
    if not identifiability["passed"]:
        reasons.append("underconstrained_deployable_fit")
    bound_margin_fraction = {
        name: float(min(value - low, high - value) / (high - low))
        for name, value, low, high in zip(
            PARAMETER_NAMES[:7], best_params[:7], lower[:7], upper[:7]
        )
    }
    if min(bound_margin_fraction.values()) < DEPLOYMENT_BOUND_MARGIN_MIN_FRACTION:
        reasons.append("deployable_parameter_at_bound")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "dataset_gate": gate,
        "parameters": dict(zip(PARAMETER_NAMES, (float(value) for value in best_params))),
        "deployability": deployability,
        "identifiability": identifiability,
        "deployable_bound_margin_fraction": bound_margin_fraction,
        "deployable_bound_margin_min_fraction": DEPLOYMENT_BOUND_MARGIN_MIN_FRACTION,
        "unconstrained_diagnostic": {
            "parameters": dict(zip(
                PARAMETER_NAMES, (float(value) for value in unconstrained.x)
            )),
            "train": unconstrained_train,
            "heldout": unconstrained_heldout,
            "deployability": deployment_roundtrip(manifest, unconstrained.x),
        },
        "train": train,
        "heldout": heldout,
        "heldout_thresholds": thresholds,
        "bound_margin": {
            name: float(min(value - low, high - value))
            for name, value, low, high in zip(PARAMETER_NAMES, best_params, lower, upper)
        },
        "site_aggregation": site_aggregation,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--output", required=True)
    parser.add_argument("--annotations", required=True)
    parser.add_argument("--real-frame", required=True)
    parser.add_argument("--twin-frame", required=True)
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--intrinsics-artifact", required=True)
    parser.add_argument(
        "--intrinsics-source-image", action="append", required=True,
        help="retained calibration source image; repeat once per artifact hash",
    )
    parser.add_argument("--depth-frame", required=True)
    parser.add_argument(
        "--site-aggregation-report",
        required=True,
        help="hash-bound four-camera report from aggregate_twin_calibration_manifests.py",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument(
        "--diagnostic-incomplete",
        action="store_true",
        help="produce a non-acceptable candidate while preserving dataset failure",
    )
    args = parser.parse_args()
    manifest_bytes = Path(args.manifest).read_bytes()
    manifest = json.loads(manifest_bytes)
    site_aggregation_report_bytes = Path(
        args.site_aggregation_report
    ).read_bytes()
    site_aggregation = verify_site_aggregation_report(
        manifest,
        manifest_bytes,
        site_aggregation_report_bytes,
    )
    if not site_aggregation["passed"]:
        report = {
            "passed": False,
            "reason": "site_aggregation_gate",
            "reasons": list(site_aggregation["reasons"]),
            "site_aggregation": site_aggregation,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 1
    depth_frame_bytes = Path(args.depth_frame).read_bytes()
    runtime_evidence = collect_runtime_calibration_evidence(
        manifest,
        args.cameras_json,
        depth_frame_bytes,
        host=args.host,
        port=args.port,
    )
    external_evidence = verify_external_evidence(
        manifest,
        annotations_bytes=Path(args.annotations).read_bytes(),
        real_frame_bytes=Path(args.real_frame).read_bytes(),
        twin_frame_bytes=Path(args.twin_frame).read_bytes(),
        cameras_bytes=Path(args.cameras_json).read_bytes(),
        intrinsics_artifact_bytes=Path(args.intrinsics_artifact).read_bytes(),
        intrinsics_source_image_bytes=[
            Path(path).read_bytes() for path in args.intrinsics_source_image
        ],
        depth_frame_bytes=depth_frame_bytes,
        runtime_evidence=runtime_evidence,
        external_evidence_paths={
            "annotations": args.annotations,
            "real_frame": args.real_frame,
            "twin_frame": args.twin_frame,
            "cameras_file": args.cameras_json,
            "intrinsics_artifact": args.intrinsics_artifact,
            "intrinsics_source_images": args.intrinsics_source_image,
            "depth_frame": args.depth_frame,
        },
    )
    if external_evidence["passed"]:
        report = optimize_manifest(
            manifest,
            manifest_bytes=manifest_bytes,
            site_aggregation_report_bytes=site_aggregation_report_bytes,
            allow_incomplete=args.diagnostic_incomplete,
            external_evidence_verified=True,
        )
    else:
        report = {
            "passed": False,
            "reason": "external_evidence_gate",
            "reasons": list(external_evidence["reasons"]),
        }
    report["external_evidence"] = external_evidence
    report["site_aggregation"] = site_aggregation
    report["runtime_calibration_evidence"] = {
        key: value for key, value in runtime_evidence.items()
        if key != "feature_worlds"
    }
    report["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
