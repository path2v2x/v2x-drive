from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from build_detection_observation_ledger import (  # noqa: E402
    LedgerError,
    build_ledger,
)
from export_detection_corpus import canonical_json_bytes  # noqa: E402


def detection():
    timestamp = "2026-07-11T03:32:21.022Z"
    return {
        "event_id": "event-1",
        "object_id": "global_car_run_1",
        "object_type": "car",
        "device_id": "cam-001-ch3",
        "timestamp_schema_version": 2,
        "timestamp_utc": timestamp,
        "media_timestamp_utc": timestamp,
        "media_time_trusted": True,
        "media_clock_status": "matched",
        "media_clock": {
            "source": "hls_ext_x_program_date_time",
            "schema_version": 1,
        },
        "gps_location": {"latitude": 37.9, "longitude": -122.3},
        "camera_data": {
            "bifocal_metadata": {
                "frame": 10,
                "bbox": {"x1": 10, "y1": 20, "x2": 110, "y2": 220},
                "world_position": {"X": 1.0, "Z": 20.0},
            }
        },
    }


def camera_config():
    return {
        "site": {"lat": 37.9, "lon": -122.3},
        "cameras": [
            {
                "id": "ch3",
                "intrinsics": {
                    "fx": 1000.0,
                    "fy": 1000.0,
                    "cx": 1280.0,
                    "cy": 960.0,
                    "width": 2560,
                    "height": 1920,
                },
            }
        ],
    }


class ObservationLedgerTests(unittest.TestCase):
    def make_snapshot(self, root, rows):
        snapshot = Path(root) / "snapshot"
        snapshot.mkdir()
        body = b"".join(canonical_json_bytes(row) for row in rows)
        (snapshot / "detections.ndjson").write_bytes(body)
        manifest = {
            "schema": "v2x-detection-corpus-snapshot/v1",
            "window": {
                "start": "2026-07-10T08:00:00.000Z",
                "end": "2026-07-11T08:00:00.000Z",
            },
            "counts": {"items": len(rows)},
            "artifacts": {"detections.ndjson": hashlib.sha256(body).hexdigest()},
        }
        (snapshot / "manifest.json").write_text(json.dumps(manifest))
        return snapshot

    def test_builds_pixel_ledger_and_quarantines_derived_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.make_snapshot(directory, [detection()])
            cameras = Path(directory) / "cameras.json"
            cameras.write_text(json.dumps(camera_config()))
            output = Path(directory) / "ledger"
            build_ledger(snapshot, cameras, output)
            observation = json.loads(
                (output / "observations.ndjson").read_text().splitlines()[0]
            )
            self.assertEqual(observation["ground_contact"]["pixel"], [60.0, 220.0])
            self.assertEqual(
                observation["derived_baseline"]["warning"], "not_optimizer_truth"
            )
            self.assertNotIn("optimizer_target", observation)
            self.assertFalse(observation["acceptance_eligible"])
            self.assertIn(
                "missing_measured_intrinsics", observation["ineligibility_reasons"]
            )
            self.assertIn(
                "missing_raw_observation_provenance",
                observation["ineligibility_reasons"],
            )
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertTrue(
                manifest["optimizer_contract"]["derived_baseline_forbidden_as_target"]
            )

    def test_rejects_tampered_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.make_snapshot(directory, [detection()])
            with (snapshot / "detections.ndjson").open("ab") as handle:
                handle.write(b"{}\n")
            cameras = Path(directory) / "cameras.json"
            cameras.write_text(json.dumps(camera_config()))
            with self.assertRaisesRegex(LedgerError, "hash does not match"):
                build_ledger(snapshot, cameras, Path(directory) / "ledger")

    def test_prefers_hash_bound_raw_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            row = detection()
            row["raw_observation"] = {
                "schema": "v2x-raw-detection-observation/v1",
                "native_resolution": [2560, 1920],
                "bbox": {"x1": 20, "y1": 30, "x2": 220, "y2": 330},
                "ground_contact": {
                    "method": "bbox_bottom_center_diagnostic",
                    "pixel": [120, 330],
                    "reviewed": False,
                },
                "fingerprints": {
                    "cameras_json_sha256": "a" * 64,
                    "camera_config_sha256": "b" * 64,
                    "detector_model_sha256": "c" * 64,
                },
            }
            snapshot = self.make_snapshot(directory, [row])
            cameras = Path(directory) / "cameras.json"
            cameras.write_text(json.dumps(camera_config()))
            output = Path(directory) / "ledger"
            build_ledger(snapshot, cameras, output)
            observation = json.loads((output / "observations.ndjson").read_text())
            self.assertEqual(observation["bbox"]["x1"], 20.0)
            self.assertEqual(observation["ground_contact"]["pixel"], [120.0, 330.0])
            self.assertNotIn(
                "missing_raw_observation_provenance",
                observation["ineligibility_reasons"],
            )
            self.assertEqual(
                observation["source"]["emitted_fingerprints"]["detector_model_sha256"],
                "c" * 64,
            )


if __name__ == "__main__":
    unittest.main()
