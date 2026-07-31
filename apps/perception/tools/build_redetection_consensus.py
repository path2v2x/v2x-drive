#!/usr/bin/env python3
"""Build fail-closed vehicle-box consensus from two exact-frame detectors."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np


SCHEMA = "v2x-selected-frame-redetection-consensus/v1"
INPUT_SCHEMA = "v2x-selected-frame-redetection/v1"
VEHICLE_LABELS = {"car", "truck", "bus", "motorcycle"}
MINIMUM_BOX_IOU = 0.60
MINIMUM_COORDINATE_UNCERTAINTY_PX = 2.0
MINIMUM_FRAME_MARGIN_FRACTION = 0.01
MAXIMUM_CONTACT_DISAGREEMENT_AT_1280_PX = 16.0


class ConsensusError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def load_report(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    try:
        report = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConsensusError("redetection report is invalid JSON") from exc
    if (
        report.get("schema") != INPUT_SCHEMA
        or report.get("acceptance_eligible") is not False
    ):
        raise ConsensusError("redetection report contract is unsupported")
    return path, raw, report


def bbox_iou(left, right):
    lx1, ly1, lx2, ly2 = (float(value) for value in left)
    rx1, ry1, rx2, ry2 = (float(value) for value in right)
    intersection = max(0.0, min(lx2, rx2) - max(lx1, rx1)) * max(
        0.0, min(ly2, ry2) - max(ly1, ry1)
    )
    left_area = max(0.0, lx2 - lx1) * max(0.0, ly2 - ly1)
    right_area = max(0.0, rx2 - rx1) * max(0.0, ry2 - ry1)
    union = left_area + right_area - intersection
    return 0.0 if union <= 0.0 else intersection / union


def selected_detection(event):
    match = event.get("event_match_proposal")
    detections = event.get("detections")
    if not isinstance(match, dict) or not isinstance(detections, list):
        return None
    index = match.get("detection_index")
    if not isinstance(index, int) or not 0 <= index < len(detections):
        raise ConsensusError("event match proposal points outside detections")
    value = detections[index]
    if value.get("label") not in VEHICLE_LABELS:
        raise ConsensusError("event match proposal is not a vehicle")
    return value


def bottom_center(bbox):
    x1, _y1, x2, y2 = (float(value) for value in bbox)
    return np.asarray([(x1 + x2) / 2.0, y2], dtype=float)


def has_frame_margin(bbox, width, height):
    x1, y1, x2, y2 = (float(value) for value in bbox)
    return (
        x1 >= MINIMUM_FRAME_MARGIN_FRACTION * width
        and x2 <= (1.0 - MINIMUM_FRAME_MARGIN_FRACTION) * width
        and y1 >= MINIMUM_FRAME_MARGIN_FRACTION * height
        and y2 <= (1.0 - MINIMUM_FRAME_MARGIN_FRACTION) * height
    )


def event_index(report):
    values = report.get("events")
    if not isinstance(values, list) or not values:
        raise ConsensusError("redetection report has no events")
    output = {}
    for event in values:
        event_id = event.get("event_id") if isinstance(event, dict) else None
        if not isinstance(event_id, str) or not event_id or event_id in output:
            raise ConsensusError("redetection event IDs are invalid or duplicated")
        output[event_id] = event
    return output


def validate_pair(left, right):
    if left.get("capture_report", {}).get("sha256") != right.get(
        "capture_report", {}
    ).get("sha256"):
        raise ConsensusError("detectors do not share one capture report")
    left_model = left.get("model", {}).get("sha256")
    right_model = right.get("model", {}).get("sha256")
    if not isinstance(left_model, str) or not isinstance(right_model, str):
        raise ConsensusError("model fingerprints are missing")
    if left_model == right_model:
        raise ConsensusError("consensus requires distinct model fingerprints")


def validate_event_binding(left, right):
    keys = ("camera_id", "selected_frame_timestamp_utc")
    if any(left.get(key) != right.get(key) for key in keys):
        raise ConsensusError("event camera/time binding differs between models")
    left_frame, right_frame = left.get("frame"), right.get("frame")
    if not isinstance(left_frame, dict) or not isinstance(right_frame, dict):
        raise ConsensusError("event frame binding is missing")
    for key in ("encoded_jpeg_sha256", "width", "height"):
        if left_frame.get(key) != right_frame.get(key):
            raise ConsensusError("event frame binding differs between models")


def consensus_event(event_id, left, right, minimum_iou):
    validate_event_binding(left, right)
    left_detection = selected_detection(left)
    right_detection = selected_detection(right)
    reasons = []
    if left_detection is None:
        reasons.append("left_model_has_no_event_match")
    if right_detection is None:
        reasons.append("right_model_has_no_event_match")
    agreement_iou = None
    contact_disagreement = None
    contact_gate = None
    if left_detection is not None and right_detection is not None:
        agreement_iou = bbox_iou(
            left_detection["bbox_xyxy"], right_detection["bbox_xyxy"]
        )
        if agreement_iou < minimum_iou:
            reasons.append("box_iou_below_consensus_gate")
        if left_detection.get("touches_frame_boundary") is not False:
            reasons.append("left_box_touches_frame_boundary")
        if right_detection.get("touches_frame_boundary") is not False:
            reasons.append("right_box_touches_frame_boundary")
        width = int(left["frame"]["width"])
        height = int(left["frame"]["height"])
        if not has_frame_margin(left_detection["bbox_xyxy"], width, height):
            reasons.append("left_box_lacks_full_visibility_margin")
        if not has_frame_margin(right_detection["bbox_xyxy"], width, height):
            reasons.append("right_box_lacks_full_visibility_margin")
        left_contact = bottom_center(left_detection["bbox_xyxy"])
        right_contact = bottom_center(right_detection["bbox_xyxy"])
        contact_disagreement = float(np.linalg.norm(left_contact - right_contact))
        contact_gate = MAXIMUM_CONTACT_DISAGREEMENT_AT_1280_PX * width / 1280.0
        if contact_disagreement > contact_gate:
            reasons.append("contact_disagreement_above_gate")
    consensus = None
    if not reasons:
        left_box = np.asarray(left_detection["bbox_xyxy"], dtype=float)
        right_box = np.asarray(right_detection["bbox_xyxy"], dtype=float)
        coordinate_uncertainty = np.maximum(
            np.abs(left_box - right_box) / 2.0,
            MINIMUM_COORDINATE_UNCERTAINTY_PX,
        )
        left_contact = bottom_center(left_box)
        right_contact = bottom_center(right_box)
        consensus = {
            "bbox_xyxy": ((left_box + right_box) / 2.0).tolist(),
            "coordinate_uncertainty_px": coordinate_uncertainty.tolist(),
            "coarse_label": "vehicle",
            "bottom_center_pixel": ((left_contact + right_contact) / 2.0).tolist(),
            "bottom_center_uncertainty_px": np.maximum(
                np.abs(left_contact - right_contact) / 2.0,
                MINIMUM_COORDINATE_UNCERTAINTY_PX,
            ).tolist(),
            "full_visibility_proposal": True,
            "uses_model_confidence_as_weight": False,
        }
    return {
        "event_id": event_id,
        "camera_id": left.get("camera_id"),
        "selected_frame_timestamp_utc": left.get("selected_frame_timestamp_utc"),
        "frame": left["frame"],
        "agreement_iou": agreement_iou,
        "bottom_center_disagreement_px": contact_disagreement,
        "maximum_bottom_center_disagreement_px": contact_gate,
        "minimum_agreement_iou": minimum_iou,
        "left_detection": left_detection,
        "right_detection": right_detection,
        "consensus": consensus,
        "rejection_reasons": reasons,
        "acceptance_eligible": False,
    }


def build_consensus(left, right, minimum_iou=MINIMUM_BOX_IOU):
    if not math.isfinite(float(minimum_iou)) or not 0.0 < minimum_iou <= 1.0:
        raise ConsensusError("minimum IoU must be in (0, 1]")
    validate_pair(left, right)
    left_events, right_events = event_index(left), event_index(right)
    if set(left_events) != set(right_events):
        raise ConsensusError("detectors do not cover identical event IDs")
    events = [
        consensus_event(
            event_id,
            left_events[event_id],
            right_events[event_id],
            float(minimum_iou),
        )
        for event_id in sorted(left_events)
    ]
    accepted = [item for item in events if item["consensus"] is not None]
    ious = [
        item["agreement_iou"]
        for item in events
        if item["agreement_iou"] is not None
    ]
    return {
        "schema": SCHEMA,
        "generated_at_utc": utc_now(),
        "acceptance_eligible": False,
        "capture_report_sha256": left["capture_report"]["sha256"],
        "models": [left["model"], right["model"]],
        "gate": {
            "minimum_box_iou": float(minimum_iou),
            "requires_both_models": True,
            "requires_non_boundary_boxes": True,
            "minimum_frame_margin_fraction": MINIMUM_FRAME_MARGIN_FRACTION,
            "maximum_bottom_center_disagreement_at_1280_px": (
                MAXIMUM_CONTACT_DISAGREEMENT_AT_1280_PX
            ),
            "minimum_coordinate_uncertainty_px": (
                MINIMUM_COORDINATE_UNCERTAINTY_PX
            ),
        },
        "events": events,
        "summary": {
            "event_count": len(events),
            "paired_match_count": len(ious),
            "consensus_count": len(accepted),
            "rejected_count": len(events) - len(accepted),
            "agreement_iou_median": (
                None if not ious else float(np.median(ious))
            ),
            "agreement_iou_min": None if not ious else float(min(ious)),
        },
        "acceptance_failures": [
            "boxes_are_cross_model_proposals_not_reviewed_wheel_contacts",
            "model_errors_can_be_correlated_through_shared_training_data",
            "consensus_does_not_supply_world_position_or_vehicle_identity",
        ],
    }


def write_json_exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", required=True)
    parser.add_argument("--right", required=True)
    parser.add_argument("--minimum-iou", type=float, default=MINIMUM_BOX_IOU)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        left_path, left_raw, left = load_report(args.left)
        right_path, right_raw, right = load_report(args.right)
        report = build_consensus(left, right, args.minimum_iou)
        report["inputs"] = [
            {"path": str(left_path), "sha256": sha256_bytes(left_raw)},
            {"path": str(right_path), "sha256": sha256_bytes(right_raw)},
        ]
        write_json_exclusive(args.output, report)
    except (OSError, ConsensusError) as exc:
        parser.error(str(exc))
    print(json.dumps({"output": str(Path(args.output).resolve()), **report["summary"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
