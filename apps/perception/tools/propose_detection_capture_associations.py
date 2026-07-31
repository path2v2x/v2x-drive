#!/usr/bin/env python3
"""Propose visual associations from hash-bound detection event captures.

Candidates are generated across all temporally plausible camera pairs,
independent of V2 model object ID. ConvNeXt crops must come from a separate,
hash-bound review on the exact retained frame. Appearance remains corroboration
until a reviewer confirms whole-track physical identity and wheel/road contact.
"""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys

import cv2
import numpy as np


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
if str(PERCEPTION_DIR) not in sys.path:
    sys.path.insert(0, str(PERCEPTION_DIR))
from tracking_utils import VehicleAppearanceExtractor  # noqa: E402


MINIMUM_PROPOSAL_SIMILARITY = 0.60


class AssociationError(RuntimeError):
    pass


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def parse_utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AssociationError("event timestamp is not canonical UTC")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise AssociationError("event timestamp is invalid") from exc
    return result.astimezone(timezone.utc)


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


def load_capture_report(path):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AssociationError("capture report is unreadable or invalid") from exc
    if value.get("schema") not in {
        "v2x-detection-event-frame-capture/v1",
        "v2x-detection-event-frame-capture/v2",
    }:
        raise AssociationError("capture report schema is unsupported")
    events = value.get("events")
    if not isinstance(events, list) or not events:
        raise AssociationError("capture report has no events")
    return path, raw, value


def load_crop_review(path, capture_report_hash):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AssociationError("crop review is unreadable or invalid") from exc
    if value.get("schema") != "v2x-selected-frame-vehicle-crop-review/v1":
        raise AssociationError("crop review schema is unsupported")
    if value.get("capture_report_sha256") != capture_report_hash:
        raise AssociationError("crop review does not bind the capture report")
    indexed = {}
    for item in value.get("crops", []):
        event_id = item.get("event_id") if isinstance(item, dict) else None
        if not isinstance(event_id, str) or event_id in indexed:
            raise AssociationError("crop review event IDs are missing or duplicated")
        if item.get("vehicle_fully_visible") is not True:
            continue
        indexed[event_id] = item
    if not indexed:
        raise AssociationError("crop review has no fully visible vehicle crops")
    return path, raw, indexed


def decode_bound_frame(event):
    descriptor = event.get("frame") if isinstance(event, dict) else None
    if not isinstance(descriptor, dict):
        raise AssociationError("event frame descriptor is missing")
    path = Path(descriptor.get("path", "")).resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AssociationError("event frame is unreadable") from exc
    if sha256_bytes(raw) != descriptor.get("sha256"):
        raise AssociationError("event frame hash does not match capture report")
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise AssociationError("event frame is not a decodable image")
    height, width = image.shape[:2]
    if [width, height] != [descriptor.get("width"), descriptor.get("height")]:
        raise AssociationError("event frame dimensions do not match capture report")
    return path, image


def vehicle_crop(image, bbox, padding_fraction=0.05):
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise AssociationError("event bbox is invalid")
    try:
        x1, y1, x2, y2 = map(float, bbox)
    except (TypeError, ValueError) as exc:
        raise AssociationError("event bbox is invalid") from exc
    if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
        raise AssociationError("event bbox contains non-finite values")
    padding_x = max(4.0, (x2 - x1) * padding_fraction)
    padding_y = max(4.0, (y2 - y1) * padding_fraction)
    height, width = image.shape[:2]
    left = max(0, int(math.floor(x1 - padding_x)))
    top = max(0, int(math.floor(y1 - padding_y)))
    right = min(width, int(math.ceil(x2 + padding_x)))
    bottom = min(height, int(math.ceil(y2 + padding_y)))
    if right - left < 20 or bottom - top < 20:
        raise AssociationError("event vehicle crop is too small")
    return image[top:bottom, left:right], [left, top, right, bottom]


def build_candidates(records, maximum_transit_seconds=90.0):
    values = sorted(records, key=lambda item: (item["timestamp"], item["event_id"]))
    candidates = []
    for left_index, left in enumerate(values):
        for right in values[left_index + 1:]:
            if left["camera_id"] == right["camera_id"]:
                continue
            delta = (right["timestamp"] - left["timestamp"]).total_seconds()
            if not 0.0 < delta <= maximum_transit_seconds:
                continue
            similarity = float(np.dot(left["embedding"], right["embedding"]))
            candidates.append({
                "source_object_ids": [left["object_id"], right["object_id"]],
                "same_model_object_id": left["object_id"] == right["object_id"],
                "source_object_types": [left["object_type"], right["object_type"]],
                "event_ids": [left["event_id"], right["event_id"]],
                "camera_ids": [left["camera_id"], right["camera_id"]],
                "timestamps_utc": [
                    left["timestamp"].isoformat(timespec="milliseconds").replace(
                        "+00:00", "Z"
                    ),
                    right["timestamp"].isoformat(timespec="milliseconds").replace(
                        "+00:00", "Z"
                    ),
                ],
                "transit_seconds": delta,
                "appearance_similarity": similarity,
                "proposal_threshold": MINIMUM_PROPOSAL_SIMILARITY,
                "visual_threshold_passed": similarity >= MINIMUM_PROPOSAL_SIMILARITY,
                "reviewed": False,
                "acceptance_eligible": False,
            })
    candidates.sort(key=lambda item: (
        item["camera_ids"], -item["appearance_similarity"],
        item["event_ids"],
    ))
    return candidates


