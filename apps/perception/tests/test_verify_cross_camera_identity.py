"""Acceptance tests for archived cross-camera vehicle identity proof."""

import copy
import sys
from pathlib import Path
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from verify_cross_camera_identity import (  # noqa: E402
    VerificationError,
    validate_report_pair,
)


def report(camera, timestamp):
    return {
        "result": {"gate_passed": True, "visual_corroborated": True},
        "detection": {
            "camera_id": camera,
            "object_id": "global_car_run_1",
            "object_type": "car",
            "persisted_media_timestamp": timestamp,
            "media_timestamp_trust": {
                "trusted": True,
                "timestamp_schema_version": 2,
                "source": "hls_ext_x_program_date_time",
            },
        },
    }


class CrossCameraIdentityTests(unittest.TestCase):
    def test_accepts_two_trusted_visual_reports_with_strong_appearance(self):
        evidence = validate_report_pair(
            report("ch4", "2026-07-11T00:05:12.069Z"),
            report("ch1", "2026-07-11T00:05:14.576Z"),
            0.6545,
        )
        self.assertEqual(evidence["cameras"], ["ch4", "ch1"])
        self.assertEqual(evidence["transit_seconds"], 2.507)
        self.assertEqual(evidence["appearance_similarity"], 0.6545)

    def test_rejects_shared_id_without_appearance_or_trusted_time(self):
        left = report("ch4", "2026-07-11T00:05:12.069Z")
        right = report("ch1", "2026-07-11T00:05:14.576Z")
        with self.assertRaisesRegex(VerificationError, "below threshold"):
            validate_report_pair(left, right, 0.59)
        untrusted = copy.deepcopy(right)
        untrusted["detection"]["media_timestamp_trust"]["trusted"] = False
        with self.assertRaisesRegex(VerificationError, "trusted schema-v2"):
            validate_report_pair(left, untrusted, 0.9)

    def test_rejects_same_camera_or_excessive_transit(self):
        left = report("ch4", "2026-07-11T00:05:12.069Z")
        with self.assertRaisesRegex(VerificationError, "different cameras"):
            validate_report_pair(
                left,
                report("ch4", "2026-07-11T00:05:13.000Z"),
                0.9,
            )
        with self.assertRaisesRegex(VerificationError, "transit time"):
            validate_report_pair(
                left,
                report("ch1", "2026-07-11T00:06:12.069Z"),
                0.9,
            )


if __name__ == "__main__":
    unittest.main()
