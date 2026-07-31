"""Unit tests for observational calibration-pair capture guards."""

import importlib.util
import hashlib
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "capture_twin_calibration_pairs.py"
SPEC = importlib.util.spec_from_file_location("capture_twin_calibration_pairs", TOOL)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_base_urls_reject_credentials_queries_and_wrong_schemes():
    assert MODULE.normalized_base("http://127.0.0.1:8090/", {"http"}) == (
        "http://127.0.0.1:8090"
    )
    for value in (
        "http://user@example.test",
        "http://example.test?token=secret",
        "file:///tmp/feed",
    ):
        with pytest.raises(MODULE.CaptureError):
            MODULE.normalized_base(value, {"http", "https"})


def test_camera_health_requires_fresh_trusted_stream():
    payload = {"cameras": {"ch1": {
        "fresh": True,
        "state": "streaming",
        "media_time_trusted": True,
        "media_clock_status": "matched",
        "source_updated_at": "2026-07-11T02:22:11.000Z",
        "frame_count": 42,
        "decode_latency_ms": 1200.0,
    }}}
    assert MODULE.camera_health(payload, "ch1")["frame_count"] == 42
    payload["cameras"]["ch1"]["media_time_trusted"] = False
    with pytest.raises(MODULE.CaptureError, match="not trusted and fresh"):
        MODULE.camera_health(payload, "ch1")


def test_twin_metadata_is_camera_mode_clock_and_hash_bound():
    jpeg = b"jpeg-evidence"
    metadata = {
        "camera_id": "ch1",
        "mode": "live",
        "frame_count": 4,
        "carla_frame": 9,
        "sensor_timestamp": 12.5,
        "jpeg_sha256": hashlib.sha256(jpeg).hexdigest(),
    }
    assert MODULE.validate_twin_metadata(metadata, "ch1", jpeg) == (
        metadata["jpeg_sha256"]
    )
    for field, bad_value, message in (
        ("camera_id", "ch2", "wrong camera"),
        ("mode", "replay", "not in LIVE"),
        ("frame_count", True, "frame_count is invalid"),
        ("carla_frame", 0, "carla_frame is invalid"),
        ("sensor_timestamp", float("nan"), "sensor_timestamp is invalid"),
        ("jpeg_sha256", "0" * 64, "JPEG hash mismatch"),
    ):
        rejected = dict(metadata)
        rejected[field] = bad_value
        with pytest.raises(MODULE.CaptureError, match=message):
            MODULE.validate_twin_metadata(rejected, "ch1", jpeg)
