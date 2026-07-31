#!/usr/bin/env python3
"""Build a non-circular calibration observation ledger from one corpus snapshot.

Persisted GPS and camera-local XZ are retained only under ``derived_baseline``.
The optimizer-facing observation consists of native image pixels, trusted time,
camera/model fingerprints, and explicit eligibility reasons.
"""

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import uuid
import re

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
from export_detection_corpus import (  # noqa: E402
    ExportError,
    VEHICLE_TYPES,
    canonical_json_bytes,
    is_trusted_v2,
    sha256_bytes,
)


class LedgerError(RuntimeError):
    pass


def load_json(path, label):
    try:
        value = json.loads(Path(path).read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"{label} is unreadable or invalid") from exc
    if not isinstance(value, dict):
        raise LedgerError(f"{label} is not an object")
    return value


def load_snapshot(snapshot_dir):
    snapshot_dir = Path(snapshot_dir).expanduser().resolve()
    manifest = load_json(snapshot_dir / "manifest.json", "snapshot manifest")
    if manifest.get("schema") != "v2x-detection-corpus-snapshot/v1":
        raise LedgerError("snapshot manifest schema is unsupported")
    detections_path = snapshot_dir / "detections.ndjson"
    try:
        raw = detections_path.read_bytes()
    except OSError as exc:
        raise LedgerError("snapshot detections are unreadable") from exc
    expected = (manifest.get("artifacts") or {}).get("detections.ndjson")
    if expected != sha256_bytes(raw):
        raise LedgerError("snapshot detections hash does not match manifest")
    rows = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(
                f"snapshot detection line {line_number} is invalid"
            ) from exc
        if not isinstance(row, dict):
            raise LedgerError(f"snapshot detection line {line_number} is not an object")
        rows.append(row)
    if len(rows) != (manifest.get("counts") or {}).get("items"):
        raise LedgerError("snapshot row count does not match manifest")
    return snapshot_dir, manifest, rows, sha256_bytes(raw)


def load_cameras(path):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        config = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError("camera config is unreadable or invalid") from exc
    cameras = config.get("cameras") if isinstance(config, dict) else None
    if not isinstance(cameras, list):
        raise LedgerError("camera config has no camera list")
    indexed = {}
    hashes = {}
    for camera in cameras:
        if not isinstance(camera, dict) or not isinstance(camera.get("id"), str):
            raise LedgerError("camera config contains an invalid camera")
        camera_id = camera["id"]
        if camera_id in indexed:
            raise LedgerError("camera config contains duplicate camera IDs")
        indexed[camera_id] = camera
        hashes[camera_id] = sha256_bytes(canonical_json_bytes(camera))
    return config, indexed, sha256_bytes(raw), hashes


def finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def camera_id_for(row):
    device = row.get("device_id")
    if not isinstance(device, str) or "-" not in device:
        return None
    return device.rsplit("-", 1)[-1]


def nested(row, *keys):
    value = row
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def normalize_bbox(value, width, height):
    if not isinstance(value, dict):
        raise LedgerError("bbox is missing")
    keys = ("x1", "y1", "x2", "y2")
    if not all(finite_number(value.get(key)) for key in keys):
        raise LedgerError("bbox contains non-finite coordinates")
    bbox = {key: float(value[key]) for key in keys}
    if bbox["x2"] <= bbox["x1"] or bbox["y2"] <= bbox["y1"]:
        raise LedgerError("bbox has non-positive dimensions")
    if (
        bbox["x2"] <= 0
        or bbox["y2"] <= 0
        or bbox["x1"] >= width
        or bbox["y1"] >= height
    ):
        raise LedgerError("bbox does not intersect the native image")
    return bbox


def measured_intrinsics(camera):
    calibration = camera.get("intrinsics_calibration")
    return (
        isinstance(calibration, dict)
        and calibration.get("method") in {"checkerboard", "charuco"}
        and isinstance(calibration.get("artifact_sha256"), str)
        and len(calibration["artifact_sha256"]) == 64
    )


def validated_raw_observation(row, width, height):
    raw = row.get("raw_observation")
    if raw is None:
        return None, "missing_raw_observation_provenance"
    fingerprints = raw.get("fingerprints") if isinstance(raw, dict) else None
    contact = raw.get("ground_contact") if isinstance(raw, dict) else None
    try:
        bbox = normalize_bbox(raw.get("bbox"), width, height)
        pixel = contact["pixel"]
    except (LedgerError, KeyError, TypeError):
        return None, "invalid_raw_observation_provenance"
    valid = (
        raw.get("schema") == "v2x-raw-detection-observation/v1"
        and raw.get("native_resolution") == [width, height]
        and isinstance(contact, dict)
        and contact.get("method") == "bbox_bottom_center_diagnostic"
        and contact.get("reviewed") is False
        and isinstance(pixel, list)
        and len(pixel) == 2
        and all(finite_number(value) for value in pixel)
        and abs(float(pixel[0]) - (bbox["x1"] + bbox["x2"]) / 2.0) <= 1e-6
        and abs(float(pixel[1]) - bbox["y2"]) <= 1e-6
        and isinstance(fingerprints, dict)
        and all(
            isinstance(fingerprints.get(key), str)
            and re.fullmatch(r"[0-9a-f]{64}", fingerprints[key]) is not None
            for key in (
                "cameras_json_sha256",
                "camera_config_sha256",
                "detector_model_sha256",
            )
        )
    )
    if not valid:
        return None, "invalid_raw_observation_provenance"
    return {
        "bbox": bbox,
        "pixel": [float(pixel[0]), float(pixel[1])],
        "fingerprints": dict(fingerprints),
        "schema": raw["schema"],
    }, None


