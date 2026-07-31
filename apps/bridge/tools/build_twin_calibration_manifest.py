#!/usr/bin/env python3
"""Resolve manual real/twin annotations into one frozen calibration manifest.

The input JSON keeps point and road-polyline truth separate from generated UE5
world coordinates. Twin pixels are back-projected through a temporary depth
sensor at the baseline rig pose. The output is accepted directly by
``optimize_twin_road_geometry.py`` and never edits cameras.json.
"""

import argparse
from contextlib import contextmanager
from io import BytesIO
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

from PIL import Image, UnidentifiedImageError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_twin_camera_landmarks import (  # noqa: E402
    depth_pixel_to_world,
    encoded_depth_meters,
    wait_for_frame,
)
from digital_twin_bridge.twin_camera_rig import (  # noqa: E402
    CARLA_DEFAULT_PINHOLE_LENS,
    compute_twin_camera_transform,
    configure_twin_camera_blueprint,
    heading_to_carla_yaw,
    horizontal_fov_deg,
    load_cameras_config,
    twin_horizontal_fov_deg,
)
from digital_twin_bridge.geo_utils import (  # noqa: E402
    MAX_STRICT_MAP_ORIGIN_ERROR_M,
)


CAMERAS = {"ch1", "ch2", "ch3", "ch4"}
INTRINSICS_METHODS = {"checkerboard", "charuco"}
DISTORTION_KEYS = ("k1", "k2", "p1", "p2", "k3")
MIN_TRAIN_HOLDOUT_WORLD_SEPARATION_M = 0.25
MIN_TRAIN_HOLDOUT_IMAGE_SEPARATION_PX = 4.0
MIN_TRAIN_HOLDOUT_TWIN_SEPARATION_PX = 2.0
OFFLINE_CARLA_TRANSFORM_TOLERANCE_M = 1e-5


def _valid_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def retained_artifact_identity(path, label, *, expected_sha256=None):
    """Resolve and hash one real retained file; hash strings alone are invalid."""
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise OSError("not a regular file")
        payload = resolved.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} artifact is missing or unreadable") from exc
    identity = {
        "path": str(resolved),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    if expected_sha256 is not None and identity["sha256"] != expected_sha256:
        raise ValueError(f"{label} artifact hash does not match its declaration")
    return identity


def stage_artifact_bytes(destination, payload, label):
    """Durably stage bytes beside a destination without exposing partial output."""
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"{label} destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(
            f"{label} destination directory is missing: {destination.parent}"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".staged", dir=destination.parent
    )
    staged = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        staged.unlink(missing_ok=True)
        raise
    return staged


def staged_artifact_identity(staged, destination, label, *, expected_sha256=None):
    """Hash staged bytes while binding their identity to the final destination."""
    identity = retained_artifact_identity(
        staged, label, expected_sha256=expected_sha256
    )
    identity["path"] = str(Path(destination).expanduser().resolve())
    return identity


def _fsync_directory(directory):
    descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_staged_artifacts_no_replace(staged_destinations):
    """Publish a staged artifact set, rolling all outputs back on any failure."""
    pairs = [
        (Path(staged), Path(destination).expanduser().resolve())
        for staged, destination in staged_destinations
    ]
    published = []
    directories = {destination.parent for _staged, destination in pairs}
    try:
        for staged, destination in pairs:
            # A same-directory hard link is atomic and fails with EEXIST.  It
            # therefore supplies the no-replace guarantee that Path.rename lacks.
            os.link(staged, destination)
            published.append(destination)
        for directory in directories:
            _fsync_directory(directory)
    except BaseException:
        for destination in reversed(published):
            destination.unlink(missing_ok=True)
        for directory in directories:
            _fsync_directory(directory)
        raise
    finally:
        for staged, _destination in pairs:
            staged.unlink(missing_ok=True)


@contextmanager
def managed_sensor_actor(spawn):
    """Always attempt both sensor stop and destroy before evidence publication."""
    actor = spawn()
    try:
        yield actor
    finally:
        failures = []
        try:
            actor.stop()
        except BaseException as exc:  # cleanup must continue through destroy
            failures.append(("stop", exc))
        try:
            destroy_result = actor.destroy()
            if destroy_result is False:
                failures.append(("destroy", RuntimeError("returned False")))
        except BaseException as exc:
            failures.append(("destroy", exc))
        try:
            is_alive = actor.is_alive
        except AttributeError:
            # Test doubles and older compatible clients may not expose the
            # property.  A readable runtime property, however, is authoritative.
            pass
        except BaseException as exc:
            failures.append(("is_alive", exc))
        else:
            if is_alive is not False:
                failures.append((
                    "is_alive",
                    RuntimeError(f"remained {is_alive!r} after destroy"),
                ))
        if failures:
            details = "; ".join(
                f"{operation} failed: {error}" for operation, error in failures
            )
            raise RuntimeError(f"UE5 depth sensor cleanup failed ({details})")


def bind_survey_record_artifacts(features):
    """Re-hash every external survey record before any UE5 connection."""
    bound = []
    for feature in features:
        item = dict(feature)
        if feature.get("type") == "point":
            item["survey_record"] = retained_artifact_identity(
                feature["survey_record_path"],
                f"{feature['id']} survey record",
                expected_sha256=feature["survey_record_sha256"],
            )
        bound.append(item)
    return bound


def validate_strict_projection_provenance(
    provenance, *, map_name, opendrive_sha256
):
    """Require the exact OpenDRIVE projection used for world back-projection."""
    if not isinstance(provenance, dict):
        raise ValueError("strict OpenDRIVE projection provenance is missing")
    origin_error = provenance.get("map_origin_error_m")
    if (
        provenance.get("source") != "opendrive_georeference"
        or provenance.get("strict") is not True
        or provenance.get("map_name") != str(map_name)
        or not _valid_sha256(opendrive_sha256)
        or provenance.get("opendrive_sha256") != opendrive_sha256
        or not _valid_sha256(provenance.get("georeference_sha256"))
        or isinstance(origin_error, bool)
        or not isinstance(origin_error, (int, float))
        or not math.isfinite(float(origin_error))
        or not 0.0 <= float(origin_error) <= MAX_STRICT_MAP_ORIGIN_ERROR_M
    ):
        raise ValueError(
            "strict OpenDRIVE projection provenance is malformed, fallback, "
            "or content-mismatched"
        )
    return {
        "source": "opendrive_georeference",
        "strict": True,
        "map_origin_error_m": float(origin_error),
        "map_name": str(map_name),
        "opendrive_sha256": opendrive_sha256,
        "georeference_sha256": provenance["georeference_sha256"],
    }


