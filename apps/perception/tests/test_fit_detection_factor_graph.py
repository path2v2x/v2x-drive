import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from export_detection_corpus import canonical_json_bytes  # noqa: E402
from fit_detection_factor_graph import validate_inputs  # noqa: E402


CAMERAS = ("ch1", "ch2", "ch3", "ch4")


def measured_camera(camera_id, root):
    source_paths = []
    hashes = []
    for index in range(12):
        path = Path(root) / f"{camera_id}-intrinsics-{index}.bin"
        path.write_bytes(f"{camera_id}-image-{index}".encode())
        source_paths.append(str(path))
        hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
    calibration = {
        "method": "charuco",
        "source_images_sha256": hashes,
        "image_count": len(hashes),
        "rms_reprojection_error_px": 0.4,
        "resolution": [1000, 800],
        "camera_matrix": [[1000.0, 0.0, 500.0], [0.0, 1000.0, 400.0], [0.0, 0.0, 1.0]],
        "distortion": {"k1": 0.0, "k2": 0.0, "p1": 0.0, "p2": 0.0, "k3": 0.0},
    }
    artifact = Path(root) / f"{camera_id}-intrinsics.json"
    artifact.write_text(json.dumps(calibration, sort_keys=True))
    report = Path(root) / f"{camera_id}-intrinsics-report.json"
    report.write_text(json.dumps({
        "schema": "v2x-charuco-calibration-report/v1",
        "accepted": [{"sha256": value} for value in hashes[:10]],
        "holdouts": [{"sha256": value} for value in hashes[10:]],
        "holdout_metrics": {"rmse_px": 0.5, "max_error_px": 1.5},
    }))
    calibration.update({
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "artifact_path": str(artifact),
        "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "report_path": str(report),
        "source_image_paths": source_paths,
    })
    return {
        "id": camera_id,
        "intrinsics": {
            "fx": 1000.0,
            "fy": 1000.0,
            "cx": 500.0,
            "cy": 400.0,
            "width": 1000,
            "height": 800,
        },
        "intrinsics_calibration": calibration,
    }


def observation(event_id, camera_id, pixel, band, timestamp):
    return {
        "schema": "v2x-detection-observation/v2",
        "event_id": event_id,
        "camera_id": camera_id,
        "object_id": f"object-{event_id}",
        "media_timestamp_utc": timestamp,
        "native_resolution": [1000, 800],
        "ground_contact": {
            "method": "reviewed_wheel_road_contact",
            "reviewed": True,
            "provenance": "manually_verified_wheel_contact",
            "pixel": pixel,
            "covariance_px2": [[4.0, 0.0], [0.0, 4.0]],
            "range_band": band,
            "frame_sha256": hashlib.sha256(event_id.encode()).hexdigest(),
        },
        "acceptance_eligible": True,
        "derived_baseline": {"gps": {"latitude": 0.0, "longitude": 0.0}},
    }


