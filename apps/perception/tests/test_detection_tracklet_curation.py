import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from apply_tracklet_reviews import (  # noqa: E402
    TrackletReviewError,
    apply_tracklet_reviews,
)
from export_detection_corpus import canonical_json_bytes  # noqa: E402
from freeze_track_split import freeze_split  # noqa: E402
from propose_detection_tracklets import propose_tracklets  # noqa: E402


class DetectionTrackletCurationTests(unittest.TestCase):
    def ledger(self, root, *, eligible=True, groups=32):
        root = Path(root)
        ledger = root / "ledger"
        ledger.mkdir()
        rows = []
        for group in range(groups):
            day = 12 if group >= groups - 2 else 11
            for event in range(3):
                rows.append({
                    "schema": "v2x-detection-observation/v2",
                    "event_id": f"event-{group}-{event}",
                    "object_id": f"object-{group}",
                    "camera_id": f"ch{group % 4 + 1}",
                    "media_timestamp_utc": f"2026-07-{day:02d}T08:{group % 50:02d}:{event:02d}.000Z",
                    "acceptance_eligible": eligible,
                })
        raw = b"".join(canonical_json_bytes(row) for row in rows)
        (ledger / "observations.ndjson").write_bytes(raw)
        (ledger / "manifest.json").write_text(json.dumps({
            "schema": "v2x-detection-observation-ledger/v2",
            "observations_sha256": hashlib.sha256(raw).hexdigest(),
        }))
        return ledger, hashlib.sha256(raw).hexdigest()

    def review_value(self, proposals_path, reviewer_kind="human"):
        proposals = json.loads(proposals_path.read_text())
        entries = []
        for index, proposal in enumerate(proposals["proposals"]):
            entries.append({
                "proposal_id": proposal["proposal_id"],
                "decision": "accept",
                "lane_path_id": f"lane-{index % 3}",
                "evidence_group_id": f"physical-object-{index}",
                "includes_turn": index % 7 == 0,
                "motion_direction_deg": float(index * 7),
                "checks": {
                    "moving": True,
                    "occlusion_free": True,
                    "not_truncated": True,
                    "optical_flow_consistent": True,
                },
            })
        return {
            "schema": "v2x-tracklet-review/v1",
            "source_proposals_sha256": hashlib.sha256(proposals_path.read_bytes()).hexdigest(),
            "reviewer": {"kind": reviewer_kind, "id": "reviewer@example.test"},
            "entries": entries,
        }

    def test_end_to_end_human_curation_and_atomic_later_day_split(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, observations_hash = self.ledger(directory)
            proposals = Path(directory) / "proposals.json"
            propose_tracklets(ledger, proposals)
            proposal_value = json.loads(proposals.read_text())
            self.assertEqual(len(proposal_value["proposals"]), 32)
            self.assertTrue(all(value["model_identity_is_truth"] is False for value in proposal_value["proposals"]))
            review = Path(directory) / "review.json"
            review.write_text(json.dumps(self.review_value(proposals)))
            tracklets = Path(directory) / "tracklets.json"
            apply_tracklet_reviews(ledger, proposals, review, tracklets)
            tracklet_value = json.loads(tracklets.read_text())
            self.assertEqual(tracklet_value["source_observations_sha256"], observations_hash)

            split = Path(directory) / "split.json"
            freeze_split(tracklets, split, "2026-07-12", "stable-test-seed")
            split_value = json.loads(split.read_text())
            self.assertEqual(set(split_value["assignments"].values()), {"fit", "validation", "holdout"})
            by_id = {value["tracklet_id"]: value for value in tracklet_value["tracklets"]}
            for tracklet_id, partition in split_value["assignments"].items():
                if by_id[tracklet_id]["start_media_timestamp_utc"].startswith("2026-07-12"):
                    self.assertEqual(partition, "holdout")

    def test_model_review_and_ineligible_contacts_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, _hash = self.ledger(directory, eligible=False, groups=1)
            proposals = Path(directory) / "proposals.json"
            propose_tracklets(ledger, proposals)
            review = Path(directory) / "review.json"
            review.write_text(json.dumps(self.review_value(proposals, reviewer_kind="model")))
            with self.assertRaisesRegex(TrackletReviewError, "named human"):
                apply_tracklet_reviews(ledger, proposals, review, Path(directory) / "tracklets.json")
            review.write_text(json.dumps(self.review_value(proposals)))
            with self.assertRaisesRegex(TrackletReviewError, "ineligible observations"):
                apply_tracklet_reviews(ledger, proposals, review, Path(directory) / "tracklets.json")


if __name__ == "__main__":
    unittest.main()
