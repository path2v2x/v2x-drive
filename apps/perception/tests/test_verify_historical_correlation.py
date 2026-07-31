import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = PERCEPTION_DIR / "tools"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "hls"
sys.path.insert(0, str(TOOLS_DIR))

from verify_historical_correlation import (  # noqa: E402
    ParsedMediaPlaylist,
    VerificationError,
    bbox_geometry,
    choose_nearest_frame,
    evaluate_acceptance,
    fetch_bytes,
    fetch_media_playlist,
    jpeg_dimensions,
    load_detection_from_snapshot,
    main,
    normalize_api_base_url,
    normalize_bbox,
    parse_media_playlist,
    parse_utc_timestamp,
    request_on_demand_session,
    resolve_inputs,
    select_segment,
    validate_media_timestamp_trust,
    write_json_evidence,
)


def utc(value):
    return parse_utc_timestamp(value, "test timestamp")


def trusted_detection(**overrides):
    detection = {
        "event_id": "event-1",
        "object_id": "global_car_run_1",
        "object_type": "car",
        "device_id": "cam-001-ch4",
        "timestamp_utc": "2026-07-10T03:57:23.138Z",
        "media_timestamp_utc": "2026-07-10T03:57:23.138Z",
        "timestamp_schema_version": 2,
        "media_time_trusted": True,
        "media_clock": {
            "source": "hls_ext_x_program_date_time",
            "schema_version": 1,
            "anchor_program_date_time_utc": "2026-07-10T03:57:20.000Z",
            "position_milliseconds": 3138.0,
        },
        "camera_data": {
            "bifocal_metadata": {"bbox": [10.0, 20.0, 100.0, 200.0]}
        },
        "confidence_score": 0.9,
    }
    detection.update(overrides)
    return detection


class PlaylistParserTests(unittest.TestCase):
    def setUp(self):
        self.media = (FIXTURES_DIR / "media.m3u8").read_text()
        self.media_url = (
            "https://archive.example.test/media.m3u8?SessionToken=top-secret"
        )

    def test_parses_fmp4_map_and_increments_program_date_time(self):
        playlist = parse_media_playlist(self.media, self.media_url)
        self.assertIsInstance(playlist, ParsedMediaPlaylist)
        self.assertEqual(len(playlist.segments), 3)
        first, second, third = playlist.segments
        self.assertEqual(first.program_date_time, utc("2026-07-10T03:57:20Z"))
        self.assertEqual(second.program_date_time, utc("2026-07-10T03:57:24Z"))
        self.assertEqual(third.program_date_time, utc("2026-07-10T03:57:29.250Z"))
        self.assertEqual(first.duration_seconds, 4.0)
        self.assertEqual(
            first.map_uri.split("?", 1)[0],
            "https://archive.example.test/init.mp4",
        )

    def test_selects_only_fragment_containing_exact_pdt(self):
        playlist = parse_media_playlist(self.media, self.media_url)
        segment, offset = select_segment(
            playlist, utc("2026-07-10T03:57:23.138Z")
        )
        self.assertEqual(segment.sequence, 0)
        self.assertAlmostEqual(offset, 3.138, places=6)

        segment, offset = select_segment(
            playlist, utc("2026-07-10T03:57:24.000Z")
        )
        self.assertEqual(segment.sequence, 1)
        self.assertEqual(offset, 0.0)

    def test_gap_is_not_silently_rounded_to_nearest_fragment(self):
        playlist = parse_media_playlist(self.media, self.media_url)
        with self.assertRaisesRegex(VerificationError, "outside HLS fragment coverage"):
            select_segment(playlist, utc("2026-07-10T03:57:28.500Z"))

    def test_rejects_missing_map_pdt_and_byte_ranges(self):
        with self.assertRaisesRegex(VerificationError, "no fMP4 EXT-X-MAP"):
            parse_media_playlist(
                "#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:2026-07-10T00:00:00Z\n"
                "#EXTINF:2,\nsegment.mp4\n",
                self.media_url,
            )
        with self.assertRaisesRegex(VerificationError, "no program date time"):
            parse_media_playlist(
                "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n"
                "#EXTINF:2,\nsegment.mp4\n",
                self.media_url,
            )
        with self.assertRaisesRegex(VerificationError, "byte-range"):
            parse_media_playlist(
                "#EXTM3U\n#EXT-X-BYTERANGE:10@0\n",
                self.media_url,
            )

    def test_rejects_cross_origin_map_or_fragment(self):
        with self.assertRaisesRegex(VerificationError, "cross-origin"):
            parse_media_playlist(
                "#EXTM3U\n"
                "#EXT-X-MAP:URI=\"https://attacker.example/init.mp4\"\n"
                "#EXT-X-PROGRAM-DATE-TIME:2026-07-10T00:00:00Z\n"
                "#EXTINF:2,\nsegment.mp4\n",
                self.media_url,
            )


class SessionServerTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests
        master = (FIXTURES_DIR / "master.m3u8").read_bytes()
        media = (FIXTURES_DIR / "media.m3u8").read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                requests.append(self.path)
                if self.path.startswith("/video/session/ch4?"):
                    hls_url = (
                        f"http://127.0.0.1:{self.server.server_port}/master.m3u8"
                        "?SessionToken=server-secret&X-Amz-Signature=signed-secret"
                    )
                    body = json.dumps(
                        {
                            "cameraId": "ch4",
                            "playbackMode": "ON_DEMAND",
                            "hlsUrl": hls_url,
                            "expiresIn": 300,
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/master.m3u8?"):
                    body = master
                elif self.path.startswith("/media.m3u8?"):
                    body = media
                elif self.path.startswith("/forbidden?"):
                    self.send_response(403)
                    self.end_headers()
                    return
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("content-type", "application/vnd.apple.mpegurl")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.server.block_on_close = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def test_obtains_on_demand_session_and_loads_media_playlist(self):
        target = utc("2026-07-10T03:57:23.138Z")
        session = request_on_demand_session(
            self.base_url, "ch4", target, window_seconds=20, timeout_seconds=2
        )
        self.assertEqual(session.playback_mode, "ON_DEMAND")
        self.assertIn("SessionToken=server-secret", session.hls_url)
        text, media_url = fetch_media_playlist(session.hls_url, timeout_seconds=2)
        playlist = parse_media_playlist(text, media_url)
        segment, offset = select_segment(playlist, target)
        self.assertEqual(segment.sequence, 0)
        self.assertAlmostEqual(offset, 3.138, places=6)
        session_request = next(
            request
            for request in self.requests
            if request.startswith("/video/session/")
        )
        self.assertIn("start=", session_request)
        self.assertIn("end=", session_request)

    def test_network_error_does_not_echo_signed_url(self):
        signed_url = (
            f"{self.base_url}/forbidden?SessionToken=top-secret&"
            "X-Amz-Signature=signature-secret"
        )
        with self.assertRaises(VerificationError) as raised:
            fetch_bytes(
                signed_url, limit=1024, timeout_seconds=2, label="test fragment"
            )
        message = str(raised.exception)
        for forbidden in (
            signed_url,
            "top-secret",
            "signature-secret",
            "SessionToken",
            "X-Amz-Signature",
        ):
            self.assertNotIn(forbidden, message)


class StructuredEvidenceTests(unittest.TestCase):
    def test_json_evidence_is_atomic_and_fail_closed_on_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "nested" / "report.json"
            write_json_evidence(output, {"gate": False})
            self.assertEqual(json.loads(output.read_text()), {"gate": False})
            self.assertEqual(list(output.parent.glob(f".{output.name}.*")), [])
            with self.assertRaisesRegex(VerificationError, "already exists"):
                write_json_evidence(output, {"gate": True})
            write_json_evidence(output, {"gate": True}, overwrite=True)
            self.assertEqual(json.loads(output.read_text()), {"gate": True})

    def test_hash_bound_snapshot_loads_exact_unique_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(temp_dir)
            first = trusted_detection(event_id="event-1")
            second = trusted_detection(event_id="event-2")
            detections = snapshot / "detections.ndjson"
            detections.write_text(
                json.dumps(first) + "\n" + json.dumps(second) + "\n"
            )
            import hashlib

            digest = hashlib.sha256(detections.read_bytes()).hexdigest()
            (snapshot / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "v2x-detection-corpus-snapshot/v1",
                        "artifacts": {"detections.ndjson": digest},
                    }
                )
            )
            self.assertEqual(
                load_detection_from_snapshot(snapshot, "event-2")["event_id"],
                "event-2",
            )

    def test_snapshot_rejects_tampering_and_duplicate_event_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(temp_dir)
            detections = snapshot / "detections.ndjson"
            row = trusted_detection(event_id="event-1")
            detections.write_text(json.dumps(row) + "\n")
            import hashlib

            digest = hashlib.sha256(detections.read_bytes()).hexdigest()
            (snapshot / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "v2x-detection-corpus-snapshot/v1",
                        "artifacts": {"detections.ndjson": digest},
                    }
                )
            )
            detections.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
            with self.assertRaisesRegex(VerificationError, "hash does not match"):
                load_detection_from_snapshot(snapshot, "event-1")

            digest = hashlib.sha256(detections.read_bytes()).hexdigest()
            (snapshot / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "v2x-detection-corpus-snapshot/v1",
                        "artifacts": {"detections.ndjson": digest},
                    }
                )
            )
            with self.assertRaisesRegex(VerificationError, "not unique"):
                load_detection_from_snapshot(snapshot, "event-1")

    def test_nearest_encoded_frame_uses_relative_pts(self):
        frames = [
            {"index": 0, "relative_seconds": 0.0},
            {"index": 1, "relative_seconds": 0.033333},
            {"index": 2, "relative_seconds": 0.066667},
        ]
        selected = choose_nearest_frame(frames, 0.052)
        self.assertEqual(selected["index"], 2)

    def test_bbox_geometry_reports_clipped_area_without_hiding_it(self):
        geometry = bbox_geometry((-10.0, 10.0, 50.0, 70.0), 100, 80)
        self.assertEqual(geometry["clipped"], [0.0, 10.0, 50.0, 70.0])
        self.assertAlmostEqual(geometry["in_frame_area_ratio"], 5 / 6, places=6)

    def test_deliberately_shifted_timestamp_outside_frame_tolerance_fails(self):
        expected = utc("2026-07-10T03:57:23.138Z")
        shifted = utc("2026-07-10T03:57:23.388Z")
        shifted_by_ms = abs((shifted - expected).total_seconds()) * 1000.0
        result = evaluate_acceptance(
            timestamp_trusted=True,
            frame_timing_error_ms=shifted_by_ms,
            maximum_frame_timing_error_ms=100.0,
            structured_passed=True,
            visual_corroborated=True,
            require_yolo=True,
        )
        self.assertFalse(result["gate_passed"])
        self.assertFalse(result["frame_timing_check_passed"])
        self.assertEqual(result["verdict"], "FRAME_TIMING_MISMATCH")

    def test_deliberately_shifted_frame_with_no_matching_object_fails(self):
        result = evaluate_acceptance(
            timestamp_trusted=True,
            frame_timing_error_ms=8.0,
            maximum_frame_timing_error_ms=100.0,
            structured_passed=True,
            visual_corroborated=False,
            require_yolo=True,
        )
        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["verdict"], "VISUAL_MISMATCH")

    def test_wrong_bbox_with_no_semantic_overlap_fails(self):
        # This control is geometrically valid and non-empty, so only the
        # semantic overlap gate can reject it.  It models a saved box shifted
        # away from the detected object rather than an easy malformed input.
        wrong_bbox = normalize_bbox("900,10,1100,250")
        wrong_geometry = bbox_geometry(wrong_bbox, 2560, 1920)
        self.assertEqual(wrong_geometry["in_frame_area_ratio"], 1.0)
        result = evaluate_acceptance(
            timestamp_trusted=True,
            frame_timing_error_ms=8.0,
            maximum_frame_timing_error_ms=100.0,
            structured_passed=True,
            visual_corroborated=False,
            require_yolo=True,
        )
        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["verdict"], "VISUAL_MISMATCH")

    def test_empty_bbox_and_blank_crop_controls_fail(self):
        with self.assertRaisesRegex(VerificationError, "positive width"):
            normalize_bbox("10,10,10,20")
        result = evaluate_acceptance(
            timestamp_trusted=True,
            frame_timing_error_ms=8.0,
            maximum_frame_timing_error_ms=100.0,
            structured_passed=False,
            visual_corroborated=None,
            require_yolo=False,
        )
        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["verdict"], "STRUCTURED_MISMATCH")

    def test_trusted_media_clock_schema_reconstructs_persisted_timestamp(self):
        target = utc("2026-07-10T03:57:23.138Z")
        trust = validate_media_timestamp_trust(
            trusted_detection(),
            "media_timestamp_utc",
            target,
        )
        self.assertTrue(trust["trusted"])
        self.assertEqual(trust["timestamp_consistency_error_ms"], 0.0)

    def test_timestamp_utc_must_equal_persisted_media_timestamp(self):
        target = utc("2026-07-10T03:57:23.138Z")
        trust = validate_media_timestamp_trust(
            trusted_detection(timestamp_utc="2026-07-10T04:57:23.138Z"),
            "media_timestamp_utc",
            target,
        )
        self.assertFalse(trust["trusted"])
        self.assertIn("does not equal", trust["reason"])

    def test_flag_and_schema_spoof_without_reconstructable_clock_is_untrusted(self):
        target = utc("2026-07-10T03:57:23.138Z")
        spoofed = trusted_detection(
            media_clock={
                "source": "hls_ext_x_program_date_time",
                "schema_version": 1,
            }
        )
        trust = validate_media_timestamp_trust(
            spoofed,
            "media_timestamp_utc",
            target,
        )
        self.assertFalse(trust["trusted"])
        self.assertIn("anchor or position", trust["reason"])

    def test_pre_fix_rows_with_media_like_field_cannot_pass_acceptance(self):
        target = utc("2026-07-10T03:57:23.138Z")
        trust = validate_media_timestamp_trust(
            {
                "timestamp_utc": "2026-07-10T03:57:23.138Z",
                "media_timestamp_utc": "2026-07-10T03:57:23.138Z",
            },
            "media_timestamp_utc",
            target,
        )
        self.assertFalse(trust["trusted"])
        self.assertIn("timestamp schema version", trust["reason"])
        result = evaluate_acceptance(
            timestamp_trusted=bool(trust["trusted"]),
            frame_timing_error_ms=0.0,
            maximum_frame_timing_error_ms=100.0,
            structured_passed=True,
            visual_corroborated=True,
            require_yolo=True,
        )
        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["verdict"], "UNTRUSTED_MEDIA_TIMESTAMP")

    def test_pre_fix_row_with_clock_like_metadata_still_cannot_pass(self):
        target = utc("2026-07-10T03:57:23.138Z")
        trust = validate_media_timestamp_trust(
            {
                "timestamp_utc": "2026-07-10T03:57:23.138Z",
                "media_timestamp_utc": "2026-07-10T03:57:23.138Z",
                "media_clock": {
                    "source": "hls_ext_x_program_date_time",
                    "schema_version": 1,
                    "anchor_program_date_time_utc": "2026-07-10T03:57:20Z",
                    "position_milliseconds": 3138.0,
                },
            },
            "media_timestamp_utc",
            target,
        )
        self.assertFalse(trust["trusted"])
        self.assertIn("timestamp schema version", trust["reason"])

    def test_wrong_or_inconsistent_clock_schema_is_untrusted(self):
        target = utc("2026-07-10T03:57:23.138Z")
        base_clock = {
            "source": "hls_ext_x_program_date_time",
            "schema_version": 2,
            "anchor_program_date_time_utc": "2026-07-10T03:57:20.000Z",
            "position_milliseconds": 3138.0,
        }
        wrong_version = validate_media_timestamp_trust(
            trusted_detection(media_clock=base_clock),
            "media_timestamp_utc",
            target,
        )
        self.assertFalse(wrong_version["trusted"])
        self.assertIn("schema version", wrong_version["reason"])

        inconsistent_clock = dict(base_clock, schema_version=1)
        inconsistent_clock["position_milliseconds"] = 100.0
        inconsistent = validate_media_timestamp_trust(
            trusted_detection(media_clock=inconsistent_clock),
            "media_timestamp_utc",
            target,
        )
        self.assertFalse(inconsistent["trusted"])
        self.assertGreater(inconsistent["timestamp_consistency_error_ms"], 5.0)

        boolean_schema = dict(base_clock, schema_version=True)
        boolean_version = validate_media_timestamp_trust(
            trusted_detection(media_clock=boolean_schema),
            "media_timestamp_utc",
            target,
        )
        self.assertFalse(boolean_version["trusted"])

        boolean_position = dict(base_clock, schema_version=1)
        boolean_position["position_milliseconds"] = True
        invalid_position = validate_media_timestamp_trust(
            trusted_detection(media_clock=boolean_position),
            "media_timestamp_utc",
            target,
        )
        self.assertFalse(invalid_position["trusted"])

        boolean_top_level = validate_media_timestamp_trust(
            trusted_detection(timestamp_schema_version=True),
            "media_timestamp_utc",
            target,
        )
        self.assertFalse(boolean_top_level["trusted"])

    def test_detection_json_rejects_all_cli_detection_field_overrides(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            detection_path = Path(temp_dir) / "detection.json"
            detection_path.write_text(json.dumps(trusted_detection()))
            overrides = {
                "camera": "ch1",
                "media_timestamp": "2026-07-10T03:57:24.000Z",
                "bbox": "1,2,3,4",
                "object_id": "different-object",
                "object_type": "person",
                "confidence": 0.1,
            }
            for attribute, value in overrides.items():
                values = {name: None for name in overrides}
                values[attribute] = value
                args = type(
                    "Args",
                    (),
                    {"detection_json": detection_path, **values},
                )()
                with self.subTest(attribute=attribute), self.assertRaisesRegex(
                    VerificationError,
                    "cannot be overridden",
                ):
                    resolve_inputs(args)

    def test_detection_json_derives_camera_from_persisted_device_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            detection_path = Path(temp_dir) / "detection.json"
            detection_path.write_text(json.dumps(trusted_detection()))
            args = type(
                "Args",
                (),
                {
                    "detection_json": detection_path,
                    "camera": None,
                    "media_timestamp": None,
                    "bbox": None,
                    "object_id": None,
                    "object_type": None,
                    "confidence": None,
                },
            )()
            self.assertEqual(resolve_inputs(args)["camera_id"], "ch4")

    def test_reads_dimensions_from_minimal_jpeg_sof(self):
        # SOI + SOF0(length=11, precision=8, height=720, width=1280,
        # components=1) + one component descriptor + EOI.
        data = (
            b"\xff\xd8\xff\xc0\x00\x0b\x08\x02\xd0\x05\x00\x01\x01\x11\x00"
            b"\xff\xd9"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.subTest("fixture is intentionally metadata-only"):
                path = Path(temp_dir) / "frame.jpg"
                path.write_bytes(data)
                self.assertEqual(jpeg_dimensions(path), (1280, 720))

    def test_remote_http_api_and_query_bearing_base_are_rejected_without_echo(self):
        with self.assertRaisesRegex(VerificationError, "must use HTTPS"):
            normalize_api_base_url("http://video.example")
        secret = "https://video.example?SessionToken=do-not-print"
        with self.assertRaises(VerificationError) as raised:
            normalize_api_base_url(secret)
        self.assertNotIn("do-not-print", str(raised.exception))

    def test_receipt_timestamp_is_not_accepted_as_media_timestamp(self):
        args = type(
            "Args",
            (),
            {
                "detection_json": None,
                "camera": "ch4",
                "media_timestamp": None,
                "bbox": "1,2,3,4",
                "object_id": "global_car_13",
                "object_type": "vehicle",
                "confidence": 0.9,
            },
        )()
        # Inject the current persisted schema shape without weakening the real loader.
        import verify_historical_correlation as verifier

        original = verifier.load_detection
        verifier.load_detection = lambda _path: {
            "timestamp_utc": "2026-07-10T03:57:23.138Z"
        }
        try:
            with self.assertRaisesRegex(
                VerificationError, "only receipt timestamp_utc"
            ):
                resolve_inputs(args)
        finally:
            verifier.load_detection = original

    def test_cli_failure_redacts_query_material(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "https://api.example?SessionToken=top-secret",
                    "--camera",
                    "ch4",
                    "--media-timestamp",
                    "2026-07-10T03:57:23Z",
                    "--bbox",
                    "1,2,3,4",
                    "--output",
                    "/tmp/never-created.jpg",
                ]
            )
        self.assertEqual(code, 1)
        self.assertNotIn("top-secret", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