class DetectionFactorGraphPreflightTests(unittest.TestCase):
    def fixture(self, root, *, associations=True):
        root = Path(root)
        cameras = {
            "site": {},
            "cameras": [measured_camera(camera_id, root) for camera_id in CAMERAS],
        }
        cameras_path = root / "cameras.json"
        cameras_path.write_text(json.dumps(cameras))

        rows = []
        tracklet_rows = []
        proposal_rows = []
        review_entries = []
        reviewer = {"kind": "human", "id": "reviewer@example.test"}
        assignments = {}
        evidence_group_assignments = {}
        for camera_index, camera_id in enumerate(CAMERAS):
            for track_index in range(30):
                tracklet_id = f"{camera_id}-track-{track_index:02d}"
                evidence_group_id = f"physical-object-{track_index:02d}"
                event_ids = []
                for event_index in range(3):
                    event_id = f"{tracklet_id}-event-{event_index}"
                    # The complete corpus covers >60% width, >40% height and
                    # exactly 20/50/30 percent near/mid/far per camera.
                    sequence = track_index * 3 + event_index
                    band = "near" if sequence < 18 else "mid" if sequence < 63 else "far"
                    x = 100.0 + 800.0 * (sequence % 10) / 9.0
                    y = 180.0 + 440.0 * ((sequence // 10) % 9) / 8.0
                    day = 12 if track_index >= 27 else 11
                    timestamp = f"2026-07-{day:02d}T08:{track_index:02d}:{event_index:02d}.000Z"
                    rows.append(
                        observation(event_id, camera_id, [x, y], band, timestamp)
                    )
                    event_ids.append(event_id)
                tracklet_rows.append({
                    "tracklet_id": tracklet_id,
                    "camera_id": camera_id,
                    "event_ids": event_ids,
                    "start_media_timestamp_utc": rows[-3]["media_timestamp_utc"],
                    "end_media_timestamp_utc": rows[-1]["media_timestamp_utc"],
                    "lane_path_id": f"lane-{track_index % 3}",
                    "includes_turn": track_index % 10 == 0,
                    "motion_direction_deg": float(camera_index * 45 + track_index * 2),
                    "evidence_group_id": evidence_group_id,
                    "review": {
                        "moving": True,
                        "occlusion_free": True,
                        "not_truncated": True,
                        "optical_flow_consistent": True,
                        "reviewer": reviewer,
                    },
                })
                proposal_rows.append({
                    "proposal_id": tracklet_id,
                    "camera_id": camera_id,
                    "event_ids": event_ids,
                    "start_media_timestamp_utc": rows[-3]["media_timestamp_utc"],
                    "end_media_timestamp_utc": rows[-1]["media_timestamp_utc"],
                    "status": "ready_for_human_track_review",
                })
                review_entry = {
                    "proposal_id": tracklet_id,
                    "decision": "accept",
                    "lane_path_id": f"lane-{track_index % 3}",
                    "evidence_group_id": evidence_group_id,
                    "includes_turn": track_index % 10 == 0,
                    "motion_direction_deg": float(
                        camera_index * 45 + track_index * 2
                    ),
                    "checks": {
                        "moving": True,
                        "occlusion_free": True,
                        "not_truncated": True,
                        "optical_flow_consistent": True,
                    },
                }
                review_entries.append(review_entry)
                tracklet_rows[-1]["review"]["review_entry_sha256"] = (
                    hashlib.sha256(json.dumps(
                        review_entry,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()).hexdigest()
                )
                assignments[tracklet_id] = (
                    "holdout"
                    if track_index >= 27
                    else ("fit", "validation")[track_index % 2]
                )
                evidence_group_assignments[evidence_group_id] = assignments[
                    tracklet_id
                ]

        ledger = root / "ledger"
        ledger.mkdir()
        observations_raw = b"".join(canonical_json_bytes(row) for row in rows)
        (ledger / "observations.ndjson").write_bytes(observations_raw)
        (ledger / "manifest.json").write_text(json.dumps({
            "schema": "v2x-detection-observation-ledger/v2",
            "observations_sha256": hashlib.sha256(observations_raw).hexdigest(),
        }))

        proposals_path = root / "proposals.json"
        proposals_path.write_text(json.dumps({
            "schema": "v2x-tracklet-proposals/v1",
            "source_observations_sha256": hashlib.sha256(
                observations_raw
            ).hexdigest(),
            "proposals": proposal_rows,
        }))
        review_path = root / "review.json"
        review_path.write_text(json.dumps({
            "schema": "v2x-tracklet-review/v1",
            "source_proposals_sha256": hashlib.sha256(
                proposals_path.read_bytes()
            ).hexdigest(),
            "reviewer": reviewer,
            "entries": review_entries,
        }))
        tracklets = {
            "schema": "v2x-tracklet-set/v1",
            "source_observations_sha256": hashlib.sha256(observations_raw).hexdigest(),
            "source_proposals_path": str(proposals_path),
            "source_proposals_sha256": hashlib.sha256(
                proposals_path.read_bytes()
            ).hexdigest(),
            "review_path": str(review_path),
            "review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
            "reviewer": reviewer,
            "tracklets": tracklet_rows,
        }
        tracklets_path = root / "tracklets.json"
        tracklets_path.write_text(json.dumps(tracklets))
        tracklets_hash = hashlib.sha256(tracklets_path.read_bytes()).hexdigest()

        split_path = root / "split.json"
        split_path.write_text(json.dumps({
            "schema": "v2x-track-split/v1",
            "source_tracklets_sha256": tracklets_hash,
            "holdout_day_utc": "2026-07-12",
            "assignments": assignments,
            "evidence_group_assignments": evidence_group_assignments,
        }))

        association_path = None
        if associations:
            association_rows = []
            partition_tracks = {
                "fit": list(range(0, 27, 2)),
                "validation": list(range(1, 27, 2)),
                "holdout": [27, 28, 29],
            }
            for association_index in range(100):
                partition = ("fit", "validation", "holdout")[association_index % 3]
                choices = partition_tracks[partition]
                track_index = choices[(association_index // 3) % len(choices)]
                ids = [f"{camera_id}-track-{track_index:02d}" for camera_id in CAMERAS]
                association_rows.append({
                    "association_id": f"association-{association_index}",
                    "tracklet_ids": ids,
                    "evidence_group_id": f"physical-object-{track_index:02d}",
                    "evidence": {"reviewed": True, "appearance_similarity": 0.95},
                })
            association_path = root / "associations.json"
            reviewed_subset = root / "reviewed-association-subset.json"
            reviewed_subset.write_text(json.dumps({
                "schema": "v2x-reviewed-association-subset/v1",
                "source_association_candidates_sha256": hashlib.sha256(
                    canonical_json_bytes(association_rows)
                ).hexdigest(),
                "true_positives": 100,
                "false_positives": 0,
                "entries": [
                    {
                        "association_id": value["association_id"],
                        "tracklet_ids": value["tracklet_ids"],
                        "label": "true_positive",
                    }
                    for value in association_rows
                ],
            }))
            association_path.write_text(json.dumps({
                "schema": "v2x-association-set/v1",
                "source_tracklets_sha256": tracklets_hash,
                "precision_evidence": {
                    "precision": 1.0,
                    "reviewed_subset_path": str(reviewed_subset),
                    "reviewed_subset_sha256": hashlib.sha256(reviewed_subset.read_bytes()).hexdigest(),
                },
                "associations": association_rows,
            }))
        return ledger, tracklets_path, association_path, split_path, cameras_path

    def report(self, fixture):
        ledger, tracklets, associations, split, cameras = fixture
        return validate_inputs(ledger, tracklets, associations, split, cameras)

    @staticmethod
    def rebind_tracklets(fixture, mutate):
        tracklets = json.loads(fixture[1].read_text())
        mutate(tracklets)
        fixture[1].write_text(json.dumps(tracklets))
        split = json.loads(fixture[3].read_text())
        split["source_tracklets_sha256"] = hashlib.sha256(
            fixture[1].read_bytes()
        ).hexdigest()
        fixture[3].write_text(json.dumps(split))

    def test_complete_four_camera_fixture_passes_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(self.fixture(directory))
            self.assertTrue(report["gate_passed"], report["reasons"])
            self.assertEqual(report["counts"]["eligible_observations"], 360)
            self.assertTrue(all(value["passed"] for value in report["cameras"].values()))
            self.assertFalse(report["optimizer_contract"]["derived_baseline_parsed"])

    def test_derived_gps_mutation_cannot_change_semantic_preflight(self):
        with tempfile.TemporaryDirectory() as left_dir, tempfile.TemporaryDirectory() as right_dir:
            left = self.fixture(left_dir, associations=False)
            right = self.fixture(right_dir, associations=False)
            right_ledger = right[0]
            rows = [json.loads(line) for line in (right_ledger / "observations.ndjson").read_text().splitlines()]
            for index, row in enumerate(rows):
                row["derived_baseline"] = {"gps": {"latitude": 89.0, "longitude": -179.0}, "poison": index}
            raw = b"".join(canonical_json_bytes(row) for row in rows)
            (right_ledger / "observations.ndjson").write_bytes(raw)
            manifest = json.loads((right_ledger / "manifest.json").read_text())
            manifest["observations_sha256"] = hashlib.sha256(raw).hexdigest()
            (right_ledger / "manifest.json").write_text(json.dumps(manifest))
            tracklets_value = json.loads(right[1].read_text())
            observations_hash = hashlib.sha256(raw).hexdigest()
            proposals_path = Path(tracklets_value["source_proposals_path"])
            proposals_value = json.loads(proposals_path.read_text())
            proposals_value["source_observations_sha256"] = observations_hash
            proposals_path.write_text(json.dumps(proposals_value))
            proposals_hash = hashlib.sha256(
                proposals_path.read_bytes()
            ).hexdigest()
            review_path = Path(tracklets_value["review_path"])
            review_value = json.loads(review_path.read_text())
            review_value["source_proposals_sha256"] = proposals_hash
            review_path.write_text(json.dumps(review_value))
            tracklets_value["source_observations_sha256"] = observations_hash
            tracklets_value["source_proposals_sha256"] = proposals_hash
            tracklets_value["review_sha256"] = hashlib.sha256(
                review_path.read_bytes()
            ).hexdigest()
            right[1].write_text(json.dumps(tracklets_value))
            new_tracklets_hash = hashlib.sha256(right[1].read_bytes()).hexdigest()
            split_value = json.loads(right[3].read_text())
            split_value["source_tracklets_sha256"] = new_tracklets_hash
            right[3].write_text(json.dumps(split_value))

            left_report = self.report(left)
            right_report = self.report(right)
            for key in ("counts", "cameras", "reasons", "optimizer_contract", "gate_passed"):
                self.assertEqual(left_report[key], right_report[key])

    def test_cross_split_association_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            associations = json.loads(fixture[2].read_text())
            associations["associations"][0]["tracklet_ids"] = ["ch1-track-00", "ch2-track-01"]
            fixture[2].write_text(json.dumps(associations))
            report = self.report(fixture)
            self.assertFalse(report["gate_passed"])
            self.assertIn("association_crosses_split", report["reasons"])

    def test_cross_camera_associations_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(self.fixture(directory, associations=False))

            self.assertFalse(report["gate_passed"])
            self.assertIn(
                "reviewed_cross_camera_associations_missing", report["reasons"]
            )

    def test_association_must_declare_one_canonical_evidence_group(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            associations = json.loads(fixture[2].read_text())
            associations["associations"][0]["tracklet_ids"] = [
                "ch1-track-00",
                "ch2-track-02",
            ]
            fixture[2].write_text(json.dumps(associations))

            report = self.report(fixture)

            self.assertFalse(report["gate_passed"])
            self.assertIn(
                "association_evidence_group_mismatch", report["reasons"]
            )

    def test_evidence_group_relabel_cannot_rebind_named_review(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory, associations=False)
            self.rebind_tracklets(
                fixture,
                lambda tracklets: tracklets["tracklets"][0].update(
                    evidence_group_id="caller-relabelled-group"
                ),
            )

            report = self.report(fixture)

            self.assertFalse(report["gate_passed"])
            self.assertIn("tracklet_review_binding", report["reasons"])

    def test_malformed_event_and_association_ids_fail_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            self.rebind_tracklets(
                fixture,
                lambda tracklets: tracklets["tracklets"][0].update(
                    event_ids=[{}, "event-two", "event-three"]
                ),
            )
            associations = json.loads(fixture[2].read_text())
            associations["associations"][0]["association_id"] = {
                "malformed": True
            }
            fixture[2].write_text(json.dumps(associations))

            report = self.report(fixture)

            self.assertFalse(report["gate_passed"])
            self.assertIn("tracklet_invalid", report["reasons"])
            self.assertIn("association_invalid", report["reasons"])

    def test_tracklets_require_explicit_evidence_group_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory, associations=False)
            self.rebind_tracklets(
                fixture,
                lambda tracklets: tracklets["tracklets"][0].pop(
                    "evidence_group_id"
                ),
            )

            report = self.report(fixture)

            self.assertFalse(report["gate_passed"])
            self.assertIn("tracklet_evidence_group", report["reasons"])

    def test_evidence_group_cannot_leak_across_partitions(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory, associations=False)
            tracklets = json.loads(fixture[1].read_text())
            target = next(
                item
                for item in tracklets["tracklets"]
                if item["tracklet_id"] == "ch1-track-01"
            )
            target["evidence_group_id"] = "physical-object-00"
            review_path = Path(tracklets["review_path"])
            review = json.loads(review_path.read_text())
            review_entry = next(
                item
                for item in review["entries"]
                if item["proposal_id"] == "ch1-track-01"
            )
            review_entry["evidence_group_id"] = "physical-object-00"
            target["review"]["review_entry_sha256"] = hashlib.sha256(
                json.dumps(
                    review_entry, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            review_path.write_text(json.dumps(review))
            tracklets["review_sha256"] = hashlib.sha256(
                review_path.read_bytes()
            ).hexdigest()
            fixture[1].write_text(json.dumps(tracklets))
            split = json.loads(fixture[3].read_text())
            split["source_tracklets_sha256"] = hashlib.sha256(
                fixture[1].read_bytes()
            ).hexdigest()
            fixture[3].write_text(json.dumps(split))

            report = self.report(fixture)

            self.assertFalse(report["gate_passed"])
            self.assertIn("evidence_group_crosses_split", report["reasons"])
            self.assertFalse(
                report["optimizer_contract"]["whole_evidence_group_atomic"]
            )

    def test_declared_evidence_group_partition_must_match_recomputed_split(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory, associations=False)
            split = json.loads(fixture[3].read_text())
            split["evidence_group_assignments"]["physical-object-00"] = (
                "validation"
            )
            fixture[3].write_text(json.dumps(split))

            report = self.report(fixture)

            self.assertFalse(report["gate_passed"])
            self.assertIn("split_evidence_group_mismatch", report["reasons"])

    def test_bad_hash_and_timestamp_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory, associations=False)
            cameras = json.loads(fixture[4].read_text())
            cameras["cameras"][0]["intrinsics_calibration"]["artifact_sha256"] = "z" * 64
            fixture[4].write_text(json.dumps(cameras))
            rows = [json.loads(line) for line in (fixture[0] / "observations.ndjson").read_text().splitlines()]
            rows[0]["media_timestamp_utc"] = "not-a-time"
            raw = b"".join(canonical_json_bytes(row) for row in rows)
            (fixture[0] / "observations.ndjson").write_bytes(raw)
            manifest = json.loads((fixture[0] / "manifest.json").read_text())
            manifest["observations_sha256"] = hashlib.sha256(raw).hexdigest()
            (fixture[0] / "manifest.json").write_text(json.dumps(manifest))
            tracklets = json.loads(fixture[1].read_text())
            tracklets["source_observations_sha256"] = hashlib.sha256(raw).hexdigest()
            fixture[1].write_text(json.dumps(tracklets))
            split = json.loads(fixture[3].read_text())
            split["source_tracklets_sha256"] = hashlib.sha256(fixture[1].read_bytes()).hexdigest()
            fixture[3].write_text(json.dumps(split))
            report = self.report(fixture)
            self.assertIn("ch1:missing_measured_intrinsics", report["reasons"])
            self.assertEqual(report["counts"]["observation_rejections"]["media_timestamp"], 1)


if __name__ == "__main__":
    unittest.main()
