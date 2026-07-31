#!/usr/bin/env python3
"""Bundle exact historical-correlation reports for downstream pixel review."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from redetect_selected_capture_frames import sha256, write_json_exclusive


SCHEMA = "v2x-detection-event-frame-capture/v2"
INPUT_VERIFIER = "historical_video_detection_correlation"
MAXIMUM_IDENTITY_ERROR_MS = 1.0


class ExactCaptureError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def load_exact_report(path: Path) -> tuple[Path, dict[str, object]]:
    path = Path(path).resolve()
    try:
        report = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ExactCaptureError("correlation report is unreadable or invalid") from exc
    if not isinstance(report, dict):
        raise ExactCaptureError("correlation report is not an object")
    if report.get("verifier") != INPUT_VERIFIER or report.get("schema_version") != 1:
        raise ExactCaptureError("correlation report contract is unsupported")
    if report.get("result", {}).get("gate_passed") is not True:
        raise ExactCaptureError("correlation report did not pass its source gate")
    if report.get("safety", {}).get("signed_urls_emitted") is not False:
        raise ExactCaptureError("correlation report does not prove signed URL redaction")
    return path, report


def normalize_event(path: Path, report: dict[str, object]) -> dict[str, object]:
    detection = report.get("detection")
    frame = report.get("frame")
    if not isinstance(detection, dict) or not isinstance(frame, dict):
        raise ExactCaptureError("correlation report lacks detection or frame binding")
    event_id = detection.get("event_id")
    camera_id = detection.get("camera_id")
    bbox = detection.get("saved_bbox")
    dimensions = frame.get("dimensions")
    timing_error = frame.get("absolute_error_ms")
    frame_path = Path(str(frame.get("path", ""))).resolve()
    if not isinstance(event_id, str) or not event_id:
        raise ExactCaptureError("correlation event id is missing")
    if camera_id not in {"ch1", "ch2", "ch3", "ch4"}:
        raise ExactCaptureError("correlation camera is invalid")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(_finite_number(value) for value in bbox)
    ):
        raise ExactCaptureError("correlation bbox is invalid")
    if (
        not isinstance(dimensions, list)
        or len(dimensions) != 2
        or not all(isinstance(value, int) and value > 0 for value in dimensions)
    ):
        raise ExactCaptureError("correlation frame dimensions are invalid")
    if not _finite_number(timing_error) or float(timing_error) > MAXIMUM_IDENTITY_ERROR_MS:
        raise ExactCaptureError("correlation frame is not exact enough for bbox identity")
    if not frame_path.is_file() or sha256(frame_path) != frame.get("sha256"):
        raise ExactCaptureError("correlation frame hash does not match")
    persisted_timestamp = detection.get("persisted_media_timestamp")
    selected_timestamp = frame.get("selected_media_timestamp")
    if (
        not isinstance(persisted_timestamp, str)
        or not isinstance(selected_timestamp, str)
        or persisted_timestamp != selected_timestamp
    ):
        raise ExactCaptureError("correlation timestamps are not exactly equal")
    width, height = dimensions
    return {
        "event_id": event_id,
        "object_id": detection.get("object_id"),
        "object_type": detection.get("object_type"),
        "camera_id": camera_id,
        "stream_name": None,
        "media_timestamp_utc": persisted_timestamp,
        "selected_frame_timestamp_utc": selected_timestamp,
        "absolute_time_offset_ms": float(timing_error),
        "bbox_xyxy": [float(value) for value in bbox],
        "bbox_frame_binding": {
            "source": "exact_archived_fmp4_frame",
            "applies_to_selected_frame": True,
            "selected_frame_identity_tolerance_ms": MAXIMUM_IDENTITY_ERROR_MS,
        },
        "frame": {
            "path": str(frame_path),
            "sha256": frame["sha256"],
            "width": width,
            "height": height,
        },
        "ground_contact_reviewed": False,
        "selected_frame_geometry_eligible": True,
        "acceptance_eligible": False,
        "acceptance_failures": [
            "ground_contact_is_not_independently_reviewed",
            "camera_calibration_has_not_passed_static_geometry_gates",
        ],
        "source_correlation_report": {
            "path": str(path),
            "sha256": sha256(path),
        },
    }


def build(report_paths: list[Path]) -> dict[str, object]:
    if not report_paths:
        raise ExactCaptureError("at least one correlation report is required")
    events = []
    seen = set()
    sources = []
    for report_path in report_paths:
        path, report = load_exact_report(report_path)
        event = normalize_event(path, report)
        event_id = event["event_id"]
        if event_id in seen:
            raise ExactCaptureError("correlation event ids are duplicated")
        seen.add(event_id)
        events.append(event)
        sources.append({"path": str(path), "sha256": sha256(path)})
    events.sort(key=lambda event: (event["camera_id"], event["event_id"]))
    cameras = sorted({event["camera_id"] for event in events})
    return {
        "schema": SCHEMA,
        "generated_at_utc": utc_now(),
        "acceptance_eligible": False,
        "sources": sources,
        "events": events,
        "summary": {
            "event_count": len(events),
            "camera_count": len(cameras),
            "cameras": cameras,
            "exact_bbox_binding_count": len(events),
        },
        "acceptance_failures": [
            "exact_frames_are_inputs_not_reviewed_ground_contact_truth",
            "static_camera_calibration_must_pass_before_backprojection",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = build(args.reports)
    write_json_exclusive(args.output, result)
    print(json.dumps({**result["summary"], "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