def propose(
    capture_report_path, crop_review_path, output, device="cpu",
    maximum_transit_seconds=90.0,
):
    path, raw, report = load_capture_report(capture_report_path)
    report_hash = sha256_bytes(raw)
    review_path, review_raw, crop_reviews = load_crop_review(
        crop_review_path, report_hash
    )
    extractor = VehicleAppearanceExtractor(device=device)
    records = []
    seen = set()
    for event in report["events"]:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in seen:
            raise AssociationError("capture event IDs are invalid or duplicated")
        seen.add(event_id)
        _frame_path, image = decode_bound_frame(event)
        crop_review = crop_reviews.get(event_id)
        if crop_review is None:
            continue
        if crop_review.get("frame_sha256") != event["frame"].get("sha256"):
            raise AssociationError("crop review is not bound to the event frame")
        crop, bounds = vehicle_crop(image, crop_review.get("bbox_xyxy"))
        height, width = crop.shape[:2]
        embedding = extractor.extract(
            crop, {"x1": 0, "y1": 0, "x2": width, "y2": height}
        )
        if embedding is None:
            raise AssociationError("vehicle appearance embedding is unavailable")
        embedding = np.asarray(embedding, dtype=float)
        norm = float(np.linalg.norm(embedding))
        if embedding.ndim != 1 or not np.isfinite(embedding).all() or norm <= 0.0:
            raise AssociationError("vehicle appearance embedding is invalid")
        records.append({
            "event_id": event_id,
            "object_id": event.get("object_id"),
            "object_type": event.get("object_type"),
            "camera_id": event.get("camera_id"),
            "timestamp": parse_utc(event.get("selected_frame_timestamp_utc")),
            "embedding": embedding / norm,
            "crop_bounds_ltrb": bounds,
        })
    candidates = build_candidates(records, maximum_transit_seconds)
    grouped = defaultdict(list)
    for candidate in candidates:
        grouped["->".join(candidate["camera_ids"])].append(candidate)
    summaries = {}
    for camera_pair, values in grouped.items():
        similarities = np.asarray(
            [value["appearance_similarity"] for value in values], dtype=float
        )
        summaries[camera_pair] = {
            "candidate_count": len(values),
            "threshold_pass_count": sum(
                value["visual_threshold_passed"] for value in values
            ),
            "similarity_min": float(np.min(similarities)),
            "similarity_median": float(np.median(similarities)),
            "similarity_max": float(np.max(similarities)),
            "camera_pairs": sorted({
                "->".join(value["camera_ids"]) for value in values
            }),
            "different_model_id_count": sum(
                not value["same_model_object_id"] for value in values
            ),
        }
    result = {
        "schema": "v2x-detection-capture-association-proposals/v2",
        "acceptance_eligible": False,
        "capture_report": {
            "path": str(path),
            "sha256": sha256_bytes(raw),
        },
        "crop_review": {
            "path": str(review_path),
            "sha256": sha256_bytes(review_raw),
        },
        "extractor": {
            "name": "torchvision_convnext_base_imagenet1k_v1",
            "checkpoint_sha256": VehicleAppearanceExtractor.CHECKPOINT_SHA256,
            "device": device,
        },
        "maximum_transit_seconds": maximum_transit_seconds,
        "camera_pair_summaries": dict(sorted(summaries.items())),
        "candidates": candidates,
        "acceptance_failures": [
            "candidate_generation_is_independent_of_model_object_id",
            "appearance_similarity_is_corroboration_not_identity_truth",
            "physical_identity_has_not_been_manually_reviewed",
            "wheel_road_contacts_have_not_been_manually_reviewed",
        ],
    }
    write_json_exclusive(output, result)
    return result


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-report", type=Path, required=True)
    parser.add_argument("--crop-review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--maximum-transit-seconds", type=float, default=90.0)
    return parser.parse_args()


def main():
    args = parse_args()
    result = propose(
        args.capture_report,
        args.crop_review,
        args.output,
        args.device,
        args.maximum_transit_seconds,
    )
    print(json.dumps({
        "output": str(args.output.resolve()),
        "candidate_count": len(result["candidates"]),
        "threshold_pass_count": sum(
            candidate["visual_threshold_passed"]
            for candidate in result["candidates"]
        ),
        "acceptance_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
