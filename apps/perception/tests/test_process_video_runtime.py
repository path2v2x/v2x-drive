import sys
import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import threading
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PERCEPTION_DIR))

import process_video  # noqa: E402
from live_capture import bounded_frame_identity  # noqa: E402
from process_video import (  # noqa: E402
    _BOUNDED_DIAGNOSTIC_FRAME_LIMIT,
    _BOUNDED_DIAGNOSTIC_STACK_BYTES,
    _BOUNDED_DIAGNOSTIC_THREAD_LIMIT,
    _COOPERATIVE_SHUTDOWN_CEILING_SECONDS,
    _COOPERATIVE_SHUTDOWN_MARGIN_SECONDS,
    DETECTION_TTL_SECONDS,
    _OUTER_SHUTDOWN_RESERVE_SECONDS,
    _emit_bounded_shutdown_diagnostics,
    _live_pipeline_shutdown_timeout_seconds,
    FrameBroadcaster,
    MultiCameraPipeline,
    VideoObjectDetector,
    attach_media_clock_metadata,
    assess_media_clock,
    camera_intrinsics_evidence,
    camera_localization_parameters,
    detector_config_fingerprint,
    load_cameras_config,
    records_ready_for_upload,
    snapshot_detector_model,
    vehicle_localization_acceptable,
)
from ffmpeg_capture import (  # noqa: E402
    NVDEC_CAPTURE_RELEASE_WAIT_RESERVE_SECONDS,
)


class FrameIdentityTests(unittest.TestCase):
    def test_sparse_identity_is_stable_for_copy_and_changes_for_content(self):
        frame = np.zeros((128, 192, 3), dtype=np.uint8)
        identity = bounded_frame_identity(frame)
        self.assertEqual(identity, bounded_frame_identity(frame.copy()))

        changed = frame.copy()
        changed[61:68, 93:100] = 255
        self.assertNotEqual(identity, bounded_frame_identity(changed))

    def test_detector_config_fingerprint_is_stable_and_configuration_bound(self):
        first = detector_config_fingerprint("a" * 64, 0.5)
        self.assertEqual(first, detector_config_fingerprint("a" * 64, 0.5))
        self.assertNotEqual(first, detector_config_fingerprint("a" * 64, 0.6))
        self.assertNotEqual(first, detector_config_fingerprint("b" * 64, 0.5))

    def test_detector_workers_load_only_one_content_addressed_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "mutable-model.pt"
            source.write_bytes(b"pinned-model-bytes")
            snapshot, digest = snapshot_detector_model(source, root / "snapshots")
            self.assertNotEqual(snapshot, source)
            self.assertEqual(
                digest, hashlib.sha256(b"pinned-model-bytes").hexdigest()
            )
            source.write_bytes(b"later-original-replacement")
            loaded = []

            class Model:
                names = {}

            with patch.object(
                process_video, "YOLO",
                side_effect=lambda path: (loaded.append(path), Model())[1],
            ):
                process_video.load_verified_detector_model(snapshot, digest)
                process_video.load_verified_detector_model(snapshot, digest)
            self.assertEqual(loaded, [str(snapshot), str(snapshot)])

    def test_detector_snapshot_replacement_during_load_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "model.pt"
            source.write_bytes(b"exact-model")
            snapshot, digest = snapshot_detector_model(source, root / "snapshots")

            def replace_snapshot(path):
                replacement = Path(path).with_suffix(".replacement")
                replacement.write_bytes(b"different-model")
                os.replace(replacement, path)
                return object()

            with patch.object(process_video, "YOLO", side_effect=replace_snapshot):
                with self.assertRaisesRegex(RuntimeError, "changed during load"):
                    process_video.load_verified_detector_model(snapshot, digest)


class WorldLocalizationUncertaintyTests(unittest.TestCase):
    @staticmethod
    def detector(calibration_uncertainty_m=0.25):
        detector = object.__new__(VideoObjectDetector)
        detector.K = np.array(
            [[1000.0, 0.0, 500.0], [0.0, 1000.0, 500.0], [0.0, 0.0, 1.0]]
        )
        detector.dist_coeffs = np.zeros(5)
        detector.camera_height = 7.0
        detector.fx = detector.fy = 1000.0
        detector.cx = detector.cy = 500.0
        detector.R = np.eye(3)
        detector.localization_pixel_sigma = 4.0
        detector.calibration_uncertainty_m = calibration_uncertainty_m
        return detector

    def test_world_uncertainty_uses_two_pixel_axes_and_calibration_error(self):
        world = self.detector().compute_world_coordinates(600.0, 900.0)
        self.assertIsNotNone(world)
        self.assertTrue(math.isfinite(world["uncertainty_meters"]))
        components = world["uncertainty_components"]
        self.assertEqual(components["pixel_sigma"], 4.0)
        self.assertGreater(components["pixel_meters"], 0.0)
        self.assertEqual(components["calibration_meters"], 0.25)
        self.assertGreaterEqual(world["uncertainty_meters"], 0.25)

    def test_missing_calibration_or_horizon_geometry_is_infinite(self):
        missing = self.detector(float("inf")).compute_world_coordinates(600.0, 900.0)
        horizon = self.detector().compute_world_coordinates(500.0, 501.0)
        self.assertIsNone(missing["uncertainty_meters"])
        self.assertIsNone(missing["uncertainty_components"]["calibration_meters"])
        self.assertIsNone(horizon["uncertainty_meters"])
        self.assertIsNone(horizon["uncertainty_components"]["pixel_meters"])

    def test_missing_or_unbounded_camera_calibration_blocks_startup(self):
        with self.assertRaisesRegex(ValueError, "no measured localization"):
            camera_localization_parameters({"id": "ch1"})
        with self.assertRaisesRegex(ValueError, "invalid"):
            camera_localization_parameters({
                "id": "ch1",
                "localization": {
                    "pixel_sigma": 4.0,
                    "calibration_uncertainty_m": 2.01,
                },
            })
        self.assertEqual(
            camera_localization_parameters({
                "id": "ch1",
                "localization": {
                    "pixel_sigma": 4.0,
                    "calibration_uncertainty_m": 0.75,
                },
            }),
            (4.0, 0.75),
        )

    def test_camera_config_is_mandatory_unless_legacy_dev_flag_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing.json")
            with patch.dict(os.environ, {"V2X_CAMERAS_JSON": missing}, clear=False):
                os.environ.pop("V2X_ALLOW_LEGACY_CAMERA_CONFIG", None)
                with self.assertRaisesRegex(RuntimeError, "required cameras config"):
                    load_cameras_config()
            with patch.dict(
                os.environ,
                {
                    "V2X_CAMERAS_JSON": missing,
                    "V2X_ALLOW_LEGACY_CAMERA_CONFIG": "true",
                },
                clear=False,
            ):
                self.assertIsNone(load_cameras_config())

    def test_measured_intrinsics_are_required_and_return_distortion(self):
        camera = {
            "id": "ch1",
            "intrinsics": {
                "fx": 1000.0,
                "fy": 1001.0,
                "cx": 500.0,
                "cy": 400.0,
                "width": 1000,
                "height": 800,
            },
        }
        with self.assertRaisesRegex(ValueError, "no measured intrinsics"):
            camera_intrinsics_evidence(camera)
        hashes = [f"{index:064x}" for index in range(1, 11)]
        camera["intrinsics_calibration"] = {
            "method": "charuco",
            "artifact_sha256": "a" * 64,
            "source_images_sha256": hashes,
            "image_count": 10,
            "rms_reprojection_error_px": 0.5,
            "resolution": [1000, 800],
            "camera_matrix": [
                [1000.0, 0.0, 500.0],
                [0.0, 1001.0, 400.0],
                [0.0, 0.0, 1.0],
            ],
            "distortion": {
                "k1": -0.1,
                "k2": 0.01,
                "p1": 0.001,
                "p2": -0.002,
                "k3": 0.0,
            },
        }
        distortion = camera_intrinsics_evidence(camera)
        np.testing.assert_allclose(
            distortion, [-0.1, 0.01, 0.001, -0.002, 0.0]
        )
        with self.assertRaisesRegex(ValueError, "artifact path"):
            camera_intrinsics_evidence(
                camera,
                evidence_root=Path("/tmp"),
                require_artifacts=True,
            )

    def test_emits_non_circular_raw_observation_provenance(self):
        detector = self.detector()
        detector.origin_lat = 37.9
        detector.origin_lon = -122.3
        detector.heading_deg = 0.0
        detector.device_id = "cam-001-ch1"
        detector.city = "Richmond"
        detector.state = "CA"
        detector.country = "USA"
        detector.image_width = 1000
        detector.image_height = 800
        detector.cameras_json_sha256 = "a" * 64
        detector.camera_config_sha256 = "b" * 64
        detector.detector_model_sha256 = "c" * 64
        detector.detector_config_sha256 = "d" * 64
        record = detector.compute_3d_detections(
            [
                {
                    "center": {"x": 600.0, "y": 700.0},
                    "bbox": {
                        "x1": 550.0,
                        "y1": 600.0,
                        "x2": 650.0,
                        "y2": 790.0,
                    },
                    "class_name": "car",
                    "confidence": 0.9,
                    "frame": 5,
                    "track_id": 7,
                }
            ],
            "2026-07-11T03:32:21.022Z",
            1783740741,
        )[0]
        raw = record["raw_observation"]
        self.assertEqual(raw["ground_contact"]["pixel"], [600.0, 790.0])
        self.assertFalse(raw["ground_contact"]["reviewed"])
        self.assertTrue(
            raw["optimizer_contract"]["gps_location_is_derived_baseline"]
        )
        self.assertFalse(raw["optimizer_contract"]["acceptance_eligible"])
        self.assertEqual(raw["fingerprints"]["camera_config_sha256"], "b" * 64)
        self.assertEqual(raw["fingerprints"]["detector_config_sha256"], "d" * 64)