def decoded_image_size(image_bytes, label):
    """Return retained image dimensions, rejecting corrupt/truncated evidence."""
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.verify()
        with Image.open(BytesIO(image_bytes)) as image:
            return tuple(int(value) for value in image.size)
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError(f"{label} frame is not a valid retained image") from exc


def depth_neighborhood_evidence(raw_data, width, height, u, v):
    """Validate and describe the exact 3x3 depth neighborhood at one pixel."""
    center_u, center_v = int(round(float(u))), int(round(float(v)))
    if not (1 <= center_u < width - 1 and 1 <= center_v < height - 1):
        raise ValueError("annotated twin pixel lacks a full depth neighborhood")
    samples = [
        encoded_depth_meters(raw_data, width, center_u + du, center_v + dv)
        for dv in (-1, 0, 1)
        for du in (-1, 0, 1)
    ]
    if any(not 0.25 <= value <= 250.0 for value in samples):
        raise ValueError("annotated twin pixel has implausible neighborhood depth")
    median = sorted(samples)[len(samples) // 2]
    tolerance = max(0.25, 0.02 * median)
    maximum_deviation = max(abs(value - median) for value in samples)
    if maximum_deviation > tolerance:
        raise ValueError("annotated twin pixel lies on a depth discontinuity")
    return {
        "center_depth_m": float(samples[4]),
        "median_depth_m": float(median),
        "minimum_depth_m": float(min(samples)),
        "maximum_depth_m": float(max(samples)),
        "maximum_deviation_m": float(maximum_deviation),
        "allowed_deviation_m": float(tolerance),
    }


def stable_depth_meters(raw_data, width, height, u, v):
    """Reject annotations on depth discontinuities before freezing world truth."""
    return depth_neighborhood_evidence(raw_data, width, height, u, v)[
        "center_depth_m"
    ]


def validate_intrinsics_calibration(camera):
    """Validate independently measured physical-camera optical evidence."""
    calibration = camera.get("intrinsics_calibration")
    intrinsics = camera.get("intrinsics") or {}
    if not isinstance(calibration, dict):
        raise ValueError("camera lacks measured intrinsics_calibration evidence")
    if calibration.get("method") not in INTRINSICS_METHODS:
        raise ValueError("intrinsics calibration method must be checkerboard or charuco")
    artifact_hash = str(calibration.get("artifact_sha256") or "")
    if len(artifact_hash) != 64 or any(char not in "0123456789abcdef" for char in artifact_hash):
        raise ValueError("intrinsics calibration artifact SHA-256 is invalid")
    image_count = calibration.get("image_count")
    rms = calibration.get("rms_reprojection_error_px")
    if isinstance(image_count, bool) or not isinstance(image_count, int) or image_count < 10:
        raise ValueError("intrinsics calibration requires at least 10 accepted images")
    source_hashes = calibration.get("source_images_sha256")
    if (
        not isinstance(source_hashes, list)
        or len(source_hashes) != image_count
        or len(set(source_hashes)) != len(source_hashes)
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
            for value in source_hashes
        )
    ):
        raise ValueError("intrinsics calibration requires one unique SHA-256 per accepted image")
    if (
        isinstance(rms, bool)
        or not isinstance(rms, (int, float))
        or not math.isfinite(float(rms))
        or not 0.0 <= float(rms) <= 2.0
    ):
        raise ValueError("intrinsics calibration RMS must be finite and no worse than 2 px")
    resolution = calibration.get("resolution")
    if resolution != [intrinsics.get("width"), intrinsics.get("height")]:
        raise ValueError("intrinsics calibration resolution does not match camera intrinsics")
    matrix = calibration.get("camera_matrix")
    expected = [
        [intrinsics.get("fx"), 0.0, intrinsics.get("cx")],
        [0.0, intrinsics.get("fy"), intrinsics.get("cy")],
        [0.0, 0.0, 1.0],
    ]
    try:
        matrix_matches = all(
            math.isclose(float(actual), float(wanted), rel_tol=0.0, abs_tol=1e-6)
            for actual_row, expected_row in zip(matrix, expected)
            for actual, wanted in zip(actual_row, expected_row)
        ) and len(matrix) == 3 and all(len(row) == 3 for row in matrix)
    except (TypeError, ValueError):
        matrix_matches = False
    if not matrix_matches:
        raise ValueError("intrinsics calibration matrix does not match camera intrinsics")
    distortion = calibration.get("distortion")
    if not isinstance(distortion, dict) or any(key not in distortion for key in DISTORTION_KEYS):
        raise ValueError("intrinsics calibration lacks Brown-Conrady distortion coefficients")
    if not all(
        isinstance(distortion[key], (int, float))
        and not isinstance(distortion[key], bool)
        and math.isfinite(float(distortion[key]))
        for key in DISTORTION_KEYS
    ):
        raise ValueError("intrinsics calibration distortion coefficients are invalid")
    return {
        "method": calibration["method"],
        "artifact_sha256": artifact_hash,
        "image_count": image_count,
        "source_images_sha256": list(source_hashes),
        "rms_reprojection_error_px": float(rms),
        "resolution": [int(value) for value in resolution],
        "camera_matrix": [[float(value) for value in row] for row in matrix],
        "distortion": {key: float(distortion[key]) for key in DISTORTION_KEYS},
    }


