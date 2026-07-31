#!/usr/bin/env python3
"""Build cross-model consensus for exact-frame segmentation contact proposals."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import cv2
import numpy as np

from propose_segmentation_ground_contacts import SCHEMA as INPUT_SCHEMA
from redetect_selected_capture_frames import (
    bbox_iou,
    load_capture_report,
    sha256,
    write_json_exclusive,
)


SCHEMA = "v2x-segmentation-ground-contact-consensus/v2"
MINIMUM_BBOX_IOU = 0.60
MINIMUM_MASK_IOU = 0.70
MAXIMUM_CONTACT_DISAGREEMENT_AT_1280_PX = 16.0


class ConsensusError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def load_report(path):
    path = Path(path).resolve()
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsensusError("contact proposal report is unreadable or invalid") from exc
    if value.get("schema") != INPUT_SCHEMA or value.get("acceptance_eligible") is not False:
        raise ConsensusError("contact proposal report contract is unsupported")
    return path, value


def event_index(report):
    output = {}
    for event in report.get("events", []):
        event_id = event.get("event_id") if isinstance(event, dict) else None
        if not isinstance(event_id, str) or not event_id or event_id in output:
            raise ConsensusError("contact proposal event IDs are invalid or duplicated")
        output[event_id] = event
    if not output:
        raise ConsensusError("contact proposal report has no events")
    return output


def load_mask(event, frame):
    descriptor = event.get("mask")
    if not isinstance(descriptor, dict):
        raise ConsensusError("accepted contact proposal lacks a mask binding")
    path = Path(descriptor.get("path", "")).expanduser().resolve()
    if not path.is_file() or sha256(path) != descriptor.get("sha256"):
        raise ConsensusError("contact proposal mask hash does not match")
    mask = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if mask is None:
        raise ConsensusError("contact proposal mask is not decodable")
    if mask.shape != (frame["height"], frame["width"]):
        raise ConsensusError("contact proposal mask dimensions do not match frame")
    return mask > 0


def mask_iou(left, right):
    if left.shape != right.shape:
        raise ConsensusError("contact proposal masks have different dimensions")
    union = np.logical_or(left, right).sum()
    return 0.0 if union == 0 else float(np.logical_and(left, right).sum() / union)


def validated_pixel(value, frame, bbox, label):
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
    ):
        raise ConsensusError(f"{label} contact proposal pixel is invalid")
    x, y = map(float, value)
    if not (0.0 <= x < float(frame["width"]) and 0.0 <= y < float(frame["height"])):
        raise ConsensusError(f"{label} contact proposal pixel is outside the frame")
    x1, y1, x2, y2 = bbox
    if not (x1 <= x <= x2 and y1 <= y <= y2):
        raise ConsensusError(f"{label} contact proposal pixel is outside its bbox")
    return np.asarray([x, y], dtype=float)


def valid_frame(frame):
    return (
        isinstance(frame, dict)
        and isinstance(frame.get("width"), int)
        and not isinstance(frame.get("width"), bool)
        and isinstance(frame.get("height"), int)
        and not isinstance(frame.get("height"), bool)
        and frame["width"] > 0
        and frame["height"] > 0
    )


def validated_bbox(value, frame, label):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or not all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
    ):
        raise ConsensusError(f"{label} bbox is invalid")
    x1, y1, x2, y2 = map(float, value)
    if not (
        0.0 <= x1 < x2 <= float(frame["width"])
        and 0.0 <= y1 < y2 <= float(frame["height"])
    ):
        raise ConsensusError(f"{label} bbox is outside the frame")
    return [x1, y1, x2, y2]


def covariance_matrix(proposal):
    try:
        covariance = np.asarray(proposal["covariance_px2"], dtype=float)
    except (KeyError, TypeError, ValueError) as exc:
        raise ConsensusError("contact proposal covariance is invalid") from exc
    if (
        covariance.shape != (2, 2)
        or not np.isfinite(covariance).all()
        or not np.allclose(covariance, covariance.T, atol=1e-9)
        or float(np.linalg.eigvalsh(covariance).min()) < -1e-9
    ):
        raise ConsensusError("contact proposal covariance is invalid")
    return covariance


def consensus_event(event_id, left, right):
    reasons = []
    if left.get("camera_id") != right.get("camera_id"):
        raise ConsensusError("contact proposal cameras differ")
    if left.get("selected_frame_timestamp_utc") != right.get("selected_frame_timestamp_utc"):
        raise ConsensusError("contact proposal timestamps differ")
    left_frame, right_frame = left.get("frame"), right.get("frame")
    if not valid_frame(left_frame) or not valid_frame(right_frame):
        raise ConsensusError("contact proposal frame binding is missing")
    for key in ("encoded_jpeg_sha256", "width", "height"):
        if left_frame.get(key) != right_frame.get(key):
            raise ConsensusError("contact proposal frame bindings differ")
    left_proposal = left.get("ground_contact_proposal")
    right_proposal = right.get("ground_contact_proposal")
    if not isinstance(left_proposal, dict):
        reasons.append("left_model_has_no_contact_proposal")
    if not isinstance(right_proposal, dict):
        reasons.append("right_model_has_no_contact_proposal")
    box_agreement = mask_agreement = None
    contact_disagreement = None
    contact_gate = np.asarray(
        [
            MAXIMUM_CONTACT_DISAGREEMENT_AT_1280_PX * left_frame["width"] / 1280.0,
            MAXIMUM_CONTACT_DISAGREEMENT_AT_1280_PX * left_frame["height"] / 960.0,
        ],
        dtype=float,
    )
    if not reasons:
        left_instance = left.get("matched_instance") or {}
        right_instance = right.get("matched_instance") or {}
        left_bbox = validated_bbox(
            left_instance.get("bbox_xyxy"), left_frame, "left model"
        )
        right_bbox = validated_bbox(
            right_instance.get("bbox_xyxy"), right_frame, "right model"
        )
        box_agreement = bbox_iou(left_bbox, right_bbox)
        if not math.isfinite(box_agreement) or not 0.0 <= box_agreement <= 1.0:
            raise ConsensusError("bbox IoU is outside [0, 1]")
        if box_agreement < MINIMUM_BBOX_IOU:
            reasons.append("bbox_iou_below_gate")
        mask_agreement = mask_iou(
            load_mask(left, left_frame), load_mask(right, right_frame)
        )
        if not math.isfinite(mask_agreement) or not 0.0 <= mask_agreement <= 1.0:
            raise ConsensusError("mask IoU is outside [0, 1]")
        if mask_agreement < MINIMUM_MASK_IOU:
            reasons.append("mask_iou_below_gate")
        left_pixel = validated_pixel(
            left_proposal.get("pixel"), left_frame, left_bbox, "left model"
        )
        right_pixel = validated_pixel(
            right_proposal.get("pixel"), right_frame, right_bbox, "right model"
        )
        contact_disagreement = np.abs(left_pixel - right_pixel)
        if np.any(contact_disagreement > contact_gate):
            reasons.append("contact_disagreement_above_gate")
    consensus = None
    if not reasons:
        midpoint = (left_pixel + right_pixel) / 2.0
        delta = np.abs(left_pixel - right_pixel) / 2.0
        left_cov = covariance_matrix(left_proposal)
        right_cov = covariance_matrix(right_proposal)
        variance = np.maximum(np.diag(left_cov), np.diag(right_cov)) + delta**2
        consensus = {
            "method": "cross_model_segmentation_support_midpoint_consensus",
            "pixel": midpoint.tolist(),
            "covariance_px2": [[float(variance[0]), 0.0], [0.0, float(variance[1])]],
            "reviewed": False,
        }
    return {
        "event_id": event_id,
        "camera_id": left.get("camera_id"),
        "frame": left_frame,
        "left": left,
        "right": right,
        "bbox_iou": box_agreement,
        "mask_iou": mask_agreement,
        "contact_disagreement_px": (
            None
            if contact_disagreement is None
            else {"x": float(contact_disagreement[0]), "y": float(contact_disagreement[1])}
        ),
        "maximum_contact_disagreement_px": {
            "x": float(contact_gate[0]),
            "y": float(contact_gate[1]),
        },
        "consensus": consensus,
        "rejection_reasons": reasons,
        "acceptance_eligible": False,
    }


def build(left_path, right_path):
    left_path, left = load_report(left_path)
    right_path, right = load_report(right_path)
    left_capture = left.get("capture_report") or {}
    right_capture = right.get("capture_report") or {}
    if left_capture.get("sha256") != right_capture.get("sha256"):
        raise ConsensusError("contact models do not share one capture report")
    if left_capture.get("path") != right_capture.get("path"):
        raise ConsensusError("contact models do not bind the same capture report path")
    try:
        _, _, capture_report = load_capture_report(
            left_capture.get("path"), expected_hash=left_capture.get("sha256")
        )
    except (OSError, RuntimeError, TypeError) as exc:
        raise ConsensusError("bound capture report is invalid or hash-mismatched") from exc
    if left.get("model", {}).get("sha256") == right.get("model", {}).get("sha256"):
        raise ConsensusError("contact consensus requires distinct model hashes")
    left_events, right_events = event_index(left), event_index(right)
    if set(left_events) != set(right_events):
        raise ConsensusError("contact models do not cover identical events")
    capture_events = event_index(capture_report)
    if set(left_events) != set(capture_events):
        raise ConsensusError("contact models do not cover the full capture denominator")
    events = [
        consensus_event(event_id, left_events[event_id], right_events[event_id])
        for event_id in sorted(left_events)
    ]
    accepted = [event for event in events if event["consensus"] is not None]
    return {
        "schema": SCHEMA,
        "generated_at_utc": utc_now(),
        "acceptance_eligible": False,
        "inputs": [
            {"path": str(left_path), "sha256": sha256(left_path), "model": left["model"]},
            {"path": str(right_path), "sha256": sha256(right_path), "model": right["model"]},
        ],
        "capture_report_sha256": left_capture["sha256"],
        "gate": {
            "minimum_bbox_iou": MINIMUM_BBOX_IOU,
            "minimum_mask_iou": MINIMUM_MASK_IOU,
            "maximum_contact_disagreement_at_1280_px": (
                MAXIMUM_CONTACT_DISAGREEMENT_AT_1280_PX
            ),
            "reference_canvas": {"width": 1280, "height": 960},
            "comparison": "per_axis_native_pixels",
            "requires_both_models": True,
            "requires_full_capture_denominator": True,
        },
        "events": events,
        "summary": {
            "event_count": len(events),
            "consensus_count": len(accepted),
            "rejected_count": len(events) - len(accepted),
            "median_mask_iou": (
                None if not accepted else float(np.median([item["mask_iou"] for item in accepted]))
            ),
            "maximum_contact_disagreement_px": (
                None
                if not accepted
                else {
                    axis: max(item["contact_disagreement_px"][axis] for item in accepted)
                    for axis in ("x", "y")
                }
            ),
        },
        "acceptance_failures": [
            "cross_model_masks_can_have_correlated_training_error",
            "consensus_is_not_independent_wheel_contact_or_world_truth",
            "static_camera_calibration_must_pass_before_backprojection",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("output")
    args = parser.parse_args()
    result = build(args.left, args.right)
    write_json_exclusive(args.output, result)
    print(json.dumps({**result["summary"], "output": str(Path(args.output).resolve())}))


if __name__ == "__main__":
    main()
