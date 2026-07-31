#!/usr/bin/env python3
"""Apply hash-bound human wheel/road-contact reviews to an observation ledger."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import uuid

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
from export_detection_corpus import canonical_json_bytes, sha256_bytes  # noqa: E402


class ReviewError(RuntimeError):
    pass


def load_object(path, label):
    try:
        value = json.loads(Path(path).read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError(f"{label} is unreadable or invalid") from exc
    if not isinstance(value, dict):
        raise ReviewError(f"{label} is not an object")
    return value


def load_ledger(directory):
    directory = Path(directory).expanduser().resolve()
    manifest = load_object(directory / "manifest.json", "ledger manifest")
    if manifest.get("schema") != "v2x-detection-observation-ledger/v2":
        raise ReviewError("ledger schema is unsupported")
    raw = (directory / "observations.ndjson").read_bytes()
    if sha256_bytes(raw) != manifest.get("observations_sha256"):
        raise ReviewError("ledger observations hash does not match manifest")
    observations = []
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReviewError(f"ledger line {number} is invalid") from exc
        if value.get("schema") != "v2x-detection-observation/v2":
            raise ReviewError(f"ledger line {number} has unsupported schema")
        observations.append(value)
    return directory, manifest, observations, sha256_bytes(raw)


def finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def validate_covariance(value):
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(not isinstance(row, list) or len(row) != 2 for row in value)
        or any(not finite(item) for row in value for item in row)
    ):
        raise ReviewError("review covariance must be a finite 2x2 matrix")
    a, b = map(float, value[0])
    c, d = map(float, value[1])
    if abs(b - c) > 1e-9 or a <= 0.0 or d <= 0.0 or a * d - b * c <= 0.0:
        raise ReviewError("review covariance must be symmetric positive definite")
    return [[a, b], [c, d]]


def resolve_evidence_path(review_path, value, label):
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(f"{label} path is missing")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = review_path.parent / path
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise ReviewError(f"{label} path is unreadable") from exc


def validate_frame_evidence(review_path, entry, observation):
    evidence = entry.get("frame_evidence")
    if not isinstance(evidence, dict):
        raise ReviewError("review entry lacks hash-bound frame evidence")
    frame_path = resolve_evidence_path(review_path, evidence.get("path"), "frame")
    report_path = resolve_evidence_path(
        review_path, evidence.get("verifier_report_path"), "frame verifier report"
    )
    expected_frame_hash = evidence.get("sha256")
    expected_report_hash = evidence.get("verifier_report_sha256")
    if expected_frame_hash != sha256_bytes(frame_path.read_bytes()):
        raise ReviewError("review frame hash does not match retained frame")
    report_raw = report_path.read_bytes()
    if expected_report_hash != sha256_bytes(report_raw):
        raise ReviewError("frame verifier report hash does not match")
    try:
        report = json.loads(report_raw)
    except json.JSONDecodeError as exc:
        raise ReviewError("frame verifier report is invalid") from exc
    detection = report.get("detection") if isinstance(report, dict) else None
    frame = report.get("frame") if isinstance(report, dict) else None
    result = report.get("result") if isinstance(report, dict) else None
    safety = report.get("safety") if isinstance(report, dict) else None
    bbox = observation.get("bbox") or {}
    expected_bbox = [
        round(float(bbox.get(key, math.nan)), 3)
        for key in ("x1", "y1", "x2", "y2")
    ]
    if (
        report.get("schema_version") != 1
        or report.get("verifier") != "historical_video_detection_correlation"
        or not isinstance(detection, dict)
        or detection.get("camera_id") != observation.get("camera_id")
        or detection.get("event_id") != observation.get("event_id")
        or detection.get("object_id") != observation.get("object_id")
        or detection.get("object_type") != observation.get("object_type")
        or detection.get("saved_bbox") != expected_bbox
        or detection.get("persisted_media_timestamp")
        != observation.get("media_timestamp_utc")
        or not isinstance(frame, dict)
        or frame.get("sha256") != expected_frame_hash
        or frame.get("dimensions") != observation.get("native_resolution")
        or not finite(frame.get("absolute_error_ms"))
        or float(frame["absolute_error_ms"]) > 100.0
        or not isinstance(result, dict)
        or result.get("gate_passed") is not True
        or result.get("trusted_media_timestamp") is not True
        or result.get("frame_timing_check_passed") is not True
        or not isinstance(safety, dict)
        or safety.get("signed_urls_emitted") is not False
    ):
        raise ReviewError("frame verifier report does not prove this exact event")
    report_frame_path = frame.get("path")
    if report_frame_path is not None:
        try:
            if Path(report_frame_path).expanduser().resolve() != frame_path:
                raise ReviewError("frame verifier report points to a different frame")
        except (OSError, RuntimeError) as exc:
            raise ReviewError("frame verifier report path is invalid") from exc
    return {
        "path": str(frame_path),
        "sha256": expected_frame_hash,
        "verifier_report_path": str(report_path),
        "verifier_report_sha256": expected_report_hash,
        "selected_media_timestamp": frame["selected_media_timestamp"],
        "absolute_error_ms": float(frame["absolute_error_ms"]),
    }


def validate_review(review, observations_hash):
    if review.get("schema") != "v2x-ground-contact-review/v1":
        raise ReviewError("ground-contact review schema is unsupported")
    if review.get("source_observations_sha256") != observations_hash:
        raise ReviewError("review source hash does not match observations")
    reviewer = review.get("reviewer")
    if (
        not isinstance(reviewer, dict)
        or reviewer.get("kind") != "human"
        or not isinstance(reviewer.get("id"), str)
        or not reviewer["id"].strip()
    ):
        raise ReviewError("review requires a named human reviewer")
    entries = review.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ReviewError("review has no entries")
    indexed = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReviewError("review entry is not an object")
        event_id = entry.get("event_id")
        if not isinstance(event_id, str) or not event_id or event_id in indexed:
            raise ReviewError("review event IDs are missing or duplicated")
        if entry.get("provenance") != "manually_verified_wheel_contact":
            raise ReviewError("review provenance is not acceptance eligible")
        pixel = entry.get("pixel")
        if not isinstance(pixel, list) or len(pixel) != 2 or not all(
            finite(value) for value in pixel
        ):
            raise ReviewError("review contact pixel is invalid")
        if entry.get("range_band") not in {"near", "mid", "far"}:
            raise ReviewError("review range band is invalid")
        indexed[event_id] = {
            **entry,
            "pixel": [float(pixel[0]), float(pixel[1])],
            "covariance_px2": validate_covariance(entry.get("covariance_px2")),
        }
    return reviewer, indexed


def apply_review(observation, entry, reviewer, frame_evidence):
    width, height = observation["native_resolution"]
    u, v = entry["pixel"]
    if not (0.0 <= u < width and 0.0 <= v < height):
        raise ReviewError("review contact lies outside the native image")
    bbox = observation["bbox"]
    bbox_height = bbox["y2"] - bbox["y1"]
    margin_x = 0.05 * (bbox["x2"] - bbox["x1"])
    if not (
        bbox["x1"] - margin_x <= u <= bbox["x2"] + margin_x
        and bbox["y1"] + 0.45 * bbox_height <= v <= bbox["y2"] + 0.05 * bbox_height
    ):
        raise ReviewError("review contact is inconsistent with the vehicle bbox")
    updated = dict(observation)
    updated["ground_contact"] = {
        "method": "reviewed_wheel_road_contact",
        "pixel": [u, v],
        "covariance_px2": entry["covariance_px2"],
        "reviewed": True,
        "frame_sha256": frame_evidence["sha256"],
        "frame_evidence": frame_evidence,
        "provenance": entry["provenance"],
        "reviewer": reviewer,
        "range_band": entry["range_band"],
    }
    reasons = [
        reason
        for reason in observation.get("ineligibility_reasons", [])
        if reason != "ground_contact_not_reviewed"
    ]
    updated["ineligibility_reasons"] = reasons
    updated["acceptance_eligible"] = not reasons
    return updated


def apply_reviews(ledger_dir, review_json, output_dir):
    ledger_dir, manifest, observations, source_hash = load_ledger(ledger_dir)
    review_path = Path(review_json).expanduser().resolve()
    reviewer, entries = validate_review(
        load_object(review_path, "ground-contact review"), source_hash
    )
    by_event = {item["event_id"]: item for item in observations}
    unknown = sorted(set(entries) - set(by_event))
    if unknown:
        raise ReviewError("review references unknown event IDs")
    frame_evidence = {
        event_id: validate_frame_evidence(review_path, entry, by_event[event_id])
        for event_id, entry in entries.items()
    }
    updated = []
    for observation in observations:
        entry = entries.get(observation["event_id"])
        updated.append(
            apply_review(
                observation,
                entry,
                reviewer,
                frame_evidence[observation["event_id"]],
            )
            if entry is not None
            else observation
        )
    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir.exists():
        raise ReviewError("reviewed ledger output already exists")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temp = output_dir.parent / f".{output_dir.name}.tmp-{uuid.uuid4().hex}"
    try:
        temp.mkdir()
        body = b"".join(canonical_json_bytes(item) for item in updated)
        (temp / "observations.ndjson").write_bytes(body)
        result = {
            **manifest,
            "schema": "v2x-detection-observation-ledger/v2",
            "source_ledger": str(ledger_dir),
            "source_observations_sha256": source_hash,
            "review_file": str(review_path),
            "review_sha256": sha256_bytes(review_path.read_bytes()),
            "observations_sha256": sha256_bytes(body),
            "counts": {
                **manifest.get("counts", {}),
                "reviewed_contacts": len(entries),
                "acceptance_eligible": sum(
                    item.get("acceptance_eligible") is True for item in updated
                ),
            },
        }
        (temp / "manifest.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.rename(temp, output_dir)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return output_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger_dir")
    parser.add_argument("review_json")
    parser.add_argument("output_dir")
    args = parser.parse_args(argv)
    try:
        output = apply_reviews(args.ledger_dir, args.review_json, args.output_dir)
    except ReviewError as exc:
        print(f"review application failed: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
