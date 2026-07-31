#!/usr/bin/env python3
"""Run pinned local vehicle detection on exact hash-bound retained frames.

Event boxes came from a different source frame whenever KVS GetImages returned
only a nearby image. This tool detects every vehicle anew on the selected JPEG,
records all candidates, and uses the old box only as a non-geometric matching
hint. Its output is proposal evidence and cannot supply wheel/road contacts.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform

import cv2
import numpy as np


VEHICLE_LABELS = {"car", "truck", "bus", "motorcycle"}


class RedetectionError(RuntimeError):
    pass


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256(path):
    return sha256_bytes(Path(path).read_bytes())


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def write_bytes_exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_exclusive(path, value):
    write_bytes_exclusive(
        path, json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
    )


def load_capture_report(path, expected_hash=None):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
        report = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise RedetectionError("capture report is unreadable or invalid") from exc
    if expected_hash is not None and sha256_bytes(raw) != expected_hash:
        raise RedetectionError("capture report hash does not match")
    if report.get("schema") not in {
        "v2x-detection-event-frame-capture/v1",
        "v2x-detection-event-frame-capture/v2",
    }:
        raise RedetectionError("capture report schema is unsupported")
    if not isinstance(report.get("events"), list) or not report["events"]:
        raise RedetectionError("capture report has no events")
    return path, raw, report


def decode_bound_frame(event):
    descriptor = event.get("frame") if isinstance(event, dict) else None
    if not isinstance(descriptor, dict):
        raise RedetectionError("event frame descriptor is missing")
    path = Path(descriptor.get("path", "")).resolve()
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise RedetectionError("event frame is unreadable") from exc
    if sha256_bytes(encoded) != descriptor.get("sha256"):
        raise RedetectionError("event frame encoded-JPEG hash does not match")
    image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RedetectionError("event frame is not a decodable JPEG")
    height, width = image.shape[:2]
    if [width, height] != [descriptor.get("width"), descriptor.get("height")]:
        raise RedetectionError("event frame dimensions do not match")
    return path, image


def bbox_iou(left, right):
    lx1, ly1, lx2, ly2 = map(float, left)
    rx1, ry1, rx2, ry2 = map(float, right)
    intersection = max(0.0, min(lx2, rx2) - max(lx1, rx1)) * max(
        0.0, min(ly2, ry2) - max(ly1, ry1)
    )
    left_area = max(0.0, lx2 - lx1) * max(0.0, ly2 - ly1)
    right_area = max(0.0, rx2 - rx1) * max(0.0, ry2 - ry1)
    union = left_area + right_area - intersection
    return 0.0 if union <= 0.0 else intersection / union


def normalized_center_distance(left, right, width, height):
    left_center = np.asarray([(left[0] + left[2]) / 2, (left[1] + left[3]) / 2])
    right_center = np.asarray([(right[0] + right[2]) / 2, (right[1] + right[3]) / 2])
    return float(np.linalg.norm(left_center - right_center) / math.hypot(width, height))


def choose_event_match(detections, event_bbox, width, height):
    candidates = []
    for index, detection in enumerate(detections):
        if detection["label"] not in VEHICLE_LABELS:
            continue
        iou = bbox_iou(event_bbox, detection["bbox_xyxy"])
        distance = normalized_center_distance(
            event_bbox, detection["bbox_xyxy"], width, height
        )
        score = 0.7 * iou + 0.3 * math.exp(-8.0 * distance)
        candidates.append((score, iou, -distance, index))
    if not candidates:
        return None
    score, iou, negative_distance, index = max(candidates)
    return {
        "detection_index": index,
        "proposal_score": score,
        "event_bbox_iou": iou,
        "normalized_center_distance": -negative_distance,
        "uses_event_bbox_as_geometry": False,
    }


def touches_boundary(bbox, width, height):
    x1, y1, x2, y2 = bbox
    return x1 <= 0.0 or y1 <= 0.0 or x2 >= width or y2 >= height


def draw_overlay(image, detections, match):
    output = image.copy()
    selected = None if match is None else match["detection_index"]
    for index, item in enumerate(detections):
        x1, y1, x2, y2 = np.rint(item["bbox_xyxy"]).astype(int)
        color = (30, 220, 30) if index == selected else (0, 190, 255)
        thickness = 6 if index == selected else 3
        cv2.rectangle(output, (x1, y1), (x2, y2), color, thickness)
        label = f"{index} {item['label']} {item['confidence']:.2f}"
        cv2.putText(
            output, label, (max(4, x1), max(30, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 5, cv2.LINE_AA,
        )
        cv2.putText(
            output, label, (max(4, x1), max(30, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA,
        )
    success, encoded = cv2.imencode(".jpg", output, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not success:
        raise RedetectionError("failed to encode detection overlay")
    return encoded.tobytes()


def make_review_sheet(entries, columns=4, tile_width=480, tile_height=390):
    rows = math.ceil(len(entries) / columns)
    sheet = np.full(
        (rows * tile_height, columns * tile_width, 3), 28, dtype=np.uint8
    )
    for index, (label, encoded) in enumerate(entries):
        image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RedetectionError("overlay cannot be decoded for review sheet")
        available_height = tile_height - 30
        scale = min(tile_width / image.shape[1], available_height / image.shape[0])
        resized = cv2.resize(
            image,
            (max(1, int(image.shape[1] * scale)), max(1, int(image.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
        row, column = divmod(index, columns)
        x = column * tile_width + (tile_width - resized.shape[1]) // 2
        y = row * tile_height + 30 + (available_height - resized.shape[0]) // 2
        sheet[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
        cv2.putText(
            sheet,
            label,
            (column * tile_width + 6, row * tile_height + 22),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (240, 240, 240),
            1,
            cv2.LINE_AA,
        )
    success, encoded = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not success:
        raise RedetectionError("failed to encode redetection review sheet")
    return encoded.tobytes()


def model_detections(model, image, confidence, iou_threshold, image_size, device):
    results = model.predict(
        source=image,
        conf=confidence,
        iou=iou_threshold,
        imgsz=image_size,
        device=device,
        verbose=False,
    )
    detections = []
    for result in results:
        for box in result.boxes:
            class_id = int(box.cls[0].item())
            label = str(result.names[class_id])
            if label not in VEHICLE_LABELS:
                continue
            detections.append({
                "label": label,
                "confidence": float(box.conf[0].item()),
                "bbox_xyxy": [float(value) for value in box.xyxy[0].tolist()],
            })
    return sorted(
        detections,
        key=lambda item: (-item["confidence"], item["label"], item["bbox_xyxy"]),
    )


def redetect(
    capture_report_path,
    model_path,
    output_directory,
    confidence=0.25,
    iou_threshold=0.7,
    image_size=1280,
    device="cpu",
    capture_report_hash=None,
):
    if not 0.0 < confidence <= 1.0 or not 0.0 < iou_threshold <= 1.0:
        raise RedetectionError("confidence and IoU thresholds must be in (0, 1]")
    if image_size < 320 or image_size > 2560 or image_size % 32:
        raise RedetectionError("image size must be a multiple of 32 in [320, 2560]")
    report_path, report_raw, report = load_capture_report(
        capture_report_path, capture_report_hash
    )
    model_path = Path(model_path).resolve()
    if not model_path.is_file():
        raise RedetectionError("model is unavailable")
    model_hash = sha256(model_path)
    try:
        import torch
        import ultralytics
        from ultralytics import YOLO
    except ImportError as exc:
        raise RedetectionError("pinned YOLO runtime is unavailable") from exc
    model = YOLO(str(model_path))
    output_directory = Path(output_directory).resolve()
    events, seen, review_entries = [], set(), []
    for event in report["events"]:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in seen:
            raise RedetectionError("capture event IDs are invalid or duplicated")
        seen.add(event_id)
        frame_path, image = decode_bound_frame(event)
        height, width = image.shape[:2]
        detections = model_detections(
            model, image, confidence, iou_threshold, image_size, device
        )
        match = choose_event_match(
            detections, event.get("bbox_xyxy"), width, height
        )
        for item in detections:
            item["touches_frame_boundary"] = touches_boundary(
                item["bbox_xyxy"], width, height
            )
        overlay_path = output_directory / "overlays" / f"{event_id}.jpg"
        overlay_encoded = draw_overlay(image, detections, match)
        write_bytes_exclusive(overlay_path, overlay_encoded)
        review_entries.append((
            f"{event.get('camera_id')} {event_id[:8]} det={len(detections)}",
            overlay_encoded,
        ))
        events.append({
            "event_id": event_id,
            "source_object_id": event.get("object_id"),
            "source_object_type": event.get("object_type"),
            "camera_id": event.get("camera_id"),
            "selected_frame_timestamp_utc": event.get(
                "selected_frame_timestamp_utc"
            ),
            "frame": {
                "path": str(frame_path),
                "encoded_jpeg_sha256": event["frame"]["sha256"],
                "width": width,
                "height": height,
            },
            "event_bbox_matching_hint": {
                "bbox_xyxy": event.get("bbox_xyxy"),
                "source_frame_timestamp_utc": event.get("media_timestamp_utc"),
                "applies_to_selected_frame": False,
            },
            "detections": detections,
            "event_match_proposal": match,
            "overlay": {"path": str(overlay_path), "sha256": sha256(overlay_path)},
            "acceptance_eligible": False,
        })
    review_sheet_path = output_directory / "redetection-review-sheet.jpg"
    write_bytes_exclusive(review_sheet_path, make_review_sheet(review_entries))
    return {
        "schema": "v2x-selected-frame-redetection/v1",
        "generated_at_utc": utc_now(),
        "acceptance_eligible": False,
        "capture_report": {
            "path": str(report_path),
            "sha256": sha256_bytes(report_raw),
        },
        "model": {"path": str(model_path), "sha256": model_hash},
        "inference": {
            "confidence": confidence,
            "nms_iou": iou_threshold,
            "image_size": image_size,
            "device": device,
            "ultralytics_version": ultralytics.__version__,
            "torch_version": torch.__version__,
            "opencv_version": cv2.__version__,
            "python_version": platform.python_version(),
            "encoded_jpeg_hashed_before_decode": True,
        },
        "events": events,
        "review_sheet": {
            "path": str(review_sheet_path),
            "sha256": sha256(review_sheet_path),
        },
        "summary": {
            "event_count": len(events),
            "detection_count": sum(len(item["detections"]) for item in events),
            "matched_event_count": sum(
                item["event_match_proposal"] is not None for item in events
            ),
            "boundary_detection_count": sum(
                detection["touches_frame_boundary"]
                for item in events
                for detection in item["detections"]
            ),
        },
        "acceptance_failures": [
            "detections_are_model_proposals_not_reviewed_vehicle_crops",
            "event_bbox_is_used_only_as_a_nearby_frame_matching_hint",
            "wheel_road_contacts_are_not_labeled",
            "selection_corpus_originates_from_prior_detection_events",
        ],
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-report", type=Path, required=True)
    parser.add_argument("--capture-report-sha256")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument("--nms-iou", type=float, default=0.7)
    parser.add_argument("--image-size", type=int, default=1280)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    result = redetect(
        args.capture_report,
        args.model,
        args.output_directory,
        args.confidence,
        args.nms_iou,
        args.image_size,
        args.device,
        args.capture_report_sha256,
    )
    write_json_exclusive(args.output, result)
    print(json.dumps({
        "output": str(args.output.resolve()),
        **result["summary"],
        "acceptance_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