class FrameBroadcasterTests(unittest.TestCase):
    def setUp(self):
        self.broadcaster = FrameBroadcaster(["ch1", "ch2"], stale_seconds=1.0)
        self.frame = np.zeros((8, 8, 3), dtype=np.uint8)

    def test_health_requires_a_fresh_real_frame_from_every_camera(self):
        self.assertFalse(self.broadcaster.snapshot_health()["ready"])

        self.broadcaster.publish("ch1", self.frame, "2026-07-10T00:00:00.000Z")
        self.assertFalse(self.broadcaster.snapshot_health()["ready"])

        self.broadcaster.publish("ch2", self.frame, "2026-07-10T00:00:00.000Z")
        self.assertFalse(self.broadcaster.snapshot_health()["ready"])

        self.broadcaster.publish_detections("ch1", [])
        self.assertFalse(self.broadcaster.snapshot_health()["ready"])
        self.broadcaster.publish_detections("ch2", [])
        health = self.broadcaster.snapshot_health()
        self.assertTrue(health["ready"])
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["cameras"]["ch1"]["inference_fresh"])
        self.assertEqual(
            health["cameras"]["ch1"]["inference_frame_count"], 1
        )
        self.assertEqual(health["decoder_topology"], {
            "capacity": 2,
            "in_use": 0,
            "urgent_waiters": 0,
            "urgent_windows": 0,
            "proactive_preparations": 0,
            "terminal_recoveries": 0,
            "proactive_preparation_snapshot": "ok",
            "proactive_preparation_states": {
                "sampled_count": 0,
                "stage_counts": {},
                "claimed_count": 0,
                "claimed_lock_busy_count": 0,
                "done_count": 0,
                "discarded_count": 0,
                "quiesced_count": 0,
            },
            "terminal_cleanups": 0,
            "terminal_cleanup_snapshot": "ok",
            "terminal_cleanup_sampled_count": 0,
            "terminal_cleanup_states": {},
            "terminal_cleanup_failures": 0,
        })

    def test_health_fails_closed_when_inference_stalls_behind_capture(self):
        broadcaster = FrameBroadcaster(
            ["ch1"], stale_seconds=15.0, inference_stale_seconds=10.0
        )
        broadcaster.publish(
            "ch1", self.frame, source_monotonic=100.0
        )
        broadcaster.publish_detections(
            "ch1", [], inference_monotonic=100.0
        )
        self.assertTrue(
            broadcaster.snapshot_health(now_monotonic=109.9)["ready"]
        )

        broadcaster.publish(
            "ch1", self.frame, source_monotonic=110.1
        )
        health = broadcaster.snapshot_health(now_monotonic=110.1)
        self.assertTrue(health["cameras"]["ch1"]["fresh"])
        self.assertFalse(health["cameras"]["ch1"]["inference_fresh"])
        self.assertEqual(
            health["cameras"]["ch1"]["inference_age_seconds"], 10.1
        )
        self.assertFalse(health["ready"])

    def test_stale_and_reconnecting_states_are_visible(self):
        self.broadcaster.publish("ch1", self.frame)
        last_frame = self.broadcaster.camera_health["ch1"]["last_frame_monotonic"]
        stale = self.broadcaster.snapshot_health(now_monotonic=last_frame + 1.1)
        self.assertEqual(stale["cameras"]["ch1"]["state"], "stale")
        self.assertFalse(stale["cameras"]["ch1"]["fresh"])

        self.broadcaster.mark_reconnecting("ch1", "frame read failed", 3)
        reconnecting = self.broadcaster.snapshot_health()
        self.assertEqual(reconnecting["cameras"]["ch1"]["state"], "reconnecting")
        self.assertEqual(reconnecting["cameras"]["ch1"]["reconnect_attempts"], 3)

    def test_last_real_frame_does_not_erase_newer_reconnect_state(self):
        self.broadcaster.mark_reconnecting("ch1", "frame read failed", 1)
        self.broadcaster.publish(
            "ch1",
            self.frame,
            "2026-07-10T00:00:00.000Z",
            source_monotonic=100.0,
        )
        health = self.broadcaster.snapshot_health(now_monotonic=100.1)
        self.assertEqual(health["cameras"]["ch1"]["state"], "reconnecting")
        self.assertTrue(health["cameras"]["ch1"]["fresh"])
        self.assertFalse(health["ready"])
        frame, count = self.broadcaster.wait_for_frame("ch1", -1, timeout=0.0)
        self.assertIsNone(frame)
        self.assertEqual(count, -1)

    def test_terminal_failover_telemetry_is_explicit_and_cumulative(self):
        self.broadcaster.mark_terminal_failover(
            "ch1", "succeeded", 4.25, "same_session_restart", "ready",
            "recent_exact_sequence",
        )
        self.broadcaster.mark_terminal_failover(
            "ch1", "failed", 8.0, "fresh_session_replacement", "capture_open",
            "exact_fragment_match",
        )
        health = self.broadcaster.snapshot_health()["cameras"]["ch1"]
        self.assertEqual(health["terminal_failover_attempts"], 2)
        self.assertEqual(health["terminal_failover_successes"], 1)
        self.assertEqual(health["terminal_failover_failures"], 1)
        self.assertEqual(health["terminal_failover_last_outcome"], "failed")
        self.assertEqual(
            health["terminal_failover_last_method"],
            "fresh_session_replacement",
        )
        self.assertEqual(
            health["terminal_failover_last_duration_seconds"], 8.0
        )
        self.assertEqual(
            health["terminal_failover_last_stage"], "capture_open"
        )
        self.assertEqual(
            health["terminal_failover_last_evidence"], "exact_fragment_match"
        )
        self.broadcaster.mark_terminal_failover(
            "ch2", "succeeded", 2.0, "same_session_restart", "ready",
            "exact_fragment_sequence",
        )
        self.assertEqual(
            self.broadcaster.snapshot_health()["cameras"]["ch2"][
                "terminal_failover_last_evidence"
            ],
            "exact_fragment_sequence",
        )
        self.broadcaster.mark_terminal_failover(
            "ch1", "succeeded", 0.4, "same_session_restart", "ready",
            "exact_same_session_pts",
        )
        self.assertEqual(
            self.broadcaster.snapshot_health()["cameras"]["ch1"][
                "terminal_failover_last_evidence"
            ],
            "exact_same_session_pts",
        )
        self.broadcaster.mark_terminal_failover(
            "ch2", "failed", 8.0, "same_session_restart",
            "active_clock_cleanup",
        )
        self.broadcaster.mark_terminal_failover(
            "ch2", "failed", 8.0, "same_session_restart",
            "deadline_exceeded:clock_resolution",
        )
        self.assertEqual(
            self.broadcaster.snapshot_health()["cameras"]["ch2"][
                "terminal_failover_last_stage"
            ],
            "deadline_exceeded:clock_resolution",
        )

        with self.assertRaisesRegex(ValueError, "outcome"):
            self.broadcaster.mark_terminal_failover(
                "ch1", "unknown", 1.0, "same_session_restart"
            )
        with self.assertRaisesRegex(ValueError, "duration"):
            self.broadcaster.mark_terminal_failover(
                "ch1", "failed", -1.0, "same_session_restart"
            )
        with self.assertRaisesRegex(ValueError, "method"):
            self.broadcaster.mark_terminal_failover(
                "ch1", "failed", 1.0, "unknown"
            )
        with self.assertRaisesRegex(ValueError, "stage"):
            self.broadcaster.mark_terminal_failover(
                "ch1", "failed", 1.0, "same_session_restart", "signed-url"
            )
        with self.assertRaisesRegex(ValueError, "evidence"):
            self.broadcaster.mark_terminal_failover(
                "ch1", "failed", 1.0, "same_session_restart", "failed",
                "receipt_time_guess",
            )

    def test_health_exposes_secret_free_anchor_match_frame_count(self):
        self.broadcaster.publish(
            "ch1",
            self.frame,
            media_clock_health={
                "status": "matched",
                "trusted": True,
                "decode_latency_ms": 250.0,
                "media_clock": {
                    "source": "hls_ext_x_program_date_time",
                    "anchor_match_frame_count": 3,
                    "signed_url": (
                        "https://example.invalid/?token=literal-secret"
                    ),
                },
                "signed_url": "https://outer.invalid/?token=literal-secret",
            },
        )
        camera = self.broadcaster.snapshot_health()["cameras"]["ch1"]
        self.assertEqual(camera["anchor_match_frame_count"], 3)
        rendered = repr(camera).lower()
        self.assertNotIn("url", rendered)
        self.assertNotIn("literal-secret", rendered)
        self.assertNotIn("https://", rendered)

        self.broadcaster.publish(
            "ch2",
            self.frame,
            media_clock_health={
                "status": "matched",
                "trusted": True,
                "media_clock": {"anchor_match_frame_count": 2},
            },
        )
        self.assertIsNone(
            self.broadcaster.snapshot_health()["cameras"]["ch2"][
                "anchor_match_frame_count"
            ]
        )

        self.broadcaster.publish(
            "ch2",
            self.frame,
            media_clock_health={
                "status": "matched",
                "trusted": True,
                "media_clock": {
                    "evidence_method": "exact_same_session_pts",
                    "signed_url": "https://invalid/?token=literal-secret",
                },
            },
        )
        transport_camera = self.broadcaster.snapshot_health()["cameras"][
            "ch2"
        ]
        self.assertEqual(
            transport_camera["media_clock_evidence_method"],
            "exact_same_session_pts",
        )
        self.assertIsNone(transport_camera["anchor_match_frame_count"])
        self.assertNotIn("literal-secret", repr(transport_camera))

    def test_terminal_failover_stage_allowlist_is_finite(self):
        base_stages = (
            None,
            "source",
            "preparation_slot",
            "clock_source",
            "decoder_slot",
            "capture_open",
            "first_frame",
            "capture_position",
            "transport_clock_validation",
            "recent_exact_anchor",
            "clock_resolution",
            "clock_validation",
            "ready",
            "failed",
            "old_capture_release",
            "active_clock_cleanup",
            "prior_terminal_cleanup",
            "proactive_quiescence",
            "preparation_deadline",
            "proactive_cleanup",
            "result_ownership",
            "candidate_cleanup",
        )
        for stage in base_stages:
            with self.subTest(stage=stage):
                self.broadcaster.mark_terminal_failover(
                    "ch1", "failed", 1.0, "same_session_restart", stage
                )
        for stage in (*base_stages[1:], "handover"):
            deadline_stage = f"deadline_exceeded:{stage}"
            with self.subTest(stage=deadline_stage):
                self.broadcaster.mark_terminal_failover(
                    "ch1", "failed", 1.0, "same_session_restart",
                    deadline_stage,
                )

        rejected = (
            "handover",
            "deadline_exceeded:",
            "deadline_exceeded:deadline_exceeded:ready",
            "https://example.invalid/?token=literal-secret",
            "ready\nliteral-secret",
            7,
            ["ready"],
        )
        for stage in rejected:
            with self.subTest(stage=stage):
                with self.assertRaisesRegex(ValueError, "stage"):
                    self.broadcaster.mark_terminal_failover(
                        "ch1", "failed", 1.0, "same_session_restart", stage
                    )

    def test_health_age_uses_capture_time_not_inference_completion_time(self):
        self.broadcaster.mark_connected("ch1")
        self.broadcaster.publish(
            "ch1",
            self.frame,
            "2026-07-10T00:00:00.000Z",
            source_monotonic=100.0,
        )
        health = self.broadcaster.snapshot_health(now_monotonic=101.1)
        self.assertEqual(health["cameras"]["ch1"]["state"], "stale")
        self.assertFalse(health["cameras"]["ch1"]["fresh"])

    def test_public_health_never_exposes_signed_source_errors(self):
        self.broadcaster.mark_reconnecting(
            "ch1",
            "failed https://video.example/live.m3u8?SessionToken=secret-value",
            2,
        )
        health = self.broadcaster.snapshot_health()
        last_error = health["cameras"]["ch1"]["last_error"]
        self.assertIn("details redacted", last_error)
        self.assertNotIn("https://", last_error)
        self.assertNotIn("video.example", last_error)
        self.assertNotIn("SessionToken", last_error)
        self.assertNotIn("secret-value", last_error)

        # Snapshot sanitization is a second boundary even if legacy/internal
        # state somehow contains an unsanitized value.
        self.broadcaster.camera_health["ch1"]["last_error"] = (
            "https://other.example/hls?token=another-secret"
        )
        last_error = self.broadcaster.snapshot_health()["cameras"]["ch1"][
            "last_error"
        ]
        self.assertNotIn("other.example", last_error)
        self.assertNotIn("another-secret", last_error)

    def test_transport_diagnostic_is_finite_and_secret_free(self):
        self.broadcaster.mark_transport_diagnostic(
            "ch1", "position_after_window"
        )
        camera = self.broadcaster.snapshot_health()["cameras"]["ch1"]
        self.assertEqual(
            camera["transport_clock_diagnostic"],
            "position_after_window",
        )
        with self.assertRaisesRegex(ValueError, "diagnostic"):
            self.broadcaster.mark_transport_diagnostic(
                "ch1", "https://example.invalid/?SessionToken=secret"
            )
        rendered = repr(
            self.broadcaster.snapshot_health()["cameras"]["ch1"]
        )
        self.assertNotIn("SessionToken", rendered)
        self.assertNotIn("https://", rendered)

    def test_latest_detection_exposes_media_clock_for_correlation(self):
        media_clock = {
            "source": "hls_ext_x_program_date_time",
            "anchor_program_date_time_utc": "2026-07-10T03:57:23.138Z",
            "position_milliseconds": 250.5,
        }
        self.broadcaster.publish_detections(
            "ch1",
            [{
                "timestamp_utc": "2026-07-10T03:57:27.000Z",
                "media_timestamp_utc": "2026-07-10T03:57:23.388Z",
                "media_clock": media_clock,
            }],
        )
        detection = self.broadcaster.snapshot_detections()["cameras"]["ch1"][
            "detections"
        ][0]
        self.assertEqual(
            detection["media_timestamp_utc"],
            "2026-07-10T03:57:23.388Z",
        )
        self.assertEqual(detection["media_clock"], media_clock)


