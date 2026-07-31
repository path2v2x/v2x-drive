#!/usr/bin/env python3
"""Attach accepted, hash-bound trajectory samples to persisted detections.

This is an offline adapter, not a reviewer or optimizer.  It accepts only
already acceptance-eligible consensus, factor-graph, and identity artifacts and
will not convert the current diagnostic factor-graph report or baseline GPS into
reviewed placement truth.
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import errno
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile

import cv2
import numpy as np


BRIDGE_APP = Path(__file__).resolve().parents[2] / "bridge"
if str(BRIDGE_APP) not in sys.path:
    sys.path.insert(0, str(BRIDGE_APP))

from digital_twin_bridge.reviewed_localization import (  # noqa: E402
    MAX_INDEPENDENT_REFERENCE_ERROR_M,
    MAX_LOCALIZATION_UNCERTAINTY_M,
    MAX_TRANSIT_SECONDS,
    MAX_VEHICLE_ACCELERATION_MPS2,
    MAX_VEHICLE_SPEED_MPS,
    MIN_APPEARANCE_SIMILARITY,
    CameraPlacementContext,
    ReviewedLocalizationError,
    ReviewedPlacementContext,
    SCHEMA,
    SHA256_RE,
    TRAJECTORY_SCHEMA,
    canonical_json_bytes,
    canonical_object_sha256,
    load_authority_registry,
    placement_key_sha256,
    seal_contract,
    sha256_bytes,
    validate_measured_intrinsics,
    validate_static_calibration,
    validate_contract,
    verify_authenticated_artifact,
)


ATTACHMENT_SCHEMA = "v2x-reviewed-localization-attachment/v1"
CONSENSUS_SCHEMA = "v2x-reviewed-footprint-consensus/v1"
FACTOR_SCHEMA = "v2x-reviewed-detection-factor-graph/v1"
IDENTITY_SCHEMA = "v2x-reviewed-trajectory-identity/v1"
REFERENCE_SCHEMA = "v2x-independent-vehicle-reference/v1"
REFERENCE_MEASUREMENTS_SCHEMA = "v2x-independent-rtk-measurements/v1"
BLUEPRINT_CATALOG_SCHEMA = "v2x-reviewed-ue5-blueprint-catalog/v1"
APPEARANCE_MODEL_SCHEMA = "v2x-pinned-vehicle-appearance-model/v1"


class AttachmentError(RuntimeError):
    pass


def _fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_directory_noreplace(source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise AttachmentError("atomic no-replace publication is unavailable")
    renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100, os.fsencode(source), -100, os.fsencode(destination), 1
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise AttachmentError("output evidence bundle already exists")
    raise AttachmentError(
        f"atomic no-replace publication failed: {os.strerror(error)}"
    )


def _write_fsynced_exclusive(path, payload):
    descriptor = os.open(
        str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_json(path, label):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AttachmentError(f"{label} is unreadable or invalid") from exc
    if not isinstance(value, dict):
        raise AttachmentError(f"{label} is not an object")
    return path, raw, value


def load_bound_json(base, descriptor, label, expected_schema=None):
    if not isinstance(descriptor, dict):
        raise AttachmentError(f"{label} descriptor is missing")
    value = descriptor.get("path")
    if not isinstance(value, str) or not value:
        raise AttachmentError(f"{label} path is missing")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(base) / path
    path, raw, parsed = load_json(path, label)
    if sha256_bytes(raw) != descriptor.get("sha256"):
        raise AttachmentError(f"{label} hash does not match")
    declared_schema = descriptor.get("schema")
    if expected_schema is not None and declared_schema != expected_schema:
        raise AttachmentError(f"{label} descriptor schema is not allowlisted")
    if declared_schema is not None and parsed.get("schema") != declared_schema:
        raise AttachmentError(f"{label} schema does not match")
    return path, raw, parsed


def load_bound_file(base, descriptor, label):
    if not isinstance(descriptor, dict):
        raise AttachmentError(f"{label} descriptor is missing")
    value = descriptor.get("path")
    if not isinstance(value, str) or not value:
        raise AttachmentError(f"{label} path is missing")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(base) / path
    try:
        path = path.resolve(strict=True)
        raw = path.read_bytes()
    except OSError as exc:
        raise AttachmentError(f"{label} is unreadable") from exc
    if sha256_bytes(raw) != descriptor.get("sha256"):
        raise AttachmentError(f"{label} hash does not match")
    return path, raw


def verify_bound_image(raw, resolution, label, *, require_nonempty=False):
    if (
        not isinstance(resolution, list)
        or len(resolution) != 2
        or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in resolution)
    ):
        raise AttachmentError(f"{label} native resolution is invalid")
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or [image.shape[1], image.shape[0]] != resolution:
        raise AttachmentError(f"{label} is not a matching native-resolution image")
    if require_nonempty:
        if image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if int(np.count_nonzero(image)) == 0:
            raise AttachmentError(f"{label} is empty")


def _array_identity(value):
    array = np.ascontiguousarray(value)
    identity = {
        "dtype": str(array.dtype),
        "shape": list(array.shape),
        "data_sha256": sha256_bytes(array.tobytes()),
    }
    return canonical_object_sha256(identity), identity


def _producer_inference_evidence(base, detection, sample, camera_id):
    evidence = (
        detection.get("raw_observation", {}).get("inference_evidence")
        if isinstance(detection.get("raw_observation"), dict) else None
    )
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema") != "v2x-persisted-inference-evidence/v1"
        or evidence.get("acceptance_eligible") is not True
        or evidence.get("reason") is not None
    ):
        raise AttachmentError("detection lacks acceptance-eligible producer inference evidence")
    _exact_keys(
        evidence,
        {
            "schema", "acceptance_eligible", "reason", "manifest_path",
            "manifest_sha256", "frame_pixel_sha256", "mask_pixel_sha256",
            "detector_output_sha256",
        },
        "producer inference evidence",
    )
    descriptor = {
        "path": evidence.get("manifest_path"),
        "sha256": evidence.get("manifest_sha256"),
        "schema": "v2x-persisted-inference-event/v1",
    }
    _path, _raw, manifest = load_bound_json(
        base, descriptor, "producer inference event",
        "v2x-persisted-inference-event/v1",
    )
    _exact_keys(
        manifest,
        {
            "schema", "event_id", "camera_id", "device_id", "session_id",
            "media_timestamp_utc", "pts_seconds", "frame", "instance_mask",
            "detector_output", "detector_output_sha256",
        },
        "producer inference event",
    )
    timing = sample.get("timing") if isinstance(sample, dict) else None
    try:
        manifest_pts = float(manifest.get("pts_seconds"))
        sample_pts = float(timing.get("pts_seconds"))
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise AttachmentError(
            "producer inference event timing/session linkage is invalid"
        ) from exc
    if (
        manifest.get("event_id") != detection.get("event_id")
        or manifest.get("camera_id") != camera_id
        or manifest.get("device_id") != detection.get("device_id")
        or not isinstance(timing, dict)
        or manifest.get("session_id") != timing.get("session_id")
        or manifest.get("media_timestamp_utc") != timing.get("media_timestamp_utc")
        or isinstance(manifest.get("pts_seconds"), bool)
        or not math.isfinite(manifest_pts)
        or not math.isfinite(sample_pts)
        or abs(manifest_pts - sample_pts) > 1e-9
    ):
        raise AttachmentError("producer inference event timing/session linkage is invalid")
    frame_descriptor = manifest.get("frame")
    mask_descriptor = manifest.get("instance_mask")
    _exact_keys(
        frame_descriptor,
        {
            "path", "sha256", "pixel_sha256", "array_identity", "encoding",
            "resolution", "stddev",
        },
        "producer inference frame",
    )
    _exact_keys(
        mask_descriptor,
        {
            "path", "sha256", "pixel_sha256", "array_identity", "encoding",
            "pixel_count", "bbox_fill_ratio", "vehicle_pixel_stddev",
        },
        "producer instance mask",
    )
    if (
        not isinstance(frame_descriptor, dict)
        or not isinstance(mask_descriptor, dict)
        or sample.get("frame") != {
            "path": frame_descriptor.get("path"),
            "sha256": frame_descriptor.get("sha256"),
        }
        or sample.get("mask") != {
            "path": mask_descriptor.get("path"),
            "sha256": mask_descriptor.get("sha256"),
        }
    ):
        raise AttachmentError("sample substituted producer-bound frame or mask")
    _frame_path, frame_raw = load_bound_file(
        base, frame_descriptor, "producer inference frame"
    )
    _mask_path, mask_raw = load_bound_file(
        base, mask_descriptor, "producer instance mask"
    )
    frame = cv2.imdecode(np.frombuffer(frame_raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    raw_mask = cv2.imdecode(np.frombuffer(mask_raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if frame is None or raw_mask is None:
        raise AttachmentError("producer frame or instance mask is undecodable")
    binary_mask = np.asarray(raw_mask > 0, dtype=np.uint8)
    frame_pixel_sha256, frame_identity = _array_identity(frame)
    mask_pixel_sha256, mask_identity = _array_identity(binary_mask)
    _exact_keys(
        frame_descriptor.get("array_identity"),
        {"dtype", "shape", "data_sha256"},
        "producer frame array identity",
    )
    _exact_keys(
        mask_descriptor.get("array_identity"),
        {"dtype", "shape", "data_sha256"},
        "producer mask array identity",
    )
    vehicle_pixels = frame[binary_mask > 0]
    bbox = detection.get("bbox")
    try:
        x1, y1, x2, y2 = [float(bbox[key]) for key in ("x1", "y1", "x2", "y2")]
        left = max(0, min(frame.shape[1] - 1, int(math.floor(x1))))
        top = max(0, min(frame.shape[0] - 1, int(math.floor(y1))))
        right = max(left + 1, min(frame.shape[1], int(math.ceil(x2))))
        bottom = max(top + 1, min(frame.shape[0], int(math.ceil(y2))))
        mask_pixels = int(np.count_nonzero(binary_mask))
        mask_in_box = int(np.count_nonzero(binary_mask[top:bottom, left:right]))
        fill_ratio = mask_in_box / float((right - left) * (bottom - top))
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise AttachmentError("producer detection bbox is invalid") from exc
    try:
        declared_fill_ratio = float(mask_descriptor.get("bbox_fill_ratio"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AttachmentError(
            "uniform or substituted producer frame/mask evidence"
        ) from exc
    if (
        frame_descriptor.get("encoding") != "lossless_png_bgr8"
        or mask_descriptor.get("encoding") != "lossless_png_binary_u8"
        or frame_descriptor.get("resolution") != [frame.shape[1], frame.shape[0]]
        or frame_descriptor.get("pixel_sha256") != frame_pixel_sha256
        or frame_descriptor.get("array_identity") != frame_identity
        or mask_descriptor.get("pixel_sha256") != mask_pixel_sha256
        or mask_descriptor.get("array_identity") != mask_identity
        or evidence.get("frame_pixel_sha256") != frame_pixel_sha256
        or evidence.get("mask_pixel_sha256") != mask_pixel_sha256
        or float(np.std(frame.astype(np.float32))) < 2.0
        or vehicle_pixels.size == 0
        or float(np.std(vehicle_pixels.astype(np.float32))) < 2.0
        or mask_pixels < 32
        or mask_in_box / max(mask_pixels, 1) < 0.90
        or not 0.05 <= fill_ratio <= 0.95
        or mask_descriptor.get("pixel_count") != mask_pixels
        or not math.isfinite(declared_fill_ratio)
        or abs(declared_fill_ratio - fill_ratio) > 1e-9
    ):
        raise AttachmentError("uniform or substituted producer frame/mask evidence")
    output = manifest.get("detector_output")
    if not isinstance(output, dict):
        raise AttachmentError("producer detector output is missing")
    _exact_keys(
        output,
        {
            "schema", "event_id", "camera_id", "device_id", "session_id",
            "frame_number",
            "track_id", "object_type", "confidence_score", "bbox",
            "segmentation_output_index", "mask_pixel_sha256",
            "detector_model_sha256", "detector_config_sha256",
        },
        "producer detector output",
    )
    segmentation_output_index = output.get("segmentation_output_index")
    if (
        not isinstance(segmentation_output_index, int)
        or isinstance(segmentation_output_index, bool)
        or segmentation_output_index < 0
    ):
        raise AttachmentError("producer detector output is invalid")
    expected_output = {
        "schema": "v2x-detector-instance-output/v1",
        "event_id": detection.get("event_id"),
        "camera_id": camera_id,
        "device_id": detection.get("device_id"),
        "session_id": timing.get("session_id"),
        "frame_number": sample.get("frame_number"),
        "track_id": detection.get("track_id"),
        "object_type": detection.get("object_type"),
        "confidence_score": detection.get("confidence_score"),
        "bbox": detection.get("bbox"),
        "segmentation_output_index": segmentation_output_index,
        "mask_pixel_sha256": mask_pixel_sha256,
        "detector_model_sha256": detection.get("raw_observation", {}).get("fingerprints", {}).get("detector_model_sha256"),
        "detector_config_sha256": detection.get("raw_observation", {}).get("fingerprints", {}).get("detector_config_sha256"),
    }
    output_sha256 = canonical_object_sha256(expected_output)
    if (
        output != expected_output
        or manifest.get("detector_output_sha256") != output_sha256
        or evidence.get("detector_output_sha256") != output_sha256
    ):
        raise AttachmentError("producer detector output identity is mismatched")
    return frame_raw, mask_raw, frame, binary_mask, {
        "inference_manifest_sha256": evidence.get("manifest_sha256"),
        "frame_pixel_sha256": frame_pixel_sha256,
        "mask_pixel_sha256": mask_pixel_sha256,
        "detector_output_sha256": output_sha256,
    }


def load_detections(path):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AttachmentError("detection NDJSON is unreadable") from exc
    rows = []
    by_event = {}
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AttachmentError(f"detection line {number} is invalid") from exc
        event_id = value.get("event_id") if isinstance(value, dict) else None
        if not isinstance(event_id, str) or not event_id or event_id in by_event:
            raise AttachmentError("detection event IDs are missing or duplicated")
        rows.append(value)
        by_event[event_id] = value
    if not rows:
        raise AttachmentError("detection NDJSON is empty")
    return path, raw, rows, by_event


def _accepted_event_index(artifact, label):
    events = artifact.get("events")
    if artifact.get("acceptance_eligible") is not True or not isinstance(events, list):
        raise AttachmentError(f"{label} is not acceptance eligible")
    output = {}
    for event in events:
        event_id = event.get("event_id") if isinstance(event, dict) else None
        if (
            not isinstance(event_id, str)
            or not event_id
            or event_id in output
            or event.get("accepted") is not True
            or event.get("ambiguity") is not False
        ):
            raise AttachmentError(f"{label} event result is invalid or ambiguous")
        output[event_id] = event
    if not output:
        raise AttachmentError(f"{label} has no accepted event results")
    return output


def _exact_keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise AttachmentError(f"{label} has unexpected or missing fields")


def _finite_vector(value, size, label):
    if (
        not isinstance(value, list)
        or len(value) != size
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in value
        )
    ):
        raise AttachmentError(f"{label} is invalid")
    return [float(item) for item in value]


def _position(value, label):
    if not isinstance(value, dict) or any(
        isinstance(value.get(axis), bool)
        or not isinstance(value.get(axis), (int, float))
        or not math.isfinite(float(value[axis]))
        for axis in ("x", "y", "z")
    ):
        raise AttachmentError(f"{label} is invalid")
    return {axis: float(value[axis]) for axis in ("x", "y", "z")}


def _matrix_psd(value, size, label):
    try:
        matrix = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise AttachmentError(f"{label} is invalid") from exc
    if (
        matrix.shape != (size, size)
        or not np.isfinite(matrix).all()
        or not np.allclose(matrix, matrix.T, rtol=0.0, atol=1e-9)
        or float(np.linalg.eigvalsh(matrix).min()) < -1e-9
    ):
        raise AttachmentError(f"{label} is not finite PSD")
    return matrix


def _parse_utc(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AttachmentError(f"{label} is invalid")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").timestamp()
    except ValueError as exc:
        raise AttachmentError(f"{label} is invalid") from exc


def _appearance_embedding(frame, mask, bbox, model):
    if (
        model.get("algorithm") != "masked-bgr-histogram-l2/v1"
        or model.get("bins") != [8, 8, 8]
        or model.get("crop_source") != "producer_instance_mask"
    ):
        raise AttachmentError("appearance model algorithm is not allowlisted")
    try:
        x1, y1, x2, y2 = [float(bbox[key]) for key in ("x1", "y1", "x2", "y2")]
    except (KeyError, TypeError, ValueError) as exc:
        raise AttachmentError("appearance crop bbox is invalid") from exc
    left = max(0, min(frame.shape[1] - 1, int(math.floor(x1))))
    top = max(0, min(frame.shape[0] - 1, int(math.floor(y1))))
    right = max(left + 1, min(frame.shape[1], int(math.ceil(x2))))
    bottom = max(top + 1, min(frame.shape[0], int(math.ceil(y2))))
    crop = frame[top:bottom, left:right]
    crop_mask = mask[top:bottom, left:right] > 0
    pixels = crop[crop_mask]
    if pixels.shape[0] < 32:
        raise AttachmentError("appearance crop has insufficient vehicle pixels")
    histogram, _edges = np.histogramdd(
        pixels.astype(np.float64),
        bins=(8, 8, 8),
        range=((0, 256), (0, 256), (0, 256)),
    )
    flattened = histogram.reshape(-1)
    norm = float(np.linalg.norm(flattened))
    if not math.isfinite(norm) or norm <= 0.0:
        raise AttachmentError("appearance embedding is degenerate")
    embedding = flattened / norm
    normalized = [round(float(value), 12) for value in embedding]
    return embedding, sha256_bytes(canonical_json_bytes(normalized)), [
        float(left), float(top), float(right), float(bottom)
    ]


def _camera_context(cameras_path, cameras_raw, cameras_config):
    cameras = cameras_config.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise AttachmentError("cameras JSON has no camera definitions")
    indexed = {}
    for camera in cameras:
        if not isinstance(camera, dict) or not isinstance(camera.get("id"), str):
            raise AttachmentError("cameras JSON contains an invalid camera")
        try:
            artifact_hash, report_hash = validate_measured_intrinsics(
                camera, cameras_path.parent
            )
        except ReviewedLocalizationError as exc:
            raise AttachmentError(
                f"camera measured intrinsics rejected: {exc.reason}"
            ) from exc
        indexed[camera["id"]] = CameraPlacementContext(
            camera_config_sha256=canonical_object_sha256(camera),
            intrinsics_artifact_sha256=artifact_hash,
            intrinsics_report_sha256=report_hash,
            native_resolution=(
                int(camera["intrinsics"]["width"]),
                int(camera["intrinsics"]["height"]),
            ),
        )
    return sha256_bytes(cameras_raw), indexed


def _mask_contact_gate(mask_raw, resolution, contact, detection, label):
    mask = cv2.imdecode(np.frombuffer(mask_raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if mask is None or [mask.shape[1], mask.shape[0]] != resolution:
        raise AttachmentError(f"{label} mask is invalid")
    mask = mask > 0
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        raise AttachmentError(f"{label} mask is empty")
    bbox = detection.get("bbox") or (
        (detection.get("camera_data") or {}).get("bifocal_metadata", {}).get("bbox")
    )
    try:
        detection_box = np.asarray(
            [bbox[key] for key in ("x1", "y1", "x2", "y2")], dtype=float
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AttachmentError(f"{label} detection bbox is invalid") from exc
    mask_box = np.asarray([xs.min(), ys.min(), xs.max() + 1, ys.max() + 1], dtype=float)
    intersection = max(0.0, min(detection_box[2], mask_box[2]) - max(detection_box[0], mask_box[0])) * max(
        0.0, min(detection_box[3], mask_box[3]) - max(detection_box[1], mask_box[1])
    )
    union = (
        (detection_box[2] - detection_box[0]) * (detection_box[3] - detection_box[1])
        + (mask_box[2] - mask_box[0]) * (mask_box[3] - mask_box[1])
        - intersection
    )
    if union <= 0.0 or intersection / union < 0.50:
        raise AttachmentError(f"{label} mask does not match the detection bbox")
    pixels = [
        _finite_vector(contact.get(key), 2, f"{label} {key}")
        for key in ("left_ground_pixel", "right_ground_pixel", "footprint_midpoint_pixel")
    ]
    support = cv2.dilate(mask.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
    bottom_tolerance = 24.0 * resolution[1] / 960.0
    for x, y in pixels:
        xi, yi = int(round(x)), int(round(y))
        if not (0 <= xi < resolution[0] and 0 <= yi < resolution[1]) or not support[yi, xi]:
            raise AttachmentError(f"{label} contact is not supported by the instance mask")
        column = np.nonzero(mask[:, max(0, xi - 2):min(resolution[0], xi + 3)])[0]
        if column.size == 0 or float(column.max()) - y > bottom_tolerance or y - float(column.max()) > 3.0:
            raise AttachmentError(f"{label} contact is not on the mask ground boundary")


def _blueprint_catalog(value):
    families = value.get("families") if isinstance(value, dict) else None
    if value.get("acceptance_eligible") is not True or not isinstance(families, dict):
        raise AttachmentError("blueprint catalog is not acceptance eligible")
    expected_families = {"passenger_car", "truck", "bus"}
    if set(families) != expected_families:
        raise AttachmentError("blueprint catalog family set is invalid")
    ids_by_family, entries = {}, {}
    for family, items in families.items():
        if not isinstance(items, list) or not items:
            raise AttachmentError("blueprint catalog family is empty")
        ids = []
        for item in items:
            bp_id = item.get("id") if isinstance(item, dict) else None
            dimensions = item.get("dimensions_m") if isinstance(item, dict) else None
            if not isinstance(bp_id, str) or not bp_id or bp_id in entries:
                raise AttachmentError("blueprint catalog IDs are invalid or duplicated")
            normalized_dimensions = _position(
                {
                    "x": dimensions.get("length") if isinstance(dimensions, dict) else None,
                    "y": dimensions.get("width") if isinstance(dimensions, dict) else None,
                    "z": dimensions.get("height") if isinstance(dimensions, dict) else None,
                },
                "blueprint catalog dimensions",
            )
            normalized_dimensions = {
                "length": normalized_dimensions["x"],
                "width": normalized_dimensions["y"],
                "height": normalized_dimensions["z"],
            }
            if any(value <= 0.0 for value in normalized_dimensions.values()):
                raise AttachmentError("blueprint catalog dimensions are invalid")
            ids.append(bp_id)
            entries[(family, bp_id)] = normalized_dimensions
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise AttachmentError("blueprint catalog pools must be sorted and unique")
        ids_by_family[family] = ids
    return ids_by_family, entries


def attach(detections_path, trajectory_path, output_path, authority_key_file):
    detections_path, detections_raw, rows, by_event = load_detections(
        detections_path
    )
    trajectory_path, trajectory_raw, trajectory = load_json(
        trajectory_path, "reviewed trajectory"
    )
    if (
        trajectory.get("schema") != TRAJECTORY_SCHEMA
        or trajectory.get("acceptance_eligible") is not True
    ):
        raise AttachmentError("reviewed trajectory is not acceptance eligible")
    source = trajectory.get("source")
    if not isinstance(source, dict):
        raise AttachmentError("reviewed trajectory source bindings are missing")
    if source.get("detections_ndjson_sha256") != sha256_bytes(detections_raw):
        raise AttachmentError("trajectory detection source hash does not match")
    base = trajectory_path.parent
    _consensus_path, consensus_raw, consensus = load_bound_json(
        base, source.get("consensus"), "reviewed contact consensus", CONSENSUS_SCHEMA
    )
    _factor_path, factor_raw, factor = load_bound_json(
        base, source.get("factor_graph"), "factor-graph result", FACTOR_SCHEMA
    )
    _identity_path, identity_raw, identity = load_bound_json(
        base, source.get("identity"), "trajectory identity result", IDENTITY_SCHEMA
    )
    _reference_path, reference_raw, reference = load_bound_json(
        base, source.get("independent_reference"), "independent reference", REFERENCE_SCHEMA
    )
    _measurements_path, measurements_raw, measurements = load_bound_json(
        base,
        reference.get("source") if isinstance(reference, dict) else None,
        "independent RTK/survey measurements",
        REFERENCE_MEASUREMENTS_SCHEMA,
    )
    _catalog_path, catalog_raw, catalog = load_bound_json(
        base, source.get("blueprint_catalog"), "UE5 blueprint catalog", BLUEPRINT_CATALOG_SCHEMA
    )
    _appearance_path, appearance_raw, appearance_model = load_bound_json(
        base,
        source.get("appearance_model"),
        "pinned vehicle appearance model",
        APPEARANCE_MODEL_SCHEMA,
    )
    static_path, static_raw, _static = load_bound_json(
        base,
        source.get("static_calibration"),
        "static calibration manifest",
        "v2x-static-camera-survey-manifest/v1",
    )
    cameras_path, cameras_raw, cameras = load_bound_json(
        base, source.get("cameras_json"), "cameras JSON"
    )
    _opendrive_path, opendrive_raw = load_bound_file(
        base, source.get("opendrive"), "OpenDRIVE map"
    )
    if (
        factor.get("gate_passed") is not True
        or factor.get("acceptance_eligible") is not True
        or factor.get("optimizer_contract") != {
            "diagnostic_until_independent_truth": False
        }
    ):
        raise AttachmentError("diagnostic factor-graph output cannot be attached")
    consensus_events = _accepted_event_index(consensus, "reviewed contact consensus")
    factor_events = _accepted_event_index(factor, "factor-graph result")
    identity_events = _accepted_event_index(identity, "trajectory identity result")
    reference_events = _accepted_event_index(reference, "independent reference")
    for event in consensus_events.values():
        _exact_keys(
            event,
            {"event_id", "accepted", "ambiguity", "camera_id", "frame_sha256", "mask_sha256", "contact"},
            "reviewed contact event",
        )
    for event in factor_events.values():
        _exact_keys(
            event,
            {"event_id", "accepted", "ambiguity", "global_track_id", "trajectory_id", "camera_id", "sample_index", "placement"},
            "factor-graph event",
        )
    for event in identity_events.values():
        _exact_keys(
            event,
            {"event_id", "accepted", "ambiguity", "global_track_id", "trajectory_id", "camera_id", "sample_index", "frame_sha256", "mask_sha256", "crop_bbox", "embedding_sha256"},
            "identity event",
        )
    for event in reference_events.values():
        _exact_keys(
            event,
            {"event_id", "accepted", "ambiguity", "global_track_id", "trajectory_id", "camera_id", "sample_index", "measurement_id"},
            "independent reference event",
        )
    reviewer = trajectory.get("reviewer")
    reviewer_ids = consensus.get("reviewer_ids")
    authority_key_id = trajectory.get("authority_key_id")
    try:
        authority_registry = load_authority_registry(authority_key_file)
    except ReviewedLocalizationError as exc:
        raise AttachmentError(f"review authority rejected: {exc.reason}") from exc
    authority_keys = {
        key_id: entry["key"] for key_id, entry in authority_registry.items()
    }
    authority_key = authority_keys.get(authority_key_id)
    if (
        not isinstance(reviewer, dict)
        or reviewer.get("kind") != "human"
        or not isinstance(reviewer.get("id"), str)
        or not isinstance(reviewer_ids, list)
        or any(not isinstance(item, str) or not item for item in reviewer_ids)
        or len(reviewer_ids) != len(set(reviewer_ids))
        or len(set(reviewer_ids)) < 2
        or reviewer["id"] not in reviewer_ids
        or reviewer["id"] != authority_key_id
        or authority_key is None
    ):
        raise AttachmentError("reviewer authority/consensus provenance is incomplete")
    try:
        trajectory_signer = verify_authenticated_artifact(
            trajectory, TRAJECTORY_SCHEMA, "reviewed_contract", authority_registry
        )
    except ReviewedLocalizationError as exc:
        raise AttachmentError(
            f"reviewed trajectory authentication rejected: {exc.reason}"
        ) from exc
    if trajectory_signer != authority_key_id:
        raise AttachmentError("reviewed trajectory signer does not match reviewer")
    _exact_keys(
        trajectory,
        {
            "schema", "authority_key_id", "acceptance_eligible",
            "global_track_id", "trajectory_id", "reviewer",
            "blueprint_dimension_tolerance_m", "source", "samples",
            "authority",
        },
        "reviewed trajectory",
    )
    authenticated_artifacts = (
        (consensus, CONSENSUS_SCHEMA, "contact_consensus", "reviewed contact consensus"),
        (factor, FACTOR_SCHEMA, "factor_graph", "factor-graph result"),
        (identity, IDENTITY_SCHEMA, "trajectory_identity", "trajectory identity result"),
        (reference, REFERENCE_SCHEMA, "independent_reference", "independent reference"),
        (measurements, REFERENCE_MEASUREMENTS_SCHEMA, "independent_reference", "independent RTK/survey measurements"),
        (catalog, BLUEPRINT_CATALOG_SCHEMA, "blueprint_catalog", "UE5 blueprint catalog"),
        (appearance_model, APPEARANCE_MODEL_SCHEMA, "appearance_model", "pinned vehicle appearance model"),
        (_static, "v2x-static-camera-survey-manifest/v1", "static_calibration", "static calibration manifest"),
    )
    artifact_signers = {}
    for artifact, schema, role, label in authenticated_artifacts:
        try:
            artifact_signers[role] = verify_authenticated_artifact(
                artifact, schema, role, authority_registry
            )
        except ReviewedLocalizationError as exc:
            raise AttachmentError(
                f"{label} authentication rejected: {exc.reason}"
            ) from exc
    if artifact_signers["independent_reference"] == authority_key_id:
        raise AttachmentError(
            "independent reference must use a distinct survey authority"
        )
    if measurements["authority"]["key_id"] != artifact_signers[
        "independent_reference"
    ]:
        raise AttachmentError(
            "independent reference and raw measurements have different authorities"
        )
    _exact_keys(
        consensus,
        {"schema", "acceptance_eligible", "reviewer_ids", "events", "authority"},
        "reviewed contact consensus",
    )
    _exact_keys(
        factor,
        {"schema", "acceptance_eligible", "gate_passed", "optimizer_contract", "events", "authority"},
        "factor-graph result",
    )
    _exact_keys(
        identity,
        {
            "schema", "acceptance_eligible", "status", "ambiguity_count",
            "global_track_id", "trajectory_id", "camera_ids",
            "appearance_model_sha256", "minimum_appearance_similarity",
            "events", "pairs", "authority",
        },
        "trajectory identity result",
    )
    _exact_keys(
        reference,
        {"schema", "acceptance_eligible", "source", "events", "authority"},
        "independent reference",
    )
    _exact_keys(
        measurements,
        {"schema", "measurements", "authority"},
        "independent RTK/survey measurements",
    )
    _exact_keys(
        catalog,
        {"schema", "acceptance_eligible", "families", "authority"},
        "UE5 blueprint catalog",
    )
    _exact_keys(
        appearance_model,
        {"schema", "algorithm", "bins", "crop_source", "authority"},
        "pinned vehicle appearance model",
    )
    measurement_rows = measurements.get("measurements")
    if not isinstance(measurement_rows, list) or not measurement_rows:
        raise AttachmentError("independent RTK/survey measurements are empty")
    measurement_index = {}
    for measurement in measurement_rows:
        measurement_id = (
            measurement.get("measurement_id")
            if isinstance(measurement, dict) else None
        )
        if (
            not isinstance(measurement_id, str)
            or not measurement_id
            or measurement_id in measurement_index
        ):
            raise AttachmentError("independent measurement IDs are invalid")
        _exact_keys(
            measurement,
            {
                "measurement_id", "event_id", "camera_id",
                "media_timestamp_utc", "map_name", "opendrive_sha256",
                "method", "source_device_id", "capture_run_id",
                "position_m", "covariance_m2", "uncertainty_m",
            },
            "independent RTK/survey measurement",
        )
        measurement_index[measurement_id] = measurement
    global_track_id = trajectory.get("global_track_id")
    trajectory_id = trajectory.get("trajectory_id")
    if (
        identity.get("status") != "unambiguous"
        or identity.get("global_track_id") != global_track_id
        or identity.get("trajectory_id") != trajectory_id
        or identity.get("acceptance_eligible") is not True
        or identity.get("ambiguity_count") != 0
    ):
        raise AttachmentError("trajectory identity is ambiguous or mismatched")
    camera_ids = identity.get("camera_ids")
    if (
        not isinstance(camera_ids, list)
        or len(camera_ids) < 2
        or any(not isinstance(item, str) or not item for item in camera_ids)
        or len(camera_ids) != len(set(camera_ids))
    ):
        raise AttachmentError("trajectory identity camera set is missing")

    cameras_json_sha256, camera_context = _camera_context(
        cameras_path, cameras_raw, cameras
    )
    opendrive_hash = sha256_bytes(opendrive_raw)
    map_name = source.get("opendrive", {}).get("map_name")
    if not isinstance(map_name, str) or not map_name:
        raise AttachmentError("OpenDRIVE map name is missing")
    camera_hashes = {
        camera_id: value.camera_config_sha256
        for camera_id, value in camera_context.items()
    }
    try:
        static_hash = validate_static_calibration(
            str(static_path),
            cameras_json_sha256,
            camera_hashes,
            {
                camera_id: value.native_resolution
                for camera_id, value in camera_context.items()
            },
            {camera["id"]: camera for camera in cameras["cameras"]},
            map_name,
            opendrive_hash,
            authority_registry,
        )
    except ReviewedLocalizationError as exc:
        raise AttachmentError(f"static calibration rejected: {exc.reason}") from exc
    if static_hash != sha256_bytes(static_raw):
        raise AttachmentError("static calibration manifest hash drifted")
    catalog_ids, catalog_entries = _blueprint_catalog(catalog)
    runtime_catalog_hash = sha256_bytes(canonical_json_bytes(catalog_ids))
    context = ReviewedPlacementContext(
        map_name=map_name,
        opendrive_sha256=opendrive_hash,
        cameras_json_sha256=cameras_json_sha256,
        cameras=camera_context,
        static_calibration_sha256=static_hash,
        authority_keys=authority_keys,
        authority_roles={
            key_id: entry["roles"]
            for key_id, entry in authority_registry.items()
        },
    )

    samples = trajectory.get("samples")
    if not isinstance(samples, list) or not samples:
        raise AttachmentError("reviewed trajectory has no samples")
    if any(not isinstance(sample, dict) for sample in samples):
        raise AttachmentError("reviewed trajectory sample is invalid")
    sample_indexes = [sample.get("sample_index") for sample in samples]
    if any(
        not isinstance(index, int) or isinstance(index, bool) or index < 0
        for index in sample_indexes
    ):
        raise AttachmentError("trajectory sample indexes are invalid")
    samples = sorted(samples, key=lambda item: item.get("sample_index", -1))
    if [sample.get("sample_index") for sample in samples] != list(range(len(samples))):
        raise AttachmentError("trajectory sample indexes must be contiguous from zero")
    sample_events = [sample.get("event_id") for sample in samples]
    if len(sample_events) != len(set(sample_events)) or any(
        not isinstance(event_id, str) or event_id not in by_event
        for event_id in sample_events
    ):
        raise AttachmentError("trajectory event IDs are unknown or duplicated")
    sample_camera_values = [sample.get("camera_id") for sample in samples]
    if any(
        not isinstance(camera_id, str) or not camera_id
        for camera_id in sample_camera_values
    ):
        raise AttachmentError("trajectory sample camera IDs are invalid")
    sample_camera_ids = set(sample_camera_values)
    if (
        set(camera_ids) != sample_camera_ids
        or any(camera_id not in camera_context for camera_id in sample_camera_ids)
    ):
        raise AttachmentError("trajectory identity camera set does not exactly match samples")
    denominators = [
        set(consensus_events), set(factor_events), set(identity_events),
        set(reference_events),
    ]
    if any(set(sample_events) != denominator for denominator in denominators):
        raise AttachmentError("semantic artifact event denominators do not match samples")
    referenced_measurement_ids = [
        reference_events[event_id].get("measurement_id")
        for event_id in sample_events
    ]
    if (
        any(
            not isinstance(measurement_id, str) or not measurement_id
            for measurement_id in referenced_measurement_ids
        )
        or len(referenced_measurement_ids)
        != len(set(referenced_measurement_ids))
        or set(referenced_measurement_ids) != set(measurement_index)
    ):
        raise AttachmentError(
            "independent measurement denominator does not exactly match samples"
        )
    pairs = identity.get("pairs")
    if not isinstance(pairs, list):
        raise AttachmentError("identity pair results are missing")
    pair_index = {}
    for pair in pairs:
        _exact_keys(
            pair,
            {"previous_event_id", "event_id", "previous_camera_id", "camera_id", "accepted", "ambiguity", "global_track_id", "trajectory_id", "appearance_similarity", "transit_seconds", "distance_m", "trajectory_covariance_m2"},
            "identity pair",
        )
        key = (
            pair.get("previous_event_id"), pair.get("event_id")
        ) if isinstance(pair, dict) else None
        if (
            key is None
            or any(not isinstance(event_id, str) or not event_id for event_id in key)
            or key in pair_index
        ):
            raise AttachmentError("identity pair results are invalid or duplicated")
        pair_index[key] = pair
    expected_pairs = set(zip(sample_events, sample_events[1:]))
    if set(pair_index) != expected_pairs:
        raise AttachmentError("identity pair evidence is not complete and exact")
    sample_camera_by_event = {
        sample["event_id"]: sample["camera_id"] for sample in samples
    }
    cross_camera_pairs = []
    for (previous_event_id, event_id), pair in pair_index.items():
        previous_camera_id = sample_camera_by_event[previous_event_id]
        camera_id = sample_camera_by_event[event_id]
        if (
            pair.get("previous_camera_id") != previous_camera_id
            or pair.get("camera_id") != camera_id
        ):
            raise AttachmentError("identity pair camera linkage is invalid")
        if previous_camera_id != camera_id:
            cross_camera_pairs.append(pair)
    if not cross_camera_pairs:
        raise AttachmentError(
            "reviewed multicamera identity lacks a cross-camera transition"
        )
    cross_camera_transition_sha256 = sha256_bytes(canonical_json_bytes(
        sorted(
            cross_camera_pairs,
            key=lambda pair: (pair["previous_event_id"], pair["event_id"]),
        )
    ))
    appearance_threshold = identity.get("minimum_appearance_similarity")
    if (
        identity.get("appearance_model_sha256") != sha256_bytes(appearance_raw)
        or not isinstance(appearance_threshold, (int, float))
        or isinstance(appearance_threshold, bool)
        or not math.isfinite(float(appearance_threshold))
        or float(appearance_threshold) < MIN_APPEARANCE_SIMILARITY
        or float(appearance_threshold) > 1.0
    ):
        raise AttachmentError("identity appearance authority is invalid")
    contracts = {}
    previous_epoch = None
    previous_position = None
    previous_speed = None
    previous_event_id = None
    previous_embedding = None
    for sample in samples:
        event_id = sample.get("event_id")
        detection = by_event.get(event_id)
        camera_id = sample.get("camera_id")
        if camera_id not in camera_ids or camera_id not in camera_context:
            raise AttachmentError("trajectory sample camera is not identity-bound")
        (
            frame_raw,
            mask_raw,
            _frame_image,
            _binary_mask,
            inference_binding,
        ) = (
            _producer_inference_evidence(
                base, detection, sample, camera_id
            )
        )
        resolution = sample.get("native_resolution")
        verify_bound_image(frame_raw, resolution, f"{event_id} native frame")
        verify_bound_image(
            mask_raw,
            resolution,
            f"{event_id} native mask",
            require_nonempty=True,
        )
        frame_hash = sha256_bytes(frame_raw)
        mask_hash = sha256_bytes(mask_raw)
        consensus_event = consensus_events[event_id]
        factor_event = factor_events[event_id]
        identity_event = identity_events[event_id]
        reference_event = reference_events[event_id]
        contact = sample.get("contact")
        embedding, embedding_sha256, crop_bbox = _appearance_embedding(
            _frame_image,
            _binary_mask,
            detection.get("bbox"),
            appearance_model,
        )
        if (
            consensus_event.get("camera_id") != camera_id
            or consensus_event.get("frame_sha256") != frame_hash
            or consensus_event.get("mask_sha256") != mask_hash
            or consensus_event.get("contact") != contact
        ):
            raise AttachmentError("reviewed contact result is not linked to the exact frame/mask/sample")
        _mask_contact_gate(mask_raw, resolution, contact, detection, event_id)
        placement = sample.get("placement")
        if (
            factor_event.get("trajectory_id") != trajectory_id
            or factor_event.get("global_track_id") != global_track_id
            or factor_event.get("camera_id") != camera_id
            or factor_event.get("sample_index") != sample.get("sample_index")
            or factor_event.get("placement") != placement
        ):
            raise AttachmentError("factor-graph result is not linked to the exact placement sample")
        if (
            identity_event.get("global_track_id") != global_track_id
            or identity_event.get("trajectory_id") != trajectory_id
            or identity_event.get("camera_id") != camera_id
            or identity_event.get("sample_index") != sample.get("sample_index")
            or identity_event.get("frame_sha256") != frame_hash
            or identity_event.get("mask_sha256") != mask_hash
            or identity_event.get("crop_bbox") != crop_bbox
            or identity_event.get("embedding_sha256") != embedding_sha256
        ):
            raise AttachmentError("identity event result is not linked to the exact sample")
        if (
            reference_event.get("global_track_id") != global_track_id
            or reference_event.get("trajectory_id") != trajectory_id
            or reference_event.get("camera_id") != camera_id
            or reference_event.get("sample_index") != sample.get("sample_index")
        ):
            raise AttachmentError(
                "independent reference is not linked to the exact sample"
            )
        measurement_id = reference_event.get("measurement_id")
        measurement = measurement_index.get(measurement_id)
        if (
            not isinstance(measurement, dict)
            or measurement.get("event_id") != event_id
            or measurement.get("camera_id") != camera_id
            or measurement.get("media_timestamp_utc")
            != sample.get("timing", {}).get("media_timestamp_utc")
            or measurement.get("map_name") != map_name
            or measurement.get("opendrive_sha256") != opendrive_hash
            or measurement.get("method") not in {
                "independent_rtk_fix",
                "independent_total_station",
            }
            or not isinstance(measurement.get("source_device_id"), str)
            or not measurement["source_device_id"]
            or measurement["source_device_id"] == detection.get("device_id")
            or not isinstance(measurement.get("capture_run_id"), str)
            or not measurement["capture_run_id"]
        ):
            raise AttachmentError(
                "independent reference lacks an exact authenticated raw measurement"
            )
        reference_position = _position(
            measurement.get("position_m"), f"{event_id} independent reference"
        )
        reference_covariance = _matrix_psd(
            measurement.get("covariance_m2"), 3, f"{event_id} reference covariance"
        )
        reference_uncertainty = measurement.get("uncertainty_m")
        if (
            not isinstance(reference_uncertainty, (int, float))
            or isinstance(reference_uncertainty, bool)
            or not 0.0 <= float(reference_uncertainty) <= 0.50
            or math.sqrt(max(0.0, float(np.linalg.eigvalsh(reference_covariance).max())))
            > float(reference_uncertainty)
        ):
            raise AttachmentError("independent reference uncertainty/source gate failed")
        position = _position(
            placement.get("position_m") if isinstance(placement, dict) else None,
            f"{event_id} placement position",
        )
        placement_covariance = _matrix_psd(
            placement.get("covariance_m2") if isinstance(placement, dict) else None,
            3,
            f"{event_id} placement covariance",
        )
        uncertainty = placement.get("uncertainty_m") if isinstance(placement, dict) else None
        if (
            not isinstance(uncertainty, (int, float))
            or isinstance(uncertainty, bool)
            or not 0.0 <= float(uncertainty) <= MAX_LOCALIZATION_UNCERTAINTY_M
            or math.sqrt(max(0.0, float(np.linalg.eigvalsh(placement_covariance).max())))
            > float(uncertainty)
        ):
            raise AttachmentError("placement covariance/uncertainty gate failed")
        reference_error = math.sqrt(sum(
            (position[axis] - reference_position[axis]) ** 2
            for axis in ("x", "y", "z")
        ))
        if reference_error > MAX_INDEPENDENT_REFERENCE_ERROR_M:
            raise AttachmentError("placement exceeds independent reference error gate")
        family = placement.get("blueprint_family")
        pool = catalog_ids.get(family)
        if not pool:
            raise AttachmentError("placement blueprint family is not cataloged")
        placement_key = placement_key_sha256(global_track_id, family)
        chosen_id = pool[int.from_bytes(bytes.fromhex(placement_key)[:8], "big") % len(pool)]
        catalog_dimensions = catalog_entries[(family, chosen_id)]
        if placement.get("dimensions_m") != catalog_dimensions:
            raise AttachmentError("reviewed geometry does not match the selected blueprint dimensions")
        dimension_tolerance = trajectory.get("blueprint_dimension_tolerance_m")
        if (
            not isinstance(dimension_tolerance, (int, float))
            or isinstance(dimension_tolerance, bool)
            or not 0.0 <= float(dimension_tolerance) <= 0.25
        ):
            raise AttachmentError("blueprint dimension tolerance is invalid")
        media_epoch = _parse_utc(
            sample.get("timing", {}).get("media_timestamp_utc"),
            f"{event_id} media timestamp",
        )
        transition = None
        if previous_epoch is not None:
            delta = media_epoch - previous_epoch
            if not 0.0 < delta <= MAX_TRANSIT_SECONDS:
                raise AttachmentError("trajectory timestamps are not strictly increasing/plausible")
            distance = math.sqrt(sum(
                (position[axis] - previous_position[axis]) ** 2
                for axis in ("x", "y", "z")
            ))
            speed = distance / delta
            acceleration = (
                (speed - previous_speed) / delta
                if previous_speed is not None else None
            )
            pair = pair_index[(previous_event_id, event_id)]
            pair_covariance = _matrix_psd(
                pair.get("trajectory_covariance_m2"), 3, "identity trajectory covariance"
            )
            similarity = pair.get("appearance_similarity")
            recomputed_similarity = float(np.dot(previous_embedding, embedding))
            declared_transit = pair.get("transit_seconds")
            declared_distance = pair.get("distance_m")
            if (
                pair.get("accepted") is not True
                or pair.get("ambiguity") is not False
                or pair.get("global_track_id") != global_track_id
                or pair.get("trajectory_id") != trajectory_id
                or not isinstance(similarity, (int, float))
                or isinstance(similarity, bool)
                or not isinstance(declared_transit, (int, float))
                or isinstance(declared_transit, bool)
                or not isinstance(declared_distance, (int, float))
                or isinstance(declared_distance, bool)
                or not all(
                    math.isfinite(float(value))
                    for value in (similarity, declared_transit, declared_distance)
                )
                or not 0.0 <= float(similarity) <= 1.0
                or abs(float(similarity) - recomputed_similarity) > 1e-9
                or float(declared_transit) <= 0.0
                or float(declared_distance) < 0.0
                or float(similarity) < float(appearance_threshold)
                or speed > MAX_VEHICLE_SPEED_MPS
                or (
                    acceleration is not None
                    and abs(acceleration) > MAX_VEHICLE_ACCELERATION_MPS2
                )
                or math.sqrt(max(0.0, float(np.linalg.eigvalsh(pair_covariance).max())))
                > MAX_LOCALIZATION_UNCERTAINTY_M
                or abs(float(declared_transit) - delta) > 1e-6
                or abs(float(declared_distance) - distance) > 1e-6
            ):
                raise AttachmentError("identity pair appearance/transit/dynamics gate failed")
            transition = {
                "previous_event_id": previous_event_id,
                "accepted": True,
                "ambiguity": False,
                "appearance_similarity": float(similarity),
                "transit_seconds": delta,
                "distance_m": distance,
                "speed_mps": speed,
                "acceleration_mps2": acceleration,
                "trajectory_covariance_m2": pair_covariance.tolist(),
                "pair_evidence_sha256": sha256_bytes(canonical_json_bytes(pair)),
            }
        raw_observation = detection.get("raw_observation")
        fingerprints = raw_observation.get("fingerprints") if isinstance(raw_observation, dict) else None
        if not isinstance(fingerprints, dict):
            raise AttachmentError("detection lacks emitted source fingerprints")
        contract = seal_contract({
            "schema": SCHEMA,
            "event_id": event_id,
            "camera_id": camera_id,
            "global_track_id": global_track_id,
            "trajectory_id": trajectory_id,
            "sample_index": sample.get("sample_index"),
            "source": {
                "frame": {
                    "source_kind": "persisted_native_frame_and_instance_mask",
                    "sha256": frame_hash,
                    "mask_sha256": mask_hash,
                    "native_resolution": resolution,
                    "frame_number": sample.get("frame_number"),
                    **inference_binding,
                },
                "detector": {
                    "model_sha256": fingerprints.get("detector_model_sha256"),
                    "config_sha256": fingerprints.get("detector_config_sha256"),
                },
                "camera": {
                    "cameras_json_sha256": cameras_json_sha256,
                    "camera_config_sha256": camera_context[camera_id].camera_config_sha256,
                    "intrinsics_artifact_sha256": camera_context[camera_id].intrinsics_artifact_sha256,
                    "intrinsics_report_sha256": camera_context[camera_id].intrinsics_report_sha256,
                    "static_calibration_sha256": static_hash,
                },
                "map": {"name": map_name, "opendrive_sha256": opendrive_hash},
            },
            "contact": contact,
            "timing": sample.get("timing"),
            "review": {
                "decision": "accepted",
                "reviewer": reviewer,
                "consensus": {
                    "method": "independent_review_consensus",
                    "artifact_sha256": sha256_bytes(consensus_raw),
                    "reviewer_ids": reviewer_ids,
                },
                "factor_graph": {
                    "artifact_sha256": sha256_bytes(factor_raw),
                    "acceptance_eligible": True,
                },
                "independent_reference": {
                    "artifact_sha256": sha256_bytes(reference_raw),
                    "acceptance_eligible": True,
                },
            },
            "identity": {
                "status": "unambiguous",
                "global_track_id": global_track_id,
                "trajectory_id": trajectory_id,
                "association_method": "reviewed_multicamera_trajectory",
                "evidence_sha256": sha256_bytes(identity_raw),
                "camera_ids": camera_ids,
                "cross_camera_transition_sha256": cross_camera_transition_sha256,
                "transition": transition,
            },
            "placement": {
                **placement,
                "independent_reference": {
                    "position_m": reference_position,
                    "error_m": reference_error,
                },
                "blueprint": {
                    "catalog_sha256": runtime_catalog_hash,
                    "pool_sha256": sha256_bytes(canonical_json_bytes(pool)),
                    "selected_blueprint_id": chosen_id,
                    "expected_dimensions_m": catalog_dimensions,
                    "dimension_tolerance_m": float(dimension_tolerance),
                },
            },
        }, authority_key_id, authority_key)
        try:
            validate_contract(contract, detection, context)
        except ReviewedLocalizationError as exc:
            raise AttachmentError(
                f"{event_id} reviewed contract rejected: {exc.reason}"
            ) from exc
        contracts[event_id] = contract
        previous_epoch = media_epoch
        previous_position = position
        previous_speed = transition["speed_mps"] if transition is not None else None
        previous_event_id = event_id
        previous_embedding = embedding

    updated = []
    for row in rows:
        value = dict(row)
        contract = contracts.get(row["event_id"])
        if contract is not None:
            value["reviewed_localization"] = contract
        updated.append(value)
    body = b"".join(canonical_json_bytes(value) for value in updated)
    bundle_path = Path(os.path.abspath(Path(output_path).expanduser()))
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": ATTACHMENT_SCHEMA,
        "source_detections": str(detections_path),
        "source_detections_sha256": sha256_bytes(detections_raw),
        "reviewed_trajectory": str(trajectory_path),
        "reviewed_trajectory_sha256": sha256_bytes(trajectory_raw),
        "authority_key_id": authority_key_id,
        "static_calibration_manifest_sha256": static_hash,
        "independent_reference_sha256": sha256_bytes(reference_raw),
        "independent_measurements_sha256": sha256_bytes(measurements_raw),
        "blueprint_catalog_sha256": sha256_bytes(catalog_raw),
        "appearance_model_sha256": sha256_bytes(appearance_raw),
        "output_sha256": sha256_bytes(body),
        "counts": {
            "detections": len(updated),
            "reviewed_localizations": len(contracts),
        },
        "static_camera_calibration_passed": False,
        "deployment_eligible": False,
    }
    try:
        staging_path = Path(tempfile.mkdtemp(
            prefix=f".{bundle_path.name}.tmp-", dir=bundle_path.parent
        ))
    except OSError as exc:
        raise AttachmentError("could not create evidence bundle staging area") from exc
    output_file = staging_path / "detections.ndjson"
    manifest_file = staging_path / "manifest.json"
    published = False
    try:
        _write_fsynced_exclusive(output_file, body)
        _write_fsynced_exclusive(
            manifest_file, canonical_json_bytes(manifest)
        )
        _fsync_directory(staging_path)
        _rename_directory_noreplace(staging_path, bundle_path)
        published = True
        _fsync_directory(bundle_path.parent)
    except Exception:
        if published:
            shutil.rmtree(bundle_path, ignore_errors=True)
            try:
                _fsync_directory(bundle_path.parent)
            except OSError:
                pass
        else:
            shutil.rmtree(staging_path, ignore_errors=True)
        raise
    return bundle_path / output_file.name, bundle_path / manifest_file.name


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detections", required=True)
    parser.add_argument("--trajectory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--authority-key-file", required=True)
    args = parser.parse_args(argv)
    try:
        output, manifest = attach(
            args.detections,
            args.trajectory,
            args.output,
            args.authority_key_file,
        )
    except AttachmentError as exc:
        print(f"reviewed localization attachment failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(output), "manifest": str(manifest)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