def validate_intrinsics_artifact(camera, artifact_bytes):
    """Bind the declared calibration values to a real, retained JSON artifact."""
    normalized = validate_intrinsics_calibration(camera)
    actual_hash = hashlib.sha256(artifact_bytes).hexdigest()
    if actual_hash != normalized["artifact_sha256"]:
        raise ValueError("intrinsics calibration artifact hash does not match camera config")
    try:
        payload = json.loads(artifact_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("intrinsics calibration artifact is not valid JSON") from exc
    declared_payload = {
        key: value for key, value in normalized.items() if key != "artifact_sha256"
    }
    if payload != declared_payload:
        raise ValueError("intrinsics calibration artifact contents do not match camera config")
    return normalized


def validate_intrinsics_source_images(camera, source_paths):
    """Bind the declared calibration source hashes to retained image files."""
    expected = set(validate_intrinsics_calibration(camera)["source_images_sha256"])
    actual = []
    expected_size = (
        int(camera["intrinsics"]["width"]),
        int(camera["intrinsics"]["height"]),
    )
    for path in source_paths:
        payload = Path(path).read_bytes()
        if decoded_image_size(payload, f"intrinsics source {path}") != expected_size:
            raise ValueError(
                "intrinsics source image dimensions do not match camera resolution"
            )
        actual.append(hashlib.sha256(payload).hexdigest())
    if len(actual) != len(expected) or len(set(actual)) != len(actual):
        raise ValueError("intrinsics source image count or uniqueness does not match")
    if set(actual) != expected:
        raise ValueError("intrinsics source image hashes do not match calibration artifact")
    return sorted(actual)


def canonical_camera_sha256(camera):
    return hashlib.sha256(json.dumps(
        camera, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")).hexdigest()


def offline_depth_pixel_to_world(baseline, u, v, depth_m, fov_deg, width, height):
    """Reproduce CARLA Transform.transform without importing a runtime client."""
    focal = (float(width) / 2.0) / math.tan(math.radians(float(fov_deg)) / 2.0)
    local = (
        float(depth_m),
        (float(u) - float(width) / 2.0) * float(depth_m) / focal,
        -(float(v) - float(height) / 2.0) * float(depth_m) / focal,
    )
    pitch = math.radians(float(baseline["pitch_deg"]))
    yaw = math.radians(float(baseline["yaw_deg"]))
    roll = math.radians(float(baseline["roll_deg"]))
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    matrix = (
        (cp * cy, cy * sp * sr - sy * cr, -cy * sp * cr - sy * sr),
        (cp * sy, sy * sp * sr + cy * cr, -sy * sp * cr + cy * sr),
        (sp, -cp * sr, cp * cr),
    )
    return [
        float(baseline["location"][axis])
        + sum(matrix[axis][index] * local[index] for index in range(3))
        for axis in range(3)
    ]


def validate_manifest_semantic_bindings(manifest):
    """Recompute one complete builder manifest from its retained source bytes."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    sources = manifest.get("source_artifacts")
    depth_identity = manifest.get("depth_frame")
    if not isinstance(sources, dict) or not isinstance(depth_identity, dict):
        raise ValueError("manifest retained artifacts are incomplete")

    def payload(key):
        identity = sources.get(key)
        if not isinstance(identity, dict):
            raise ValueError(f"manifest {key} identity is missing")
        return Path(identity["path"]).read_bytes()

    try:
        annotation_bytes = payload("annotations")
        annotation_payload = json.loads(annotation_bytes)
        cameras_bytes = payload("cameras_file")
        cameras_payload = json.loads(cameras_bytes)
        intrinsics_bytes = payload("intrinsics_artifact")
    except (OSError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("retained semantic JSON artifacts are invalid") from exc
    if not isinstance(annotation_payload, dict) or not isinstance(cameras_payload, dict):
        raise ValueError("retained semantic JSON artifacts must be objects")
    cameras = cameras_payload.get("cameras")
    matches = [
        camera for camera in cameras or []
        if isinstance(camera, dict) and camera.get("id") == manifest.get("camera_id")
    ]
    if len(matches) != 1:
        raise ValueError("cameras file must contain exactly one selected camera")
    camera = matches[0]
    if canonical_camera_sha256(camera) != manifest.get("camera_config_sha256"):
        raise ValueError("selected camera canonical SHA-256 mismatches manifest")
    if hashlib.sha256(annotation_bytes).hexdigest() != manifest.get("annotation_sha256"):
        raise ValueError("annotation semantic bytes mismatch manifest")
    if hashlib.sha256(cameras_bytes).hexdigest() != manifest.get("cameras_file_sha256"):
        raise ValueError("cameras semantic bytes mismatch manifest")

    real_bytes = payload("real_frame")
    twin_bytes = payload("twin_frame")
    real_size = decoded_image_size(real_bytes, "retained real")
    twin_size = decoded_image_size(twin_bytes, "retained twin")
    expected_real_size = (
        int(camera["intrinsics"]["width"]),
        int(camera["intrinsics"]["height"]),
    )
    if (
        real_size != expected_real_size
        or real_size != (manifest.get("width"), manifest.get("height"))
        or twin_size != (depth_identity.get("width"), depth_identity.get("height"))
    ):
        raise ValueError("retained real/twin image dimensions mismatch manifest")
    if annotation_payload.get("real_frame_sha256") != hashlib.sha256(real_bytes).hexdigest():
        raise ValueError("annotation real frame binding mismatches retained image")
    if annotation_payload.get("twin_frame_sha256") != hashlib.sha256(twin_bytes).hexdigest():
        raise ValueError("annotation twin frame binding mismatches retained image")
    if annotation_payload.get("cameras_file_sha256") != hashlib.sha256(cameras_bytes).hexdigest():
        raise ValueError("annotation cameras binding mismatches retained config")

    normalized_calibration = validate_intrinsics_artifact(camera, intrinsics_bytes)
    if normalized_calibration != manifest.get("intrinsics_calibration"):
        raise ValueError("manifest intrinsics calibration mismatches retained evidence")
    source_images = sources.get("intrinsics_source_images")
    if not isinstance(source_images, list):
        raise ValueError("manifest intrinsics source image identities are missing")
    validate_intrinsics_source_images(
        camera,
        [identity["path"] for identity in source_images if isinstance(identity, dict)],
    )

    annotations = bind_survey_record_artifacts(validate_annotations(
        annotation_payload,
        manifest["camera_id"],
        real_size,
        twin_size,
    ))
    try:
        depth_raw = Path(depth_identity["path"]).read_bytes()
    except (OSError, KeyError, TypeError) as exc:
        raise ValueError("retained depth artifact is unreadable") from exc
    if (
        len(depth_raw) != depth_identity.get("raw_data_size")
        or hashlib.sha256(depth_raw).hexdigest()
        != depth_identity.get("raw_data_sha256")
    ):
        raise ValueError("retained depth artifact mismatches manifest")
    baseline = manifest.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError("manifest baseline is missing")
    expected_fov = twin_horizontal_fov_deg(camera)
    expected_rotation = {
        "pitch_deg": float(camera["pitch_deg"])
        + float((camera.get("twin_pose") or {}).get("pitch_offset_deg", 0.0)),
        "yaw_deg": heading_to_carla_yaw(
            float(camera["heading_deg"]), float(camera["yaw_deg"])
        ) + float((camera.get("twin_pose") or {}).get("yaw_offset_deg", 0.0)),
        "roll_deg": float(camera.get("roll_deg", 0.0))
        + float((camera.get("twin_pose") or {}).get("roll_offset_deg", 0.0)),
        "fov_deg": expected_fov,
        "cx": float(camera["intrinsics"]["cx"]),
        "cy": float(camera["intrinsics"]["cy"]),
        "k1": 0.0,
    }
    if any(
        not math.isclose(
            float(baseline.get(key, math.nan)), value, rel_tol=0.0, abs_tol=1e-9
        )
        for key, value in expected_rotation.items()
    ):
        raise ValueError("manifest baseline mismatches selected camera config")
    transform = SimpleNamespace(
        location=SimpleNamespace(
            x=float(baseline["location"][0]),
            y=float(baseline["location"][1]),
            z=float(baseline["location"][2]),
        ),
        rotation=SimpleNamespace(
            pitch=float(baseline["pitch_deg"]),
            yaw=float(baseline["yaw_deg"]),
            roll=float(baseline["roll_deg"]),
        ),
    )
    if build_deployment_model(camera, transform) != manifest.get("deployment_model"):
        raise ValueError("deployment model mismatches selected camera config")

    features = manifest.get("features")
    if not isinstance(features, list) or len(features) != len(annotations):
        raise ValueError("manifest features mismatch annotation count")
    for feature, annotation in zip(features, annotations):
        annotation_keys = (
            (
                "id", "global_landmark_id", "surveyed_world",
                "survey_record_sha256", "survey_record_path", "type", "split",
                "provenance", "category", "description", "twin", "image",
            )
            if annotation["type"] == "point"
            else (
                "id", "type", "split", "provenance", "category", "description",
                "twin_polyline", "image_polyline",
            )
        )
        if any(feature.get(key) != annotation.get(key) for key in annotation_keys):
            raise ValueError("manifest feature semantics mismatch annotations")
        if annotation["type"] == "point":
            if feature.get("survey_record") != annotation.get("survey_record"):
                raise ValueError("manifest survey record identity mismatch annotations")
            pixels = [annotation["twin"]]
            stored_worlds = [feature.get("world")]
            stored_depths = [feature.get("depth_neighborhood")]
        else:
            pixels = annotation["twin_polyline"]
            stored_worlds = feature.get("world")
            stored_depths = feature.get("depth_neighborhoods")
        if (
            not isinstance(stored_worlds, list)
            or len(stored_worlds) != len(pixels)
            or not isinstance(stored_depths, list)
            or len(stored_depths) != len(pixels)
        ):
            raise ValueError("manifest resolved geometry cardinality mismatches annotations")
        for pixel, stored_world, stored_depth in zip(
            pixels, stored_worlds, stored_depths
        ):
            expected_depth = depth_neighborhood_evidence(
                depth_raw,
                int(depth_identity["width"]),
                int(depth_identity["height"]),
                pixel[0],
                pixel[1],
            )
            if stored_depth != expected_depth:
                raise ValueError("manifest depth neighborhood mismatches raw depth")
            expected_world = offline_depth_pixel_to_world(
                baseline,
                pixel[0],
                pixel[1],
                expected_depth["center_depth_m"],
                expected_fov,
                int(depth_identity["width"]),
                int(depth_identity["height"]),
            )
            if (
                not isinstance(stored_world, list)
                or len(stored_world) != 3
                or math.dist([float(value) for value in stored_world], expected_world)
                > OFFLINE_CARLA_TRANSFORM_TOLERANCE_M
            ):
                raise ValueError("manifest world geometry mismatches retained depth")
    validate_resolved_point_independence(features)
    return {
        "camera_id": manifest["camera_id"],
        "feature_count": len(features),
        "real_size": list(real_size),
        "twin_size": list(twin_size),
    }


def build_deployment_model(camera, transform):
    """Freeze the exact rig anchor/base needed to round-trip an absolute fit."""
    if camera.get("twin_lens"):
        raise ValueError("twin lens overrides are held for runtime safety")
    intrinsics = camera["intrinsics"]
    twin_pose = camera.get("twin_pose") or {}
    final_yaw = math.radians(float(transform.rotation.yaw))
    forward = float(twin_pose.get("forward_offset_m", 0.0))
    right = float(twin_pose.get("right_offset_m", 0.0))
    anchor_location = [
        float(transform.location.x)
        - forward * math.cos(final_yaw)
        + right * math.sin(final_yaw),
        float(transform.location.y)
        - forward * math.sin(final_yaw)
        - right * math.cos(final_yaw),
        float(transform.location.z) - float(twin_pose.get("height_offset_m", 0.0)),
    ]
    return {
        "type": "twin_camera_rig_v1",
        "anchor_location": anchor_location,
        "base": {
            "pitch_deg": float(camera["pitch_deg"]),
            "yaw_deg": heading_to_carla_yaw(
                float(camera["heading_deg"]), float(camera["yaw_deg"])
            ),
            "roll_deg": float(camera.get("roll_deg", 0.0)),
            "fov_deg": horizontal_fov_deg(intrinsics),
        },
        "lens": dict(CARLA_DEFAULT_PINHOLE_LENS),
    }


def _pixel(value, label, width, height):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{label} must be a two-number pixel")
    try:
        u, v = (float(value[0]), float(value[1]))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a two-number pixel") from exc
    if not all(math.isfinite(number) for number in (u, v)):
        raise ValueError(f"{label} contains a non-finite coordinate")
    if not (0.0 <= u < width and 0.0 <= v < height):
        raise ValueError(f"{label} lies outside its source image")
    return [u, v]


def convex_hull_area(points):
    """Return the 2-D convex-hull area without adding a geometry dependency."""
    ordered = sorted(set((float(point[0]), float(point[1])) for point in points))
    if len(ordered) < 3:
        return 0.0

    def cross(origin, left, right):
        return (
            (left[0] - origin[0]) * (right[1] - origin[1])
            - (left[1] - origin[1]) * (right[0] - origin[0])
        )

    lower = []
    for point in ordered:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0.0:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(ordered):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0.0:
            upper.pop()
        upper.append(point)
    hull = lower[:-1] + upper[:-1]
    return abs(sum(
        left[0] * right[1] - right[0] * left[1]
        for left, right in zip(hull, hull[1:] + hull[:1])
    )) / 2.0


def point_to_polyline_distance(point, polyline):
    """Return the shortest pixel distance from one point to finite segments."""
    best = math.inf
    for start, end in zip(polyline, polyline[1:]):
        dx, dy = end[0] - start[0], end[1] - start[1]
        denominator = dx * dx + dy * dy
        if denominator == 0.0:
            continue
        position = max(0.0, min(1.0, (
            (point[0] - start[0]) * dx + (point[1] - start[1]) * dy
        ) / denominator))
        projected = [start[0] + position * dx, start[1] + position * dy]
        best = min(best, math.hypot(
            point[0] - projected[0], point[1] - projected[1]
        ))
    return best


def validate_annotation_geometry(features, real_size, twin_size):
    """Reject collinear/clustered truth and holdouts copied from fit geometry."""
    for split in ("train", "holdout"):
        points = [
            feature for feature in features
            if feature["type"] == "point" and feature["split"] == split
        ]
        for field, size in (("image", real_size), ("twin", twin_size)):
            pixels = [feature[field] for feature in points]
            width, height = size
            horizontal = (max(p[0] for p in pixels) - min(p[0] for p in pixels)) / width
            vertical = (max(p[1] for p in pixels) - min(p[1] for p in pixels)) / height
            area = convex_hull_area(pixels) / (width * height)
            if horizontal < 0.5 or vertical < 0.3:
                raise ValueError(f"{split} {field} points lack required image coverage")
            if area < 0.02:
                raise ValueError(f"{split} {field} points are collinear or clustered")

    train_roads = [
        feature for feature in features
        if feature["type"] == "polyline" and feature["split"] == "train"
    ]
    holdouts = [
        feature for feature in features
        if feature["type"] == "point" and feature["split"] == "holdout"
    ]
    for field in ("image", "twin"):
        pixels = [tuple(feature[field]) for feature in features if feature["type"] == "point"]
        if len(set(pixels)) != len(pixels):
            raise ValueError(f"point {field} pixels must be distinct across train and holdout")

    roads = [feature for feature in features if feature["type"] == "polyline"]
    for line_field, size in (
        ("image_polyline", real_size), ("twin_polyline", twin_size)
    ):
        for road in roads:
            polyline = road[line_field]
            if any(left == right for left, right in zip(polyline, polyline[1:])):
                raise ValueError(f"{road['id']}: {line_field} has a zero-length segment")
            length = sum(
                math.dist(left, right) for left, right in zip(polyline, polyline[1:])
            )
            if length < 0.01 * size[0]:
                raise ValueError(f"{road['id']}: {line_field} is too short")
        canonical = [
            min(tuple(map(tuple, road[line_field])), tuple(reversed(tuple(map(tuple, road[line_field])))))
            for road in roads
        ]
        if len(set(canonical)) != len(canonical):
            raise ValueError(f"{line_field} roads must be geometrically unique")

    for field, line_field, size in (
        ("image", "image_polyline", real_size),
        ("twin", "twin_polyline", twin_size),
    ):
        threshold = 0.005 * size[0]
        independent = sum(
            min(
                point_to_polyline_distance(point[field], road[line_field])
                for road in train_roads
            ) > threshold
            for point in holdouts
        )
        if independent < 2:
            raise ValueError(f"holdout {field} points are not independent of fit roads")

    holdout_roads = [road for road in roads if road["split"] == "holdout"]
    for line_field, size in (
        ("image_polyline", real_size), ("twin_polyline", twin_size)
    ):
        threshold = 0.005 * size[0]
        for holdout in holdout_roads:
            for train in train_roads:
                distances = [
                    point_to_polyline_distance(point, train[line_field])
                    for point in holdout[line_field]
                ] + [
                    point_to_polyline_distance(point, holdout[line_field])
                    for point in train[line_field]
                ]
                if distances and max(distances) <= threshold:
                    raise ValueError(
                        f"holdout {line_field} road duplicates fit geometry"
                    )


def validate_resolved_point_independence(features):
    """Reject train/holdout landmark reuse in image, twin, or world space."""
    train = [
        feature
        for feature in features
        if feature["type"] == "point" and feature["split"] == "train"
    ]
    holdout = [
        feature
        for feature in features
        if feature["type"] == "point" and feature["split"] == "holdout"
    ]
    spaces = (
        ("image", MIN_TRAIN_HOLDOUT_IMAGE_SEPARATION_PX, "px"),
        ("twin", MIN_TRAIN_HOLDOUT_TWIN_SEPARATION_PX, "px"),
        ("world", MIN_TRAIN_HOLDOUT_WORLD_SEPARATION_M, "m"),
    )
    for holdout_feature in holdout:
        for train_feature in train:
            for field, threshold, unit in spaces:
                left = train_feature.get(field)
                right = holdout_feature.get(field)
                if (
                    not isinstance(left, list)
                    or not isinstance(right, list)
                    or len(left) != len(right)
                    or len(left) not in {2, 3}
                    or not all(
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and math.isfinite(float(value))
                        for value in left + right
                    )
                ):
                    raise ValueError(
                        f"resolved point {field} coordinates are invalid"
                    )
                distance = math.dist(left, right)
                if distance < threshold:
                    raise ValueError(
                        "holdout point is not independent of train point in "
                        f"{field} space ({distance:.6f}{unit} < "
                        f"{threshold:.6f}{unit})"
                    )


def validate_annotations(payload, camera_id, real_size, twin_size):
    """Return normalized annotations while preserving the frozen split."""
    if not isinstance(payload, dict):
        raise ValueError("annotation payload must be an object")
    if payload.get("camera_id") != camera_id or camera_id not in CAMERAS:
        raise ValueError("annotation camera_id does not match the requested channel")
    source_hash = str(payload.get("real_frame_sha256") or "")
    twin_hash = str(payload.get("twin_frame_sha256") or "")
    for value, label in ((source_hash, "real"), (twin_hash, "twin")):
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError(f"annotation {label} frame hash is invalid")
    config_hash = str(payload.get("cameras_file_sha256") or "")
    if len(config_hash) != 64 or any(
        char not in "0123456789abcdef" for char in config_hash
    ):
        raise ValueError("annotation cameras file hash is invalid")
    points = payload.get("points")
    roads = payload.get("roads")
    if not isinstance(points, list) or not isinstance(roads, list):
        raise ValueError("annotations require point and road lists")
    normalized = []
    identifiers = set()
    global_landmark_ids = set()
    descriptions = set()
    for feature in points:
        if not isinstance(feature, dict):
            raise ValueError("point annotation entries must be objects")
        identifier = str(feature.get("id") or "").strip()
        if not identifier or identifier in identifiers:
            raise ValueError("annotation feature IDs must be nonblank and unique")
        identifiers.add(identifier)
        split = feature.get("split")
        if split not in {"train", "holdout"}:
            raise ValueError(f"{identifier}: split must be frozen train or holdout")
        if feature.get("provenance") != "manually_verified_unique":
            raise ValueError(f"{identifier}: point provenance is not independently verified")
        global_landmark_id = feature.get("global_landmark_id")
        if (
            not isinstance(global_landmark_id, str)
            or not global_landmark_id
            or global_landmark_id.strip() != global_landmark_id
            or global_landmark_id in global_landmark_ids
        ):
            raise ValueError(
                "point global landmark IDs must be nonblank and globally unique"
            )
        global_landmark_ids.add(global_landmark_id)
        surveyed_world = feature.get("surveyed_world")
        survey_record_sha256 = feature.get("survey_record_sha256")
        survey_record_path = feature.get("survey_record_path")
        if (
            not isinstance(surveyed_world, list)
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
            or survey_record_path.strip() != survey_record_path
        ):
            raise ValueError(
                f"{identifier}: surveyed world identity is invalid or unbound"
            )
        description = str(feature.get("description") or "").strip()
        description_key = description.casefold()
        if len(description) < 8 or description_key in descriptions:
            raise ValueError(f"{identifier}: semantic description must be detailed and unique")
        descriptions.add(description_key)
        category = str(feature.get("category") or "").strip()
        if not category:
            raise ValueError(f"{identifier}: category is required")
        normalized.append({
            "id": identifier,
            "global_landmark_id": global_landmark_id,
            "surveyed_world": [float(value) for value in surveyed_world],
            "survey_record_sha256": survey_record_sha256,
            "survey_record_path": survey_record_path,
            "type": "point",
            "split": split,
            "provenance": "manually_verified_unique",
            "category": category,
            "description": description,
            "twin": _pixel(feature.get("twin"), f"{identifier}.twin", *twin_size),
            "image": _pixel(feature.get("image"), f"{identifier}.image", *real_size),
        })
    for feature in roads:
        if not isinstance(feature, dict):
            raise ValueError("road annotation entries must be objects")
        identifier = str(feature.get("id") or "").strip()
        if not identifier or identifier in identifiers:
            raise ValueError("annotation feature IDs must be nonblank and unique")
        identifiers.add(identifier)
        split = feature.get("split")
        if split not in {"train", "holdout"}:
            raise ValueError(f"{identifier}: split must be frozen train or holdout")
        if feature.get("provenance") != "manually_traced_geometry":
            raise ValueError(f"{identifier}: road provenance is not manually traced")
        description = str(feature.get("description") or "").strip()
        description_key = description.casefold()
        if len(description) < 8 or description_key in descriptions:
            raise ValueError(f"{identifier}: semantic description must be detailed and unique")
        descriptions.add(description_key)
        category = str(feature.get("category") or "").strip()
        if not category:
            raise ValueError(f"{identifier}: category is required")
        twin_polyline = feature.get("twin_polyline")
        image_polyline = feature.get("image_polyline")
        if (
            not isinstance(twin_polyline, list)
            or len(twin_polyline) < 2
            or not isinstance(image_polyline, list)
            or len(image_polyline) < 2
        ):
            raise ValueError(f"{identifier}: road polylines require at least two vertices")
        normalized.append({
            "id": identifier,
            "type": "polyline",
            "split": split,
            "provenance": "manually_traced_geometry",
            "category": category,
            "description": description,
            "twin_polyline": [
                _pixel(pixel, f"{identifier}.twin_polyline", *twin_size)
                for pixel in twin_polyline
            ],
            "image_polyline": [
                _pixel(pixel, f"{identifier}.image_polyline", *real_size)
                for pixel in image_polyline
            ],
        })
    counts = {
        (kind, split): sum(
            feature["type"] == kind and feature["split"] == split
            for feature in normalized
        )
        for kind in ("point", "polyline")
        for split in ("train", "holdout")
    }
    if counts[("point", "train")] < 8 or counts[("point", "holdout")] < 4:
        raise ValueError("annotations require at least 8 train and 4 holdout points")
    if counts[("polyline", "train")] < 3 or counts[("polyline", "holdout")] < 2:
        raise ValueError("annotations require at least 3 train and 2 holdout roads")
    validate_annotation_geometry(normalized, real_size, twin_size)
    return normalized


def resolve_manifest(
    annotations,
    *,
    camera_id,
    camera,
    transform,
    depth_image,
    depth_raw,
    expected_twin_size,
    real_frame_sha256,
    twin_frame_sha256,
    annotation_sha256,
    cameras_file_sha256,
    camera_config_sha256,
    depth_raw_sha256,
    depth_frame_artifact,
    source_artifacts,
    projection_provenance,
    ue5_map,
    ue5_map_opendrive_sha256,
):
    """Back-project validated annotations using one frozen UE5 depth frame."""
    projection = validate_strict_projection_provenance(
        projection_provenance,
        map_name=ue5_map,
        opendrive_sha256=ue5_map_opendrive_sha256,
    )
    if (int(depth_image.width), int(depth_image.height)) != tuple(expected_twin_size):
        raise ValueError("UE5 depth resolution does not match annotated twin frame")
    if len(depth_raw) != int(depth_image.width) * int(depth_image.height) * 4:
        raise ValueError("UE5 depth raw buffer size is invalid")
    fov = twin_horizontal_fov_deg(camera)

    def world_at(pixel):
        depth_evidence = depth_neighborhood_evidence(
            depth_raw,
            depth_image.width,
            depth_image.height,
            pixel[0],
            pixel[1],
        )
        depth = depth_evidence["center_depth_m"]
        if not 0.25 <= depth <= 250.0:
            raise ValueError(f"implausible UE5 depth {depth:.3f}m")
        location = depth_pixel_to_world(
            transform,
            pixel[0],
            pixel[1],
            depth,
            fov,
            depth_image.width,
            depth_image.height,
        )
        return [float(location.x), float(location.y), float(location.z)], depth_evidence

    features = []
    for feature in annotations:
        base = {
            key: feature[key]
            for key in (
                "id", "type", "split", "provenance", "category", "description"
            )
        }
        if feature["type"] == "point":
            base["global_landmark_id"] = feature["global_landmark_id"]
            base["surveyed_world"] = feature["surveyed_world"]
            base["survey_record_sha256"] = feature["survey_record_sha256"]
            base["survey_record_path"] = feature["survey_record_path"]
            base["survey_record"] = feature["survey_record"]
            world, depth_evidence = world_at(feature["twin"])
            base.update({
                "world": world,
                "twin": feature["twin"],
                "image": feature["image"],
                "depth_neighborhood": depth_evidence,
            })
        else:
            resolved = [world_at(pixel) for pixel in feature["twin_polyline"]]
            base.update({
                "world": [item[0] for item in resolved],
                "twin_polyline": feature["twin_polyline"],
                "image_polyline": feature["image_polyline"],
                "depth_neighborhoods": [item[1] for item in resolved],
            })
        features.append(base)
    validate_resolved_point_independence(features)
    intrinsics = camera["intrinsics"]
    intrinsics_calibration = validate_intrinsics_calibration(camera)
    return {
        "schema_version": 1,
        "camera_id": camera_id,
        "width": int(intrinsics["width"]),
        "height": int(intrinsics["height"]),
        "source_frame_sha256": real_frame_sha256,
        "twin_frame_sha256": twin_frame_sha256,
        "annotation_sha256": annotation_sha256,
        "cameras_file_sha256": cameras_file_sha256,
        "camera_config_sha256": camera_config_sha256,
        "source_artifacts": source_artifacts,
        "ue5_map": str(ue5_map),
        "ue5_map_opendrive_sha256": ue5_map_opendrive_sha256,
        "projection": projection,
        "depth_frame": {
            "carla_frame": int(depth_image.frame),
            "sensor_timestamp": float(depth_image.timestamp),
            "width": int(depth_image.width),
            "height": int(depth_image.height),
            "raw_data_sha256": depth_raw_sha256,
            "raw_data_size": len(depth_raw),
            "path": depth_frame_artifact["path"],
        },
        "baseline": {
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
        },
        "deployment_model": build_deployment_model(camera, transform),
        "intrinsics_calibration": intrinsics_calibration,
        "features": features,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("annotations")
    parser.add_argument("output")
    parser.add_argument("--camera", required=True, choices=sorted(CAMERAS))
    parser.add_argument("--real-frame", required=True)
    parser.add_argument("--twin-frame", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--twin-width", type=int, default=1280)
    parser.add_argument("--twin-height", type=int, default=960)
    parser.add_argument("--cameras-json")
    parser.add_argument(
        "--intrinsics-artifact",
        required=True,
        help="retained measured intrinsics JSON whose SHA-256 is frozen in cameras.json",
    )
    parser.add_argument(
        "--intrinsics-source-image",
        action="append",
        required=True,
        help="retained calibration source image; repeat once per artifact hash",
    )
    parser.add_argument(
        "--depth-frame-output",
        required=True,
        help="retained raw BGRA depth buffer used to resolve every twin annotation",
    )
    args = parser.parse_args()

    annotation_artifact = retained_artifact_identity(args.annotations, "annotation")
    annotation_bytes = Path(annotation_artifact["path"]).read_bytes()
    payload = json.loads(annotation_bytes)
    real_frame_artifact = retained_artifact_identity(args.real_frame, "real frame")
    twin_frame_artifact = retained_artifact_identity(args.twin_frame, "twin frame")
    real_bytes = Path(real_frame_artifact["path"]).read_bytes()
    twin_bytes = Path(twin_frame_artifact["path"]).read_bytes()
    real_hash = hashlib.sha256(real_bytes).hexdigest()
    twin_hash = hashlib.sha256(twin_bytes).hexdigest()
    if payload.get("real_frame_sha256") != real_hash:
        raise SystemExit("annotation real frame hash does not match input")
    if payload.get("twin_frame_sha256") != twin_hash:
        raise SystemExit("annotation twin frame hash does not match input")

    cameras_path = Path(args.cameras_json) if args.cameras_json else (
        Path(__file__).resolve().parents[3] / "config" / "cameras.json"
    )
    cameras_artifact = retained_artifact_identity(cameras_path, "cameras file")
    cameras_path = Path(cameras_artifact["path"])
    cameras_bytes = cameras_path.read_bytes()
    cameras_hash = hashlib.sha256(cameras_bytes).hexdigest()
    if payload.get("cameras_file_sha256") != cameras_hash:
        raise SystemExit("annotation cameras file hash does not match input")
    config = load_cameras_config(str(cameras_path))
    camera = next(item for item in config["cameras"] if item["id"] == args.camera)
    # Fail before connecting to or spawning anything in UE5 if the physical
    # camera's optical model has no independent measurement evidence.
    intrinsics_artifact = retained_artifact_identity(
        args.intrinsics_artifact, "intrinsics calibration"
    )
    validate_intrinsics_artifact(
        camera, Path(intrinsics_artifact["path"]).read_bytes()
    )
    validate_intrinsics_source_images(camera, args.intrinsics_source_image)
    intrinsics_source_artifacts = [
        retained_artifact_identity(path, f"intrinsics source {index}")
        for index, path in enumerate(args.intrinsics_source_image)
    ]
    real_size = decoded_image_size(real_bytes, "real")
    twin_size = decoded_image_size(twin_bytes, "twin")
    expected_real_size = (
        int(camera["intrinsics"]["width"]),
        int(camera["intrinsics"]["height"]),
    )
    if real_size != expected_real_size:
        raise SystemExit(
            f"real frame dimensions {real_size} do not match intrinsics {expected_real_size}"
        )
    if twin_size != (args.twin_width, args.twin_height):
        raise SystemExit(
            f"twin frame dimensions {twin_size} do not match depth render "
            f"{(args.twin_width, args.twin_height)}"
        )
    annotations = bind_survey_record_artifacts(validate_annotations(
        payload,
        args.camera,
        (int(camera["intrinsics"]["width"]), int(camera["intrinsics"]["height"])),
        (args.twin_width, args.twin_height),
    ))

    depth_output_path = Path(args.depth_frame_output).expanduser().resolve()
    manifest_output_path = Path(args.output).expanduser().resolve()
    retained_input_paths = {
        identity["path"]
        for identity in (
            annotation_artifact,
            real_frame_artifact,
            twin_frame_artifact,
            cameras_artifact,
            intrinsics_artifact,
            *intrinsics_source_artifacts,
        )
    }
    if depth_output_path.exists() or manifest_output_path.exists():
        raise SystemExit("manifest and depth outputs must not already exist")
    if (
        depth_output_path == manifest_output_path
        or str(depth_output_path) in retained_input_paths
        or str(manifest_output_path) in retained_input_paths
    ):
        raise SystemExit("manifest and depth outputs must be distinct from inputs")
    source_artifacts = {
        "annotations": annotation_artifact,
        "real_frame": real_frame_artifact,
        "twin_frame": twin_frame_artifact,
        "cameras_file": cameras_artifact,
        "intrinsics_artifact": intrinsics_artifact,
        "intrinsics_source_images": intrinsics_source_artifacts,
    }

    import carla

    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    carla_map = world.get_map()
    map_name = str(carla_map.name)
    opendrive = carla_map.to_opendrive()
    if not isinstance(opendrive, str) or not opendrive:
        raise SystemExit("active UE5 map has no usable OpenDRIVE document")
    opendrive_sha256 = hashlib.sha256(opendrive.encode("utf-8")).hexdigest()
    transform, projection_provenance = compute_twin_camera_transform(
        carla_map,
        config["site"],
        camera,
        require_opendrive_georeference=True,
        return_projection_provenance=True,
    )
    projection_provenance = validate_strict_projection_provenance(
        projection_provenance,
        map_name=map_name,
        opendrive_sha256=opendrive_sha256,
    )
    blueprint = world.get_blueprint_library().find("sensor.camera.depth")
    configure_twin_camera_blueprint(
        blueprint, camera, args.twin_width, args.twin_height
    )
    frames = []
    depth_stage = None
    manifest_stage = None
    try:
        with managed_sensor_actor(
            lambda: world.spawn_actor(blueprint, transform)
        ) as actor:
            actor.listen(frames.append)
            depth_image = wait_for_frame(world, frames)
            if depth_image is None:
                raise RuntimeError("no UE5 depth frame received")
            depth_raw = bytes(depth_image.raw_data)
            depth_sha256 = hashlib.sha256(depth_raw).hexdigest()
            depth_stage = stage_artifact_bytes(
                depth_output_path, depth_raw, "depth frame"
            )
            depth_frame_artifact = staged_artifact_identity(
                depth_stage,
                depth_output_path,
                "depth frame",
                expected_sha256=depth_sha256,
            )
            manifest = resolve_manifest(
                annotations,
                camera_id=args.camera,
                camera=camera,
                transform=transform,
                depth_image=depth_image,
                depth_raw=depth_raw,
                expected_twin_size=(args.twin_width, args.twin_height),
                real_frame_sha256=real_hash,
                twin_frame_sha256=twin_hash,
                annotation_sha256=hashlib.sha256(annotation_bytes).hexdigest(),
                cameras_file_sha256=cameras_hash,
                camera_config_sha256=canonical_camera_sha256(camera),
                depth_raw_sha256=depth_sha256,
                depth_frame_artifact=depth_frame_artifact,
                source_artifacts=source_artifacts,
                projection_provenance=projection_provenance,
                ue5_map=map_name,
                ue5_map_opendrive_sha256=opendrive_sha256,
            )
            manifest_stage = stage_artifact_bytes(
                manifest_output_path,
                (json.dumps(manifest, indent=2) + "\n").encode("utf-8"),
                "calibration manifest",
            )
        publish_staged_artifacts_no_replace((
            (depth_stage, depth_output_path),
            (manifest_stage, manifest_output_path),
        ))
        depth_stage = None
        manifest_stage = None
    finally:
        if depth_stage is not None:
            depth_stage.unlink(missing_ok=True)
        if manifest_stage is not None:
            manifest_stage.unlink(missing_ok=True)
    print(f"wrote {len(manifest['features'])} frozen features to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