def build_observation(row, camera, config_hash, camera_hash, snapshot_hash):
    camera_id = camera["id"]
    intrinsics = camera.get("intrinsics") or {}
    width = intrinsics.get("width")
    height = intrinsics.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        raise LedgerError("camera native resolution is invalid")
    raw, raw_reason = validated_raw_observation(row, width, height)
    if raw is None:
        bbox = normalize_bbox(
            nested(row, "camera_data", "bifocal_metadata", "bbox"), width, height
        )
        ground_pixel = [(bbox["x1"] + bbox["x2"]) / 2.0, bbox["y2"]]
        raw_fingerprints = None
    else:
        bbox = raw["bbox"]
        ground_pixel = raw["pixel"]
        raw_fingerprints = raw["fingerprints"]
    world = nested(row, "camera_data", "bifocal_metadata", "world_position")
    gps = row.get("gps_location")
    reasons = ["ground_contact_not_reviewed"]
    if raw_reason:
        reasons.append(raw_reason)
    if not measured_intrinsics(camera):
        reasons.append("missing_measured_intrinsics")
    association = row.get("identity_association")
    return {
        "schema": "v2x-detection-observation/v2",
        "event_id": row["event_id"],
        "object_id": row.get("object_id"),
        "object_type": row.get("object_type"),
        "camera_id": camera_id,
        "device_id": row.get("device_id"),
        "perception_run_id": row.get("perception_run_id"),
        "track_id": row.get("track_id"),
        "media_timestamp_utc": row.get("media_timestamp_utc"),
        "media_clock": row.get("media_clock"),
        "native_resolution": [width, height],
        "bbox": bbox,
        "ground_contact": {
            "method": "bbox_bottom_center_diagnostic",
            "pixel": ground_pixel,
            "covariance_px2": None,
            "reviewed": False,
        },
        "identity_association": association,
        "source": {
            "snapshot_detections_sha256": snapshot_hash,
            "cameras_json_sha256": config_hash,
            "camera_sha256": camera_hash,
            "frame_number": nested(
                row, "camera_data", "bifocal_metadata", "frame"
            ),
            "raw_observation_schema": raw.get("schema") if raw else None,
            "emitted_fingerprints": raw_fingerprints,
        },
        "camera_model": {
            "intrinsics": intrinsics,
            "intrinsics_calibration": camera.get("intrinsics_calibration"),
            "pose_is_nominal": not isinstance(camera.get("extrinsics_calibration"), dict),
        },
        "derived_baseline": {
            "warning": "not_optimizer_truth",
            "gps_location": gps,
            "camera_local_world_position": world,
        },
        "acceptance_eligible": not reasons,
        "ineligibility_reasons": reasons,
    }


def build_ledger(snapshot_dir, cameras_json, output_dir):
    snapshot_dir, snapshot, rows, snapshot_hash = load_snapshot(snapshot_dir)
    _config, cameras, config_hash, camera_hashes = load_cameras(cameras_json)
    observations = []
    rejected = Counter()
    for row in rows:
        if not is_trusted_v2(row):
            rejected["untrusted_or_legacy"] += 1
            continue
        if str(row.get("object_type", "")).lower() not in VEHICLE_TYPES:
            rejected["non_vehicle"] += 1
            continue
        camera_id = camera_id_for(row)
        camera = cameras.get(camera_id)
        if camera is None:
            rejected["unknown_camera"] += 1
            continue
        try:
            observations.append(
                build_observation(
                    row,
                    camera,
                    config_hash,
                    camera_hashes[camera_id],
                    snapshot_hash,
                )
            )
        except LedgerError:
            rejected["invalid_geometry"] += 1

    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir.exists():
        raise LedgerError("ledger output already exists")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temp = output_dir.parent / f".{output_dir.name}.tmp-{uuid.uuid4().hex}"
    try:
        temp.mkdir()
        ordered = sorted(
            observations,
            key=lambda item: (item["media_timestamp_utc"], item["event_id"]),
        )
        body = b"".join(canonical_json_bytes(item) for item in ordered)
        (temp / "observations.ndjson").write_bytes(body)
        manifest = {
            "schema": "v2x-detection-observation-ledger/v2",
            "source_snapshot": str(snapshot_dir),
            "source_snapshot_manifest_sha256": sha256_bytes(
                (snapshot_dir / "manifest.json").read_bytes()
            ),
            "source_detections_sha256": snapshot_hash,
            "cameras_json": str(Path(cameras_json).expanduser().resolve()),
            "cameras_json_sha256": config_hash,
            "window": snapshot.get("window"),
            "counts": {
                "source_rows": len(rows),
                "observations": len(ordered),
                "acceptance_eligible": sum(
                    item["acceptance_eligible"] for item in ordered
                ),
                "observations_by_camera": dict(
                    sorted(Counter(item["camera_id"] for item in ordered).items())
                ),
                "rejected": dict(sorted(rejected.items())),
            },
            "observations_sha256": sha256_bytes(body),
            "optimizer_contract": {
                "pixel_observations_only": True,
                "derived_baseline_forbidden_as_target": True,
                "required_ground_contact_method": "reviewed_wheel_road_contact",
            },
        }
        (temp / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.rename(temp, output_dir)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return output_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot_dir")
    parser.add_argument("cameras_json")
    parser.add_argument("output_dir")
    args = parser.parse_args(argv)
    try:
        output = build_ledger(
            args.snapshot_dir, args.cameras_json, args.output_dir
        )
    except (LedgerError, ExportError) as exc:
        print(f"ledger build failed: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
