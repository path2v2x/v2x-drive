#!/usr/bin/env python3
"""Verify persisted, evidence-backed vehicle identity across street cameras."""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys

PERCEPTION_DIR = Path(__file__).resolve().parents[1]
if str(PERCEPTION_DIR) not in sys.path:
    sys.path.insert(0, str(PERCEPTION_DIR))
from tools.verify_detection_persistence import (  # noqa: E402
    camera_id_for_item,
    fetch_detection_window,
    iso_millis,
    trusted_media_time,
)

VEHICLE_TYPES = {"car", "truck", "bus"}
MAX_UNCERTAINTY_M = 2.0
MIN_APPEARANCE = 0.60


def haversine_m(left, right):
    radius = 6_371_000.0
    lat1, lon1 = math.radians(left[0]), math.radians(left[1])
    lat2, lon2 = math.radians(right[0]), math.radians(right[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


def persisted_vehicle(item):
    timestamp, clock_reasons = trusted_media_time(item)
    reasons = list(clock_reasons)
    camera_id = camera_id_for_item(item)
    if camera_id is None:
        reasons.append("camera_id")
    if item.get("object_type") not in VEHICLE_TYPES:
        reasons.append("vehicle_type")
    if not isinstance(item.get("object_id"), str) or not item["object_id"]:
        reasons.append("object_id")
    if not isinstance(item.get("perception_run_id"), str) or not item["perception_run_id"]:
        reasons.append("perception_run_id")
    gps = item.get("gps_location") or {}
    try:
        position = (float(gps["latitude"]), float(gps["longitude"]))
    except (KeyError, TypeError, ValueError):
        position = None
        reasons.append("gps_location")
    world = (
        (item.get("camera_data") or {})
        .get("bifocal_metadata", {})
        .get("world_position", {})
    )
    try:
        uncertainty = float(world["uncertainty_meters"])
    except (KeyError, TypeError, ValueError):
        uncertainty = None
        reasons.append("world_uncertainty")
    if uncertainty is not None and (
        not math.isfinite(uncertainty) or not 0.0 <= uncertainty <= MAX_UNCERTAINTY_M
    ):
        reasons.append("world_uncertainty")
    bbox = (item.get("camera_data") or {}).get("bifocal_metadata", {}).get("bbox") or {}
    try:
        x1, y1, x2, y2 = (float(bbox[key]) for key in ("x1", "y1", "x2", "y2"))
        bbox_valid = all(math.isfinite(value) for value in (x1, y1, x2, y2)) and x2 > x1 and y2 > y1
    except (KeyError, TypeError, ValueError):
        bbox_valid = False
    if not bbox_valid:
        reasons.append("bbox")
    return {
        "passed": timestamp is not None and not reasons,
        "reasons": reasons,
        "timestamp": timestamp,
        "camera_id": camera_id,
        "position": position,
        "uncertainty_m": uncertainty,
        "item": item,
    }


def association_passes(earlier, later, distance_m, delta_s):
    evidence = later["item"].get("identity_association")
    reasons = []
    if not isinstance(evidence, dict):
        return False, ["identity_association_missing"]
    if evidence.get("method") != "cross_camera_spatiotemporal_convnext":
        reasons.append("association_method")
    if evidence.get("previous_device_id") != earlier["item"].get("device_id"):
        reasons.append("previous_device_id")
    try:
        appearance = float(evidence["appearance_similarity"])
        recorded_distance = float(evidence["distance_meters"])
    except (KeyError, TypeError, ValueError):
        appearance = recorded_distance = math.nan
    if not math.isfinite(appearance) or appearance < MIN_APPEARANCE:
        reasons.append("appearance_similarity")
    tolerance = earlier["uncertainty_m"] + later["uncertainty_m"] + 0.5
    if not math.isfinite(recorded_distance) or abs(recorded_distance - distance_m) > tolerance:
        reasons.append("association_distance")
    if delta_s <= 0.0:
        reasons.append("temporal_order")
    return not reasons, reasons


def evaluate_cross_camera_tracks(items, max_transit_seconds=30.0, max_speed_mps=25.0):
    groups = defaultdict(list)
    rejected = 0
    for item in items:
        if not isinstance(item, dict) or item.get("object_type") not in VEHICLE_TYPES:
            continue
        record = persisted_vehicle(item)
        if not record["passed"]:
            rejected += 1
            continue
        groups[(item["perception_run_id"], item["object_id"])].append(record)
    accepted, diagnostics = [], []
    for (run_id, object_id), records in groups.items():
        records.sort(key=lambda record: record["timestamp"])
        for earlier in records:
            for later in records:
                if earlier["camera_id"] == later["camera_id"]:
                    continue
                delta_s = (later["timestamp"] - earlier["timestamp"]).total_seconds()
                if not 0.0 < delta_s <= float(max_transit_seconds):
                    continue
                distance_m = haversine_m(earlier["position"], later["position"])
                uncertainty = earlier["uncertainty_m"] + later["uncertainty_m"]
                plausible = distance_m <= float(max_speed_mps) * delta_s + uncertainty
                association_ok, association_reasons = association_passes(
                    earlier, later, distance_m, delta_s
                )
                result = {
                    "perception_run_id": run_id,
                    "object_id": object_id,
                    "object_type": later["item"]["object_type"],
                    "from_camera": earlier["camera_id"],
                    "to_camera": later["camera_id"],
                    "from_timestamp": iso_millis(earlier["timestamp"]),
                    "to_timestamp": iso_millis(later["timestamp"]),
                    "transit_seconds": round(delta_s, 3),
                    "distance_meters": round(distance_m, 3),
                    "plausible_motion": plausible,
                    "association_reasons": association_reasons,
                }
                (accepted if plausible and association_ok else diagnostics).append(result)
    diagnostics.sort(key=lambda pair: (
        not pair["plausible_motion"],
        len(pair["association_reasons"]),
        -datetime.fromisoformat(
            pair["to_timestamp"].replace("Z", "+00:00")
        ).timestamp(),
        pair["transit_seconds"],
    ))
    accepted.sort(key=lambda pair: pair["to_timestamp"], reverse=True)
    return {
        "gate_passed": bool(accepted),
        "accepted_pair_count": len(accepted),
        "accepted_pairs": accepted[:20],
        "diagnostic_pair_count": len(diagnostics),
        "diagnostic_pairs": diagnostics[:20],
        "rejected_vehicle_records": rejected,
        "tracked_vehicle_ids": len(groups),
        "thresholds": {
            "max_transit_seconds": float(max_transit_seconds),
            "max_speed_mps": float(max_speed_mps),
            "max_uncertainty_m": MAX_UNCERTAINTY_M,
            "minimum_appearance_similarity": MIN_APPEARANCE,
        },
        "reasons": [] if accepted else ["no evidence-backed cross-camera vehicle pair"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("api_base_url")
    parser.add_argument("--window-hours", type=float, default=24.0)
    parser.add_argument("--max-transit-seconds", type=float, default=30.0)
    parser.add_argument("--max-speed-mps", type=float, default=25.0)
    args = parser.parse_args()
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.window_hours)
    items, pages = fetch_detection_window(args.api_base_url, start, end)
    report = evaluate_cross_camera_tracks(
        items,
        max_transit_seconds=args.max_transit_seconds,
        max_speed_mps=args.max_speed_mps,
    )
    report.update({
        "window": {"start": iso_millis(start), "end": iso_millis(end)},
        "pages": pages,
        "total_items": len(items),
    })
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
