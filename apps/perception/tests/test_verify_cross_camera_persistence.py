"""Strict persisted cross-camera vehicle identity acceptance tests."""

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "verify_cross_camera_persistence.py"
SPEC = importlib.util.spec_from_file_location("verify_cross_camera_persistence", TOOL)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def item(camera, timestamp, *, association=None):
    media_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    anchor = media_time - timedelta(milliseconds=667)
    payload = {
        "object_id": "global_car_run_42",
        "object_type": "car",
        "perception_run_id": "run",
        "device_id": f"cam-001-{camera}",
        "timestamp_schema_version": 2,
        "media_time_trusted": True,
        "media_clock_status": "matched",
        "media_clock": {
            "source": "hls_ext_x_program_date_time",
            "schema_version": 1,
            "evidence_method": "exact_same_session_pts",
            "anchor_program_date_time_utc": anchor.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "position_milliseconds": 667.0,
            "anchor_fragment_frame_offset_milliseconds": 667.0,
            "capture_position_milliseconds": 667.0,
            "anchor_capture_position_milliseconds": 667.0,
            "source_pts": 667,
            "source_time_base_numerator": 1,
            "source_time_base_denominator": 1000,
        },
        "timestamp_utc": timestamp,
        "media_timestamp_utc": timestamp,
        "decode_received_at_utc": timestamp,
        "decode_received_at_epoch": media_time.timestamp(),
        "decode_latency_ms": 0.0,
        "ingested_at_epoch": int(media_time.timestamp()),
        "expires_at": int(media_time.timestamp()) + 7 * 24 * 60 * 60,
        "gps_location": {"latitude": 37.9156, "longitude": -122.3347},
        "camera_data": {"bifocal_metadata": {
            "bbox": {"x1": 10, "y1": 20, "x2": 100, "y2": 120},
            "world_position": {"uncertainty_meters": 0.2},
        }},
    }
    if association is not None:
        payload["identity_association"] = association
    return payload


def test_accepts_explicit_cross_camera_association():
    earlier = item("ch4", "2026-07-11T00:27:41.000Z")
    later = item("ch2", "2026-07-11T00:27:49.000Z", association={
        "method": "cross_camera_spatiotemporal_convnext",
        "previous_device_id": "cam-001-ch4",
        "appearance_similarity": 0.82,
        "distance_meters": 0.0,
    })
    report = MODULE.evaluate_cross_camera_tracks([earlier, later])
    assert report["gate_passed"]
    assert report["accepted_pairs"][0]["from_camera"] == "ch4"
    assert report["accepted_pairs"][0]["to_camera"] == "ch2"


def test_shared_object_id_without_association_is_diagnostic_only():
    report = MODULE.evaluate_cross_camera_tracks([
        item("ch4", "2026-07-11T00:27:41.000Z"),
        item("ch2", "2026-07-11T00:27:49.000Z"),
    ])
    assert not report["gate_passed"]
    assert report["diagnostic_pairs"][0]["association_reasons"] == [
        "identity_association_missing"
    ]


def test_rejects_untrusted_or_uncertain_vehicle_records():
    untrusted = item("ch4", "2026-07-11T00:27:41.000Z")
    untrusted["media_time_trusted"] = False
    uncertain = item("ch2", "2026-07-11T00:27:49.000Z")
    uncertain["camera_data"]["bifocal_metadata"]["world_position"]["uncertainty_meters"] = 3.0
    report = MODULE.evaluate_cross_camera_tracks([untrusted, uncertain])
    assert not report["gate_passed"]
    assert report["rejected_vehicle_records"] == 2
