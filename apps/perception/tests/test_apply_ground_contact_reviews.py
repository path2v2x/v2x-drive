import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from apply_ground_contact_reviews import ReviewError, apply_reviews  # noqa: E402
from export_detection_corpus import canonical_json_bytes  # noqa: E402


class GroundContactReviewTests(unittest.TestCase):
    def setUp(self):
        self.observation = {
            "schema": "v2x-detection-observation/v2",
            "event_id": "event-1",
            "camera_id": "ch3",
            "object_id": "object-1",
            "object_type": "car",
            "media_timestamp_utc": "2026-07-11T08:00:00.000Z",
            "native_resolution": [1000, 800],
            "bbox": {"x1": 100.0, "y1": 200.0, "x2": 300.0, "y2": 500.0},
            "ground_contact": {
                "method": "bbox_bottom_center_diagnostic",
                "pixel": [200.0, 500.0],
                "reviewed": False,
            },
            "acceptance_eligible": False,
            "ineligibility_reasons": ["ground_contact_not_reviewed"],
        }

    def write_ledger(self, root):
        ledger = Path(root) / "ledger"
        ledger.mkdir()
        body = canonical_json_bytes(self.observation)
        (ledger / "observations.ndjson").write_bytes(body)
        (ledger / "manifest.json").write_text(
            json.dumps(
                {
                    "schema": "v2x-detection-observation-ledger/v2",
                    "observations_sha256": hashlib.sha256(body).hexdigest(),
                    "counts": {"observations": 1, "acceptance_eligible": 0},
                }
            )
        )
        return ledger, hashlib.sha256(body).hexdigest()

    def write_frame_evidence(self, root):
        frame = Path(root) / "event-1.jpg"
        frame.write_bytes(b"retained-frame-evidence")
        frame_hash = hashlib.sha256(frame.read_bytes()).hexdigest()
        report = Path(root) / "event-1-report.json"
        report_value = {
            "schema_version": 1,
            "verifier": "historical_video_detection_correlation",
            "detection": {
                "camera_id": "ch3",
                "event_id": "event-1",
                "object_id": "object-1",
                "object_type": "car",
                "persisted_media_timestamp": "2026-07-11T08:00:00.000Z",
                "saved_bbox": [100.0, 200.0, 300.0, 500.0],
            },
            "frame": {
                "path": str(frame.resolve()),
                "sha256": frame_hash,
                "selected_media_timestamp": "2026-07-11T08:00:00.000Z",
                "absolute_error_ms": 0.0,
                "dimensions": [1000, 800],
            },
            "result": {
                "gate_passed": True,
                "trusted_media_timestamp": True,
                "frame_timing_check_passed": True,
            },
            "safety": {"signed_urls_emitted": False},
        }
        report.write_text(json.dumps(report_value))
        return {
            "path": str(frame),
            "sha256": frame_hash,
            "verifier_report_path": str(report),
            "verifier_report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        }

    def review(self, source_hash, frame_evidence, reviewer_kind="human"):
        return {
            "schema": "v2x-ground-contact-review/v1",
            "source_observations_sha256": source_hash,
            "reviewer": {"kind": reviewer_kind, "id": "reviewer@example.test"},
            "entries": [
                {
                    "event_id": "event-1",
                    "provenance": "manually_verified_wheel_contact",
                    "frame_evidence": frame_evidence,
                    "pixel": [210.0, 490.0],
                    "range_band": "mid",
                    "covariance_px2": [[4.0, 0.0], [0.0, 9.0]],
                }
            ],
        }

    def test_applies_human_review_and_makes_complete_row_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source_hash = self.write_ledger(directory)
            review = Path(directory) / "review.json"
            frame_evidence = self.write_frame_evidence(directory)
            review.write_text(json.dumps(self.review(source_hash, frame_evidence)))
            output = Path(directory) / "reviewed"
            apply_reviews(ledger, review, output)
            row = json.loads((output / "observations.ndjson").read_text())
            self.assertTrue(row["acceptance_eligible"])
            self.assertEqual(
                row["ground_contact"]["method"], "reviewed_wheel_road_contact"
            )
            self.assertEqual(
                row["ground_contact"]["frame_sha256"], frame_evidence["sha256"]
            )

    def test_rejects_model_only_review_and_non_psd_covariance(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source_hash = self.write_ledger(directory)
            frame_evidence = self.write_frame_evidence(directory)
            review_value = self.review(
                source_hash, frame_evidence, reviewer_kind="model"
            )
            review = Path(directory) / "review.json"
            review.write_text(json.dumps(review_value))
            with self.assertRaisesRegex(ReviewError, "human reviewer"):
                apply_reviews(ledger, review, Path(directory) / "out")
            review_value = self.review(source_hash, frame_evidence)
            review_value["entries"][0]["covariance_px2"] = [[1, 2], [2, 1]]
            review.write_text(json.dumps(review_value))
            with self.assertRaisesRegex(ReviewError, "positive definite"):
                apply_reviews(ledger, review, Path(directory) / "out")

    def test_rejects_unbound_or_tampered_frame_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, source_hash = self.write_ledger(directory)
            frame_evidence = self.write_frame_evidence(directory)
            review = Path(directory) / "review.json"
            review.write_text(json.dumps(self.review(source_hash, frame_evidence)))
            Path(frame_evidence["path"]).write_bytes(b"tampered")
            with self.assertRaisesRegex(ReviewError, "frame hash"):
                apply_reviews(ledger, review, Path(directory) / "out")


if __name__ == "__main__":
    unittest.main()
