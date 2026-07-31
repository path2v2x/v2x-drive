#!/usr/bin/env python3
"""Build hash-bound wheel-contact review crops from strict detector consensus."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import uuid

import cv2
import numpy as np


INPUT_SCHEMA = "v2x-selected-frame-redetection-consensus/v1"
OUTPUT_SCHEMA = "v2x-consensus-contact-review-sheet/v1"


class ContactReviewError(RuntimeError):
    pass


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def validate_bbox(value, width, height, label):
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
    ):
        raise ContactReviewError(f"{label} bbox is invalid")
    x1, y1, x2, y2 = map(float, value)
    if not (0.0 <= x1 < x2 <= width and 0.0 <= y1 < y2 <= height):
        raise ContactReviewError(f"{label} bbox is outside the retained frame")
    return np.asarray([x1, y1, x2, y2], dtype=float)


def load_consensus(path):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
        report = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContactReviewError("consensus report is unreadable or invalid") from exc
    if (
        report.get("schema") != INPUT_SCHEMA
        or report.get("acceptance_eligible") is not False
    ):
        raise ContactReviewError("consensus report contract is unsupported")
    return path, raw, report


def load_frame(event):
    frame = event.get("frame")
    if not isinstance(frame, dict):
        raise ContactReviewError("consensus event frame binding is missing")
    path = Path(frame.get("path", "")).expanduser().resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ContactReviewError("retained consensus frame is unreadable") from exc
    if sha256_bytes(raw) != frame.get("encoded_jpeg_sha256"):
        raise ContactReviewError("retained consensus frame hash does not match")
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ContactReviewError("retained consensus frame cannot be decoded")
    height, width = image.shape[:2]
    if [width, height] != [frame.get("width"), frame.get("height")]:
        raise ContactReviewError("retained consensus frame dimensions do not match")
    return path, raw, image


def encode_png(image):
    ok, payload = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 4])
    if not ok:
        raise ContactReviewError("contact review crop encoding failed")
    return payload.tobytes()


def crop_bounds(bbox, width, height, padding_fraction):
    x1, y1, x2, y2 = bbox
    box_width, box_height = x2 - x1, y2 - y1
    padding_x = max(16.0, padding_fraction * box_width)
    padding_top = max(16.0, padding_fraction * box_height)
    padding_bottom = max(32.0, 0.75 * box_height)
    return [
        max(0, int(math.floor(x1 - padding_x))),
        max(0, int(math.floor(y1 - padding_top))),
        min(width, int(math.ceil(x2 + padding_x))),
        min(height, int(math.ceil(y2 + padding_bottom))),
    ]


def draw_review(image, event, origin):
    output = image.copy()
    x0, y0 = origin
    colors = {
        "left_detection": (0, 0, 255),
        "right_detection": (255, 0, 0),
        "consensus": (0, 255, 255),
    }
    for key in ("left_detection", "right_detection"):
        value = event[key]["bbox_xyxy"]
        p1 = (round(value[0] - x0), round(value[1] - y0))
        p2 = (round(value[2] - x0), round(value[3] - y0))
        cv2.rectangle(output, p1, p2, colors[key], 2)
    value = event["consensus"]["bbox_xyxy"]
    p1 = (round(value[0] - x0), round(value[1] - y0))
    p2 = (round(value[2] - x0), round(value[3] - y0))
    cv2.rectangle(output, p1, p2, colors["consensus"], 3)
    contact = event["consensus"]["bottom_center_pixel"]
    point = (round(contact[0] - x0), round(contact[1] - y0))
    cv2.drawMarker(
        output,
        point,
        (0, 255, 255),
        markerType=cv2.MARKER_CROSS,
        markerSize=24,
        thickness=2,
    )
    cv2.putText(
        output,
        f"{event['camera_id']} {event['event_id']}",
        (10, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return output


def build(consensus_path, output_dir, padding_fraction=0.30):
    if not math.isfinite(float(padding_fraction)) or not 0.0 <= padding_fraction <= 1.0:
        raise ContactReviewError("crop padding fraction is invalid")
    consensus_path, consensus_raw, report = load_consensus(consensus_path)
    events = [
        item
        for item in report.get("events", [])
        if isinstance(item, dict) and item.get("consensus") is not None
    ]
    if not events:
        raise ContactReviewError("consensus report has no accepted box proposals")
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise ContactReviewError("contact review output already exists")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_dir.parent / f".{output_dir.name}.tmp-{uuid.uuid4().hex}"
    rows = []
    try:
        temporary.mkdir()
        for event in events:
            event_id = event.get("event_id")
            camera_id = event.get("camera_id")
            if (
                not isinstance(event_id, str)
                or not event_id
                or not isinstance(camera_id, str)
            ):
                raise ContactReviewError("consensus event identity is invalid")
            frame_path, frame_raw, image = load_frame(event)
            height, width = image.shape[:2]
            consensus = event["consensus"]
            bbox = validate_bbox(consensus.get("bbox_xyxy"), width, height, "consensus")
            validate_bbox(
                event.get("left_detection", {}).get("bbox_xyxy"),
                width,
                height,
                "left detector",
            )
            validate_bbox(
                event.get("right_detection", {}).get("bbox_xyxy"),
                width,
                height,
                "right detector",
            )
            contact = consensus.get("bottom_center_pixel")
            uncertainty = consensus.get("bottom_center_uncertainty_px")
            if (
                not isinstance(contact, list)
                or len(contact) != 2
                or not all(math.isfinite(float(item)) for item in contact)
                or not isinstance(uncertainty, list)
                or len(uncertainty) != 2
                or not all(0.0 < float(item) <= 64.0 for item in uncertainty)
            ):
                raise ContactReviewError("consensus road-contact proposal is invalid")
            bounds = crop_bounds(bbox, width, height, float(padding_fraction))
            x1, y1, x2, y2 = bounds
            raw_crop = image[y1:y2, x1:x2]
            if raw_crop.size == 0:
                raise ContactReviewError("consensus review crop is empty")
            annotated = draw_review(raw_crop, event, (x1, y1))
            raw_payload = encode_png(raw_crop)
            annotated_payload = encode_png(annotated)
            raw_name = f"{camera_id}-{event_id}-raw.png"
            annotated_name = f"{camera_id}-{event_id}-annotated.png"
            (temporary / raw_name).write_bytes(raw_payload)
            (temporary / annotated_name).write_bytes(annotated_payload)
            rows.append({
                "event_id": event_id,
                "camera_id": camera_id,
                "selected_frame_timestamp_utc": event.get(
                    "selected_frame_timestamp_utc"
                ),
                "frame": {
                    "path": str(frame_path),
                    "sha256": sha256_bytes(frame_raw),
                    "resolution": [width, height],
                },
                "consensus_bbox_xyxy": bbox.tolist(),
                "proposed_bottom_center_pixel": list(map(float, contact)),
                "bottom_center_uncertainty_px": list(map(float, uncertainty)),
                "crop_bounds_xyxy": bounds,
                "crop_origin_pixel": [x1, y1],
                "raw_crop": {"path": raw_name, "sha256": sha256_bytes(raw_payload)},
                "annotated_crop": {
                    "path": annotated_name,
                    "sha256": sha256_bytes(annotated_payload),
                },
                "wheel_road_contact_reviewed": False,
                "acceptance_eligible": False,
            })
        result = {
            "schema": OUTPUT_SCHEMA,
            "acceptance_eligible": False,
            "consensus_report": {
                "path": str(consensus_path),
                "sha256": sha256_bytes(consensus_raw),
            },
            "padding_fraction": float(padding_fraction),
            "crops": rows,
            "acceptance_failures": [
                "consensus_bottom_centres_are_not_reviewed_wheel_contacts",
                "codex_contact_review_is_not_independent_human_review",
                "crop_evidence_does_not_establish_vehicle_identity_or_world_position",
            ],
        }
        payload = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
        (temporary / "contact-review-sheet.json").write_bytes(payload)
        os.rename(temporary, output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output_dir / "contact-review-sheet.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consensus-report", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--padding-fraction", type=float, default=0.30)
    args = parser.parse_args(argv)
    try:
        output = build(
            args.consensus_report,
            args.output_dir,
            args.padding_fraction,
        )
    except ContactReviewError as exc:
        parser.error(str(exc))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
