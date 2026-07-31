import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from build_exact_frame_capture_report import (  # noqa: E402
    ExactCaptureError,
    build,
)


class ExactFrameCaptureReportTests(unittest.TestCase):
    def make_report(self, root, event="event-1", camera="ch1", error=0.0):
        frame = root / f"{event}.jpg"
        frame.write_bytes(b"exact-frame")
        digest = hashlib.sha256(frame.read_bytes()).hexdigest()
        report = {
            "schema_version": 1,
            "verifier": "historical_video_detection_correlation",
            "safety": {"signed_urls_emitted": False},
            "detection": {
                "event_id": event,
                "object_id": "same-car-1",
                "object_type": "car",
                "camera_id": camera,
                "persisted_media_timestamp": "2026-07-12T01:00:00.000Z",
                "saved_bbox": [1, 2, 10, 20],
            },
            "frame": {
                "path": str(frame),
                "sha256": digest,
                "dimensions": [1280, 960],
                "absolute_error_ms": error,
                "selected_media_timestamp": "2026-07-12T01:00:00.000Z",
            },
            "result": {"gate_passed": True},
        }
        report_path = root / f"{event}.json"
        report_path.write_text(json.dumps(report))
        return report_path

    def test_builds_hash_bound_exact_capture(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = build(
                [
                    self.make_report(root, "event-2", "ch2"),
                    self.make_report(root, "event-1", "ch1"),
                ]
            )
            self.assertEqual(result["summary"]["event_count"], 2)
            self.assertEqual(result["summary"]["cameras"], ["ch1", "ch2"])
            self.assertTrue(
                all(
                    event["bbox_frame_binding"]["applies_to_selected_frame"]
                    for event in result["events"]
                )
            )
            self.assertFalse(result["acceptance_eligible"])

    def test_rejects_non_exact_frame_and_tampered_frame(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report = self.make_report(root, error=1.1)
            with self.assertRaisesRegex(ExactCaptureError, "not exact enough"):
                build([report])

            report = self.make_report(root, event="event-2")
            (root / "event-2.jpg").write_bytes(b"tampered")
            with self.assertRaisesRegex(ExactCaptureError, "hash does not match"):
                build([report])


if __name__ == "__main__":
    unittest.main()
