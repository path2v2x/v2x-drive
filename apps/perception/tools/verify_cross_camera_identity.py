#!/usr/bin/env python3
"""Verify one physical vehicle identity across two archived camera frames."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracking_utils import VehicleAppearanceExtractor  # noqa: E402


MINIMUM_SIMILARITY = 0.60
MAXIMUM_TRANSIT_SECONDS = 15.0


class VerificationError(RuntimeError):
    pass


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationError("historical report timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise VerificationError("historical report timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def validate_report_pair(left, right, similarity, maximum_transit_seconds=15.0):
    for report in (left, right):
        if report.get("result", {}).get("gate_passed") is not True:
            raise VerificationError("historical visual correlation did not pass")
        if report.get("result", {}).get("visual_corroborated") is not True:
            raise VerificationError("historical vehicle lacks visual corroboration")
        trust = report.get("detection", {}).get("media_timestamp_trust", {})
        if (
            trust.get("trusted") is not True
            or trust.get("timestamp_schema_version") != 2
            or trust.get("source") != "hls_ext_x_program_date_time"
        ):
            raise VerificationError("historical vehicle lacks trusted schema-v2 time")
    left_detection, right_detection = left["detection"], right["detection"]
    if left_detection.get("object_id") != right_detection.get("object_id"):
        raise VerificationError("historical reports do not share one object_id")
    if left_detection.get("object_type") not in {"car", "truck", "bus"}:
        raise VerificationError("historical object is not a supported vehicle")
    if left_detection.get("object_type") != right_detection.get("object_type"):
        raise VerificationError("historical reports disagree on vehicle type")
    if left_detection.get("camera_id") == right_detection.get("camera_id"):
        raise VerificationError("cross-camera proof requires two different cameras")
    left_time = _timestamp(left_detection.get("persisted_media_timestamp"))
    right_time = _timestamp(right_detection.get("persisted_media_timestamp"))
    delta = abs((right_time - left_time).total_seconds())
    if not 0.0 < delta <= min(maximum_transit_seconds, MAXIMUM_TRANSIT_SECONDS):
        raise VerificationError("cross-camera vehicle transit time is outside the gate")
    similarity = float(similarity)
    if not np.isfinite(similarity) or similarity < MINIMUM_SIMILARITY:
        raise VerificationError("cross-camera vehicle appearance is below threshold")
    return {
        "object_id": left_detection["object_id"],
        "object_type": left_detection["object_type"],
        "cameras": [left_detection["camera_id"], right_detection["camera_id"]],
        "timestamps": [
            left_detection["persisted_media_timestamp"],
            right_detection["persisted_media_timestamp"],
        ],
        "transit_seconds": round(delta, 3),
        "appearance_similarity": round(similarity, 4),
        "minimum_similarity": MINIMUM_SIMILARITY,
    }


def _frame_crop(report, frame_path):
    data = Path(frame_path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != report.get("frame", {}).get("sha256"):
        raise VerificationError("archived frame hash does not match its report")
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise VerificationError("archived frame could not be decoded")
    bbox = report.get("detection", {}).get("saved_bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise VerificationError("historical report has no saved vehicle bbox")
    x1, y1, x2, y2 = map(int, bbox)
    height, width = image.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(width, x2), min(height, y2)
    if x2 - x1 < 20 or y2 - y1 < 20:
        raise VerificationError("historical vehicle crop is too small")
    return image[y1:y2, x1:x2], digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left-report", required=True)
    parser.add_argument("--right-report", required=True)
    parser.add_argument("--left-frame", required=True)
    parser.add_argument("--right-frame", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--maximum-transit-seconds", type=float, default=15.0)
    args = parser.parse_args()
    try:
        left = json.loads(Path(args.left_report).read_text())
        right = json.loads(Path(args.right_report).read_text())
        left_crop, left_hash = _frame_crop(left, args.left_frame)
        right_crop, right_hash = _frame_crop(right, args.right_frame)
        extractor = VehicleAppearanceExtractor(device=args.device)

        def embedding(crop):
            height, width = crop.shape[:2]
            return extractor.extract(
                crop, {"x1": 0, "y1": 0, "x2": width, "y2": height}
            )

        left_embedding, right_embedding = embedding(left_crop), embedding(right_crop)
        if left_embedding is None or right_embedding is None:
            raise VerificationError("vehicle appearance embedding is unavailable")
        similarity = float(np.dot(left_embedding, right_embedding))
        evidence = validate_report_pair(
            left, right, similarity, args.maximum_transit_seconds
        )
        evidence.update({
            "gate_passed": True,
            "frame_sha256": [left_hash, right_hash],
            "extractor": "torchvision_convnext_base_imagenet1k_v1",
            "extractor_checkpoint_sha256": (
                VehicleAppearanceExtractor.CHECKPOINT_SHA256
            ),
        })
        Path(args.output).write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"gate_passed": False, "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