class MediaClockPersistenceTests(unittest.TestCase):
    def test_same_session_pts_evidence_is_preserved_without_match_count(self):
        record = {
            "event_id": "transport-1",
            "timestamp_utc": "2026-07-10T03:57:27.000Z",
            "ingested_at_epoch": 1_783_655_847.0,
        }
        attach_media_clock_metadata(
            [record],
            {
                "media_timestamp_utc": "2026-07-10T03:57:23.388Z",
                "media_clock": {
                    "source": "hls_ext_x_program_date_time",
                    "schema_version": 1,
                    "session_id": "session-frag-123",
                    "anchor_program_date_time_utc": (
                        "2026-07-10T03:57:23.138Z"
                    ),
                    "position_milliseconds": 250.0,
                    "capture_position_milliseconds": 2454.0,
                    "anchor_capture_position_milliseconds": 2454.0,
                    "anchor_fragment_frame_offset_milliseconds": 250.0,
                    "anchor_fragment_id": "frag-transport-1",
                    "anchor_media_sequence": 7,
                    "segment_duration_seconds": 2.0,
                    "evidence_method": "exact_same_session_pts",
                    "source_pts": 220860,
                    "source_time_base_numerator": 1,
                    "source_time_base_denominator": 90000,
                    "fragment_sample_index": 8,
                    "anchor_match_frame_count": 99,
                    "signed_url": "https://example.invalid/?token=secret",
                },
            },
        )

        clock = record["media_clock"]
        self.assertEqual(clock["evidence_method"], "exact_same_session_pts")
        self.assertEqual(clock["source_pts"], 220860)
        self.assertEqual(clock["source_time_base_denominator"], 90000)
        self.assertNotIn("anchor_match_frame_count", clock)
        self.assertNotIn("signed_url", clock)

        invalid = assess_media_clock(
            {
                "media_timestamp_utc": "2026-07-10T03:57:23.388Z",
                "media_clock": {
                    "source": "hls_ext_x_program_date_time",
                    "schema_version": 1,
                    "session_id": "session-transport-1",
                    "anchor_program_date_time_utc": (
                        "2026-07-10T03:57:23.138Z"
                    ),
                    "position_milliseconds": 250.0,
                    "evidence_method": "exact_same_session_pts",
                    "source_pts": 220860,
                    "source_time_base_numerator": 1,
                    # Denominator and sample index are deliberately missing.
                },
            },
            1_783_655_847.0,
        )
        self.assertFalse(invalid["trusted"])
        self.assertEqual(
            invalid["status"], "invalid_transport_provenance"
        )

        inconsistent = copy.deepcopy(record["media_clock"])
        inconsistent["source_pts"] = 999_999_999
        inconsistent_assessment = assess_media_clock(
            {
                "media_timestamp_utc": record["media_timestamp_utc"],
                "media_clock": inconsistent,
            },
            1_783_655_847.0,
        )
        self.assertFalse(inconsistent_assessment["trusted"])
        self.assertEqual(
            inconsistent_assessment["status"],
            "inconsistent_transport_provenance",
        )

    def test_media_time_becomes_replay_index_and_receipt_is_preserved(self):
        record = {
            "event_id": "event-1",
            "timestamp_utc": "2026-07-10T03:57:27.000Z",
            "ingested_at_epoch": 1_783_655_847.0,
        }
        attach_media_clock_metadata(
            [record],
            {
                "media_timestamp_utc": "2026-07-10T03:57:23.388Z",
                "media_clock": {
                    "source": "hls_ext_x_program_date_time",
                    "schema_version": 1,
                    "session_id": "session-inconsistent-anchor",
                    "anchor_program_date_time_utc": "2026-07-10T03:57:23.138Z",
                    "anchor_fragment_id": "frag-123",
                    "position_milliseconds": 250.5,
                    "anchor_match_frame_count": 3,
                    "signed_url": "https://example.invalid/?SessionToken=secret",
                },
            },
        )

        self.assertEqual(record["timestamp_utc"], "2026-07-10T03:57:23.388Z")
        self.assertEqual(
            record["decode_received_at_utc"], "2026-07-10T03:57:27.000Z"
        )
        self.assertEqual(record["decode_received_at_epoch"], 1_783_655_847.0)
        self.assertEqual(
            record["media_timestamp_utc"], "2026-07-10T03:57:23.388Z"
        )
        self.assertEqual(
            record["ts_event"], "2026-07-10T03:57:23.388Z#event-1"
        )
        self.assertEqual(record["media_clock_status"], "matched")
        self.assertEqual(
            record["expires_at"],
            1_783_655_843 + DETECTION_TTL_SECONDS,
        )
        self.assertEqual(
            record["media_clock"]["anchor_match_frame_count"], 3
        )
        self.assertNotIn("signed_url", record["media_clock"])

    def test_missing_exact_match_is_marked_unavailable(self):
        record = {"timestamp_utc": "2026-07-10T03:57:27.000Z"}
        attach_media_clock_metadata([record], None)
        self.assertEqual(record["media_clock_status"], "unavailable")
        self.assertNotIn("media_timestamp_utc", record)

    def test_wrong_schema_or_implausible_latency_is_not_trusted(self):
        base = {
            "media_timestamp_utc": "2026-07-10T03:57:23.388Z",
            "media_clock": {
                "source": "hls_ext_x_program_date_time",
                "schema_version": 1,
                "anchor_program_date_time_utc": "2026-07-10T03:57:23.138Z",
                "position_milliseconds": 250.0,
            },
        }
        wrong_schema = copy.deepcopy(base)
        wrong_schema["media_clock"]["schema_version"] = 2
        self.assertEqual(
            assess_media_clock(wrong_schema, 1_783_655_847.0)["status"],
            "unsupported_schema",
        )
        implausible = assess_media_clock(
            base,
            1_783_655_847.0 + 121.0,
        )
        self.assertFalse(implausible["trusted"])
        self.assertEqual(implausible["status"], "latency_out_of_bounds")

    def test_anchor_position_must_reconstruct_media_timestamp(self):
        assessment = assess_media_clock(
            {
                "media_timestamp_utc": "2026-07-10T03:57:23.388Z",
                "media_clock": {
                    "source": "hls_ext_x_program_date_time",
                    "schema_version": 1,
                    "session_id": "session-inconsistent-provenance",
                    "anchor_program_date_time_utc": "2026-07-10T03:57:20.000Z",
                    "position_milliseconds": 100.0,
                },
            },
            1_783_655_847.0,
        )
        self.assertFalse(assessment["trusted"])
        self.assertEqual(assessment["status"], "inconsistent_provenance")

    def test_live_uploads_fail_closed_without_trusted_media_schema(self):
        trusted = {
            "event_id": "trusted",
            "timestamp_schema_version": 2,
            "media_time_trusted": True,
        }
        unavailable = {
            "event_id": "unavailable",
            "timestamp_schema_version": 2,
            "media_time_trusted": False,
        }
        legacy = {"event_id": "legacy"}
        records = [trusted, unavailable, legacy]
        self.assertEqual(records_ready_for_upload(records, True), [trusted])
        self.assertEqual(records_ready_for_upload(records, False), records)

    def test_live_vehicle_upload_requires_bounded_localization_uncertainty(self):
        trusted = {
            "object_type": "car",
            "timestamp_schema_version": 2,
            "media_time_trusted": True,
            "camera_data": {
                "bifocal_metadata": {
                    "world_position": {"uncertainty_meters": 0.75}
                }
            },
        }
        self.assertTrue(vehicle_localization_acceptable(trusted))
        self.assertEqual(records_ready_for_upload([trusted], True), [trusted])
        for value in (None, float("inf"), 2.01):
            candidate = copy.deepcopy(trusted)
            candidate["camera_data"]["bifocal_metadata"]["world_position"][
                "uncertainty_meters"
            ] = value
            self.assertFalse(vehicle_localization_acceptable(candidate))
            self.assertEqual(records_ready_for_upload([candidate], True), [])


