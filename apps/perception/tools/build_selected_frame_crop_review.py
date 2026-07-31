#!/usr/bin/env python3
"""Resolve explicit visual crop decisions against a redetection report.

The reviewer chooses an exact event and detection index after inspecting the
redetection review sheet. This builder binds that decision to the encoded JPEG,
model report, and selected-frame box. It never upgrades Codex review to
independent acceptance evidence.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path


class CropReviewError(RuntimeError):
    pass


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def read_json(path, label):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise CropReviewError(f"{label} is unreadable or invalid") from exc
    return path, raw, value


def write_json_exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build(redetection_path, decisions_path):
    redetection_path, redetection_raw, redetection = read_json(
        redetection_path, "redetection report"
    )
    decisions_path, decisions_raw, decisions = read_json(
        decisions_path, "crop decisions"
    )
    if redetection.get("schema") != "v2x-selected-frame-redetection/v1":
        raise CropReviewError("redetection schema is unsupported")
    if decisions.get("schema") != "v2x-selected-frame-crop-decisions/v1":
        raise CropReviewError("crop decision schema is unsupported")
    redetection_hash = sha256_bytes(redetection_raw)
    if decisions.get("redetection_report_sha256") != redetection_hash:
        raise CropReviewError("crop decisions do not bind the redetection report")
    if decisions.get("reviewer_kind") not in {
        "codex_visual_review",
        "independent_human_review",
    }:
        raise CropReviewError("crop decision reviewer kind is unsupported")
    indexed = {}
    for event in redetection.get("events", []):
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in indexed:
            raise CropReviewError("redetection event IDs are invalid or duplicated")
        indexed[event_id] = event
    crops, seen = [], set()
    for decision in decisions.get("decisions", []):
        event_id = decision.get("event_id") if isinstance(decision, dict) else None
        if not isinstance(event_id, str) or event_id in seen:
            raise CropReviewError("crop decision event IDs are invalid or duplicated")
        seen.add(event_id)
        event = indexed.get(event_id)
        if event is None:
            raise CropReviewError("crop decision event is absent from redetection")
        index = decision.get("detection_index")
        if not isinstance(index, int) or not 0 <= index < len(event["detections"]):
            raise CropReviewError("crop decision detection index is invalid")
        detection = event["detections"][index]
        if detection.get("touches_frame_boundary") is True:
            raise CropReviewError("reviewed crop detection touches the frame boundary")
        if decision.get("vehicle_fully_visible") is not True:
            raise CropReviewError("accepted crop must be reviewed as fully visible")
        crops.append({
            "event_id": event_id,
            "camera_id": event["camera_id"],
            "selected_frame_timestamp_utc": event["selected_frame_timestamp_utc"],
            "frame_sha256": event["frame"]["encoded_jpeg_sha256"],
            "bbox_xyxy": detection["bbox_xyxy"],
            "model_label": detection["label"],
            "model_confidence": detection["confidence"],
            "detection_index": index,
            "vehicle_fully_visible": True,
            "review_notes": decision.get("review_notes"),
            "wheel_road_contact_reviewed": False,
            "acceptance_eligible": False,
        })
    if not crops:
        raise CropReviewError("crop decisions contain no accepted crops")
    return {
        "schema": "v2x-selected-frame-vehicle-crop-review/v1",
        "acceptance_eligible": False,
        "reviewer_kind": decisions["reviewer_kind"],
        "redetection_report": {
            "path": str(redetection_path),
            "sha256": redetection_hash,
        },
        "decisions": {
            "path": str(decisions_path),
            "sha256": sha256_bytes(decisions_raw),
        },
        "capture_report_sha256": redetection["capture_report"]["sha256"],
        "review_sheet": redetection["review_sheet"],
        "crops": crops,
        "acceptance_failures": [
            "codex_visual_review_is_not_independent_human_review"
            if decisions["reviewer_kind"] == "codex_visual_review"
            else "crop_identity_does_not_prove_calibration",
            "wheel_road_contacts_are_not_reviewed",
            "crop_review_does_not_establish_cross_camera_identity",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redetection-report", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.redetection_report, args.decisions)
    write_json_exclusive(args.output, result)
    print(json.dumps({
        "output": str(args.output.resolve()),
        "accepted_crop_count": len(result["crops"]),
        "acceptance_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
