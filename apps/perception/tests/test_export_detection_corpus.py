from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from export_detection_corpus import (  # noqa: E402
    ExportError,
    export_detection_corpus,
    normalize_api_base_url,
    prune_snapshots,
    sanitize_tree,
)

NOW = datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc)


def row(event_id, camera="ch1", offset_minutes=0, object_type="car"):
    when = NOW - timedelta(minutes=offset_minutes)
    timestamp = when.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "event_id": event_id,
        "device_id": f"cam-001-{camera}",
        "object_type": object_type,
        "timestamp_schema_version": 2,
        "timestamp_utc": timestamp,
        "media_timestamp_utc": timestamp,
        "media_time_trusted": True,
        "media_clock_status": "matched",
        "media_clock": {
            "source": "hls_ext_x_program_date_time",
            "schema_version": 1,
        },
        "camera_data": {
            "image_reference_url": "https://media.test/frame.jpg?Signature=secret"
        },
    }


def response(payload):
    return payload, json.dumps(payload, separators=(",", ":")).encode()


def timeline(total, **extra):
    return {
        "totalDetections": total,
        "events": [],
        "start": "2026-07-10T08:00:00.000Z",
        "end": "2026-07-11T08:00:00.000Z",
        "bucketSeconds": 60,
        **extra,
    }


class DetectionCorpusExporterTests(unittest.TestCase):
    def test_query_bearing_api_base_is_rejected_without_echoing_secret(self):
        with self.assertRaises(ExportError) as raised:
            normalize_api_base_url("https://api.test?token=hidden")
        self.assertNotIn("hidden", str(raised.exception))

    def test_sanitizes_nested_url_query_and_fragment(self):
        value = sanitize_tree(
            {
                "hlsUrl": "https://media.test/live.m3u8?token=secret#fragment",
                "unexpected_link_field": "https://media.test/frame?Credential=secret",
                "credential_url": "https://user:password@media.test/frame?secret=1",
            }
        )
        self.assertEqual(value["hlsUrl"], "https://media.test/live.m3u8")
        self.assertEqual(
            value["unexpected_link_field"], "https://media.test/frame"
        )
        self.assertEqual(value["credential_url"], "https://media.test/frame")

    def test_prunes_only_old_canonical_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for hour in range(4):
                snapshot = root / f"20260711T{hour:02d}0000Z"
                snapshot.mkdir()
                (snapshot / "manifest.json").write_text(json.dumps({
                    "schema": "v2x-detection-corpus-snapshot/v1"
                }))
            unrelated = root / "20260711T000000Z-ledger"
            unrelated.mkdir()
            removed = prune_snapshots(root, 2)
            self.assertEqual(removed, ["20260711T000000Z", "20260711T010000Z"])
            self.assertTrue(unrelated.exists())
            self.assertTrue((root / "20260711T030000Z").exists())

    @patch("export_detection_corpus._fetch_json_bytes")
    def test_rejects_symlink_output_root(self, fetch):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real"
            real.mkdir()
            linked = Path(directory) / "linked"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ExportError, "symlink"):
                export_detection_corpus("https://api.test", linked, now=NOW)
            fetch.assert_not_called()

    @patch("export_detection_corpus._fetch_json_bytes")
    @patch("export_detection_corpus.shutil.disk_usage")
    def test_refuses_export_below_free_space_floor(self, disk_usage, fetch):
        disk_usage.return_value = type(
            "Usage", (), {"total": 100, "used": 90, "free": 10}
        )()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ExportError, "free space"):
                export_detection_corpus(
                    "https://api.test",
                    directory,
                    now=NOW,
                    minimum_free_bytes=11,
                )
        fetch.assert_not_called()

    @patch("export_detection_corpus._fetch_json_bytes")
    def test_exports_reconciled_pages_atomically(self, fetch):
        first = {"items": [row("event-1", offset_minutes=5)], "next": "page-2"}
        second = {"items": [row("event-2", "ch4", 2)], "next": None}
        timeline_value = timeline(2)
        fetch.side_effect = [response(first), response(second), response(timeline_value)]
        with tempfile.TemporaryDirectory() as directory:
            output = export_detection_corpus(
                "https://api.test", directory, now=NOW
            )
            self.assertEqual(output.name, "20260711T080000Z")
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["counts"]["items"], 2)
            self.assertEqual(manifest["counts"]["trusted_vehicles"], 2)
            self.assertFalse(manifest["acceptance_eligible"])
            detections = (output / "detections.ndjson").read_text()
            self.assertNotIn("Signature", detections)
            self.assertNotIn("secret", detections)
            self.assertIn("https://media.test/frame.jpg", detections)
            sums = (output / "SHA256SUMS").read_text()
            self.assertIn("manifest.json", sums)
            expected = hashlib.sha256(
                (output / "detections.ndjson").read_bytes()
            ).hexdigest()
            self.assertEqual(manifest["artifacts"]["detections.ndjson"], expected)

    @patch("export_detection_corpus._fetch_json_bytes")
    def test_rejects_duplicate_event_ids(self, fetch):
        payload = {"items": [row("duplicate"), row("duplicate")], "next": None}
        fetch.side_effect = [response(payload)]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ExportError, "duplicate event IDs"):
                export_detection_corpus("https://api.test", directory, now=NOW)

    @patch("export_detection_corpus._fetch_json_bytes")
    def test_rejects_timeline_range_mismatch(self, fetch):
        payload = {"items": [row("event-1")], "next": None}
        timeline_value = timeline(2)
        fetch.side_effect = [response(payload), response(timeline_value)]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ExportError, "count mismatch"):
                export_detection_corpus("https://api.test", directory, now=NOW)

    @patch("export_detection_corpus._fetch_json_bytes")
    def test_rejects_truncated_timeline(self, fetch):
        payload = {"items": [row("event-1")], "next": None}
        timeline_value = timeline(1, truncated=True)
        fetch.side_effect = [response(payload), response(timeline_value)]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ExportError, "truncated"):
                export_detection_corpus("https://api.test", directory, now=NOW)

    @patch("export_detection_corpus._fetch_json_bytes")
    def test_rejects_timeline_for_a_different_window(self, fetch):
        payload = {"items": [row("event-1")], "next": None}
        timeline_value = timeline(1, start="2026-07-10T09:00:00.000Z")
        fetch.side_effect = [response(payload), response(timeline_value)]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ExportError, "window/bucket"):
                export_detection_corpus("https://api.test", directory, now=NOW)


if __name__ == "__main__":
    unittest.main()