class RunScopedIdentityTests(unittest.TestCase):
    @staticmethod
    def pipeline(run_id, cross_camera_vehicle_association=False):
        pipeline = object.__new__(MultiCameraPipeline)
        pipeline.global_tracks = {}
        pipeline.local_to_global = {}
        pipeline.next_global_id = 0
        pipeline.perception_run_id = run_id
        pipeline.perception_run_prefix = run_id.replace("-", "")[:8]
        pipeline.cross_camera_vehicle_association = bool(
            cross_camera_vehicle_association
        )
        return pipeline

    @staticmethod
    def detection(
        camera="ch1",
        confidence=0.8,
        media_timestamp="2026-07-10T00:00:00.000Z",
    ):
        return {
            "event_id": f"event-{camera}",
            "object_id": f"car_{camera}_7",
            "object_type": "car",
            "confidence_score": confidence,
            "gps_location": {"latitude": 37.0, "longitude": -122.0},
            "device_id": camera,
            "track_id": 7,
            "embedding": None,
            "timestamp_utc": media_timestamp,
            "media_timestamp_utc": media_timestamp,
            "timestamp_schema_version": 2,
            "media_time_trusted": True,
            "media_clock_status": "matched",
            "media_clock": {
                "source": "hls_ext_x_program_date_time",
                "schema_version": 1,
            },
            "camera_data": {
                "bifocal_metadata": {
                    "bbox": {},
                    "world_position": {"uncertainty_meters": 0.25},
                }
            },
        }

    def test_same_local_track_in_different_runs_gets_different_global_id(self):
        run_one = "123e4567-e89b-12d3-a456-426614174000"
        run_two = "abcdef01-e89b-12d3-a456-426614174000"
        first = self.pipeline(run_one).deduplicate(
            [self.detection()], 1_000.0
        )[0]
        second = self.pipeline(run_two).deduplicate(
            [self.detection()], 1_000.0
        )[0]

        self.assertEqual(first["object_id"], "global_car_123e4567_1")
        self.assertEqual(second["object_id"], "global_car_abcdef01_1")
        self.assertNotEqual(first["object_id"], second["object_id"])
        self.assertEqual(first["perception_run_id"], run_one)
        self.assertEqual(first["track_id"], 7)

    def test_vehicle_embedding_cache_never_aliases_missing_track_ids(self):
        pipeline = self.pipeline("123e4567-e89b-12d3-a456-426614174000")
        pipeline.vehicle_embedding_cache = {}
        pipeline.vehicle_extractor = Mock()
        pipeline.vehicle_extractor.extract.side_effect = [
            np.array([1.0, 0.0]),
            np.array([0.0, 1.0]),
        ]
        detection = self.detection()
        detection["track_id"] = None
        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        first = pipeline._vehicle_embedding(frame, detection, 1)
        second = pipeline._vehicle_embedding(frame, detection, 2)
        self.assertFalse(np.array_equal(first, second))
        self.assertEqual(pipeline.vehicle_embedding_cache, {})

    def test_cross_camera_winner_keeps_one_consistent_media_observation(self):
        run_id = "123e4567-e89b-12d3-a456-426614174000"
        pipeline = self.pipeline(run_id, True)
        older = self.detection("ch1", 0.7, "2026-07-10T00:00:00.000Z")
        winner = self.detection("ch2", 0.9, "2026-07-10T00:00:00.100Z")
        older["embedding"] = np.array([1.0, 0.0])
        winner["embedding"] = np.array([0.95, 0.05])
        winner["embedding"] /= np.linalg.norm(winner["embedding"])
        result = pipeline.deduplicate(
            [copy.deepcopy(older), copy.deepcopy(winner)], 1_000.0
        )[0]

        self.assertEqual(result["device_id"], "ch2")
        self.assertEqual(result["timestamp_utc"], "2026-07-10T00:00:00.100Z")
        self.assertEqual(
            result["media_timestamp_utc"], "2026-07-10T00:00:00.100Z"
        )
        self.assertEqual(result["event_id"], "event-ch2")
        self.assertEqual(
            result["cross_camera_dedup"]["method"],
            "spatiotemporal_convnext",
        )
        self.assertGreaterEqual(
            result["cross_camera_dedup"]["appearance_similarity"], 0.60
        )

    def test_close_cross_camera_vehicles_require_appearance_evidence(self):
        pipeline = self.pipeline("123e4567-e89b-12d3-a456-426614174000", True)
        first = self.detection("ch1", 0.9, "2026-07-10T00:00:00.000Z")
        second = self.detection("ch2", 0.9, "2026-07-10T00:00:00.100Z")
        result = pipeline.deduplicate([copy.deepcopy(first), copy.deepcopy(second)], 1_000.0)
        self.assertEqual(len(result), 2)

        first["embedding"] = np.array([1.0, 0.0])
        second["embedding"] = np.array([0.0, 1.0])
        result = self.pipeline(
            "123e4567-e89b-12d3-a456-426614174000", True
        ).deduplicate([first, second], 1_000.0)
        self.assertEqual(len(result), 2)

    def test_same_camera_new_track_cannot_inherit_existing_global_id(self):
        run_id = "123e4567-e89b-12d3-a456-426614174000"
        pipeline = self.pipeline(run_id)
        first = pipeline.deduplicate(
            [self.detection("ch2")], 1_000.0
        )[0]
        replacement = self.detection("ch2")
        replacement["track_id"] = 53
        second = pipeline.deduplicate([replacement], 1_001.0)[0]

        self.assertEqual(first["object_id"], "global_car_123e4567_1")
        self.assertEqual(second["object_id"], "global_car_123e4567_2")
        self.assertNotEqual(first["object_id"], second["object_id"])

    def test_same_camera_same_track_keeps_existing_global_id(self):
        run_id = "123e4567-e89b-12d3-a456-426614174000"
        pipeline = self.pipeline(run_id)
        first = pipeline.deduplicate(
            [self.detection("ch2")], 1_000.0
        )[0]
        second = pipeline.deduplicate(
            [self.detection("ch2")], 1_001.0
        )[0]

        self.assertEqual(first["object_id"], second["object_id"])

    def test_cross_camera_vehicle_association_is_fail_closed_by_default(self):
        run_id = "123e4567-e89b-12d3-a456-426614174000"
        pipeline = self.pipeline(run_id)
        ch1 = self.detection("ch1", 0.9, "same-time")
        ch2 = self.detection("ch2", 0.8, "same-time")

        result = pipeline.deduplicate(
            [copy.deepcopy(ch1), copy.deepcopy(ch2)], 1_000.0
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(
            {item["object_id"] for item in result},
            {"global_car_123e4567_1", "global_car_123e4567_2"},
        )

    def test_distinct_vehicles_seven_meters_apart_are_not_merged(self):
        pipeline = self.pipeline("123e4567-e89b-12d3-a456-426614174000", True)
        first = self.detection("ch1", 0.9, "2026-07-10T00:00:00.000Z")
        second = self.detection("ch2", 0.9, "2026-07-10T00:00:00.100Z")
        second["gps_location"]["latitude"] += 7.0 / 111_320.0
        result = pipeline.deduplicate([first, second], 1_000.0)
        self.assertEqual(len(result), 2)
        self.assertNotEqual(result[0]["object_id"], result[1]["object_id"])

    def test_equally_plausible_adjacent_vehicles_fail_identity_closed(self):
        first = self.detection("ch1")
        second = self.detection("ch2")
        third = self.detection("ch3")
        second["gps_location"]["latitude"] += 4.0 / 111_320.0
        third["gps_location"]["latitude"] += 2.0 / 111_320.0
        for detection in (first, second, third):
            detection["embedding"] = np.array([1.0, 0.0])
        result = self.pipeline(
            "123e4567-e89b-12d3-a456-426614174000", True
        ).deduplicate([first, second, third], 1_000.0)
        self.assertEqual(len(result), 3)
        ambiguous = next(item for item in result if item["device_id"] == "ch3")
        self.assertEqual(
            ambiguous["identity_ambiguity"]["method"],
            "ambiguous_spatiotemporal_convnext",
        )
        self.assertEqual(
            ambiguous["identity_association"]["method"], "new_track"
        )

    def test_third_candidate_with_conflicting_appearance_fails_closed(self):
        pipeline = self.pipeline("123e4567-e89b-12d3-a456-426614174000")
        candidates = [
            {
                "distance_meters": 0.3,
                "appearance_similarity": 0.62,
                "record": {"device_id": "ch1", "object_id": "car-1"},
            },
            {
                "distance_meters": 1.9,
                "appearance_similarity": 0.60,
                "record": {"device_id": "ch2", "object_id": "car-2"},
            },
            {
                "distance_meters": 2.0,
                "appearance_similarity": 0.95,
                "record": {"device_id": "ch3", "object_id": "car-3"},
            },
        ]

        selected, ambiguity = pipeline._select_unambiguous_vehicle_candidate(
            candidates
        )

        self.assertIsNone(selected)
        self.assertEqual(
            ambiguity["method"], "ambiguous_spatiotemporal_convnext"
        )
        self.assertEqual(len(ambiguity["candidates"]), 3)

    def test_ambiguous_temporal_reattachment_starts_distinct_track(self):
        pipeline = self.pipeline("123e4567-e89b-12d3-a456-426614174000", True)
        first = self.detection("ch1")
        second = self.detection("ch2")
        second["gps_location"]["latitude"] += 4.0 / 111_320.0
        first["embedding"] = second["embedding"] = np.array([1.0, 0.0])
        initial = pipeline.deduplicate([first, second], 1_000.0)
        self.assertEqual(len({item["object_id"] for item in initial}), 2)

        third = self.detection(
            "ch3", media_timestamp="2026-07-10T00:00:01.000Z"
        )
        third["track_id"] = 99
        third["gps_location"]["latitude"] += 2.0 / 111_320.0
        third["embedding"] = np.array([1.0, 0.0])
        result = pipeline.deduplicate([third], 1_001.0)[0]
        self.assertNotIn(result["object_id"], {
            item["object_id"] for item in initial
        })
        self.assertEqual(
            result["identity_ambiguity"]["method"],
            "ambiguous_track_reattachment",
        )

    def test_cross_camera_observations_outside_media_window_are_not_merged(self):
        pipeline = self.pipeline("123e4567-e89b-12d3-a456-426614174000", True)
        first = self.detection("ch1", 0.9, "2026-07-10T00:00:00.000Z")
        second = self.detection("ch2", 0.9, "2026-07-10T00:00:04.000Z")
        result = pipeline.deduplicate([first, second], 1_000.0)
        self.assertEqual(len(result), 2)

    def test_cross_camera_vehicles_without_trusted_time_are_not_merged(self):
        first = self.detection("ch1")
        second = self.detection("ch2")
        first["embedding"] = second["embedding"] = np.array([1.0, 0.0])
        second.pop("media_time_trusted")
        result = self.pipeline(
            "123e4567-e89b-12d3-a456-426614174000", True
        ).deduplicate([first, second], 1_000.0)
        self.assertEqual(len(result), 2)

    def test_high_or_missing_uncertainty_cannot_merge_vehicles(self):
        for uncertainty in (None, 999.0):
            first = self.detection("ch1")
            second = self.detection("ch2")
            first["embedding"] = second["embedding"] = np.array([1.0, 0.0])
            world = second["camera_data"]["bifocal_metadata"]["world_position"]
            if uncertainty is None:
                world.pop("uncertainty_meters")
            else:
                world["uncertainty_meters"] = uncertainty
            result = self.pipeline(
                "123e4567-e89b-12d3-a456-426614174000", True
            ).deduplicate([first, second], 1_000.0)
            self.assertEqual(len(result), 2)

    def test_temporal_cross_camera_track_requires_matching_vehicle_embedding(self):
        pipeline = self.pipeline("123e4567-e89b-12d3-a456-426614174000", True)
        first = self.detection("ch1", 0.9, "2026-07-10T00:00:00.000Z")
        first["embedding"] = np.array([1.0, 0.0])
        first_result = pipeline.deduplicate([first], 1_000.0)[0]

        mismatch = self.detection("ch2", 0.9, "2026-07-10T00:00:01.000Z")
        mismatch["embedding"] = np.array([0.0, 1.0])
        mismatch_result = pipeline.deduplicate([mismatch], 1_001.0)[0]
        self.assertNotEqual(first_result["object_id"], mismatch_result["object_id"])

        matching_pipeline = self.pipeline(
            "abcdef01-e89b-12d3-a456-426614174000", True
        )
        first = self.detection("ch1", 0.9, "2026-07-10T00:00:00.000Z")
        first["embedding"] = np.array([1.0, 0.0])
        first_result = matching_pipeline.deduplicate([first], 2_000.0)[0]
        match = self.detection("ch2", 0.9, "2026-07-10T00:00:01.000Z")
        match["embedding"] = np.array([0.95, 0.05])
        match["embedding"] /= np.linalg.norm(match["embedding"])
        match_result = matching_pipeline.deduplicate([match], 2_001.0)[0]
        self.assertEqual(first_result["object_id"], match_result["object_id"])
        self.assertEqual(
            match_result["identity_association"]["method"],
            "cross_camera_spatiotemporal_convnext",
        )
        self.assertGreaterEqual(
            match_result["identity_association"]["appearance_similarity"], 0.60
        )

    def test_same_camera_vehicle_reattachment_requires_appearance_after_id_change(self):
        pipeline = self.pipeline("123e4567-e89b-12d3-a456-426614174000", True)
        first = self.detection("ch1", 0.9, "2026-07-10T00:00:00.000Z")
        first["embedding"] = np.array([1.0, 0.0])
        first_result = pipeline.deduplicate([first], 1_000.0)[0]
        second = self.detection("ch1", 0.9, "2026-07-10T00:00:01.000Z")
        second["track_id"] = 8
        second["embedding"] = np.array([0.0, 1.0])
        second_result = pipeline.deduplicate([second], 1_001.0)[0]
        self.assertNotEqual(first_result["object_id"], second_result["object_id"])

    def test_vehicle_class_conflict_is_recorded_without_masking_observation(self):
        pipeline = self.pipeline("123e4567-e89b-12d3-a456-426614174000", True)
        first = self.detection("ch1")
        first["embedding"] = np.array([1.0, 0.0])
        first_result = pipeline.deduplicate([first], 1_000.0)[0]
        second = self.detection("ch2", media_timestamp="2026-07-10T00:00:01.000Z")
        second["object_type"] = "truck"
        second["track_id"] = 9
        second["embedding"] = np.array([1.0, 0.0])
        second_result = pipeline.deduplicate([second], 1_001.0)[0]
        self.assertEqual(second_result["object_id"], first_result["object_id"])
        self.assertEqual(second_result["object_type"], "truck")
        self.assertEqual(
            second_result["identity_association"]["class_conflict"],
            {"track_type": "car", "observed_type": "truck"},
        )

    def test_stale_tracks_and_local_aliases_are_pruned(self):
        pipeline = self.pipeline("123e4567-e89b-12d3-a456-426614174000")
        pipeline.deduplicate([self.detection()], 1_000.0)
        self.assertTrue(pipeline.global_tracks)
        pipeline.deduplicate([], 1_000.0 + pipeline.TRACK_MAX_IDLE_SEC + 0.1)
        self.assertEqual(pipeline.global_tracks, {})
        self.assertEqual(pipeline.local_to_global, {})


class BatchUploadTests(unittest.TestCase):
    def setUp(self):
        self.detector = object.__new__(VideoObjectDetector)
        self.detector.v2x_endpoint = "https://example.invalid/detections"
        self.records = [{"event_id": "one"}, {"event_id": "two"}]

    @patch("process_video.requests.post")
    def test_batch_upload_returns_true_for_complete_item_level_success(self, post):
        response = Mock(status_code=200, text="")
        response.json.return_value = {
            "ok": True,
            "inserted": 2,
            "failed": 0,
            "results": [{"ok": True}, {"ok": True}],
        }
        post.return_value = response
        self.assertTrue(self.detector.upload_batch(self.records))

    @patch("process_video.requests.post")
    def test_batch_upload_returns_false_for_partial_http_200(self, post):
        response = Mock(status_code=200, text="")
        response.json.return_value = {
            "ok": False,
            "inserted": 1,
            "failed": 1,
            "results": [{"ok": True}, {"ok": False}],
        }
        post.return_value = response
        self.assertFalse(self.detector.upload_batch(self.records))


class LivePipelineTimestampTests(unittest.TestCase):
    class StopPipeline(Exception):
        pass

    class FakeModel:
        def track(self, *_args, **_kwargs):
            return [object()]

    class FakeDetector:
        def __init__(self):
            self.model = LivePipelineTimestampTests.FakeModel()
            self.conf = 0.4
            self.event_times = []

        def extract_detections(self, _result, _frame_count):
            return []

        def compute_3d_detections(self, _detections, timestamp, epoch):
            self.event_times.append((timestamp, epoch))
            return []

        def draw_detections_3d(self, frame, _detections):
            return frame

    class FakeReader:
        instances = []

        def __init__(self, **_kwargs):
            self.snapshot_calls = 0
            self.stop_requested = False
            self.shutdown_deadline = None
            self.joined = False
            self.kwargs = _kwargs
            self.instances.append(self)

        def start(self):
            callback = self.kwargs.get("frame_callback")
            if callback is not None:
                callback(
                    np.zeros((8, 8, 3), dtype=np.uint8),
                    1_000.25,
                    500.0,
                    None,
                )
            return None

        def snapshot(self, _after_sequence):
            self.snapshot_calls += 1
            if self.snapshot_calls == 1:
                return {
                    "sequence": 1,
                    "frame": np.zeros((8, 8, 3), dtype=np.uint8),
                    "source_epoch": 1_000.25,
                    "source_monotonic": 500.0,
                }
            raise LivePipelineTimestampTests.StopPipeline()

        def request_stop(self, deadline=None):
            self.stop_requested = True
            self.shutdown_deadline = deadline
            return None

        def join(self, _timeout=None):
            self.joined = True
            return None

        def is_alive(self):
            return False

    class ThrottledFakeReader(FakeReader):
        def snapshot(self, after_sequence):
            self.snapshot_calls += 1
            if after_sequence >= 3:
                raise LivePipelineTimestampTests.StopPipeline()
            sequence = after_sequence + 1
            return {
                "sequence": sequence,
                "frame": np.full((8, 8, 3), sequence, dtype=np.uint8),
                "source_epoch": 1_000.0 + sequence,
                "source_monotonic": 500.0 + sequence,
            }

    class ConcurrentFakeReader(FakeReader):
        def snapshot(self, after_sequence):
            self.snapshot_calls += 1
            if after_sequence > 0:
                raise LivePipelineTimestampTests.StopPipeline()
            index = self.instances.index(self)
            return {
                "sequence": 1,
                "frame": np.full((8, 8, 3), index, dtype=np.uint8),
                "source_epoch": 1_000.0 + index,
                "source_monotonic": 500.0 + index,
            }

    def test_nvdec_shutdown_budget_covers_unclaimed_open_then_cleanup(self):
        timeout = _live_pipeline_shutdown_timeout_seconds(
            "ffmpeg_nvdec", 10_000, 10_000
        )
        self.assertGreaterEqual(
            timeout,
            10.0
            + NVDEC_CAPTURE_RELEASE_WAIT_RESERVE_SECONDS
            + _COOPERATIVE_SHUTDOWN_MARGIN_SECONDS,
        )
        self.assertLess(timeout, _COOPERATIVE_SHUTDOWN_CEILING_SECONDS)

    def test_bounded_shutdown_diagnostics_are_capped_and_secret_free(self):
        release_workers = threading.Event()
        workers_ready = threading.Barrier(
            _BOUNDED_DIAGNOSTIC_THREAD_LIMIT + 6
        )
        secret = "signed-token-local-value-must-not-appear"

        def blocked_worker():
            local_secret = secret
            workers_ready.wait(timeout=2.0)
            release_workers.wait(2.0)
            self.assertTrue(local_secret)

        workers = [
            threading.Thread(target=blocked_worker, name=secret, daemon=True)
            for _ in range(_BOUNDED_DIAGNOSTIC_THREAD_LIMIT + 4)
        ]
        capture_namespace = {
            "workers_ready": workers_ready,
            "release_workers": release_workers,
        }
        exec(compile(
            "def capture_worker():\n"
            "    workers_ready.wait(timeout=2.0)\n"
            "    release_workers.wait(2.0)\n",
            f"/{secret}/live_capture.py",
            "exec",
        ), capture_namespace)
        relevant_worker = threading.Thread(
            target=capture_namespace["capture_worker"],
            name=secret,
            daemon=True,
        )
        for worker in workers:
            worker.start()
        relevant_worker.start()
        workers_ready.wait(timeout=2.0)
        stream = io.StringIO()
        topology = {
            "proactive_preparations": 0,
            "terminal_cleanups": 0,
        }
        try:
            with patch(
                "process_video.capture_preparation_topology",
                return_value=topology,
            ), patch("process_video.sys.stderr", stream):
                _emit_bounded_shutdown_diagnostics(
                    [
                        "terminal_cleanup_timeout",
                        "https://invalid?token=diagnostic-secret",
                        "reader_timeout",
                    ],
                    999,
                )
        finally:
            release_workers.set()
            for worker in [*workers, relevant_worker]:
                worker.join(1.0)

        encoded = stream.getvalue()
        payload = json.loads(encoded)
        self.assertEqual(
            payload["failure_causes"],
            ["reader_timeout", "terminal_cleanup_timeout"],
        )
        self.assertEqual(payload["live_reader_alive_count"], 64)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("diagnostic-secret", encoded)
        self.assertNotIn("0x", encoded)
        stacks = payload["python_stacks"]
        self.assertLessEqual(
            stacks["reported_thread_count"],
            _BOUNDED_DIAGNOSTIC_THREAD_LIMIT,
        )
        self.assertTrue(stacks["truncated"])
        self.assertLessEqual(
            len(json.dumps(stacks, separators=(",", ":")).encode("utf-8")),
            _BOUNDED_DIAGNOSTIC_STACK_BYTES,
        )
        allowed_categories = {
            "reporter", "main", "inference", "capture", "decoder",
            "http", "other",
        }
        self.assertIn(
            "capture",
            [thread["category"] for thread in stacks["threads"]],
        )
        for thread in stacks["threads"]:
            self.assertIn(thread["category"], allowed_categories)
            self.assertLessEqual(
                len(thread["frames"]), _BOUNDED_DIAGNOSTIC_FRAME_LIMIT
            )
            for frame in thread["frames"]:
                self.assertNotIn("/", frame["file"])
                self.assertNotIn("\\", frame["file"])

    def test_nvdec_shutdown_budget_covers_concurrent_claimed_cleanup(self):
        timeout = _live_pipeline_shutdown_timeout_seconds(
            "ffmpeg_nvdec", 10_000, 10_000
        )
        self.assertGreaterEqual(
            timeout,
            NVDEC_CAPTURE_RELEASE_WAIT_RESERVE_SECONDS
            + _COOPERATIVE_SHUTDOWN_MARGIN_SECONDS,
        )
        self.assertLess(
            timeout,
            2.0 * NVDEC_CAPTURE_RELEASE_WAIT_RESERVE_SECONDS
            + _COOPERATIVE_SHUTDOWN_MARGIN_SECONDS,
        )
        self.assertLess(timeout, _COOPERATIVE_SHUTDOWN_CEILING_SECONDS)

    def test_shutdown_budget_reserves_outer_service_cleanup(self):
        timeout = _live_pipeline_shutdown_timeout_seconds(
            "ffmpeg_nvdec", 10_000, 10_000
        )
        self.assertLess(
            timeout + _OUTER_SHUTDOWN_RESERVE_SECONDS,
            _COOPERATIVE_SHUTDOWN_CEILING_SECONDS,
        )

    def test_nvdec_shutdown_budget_rejects_configuration_at_ceiling(self):
        with self.assertRaisesRegex(ValueError, "sub-45-second"):
            _live_pipeline_shutdown_timeout_seconds(
                "ffmpeg_nvdec", 30_000, 30_000
            )

    def test_shutdown_budget_rejects_disabled_native_timeout(self):
        with self.assertRaisesRegex(ValueError, "finite and positive"):
            _live_pipeline_shutdown_timeout_seconds(
                "ffmpeg_nvdec", 0, 10_000
            )

    def test_service_stop_timeout_exceeds_cooperative_ceiling(self):
        unit = (
            PERCEPTION_DIR.parents[1]
            / "scripts/systemd/v2x-perception.service"
        ).read_text(encoding="utf-8")
        value = next(
            float(line.split("=", 1)[1])
            for line in unit.splitlines()
            if line.startswith("TimeoutStopSec=")
        )
        self.assertGreater(value, _COOPERATIVE_SHUTDOWN_CEILING_SECONDS)
        self.assertEqual(value, 60.0)
        self.assertIn("KillMode=mixed", unit.splitlines())
        self.assertNotIn("KillMode=process", unit.splitlines())
        self.assertIn("KillSignal=SIGTERM", unit.splitlines())
        self.assertIn("SendSIGKILL=yes", unit.splitlines())
        self.assertIn("FinalKillSignal=SIGKILL", unit.splitlines())
        launch = (
            PERCEPTION_DIR.parents[1] / "scripts/launch-perception.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('exec "${PYTHON_BIN}" process_video.py', launch)

    @patch("process_video.LiveStreamReader", FakeReader)
    def test_pre_requested_shutdown_cleans_up_without_consuming_frames(self):
        self.FakeReader.instances.clear()
        pipeline = object.__new__(MultiCameraPipeline)
        pipeline.detectors = [self.FakeDetector()]
        pipeline.all_clean_detections = []
        pipeline.global_tracks = {}
        pipeline.local_to_global = {}
        pipeline.next_global_id = 0
        pipeline.extractor = Mock()
        shutdown = threading.Event()
        shutdown.set()

        with patch.dict(
            os.environ,
            {"V2X_PERCEPTION_CAPTURE_BACKEND": "ffmpeg_nvdec"},
        ):
            pipeline.process_streams(
                ["v2x-backend-cam-ch1"],
                show_live=False,
                upload=False,
                stream_broadcaster=FrameBroadcaster(["ch1"]),
                camera_ids=["ch1"],
                shutdown_event=shutdown,
            )

        self.assertEqual(len(self.FakeReader.instances), 1)
        reader = self.FakeReader.instances[0]
        self.assertEqual(reader.snapshot_calls, 0)
        self.assertTrue(reader.stop_requested)
        self.assertTrue(reader.joined)
        self.assertTrue(reader.kwargs["reserve_proactive_decoder_slot"])

    def test_pipeline_cancels_proactive_helpers_before_reader_join(self):
        order = []

        class OrderedReader(self.FakeReader):
            instances = []

            def request_stop(self, deadline=None):
                order.append("reader_stop")
                return super().request_stop(deadline=deadline)

            def join(self, timeout=None):
                order.append("reader_join")
                return super().join(timeout)

        def cancel_helpers(timeout=0.0):
            self.assertEqual(timeout, 0.0)
            order.append("helper_cancel")
            return False

        pipeline = object.__new__(MultiCameraPipeline)
        pipeline.detectors = [self.FakeDetector()]
        pipeline.all_clean_detections = []
        pipeline.global_tracks = {}
        pipeline.local_to_global = {}
        pipeline.next_global_id = 0
        pipeline.extractor = Mock()
        shutdown = threading.Event()
        shutdown.set()

        with patch("process_video.LiveStreamReader", OrderedReader), patch(
            "process_video._cancel_proactive_preparations",
            side_effect=cancel_helpers,
        ):
            pipeline.process_streams(
                ["v2x-backend-cam-ch1"],
                show_live=False,
                upload=False,
                stream_broadcaster=FrameBroadcaster(["ch1"]),
                camera_ids=["ch1"],
                shutdown_event=shutdown,
            )

        self.assertEqual(
            order[:3], ["reader_stop", "helper_cancel", "reader_join"]
        )

    def test_pipeline_shutdown_uses_only_bounded_reader_join(self):
        class BoundedReader(self.FakeReader):
            instances = []

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.alive = True
                self.join_timeouts = []

            def join(self, timeout=None):
                self.join_timeouts.append(timeout)
                if timeout is not None:
                    self.alive = False
                self.joined = True

            def is_alive(self):
                return self.alive

        pipeline = object.__new__(MultiCameraPipeline)
        pipeline.detectors = [self.FakeDetector()]
        pipeline.all_clean_detections = []
        pipeline.global_tracks = {}
        pipeline.local_to_global = {}
        pipeline.next_global_id = 0
        pipeline.extractor = Mock()
        shutdown = threading.Event()
        shutdown.set()

        with patch("process_video.LiveStreamReader", BoundedReader):
            pipeline.process_streams(
                ["v2x-backend-cam-ch1"],
                show_live=False,
                upload=False,
                stream_broadcaster=FrameBroadcaster(["ch1"]),
                camera_ids=["ch1"],
                shutdown_event=shutdown,
            )

        reader = BoundedReader.instances[0]
        self.assertEqual(len(reader.join_timeouts), 1)
        self.assertIsNotNone(reader.join_timeouts[0])
        self.assertIsNotNone(reader.shutdown_deadline)
        self.assertFalse(reader.is_alive())

    def test_pipeline_shutdown_rejects_stubborn_reader_inside_wall_bound(self):
        class StubbornReader(self.FakeReader):
            instances = []

            def is_alive(self):
                return True

        pipeline = object.__new__(MultiCameraPipeline)
        pipeline.detectors = [self.FakeDetector()]
        pipeline.all_clean_detections = []
        pipeline.global_tracks = {}
        pipeline.local_to_global = {}
        pipeline.next_global_id = 0
        pipeline.extractor = Mock()
        shutdown = threading.Event()
        shutdown.set()

        started = time.monotonic()
        with patch("process_video.LiveStreamReader", StubbornReader), patch(
            "process_video.wait_for_terminal_cleanups", return_value=False
        ), patch(
            "process_video._emit_bounded_shutdown_diagnostics"
        ) as emit_diagnostics, patch.dict(
            os.environ,
            {
                "V2X_PERCEPTION_OPEN_TIMEOUT_MS": "1",
                "V2X_PERCEPTION_READ_TIMEOUT_MS": "1",
            },
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "terminal decoder cleanup exceeded its bounded deadline",
            ):
                pipeline.process_streams(
                    ["v2x-backend-cam-ch1"],
                    show_live=False,
                    upload=False,
                    stream_broadcaster=FrameBroadcaster(["ch1"]),
                    camera_ids=["ch1"],
                    shutdown_event=shutdown,
                )
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(StubbornReader.instances[0].stop_requested)
        self.assertIsNotNone(StubbornReader.instances[0].shutdown_deadline)
        emit_diagnostics.assert_called_once_with(
            ["reader_timeout", "terminal_cleanup_timeout"], 1
        )

    def test_active_inference_observes_shutdown_inside_wall_bound(self):
        inference_entered = threading.Event()
        release_inference = threading.Event()
        shutdown = threading.Event()
        detector = self.FakeDetector()

        def blocked_track(*_args, **_kwargs):
            inference_entered.set()
            release_inference.wait(3.0)
            return []

        detector.model.track = blocked_track
        pipeline = object.__new__(MultiCameraPipeline)
        pipeline.detectors = [detector]
        pipeline.all_clean_detections = []
        pipeline.global_tracks = {}
        pipeline.local_to_global = {}
        pipeline.next_global_id = 0
        pipeline.extractor = Mock()

        def request_shutdown():
            self.assertTrue(inference_entered.wait(1.0))
            shutdown.set()

        stopper = threading.Thread(target=request_shutdown)
        stopper.start()
        started = time.monotonic()
        try:
            with patch("process_video.LiveStreamReader", self.FakeReader), patch.dict(
                os.environ,
                {
                    "V2X_PERCEPTION_OPEN_TIMEOUT_MS": "1",
                    "V2X_PERCEPTION_READ_TIMEOUT_MS": "1",
                },
            ):
                pipeline.process_streams(
                    ["v2x-backend-cam-ch1"],
                    show_live=False,
                    upload=False,
                    stream_broadcaster=FrameBroadcaster(["ch1"]),
                    camera_ids=["ch1"],
                    shutdown_event=shutdown,
                )
        finally:
            release_inference.set()
            stopper.join(1.0)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and any(
            thread.name.startswith("v2x-inference-")
            for thread in threading.enumerate()
        ):
            time.sleep(0.01)
        self.assertFalse(any(
            thread.name.startswith("v2x-inference-")
            for thread in threading.enumerate()
        ))

    def test_model_timeout_error_propagates_instead_of_polling_forever(self):
        detector = self.FakeDetector()
        detector.model.track = Mock(side_effect=TimeoutError("model timeout"))
        pipeline = object.__new__(MultiCameraPipeline)
        pipeline.detectors = [detector]
        pipeline.all_clean_detections = []
        pipeline.global_tracks = {}
        pipeline.local_to_global = {}
        pipeline.next_global_id = 0
        pipeline.extractor = Mock()

        started = time.monotonic()
        with patch("process_video.LiveStreamReader", self.FakeReader):
            with self.assertRaisesRegex(TimeoutError, "model timeout"):
                pipeline.process_streams(
                    ["v2x-backend-cam-ch1"],
                    show_live=False,
                    upload=False,
                    stream_broadcaster=FrameBroadcaster(["ch1"]),
                    camera_ids=["ch1"],
                )
        self.assertLess(time.monotonic() - started, 0.5)

    @patch("process_video.LiveStreamReader", FakeReader)
    def test_pipeline_uses_per_camera_capture_time_and_source_age(self):
        self.FakeReader.instances.clear()
        detector = self.FakeDetector()
        pipeline = object.__new__(MultiCameraPipeline)
        pipeline.detectors = [detector]
        pipeline.all_clean_detections = []
        pipeline.global_tracks = {}
        pipeline.local_to_global = {}
        pipeline.next_global_id = 0
        pipeline.extractor = Mock()
        broadcaster = FrameBroadcaster(["ch1"], stale_seconds=1.0)

        with self.assertRaises(self.StopPipeline):
            pipeline.process_streams(
                ["v2x-backend-cam-ch1"],
                show_live=False,
                upload=False,
                stream_broadcaster=broadcaster,
                camera_ids=["ch1"],
            )

        self.assertEqual(detector.event_times[0][1], 1_000.25)
        health = broadcaster.snapshot_health(now_monotonic=500.1)
        self.assertEqual(
            health["cameras"]["ch1"]["source_updated_at"],
            detector.event_times[0][0],
        )
        self.assertAlmostEqual(health["cameras"]["ch1"]["age_seconds"], 0.1)
        self.assertEqual(len(self.FakeReader.instances), 1)
        self.assertEqual(
            self.FakeReader.instances[0].kwargs["connection_max_age_seconds"],
            240.0,
        )
        self.assertEqual(
            self.FakeReader.instances[0].kwargs[
                "terminal_read_failover_seconds"
            ],
            8.0,
        )
        self.assertTrue(callable(
            self.FakeReader.instances[0].kwargs["frame_callback"]
        ))

    @patch("process_video.LiveStreamReader", ThrottledFakeReader)
    def test_live_throttle_does_not_consume_skipped_camera_sequence(self):
        self.ThrottledFakeReader.instances.clear()
        detector = self.FakeDetector()
        pipeline = object.__new__(MultiCameraPipeline)
        pipeline.detectors = [detector]
        pipeline.all_clean_detections = []
        pipeline.global_tracks = {}
        pipeline.local_to_global = {}
        pipeline.next_global_id = 0
        pipeline.extractor = Mock()

        with self.assertRaises(self.StopPipeline):
            pipeline.process_streams(
                ["v2x-backend-cam-ch1"],
                show_live=False,
                upload=False,
                stream_broadcaster=FrameBroadcaster(["ch1"]),
                camera_ids=["ch1"],
            )

        self.assertEqual(len(detector.event_times), 3)
        self.assertEqual(
            [round(epoch) for _timestamp, epoch in detector.event_times],
            [1_001, 1_002, 1_003],
        )

    @patch("process_video.LiveStreamReader", ConcurrentFakeReader)
    def test_live_camera_inference_uses_bounded_parallel_workers(self):
        self.ConcurrentFakeReader.instances.clear()
        barrier = threading.Barrier(2)

        class BarrierModel:
            def track(self, *_args, **_kwargs):
                barrier.wait(timeout=1.0)
                return [object()]

        detectors = [self.FakeDetector(), self.FakeDetector()]
        for detector in detectors:
            detector.model = BarrierModel()
        pipeline = object.__new__(MultiCameraPipeline)
        pipeline.detectors = detectors
        pipeline.all_clean_detections = []
        pipeline.global_tracks = {}
        pipeline.local_to_global = {}
        pipeline.next_global_id = 0
        pipeline.extractor = Mock()

        with self.assertRaises(self.StopPipeline):
            pipeline.process_streams(
                ["v2x-backend-cam-ch1", "v2x-backend-cam-ch2"],
                show_live=False,
                upload=False,
                stream_broadcaster=FrameBroadcaster(["ch1", "ch2"]),
                camera_ids=["ch1", "ch2"],
            )

        self.assertEqual([len(d.event_times) for d in detectors], [1, 1])


if __name__ == "__main__":
    unittest.main()
