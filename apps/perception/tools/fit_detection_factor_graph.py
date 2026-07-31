#!/usr/bin/env python3
"""Fail-closed preflight and offline detection factor-graph entry point.

The current command implements the complete hash/data/observability contract.
It writes a deterministic preflight report and refuses optimization until
reviewed contacts, measured optics, tracklets, associations, and a frozen split
all pass. Derived GPS/local-XZ values are intentionally never parsed.
"""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from datetime import datetime

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
from export_detection_corpus import canonical_json_bytes, sha256_bytes  # noqa: E402

CAMERAS = ("ch1", "ch2", "ch3", "ch4")
SPLITS = frozenset({"fit", "validation", "holdout"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ACCEPT_REVIEW_ENTRY_KEYS = frozenset({
    "proposal_id",
    "decision",
    "lane_path_id",
    "evidence_group_id",
    "includes_turn",
    "motion_direction_deg",
    "checks",
})


class FactorGraphError(RuntimeError):
    pass


def load_object(path, label):
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise FactorGraphError(f"{label} is unreadable or invalid") from exc
    if not isinstance(value, dict):
        raise FactorGraphError(f"{label} is not an object")
    return path, raw, value


def verify_bound_file(base_directory, descriptor, path_key, hash_key, label,
                      *, require_json=False):
    value = descriptor.get(path_key) if isinstance(descriptor, dict) else None
    expected = descriptor.get(hash_key) if isinstance(descriptor, dict) else None
    if not isinstance(value, str) or not value.strip() or not sha256_valid(expected):
        raise FactorGraphError(f"{label} descriptor is incomplete")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(base_directory) / path
    try:
        path = path.resolve(strict=True)
        raw = path.read_bytes()
    except OSError as exc:
        raise FactorGraphError(f"{label} is unreadable") from exc
    if sha256_bytes(raw) != expected:
        raise FactorGraphError(f"{label} hash does not match")
    parsed = None
    if require_json:
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FactorGraphError(f"{label} is invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise FactorGraphError(f"{label} JSON is not an object")
    return path, raw, parsed


def verify_intrinsics_files(camera, base_directory):
    calibration = camera["intrinsics_calibration"]
    _artifact_path, artifact_raw, artifact = verify_bound_file(
        base_directory,
        calibration,
        "artifact_path",
        "artifact_sha256",
        f"{camera['id']} intrinsics artifact",
        require_json=True,
    )
    _report_path, report_raw, report = verify_bound_file(
        base_directory,
        calibration,
        "report_path",
        "report_sha256",
        f"{camera['id']} intrinsics report",
        require_json=True,
    )
    declared = {
        key: calibration[key]
        for key in (
            "method",
            "image_count",
            "source_images_sha256",
            "rms_reprojection_error_px",
            "resolution",
            "camera_matrix",
            "distortion",
        )
    }
    if artifact != declared:
        raise FactorGraphError(
            f"{camera['id']} intrinsics artifact does not match camera config"
        )
    accepted = report.get("accepted")
    holdouts = report.get("holdouts")
    metrics = report.get("holdout_metrics")
    try:
        holdout_rmse = float(metrics["rmse_px"])
        holdout_max = float(metrics["max_error_px"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise FactorGraphError(
            f"{camera['id']} intrinsics holdout metrics are invalid"
        ) from exc
    expected_report_schema = {
        "checkerboard": "v2x-checkerboard-calibration-report/v1",
        "charuco": "v2x-charuco-calibration-report/v1",
    }.get(calibration.get("method"))
    if (
        report.get("schema") != expected_report_schema
        or not isinstance(accepted, list)
        or len(accepted) < 10
        or not isinstance(holdouts, list)
        or len(holdouts) < 2
        or not math.isfinite(holdout_rmse)
        or not math.isfinite(holdout_max)
        or holdout_rmse > 2.0
        or holdout_max > 5.0
    ):
        raise FactorGraphError(
            f"{camera['id']} intrinsics holdout gate did not pass"
        )
    source_values = calibration.get("source_image_paths")
    expected_hashes = calibration["source_images_sha256"]
    if not isinstance(source_values, list) or len(source_values) != len(expected_hashes):
        raise FactorGraphError(f"{camera['id']} intrinsics source paths are incomplete")
    actual_hashes = []
    for value in source_values:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(base_directory) / path
        try:
            raw = path.resolve(strict=True).read_bytes()
        except OSError as exc:
            raise FactorGraphError(
                f"{camera['id']} intrinsics source image is unreadable"
            ) from exc
        actual_hashes.append(sha256_bytes(raw))
    report_hashes = {
        item.get("sha256")
        for item in accepted + holdouts
        if isinstance(item, dict)
    }
    if (
        len(set(actual_hashes)) != len(actual_hashes)
        or set(actual_hashes) != set(expected_hashes)
        or report_hashes != set(expected_hashes)
    ):
        raise FactorGraphError(f"{camera['id']} intrinsics source hashes do not match")
    return {
        "artifact_sha256": sha256_bytes(artifact_raw),
        "report_sha256": sha256_bytes(report_raw),
        "source_images_sha256": sorted(actual_hashes),
        "holdout_rmse_px": holdout_rmse,
        "holdout_max_px": holdout_max,
    }


def finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def sha256_valid(value):
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def utc_timestamp_valid(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def circular_direction_span(values):
    values = sorted(float(value) % 360.0 for value in values)
    if len(values) < 2:
        return 0.0
    gaps = [right - left for left, right in zip(values, values[1:])]
    gaps.append(values[0] + 360.0 - values[-1])
    return 360.0 - max(gaps)


def covariance_valid(value):
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(not isinstance(row, list) or len(row) != 2 for row in value)
        or any(not finite(item) for row in value for item in row)
    ):
        return False
    a, b = map(float, value[0])
    c, d = map(float, value[1])
    return (
        abs(b - c) <= 1e-9
        and a > 0.0
        and d > 0.0
        and a * d - b * c > 0.0
    )


def load_ledger(directory):
    directory = Path(directory).expanduser().resolve()
    _path, _raw, manifest = load_object(
        directory / "manifest.json", "ledger manifest"
    )
    if manifest.get("schema") != "v2x-detection-observation-ledger/v2":
        raise FactorGraphError("ledger schema is unsupported")
    raw = (directory / "observations.ndjson").read_bytes()
    if sha256_bytes(raw) != manifest.get("observations_sha256"):
        raise FactorGraphError("ledger observations hash does not match manifest")
    observations = {}
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FactorGraphError(f"ledger line {number} is invalid") from exc
        event_id = item.get("event_id") if isinstance(item, dict) else None
        if not isinstance(event_id, str) or not event_id or event_id in observations:
            raise FactorGraphError("ledger event IDs are missing or duplicated")
        observations[event_id] = item
    return manifest, observations, sha256_bytes(raw)


def valid_measured_intrinsics(camera):
    intrinsics = camera.get("intrinsics") if isinstance(camera, dict) else None
    calibration = (
        camera.get("intrinsics_calibration") if isinstance(camera, dict) else None
    )
    if not isinstance(intrinsics, dict) or not isinstance(calibration, dict):
        return False
    distortion = calibration.get("distortion")
    hashes = calibration.get("source_images_sha256")
    matrix = calibration.get("camera_matrix")
    try:
        expected = [
            [float(intrinsics["fx"]), 0.0, float(intrinsics["cx"])],
            [0.0, float(intrinsics["fy"]), float(intrinsics["cy"])],
            [0.0, 0.0, 1.0],
        ]
        observed = [[float(value) for value in row] for row in matrix]
        values = [float(distortion[key]) for key in ("k1", "k2", "p1", "p2", "k3")]
        rms = float(calibration["rms_reprojection_error_px"])
        image_count = int(calibration["image_count"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return (
        calibration.get("method") in {"checkerboard", "charuco"}
        and sha256_valid(calibration.get("artifact_sha256"))
        and isinstance(hashes, list)
        and len(hashes) >= 10
        and all(sha256_valid(value) for value in hashes)
        and len(set(hashes)) == len(hashes)
        and image_count == len(hashes)
        and 0.0 <= rms <= 2.0
        and calibration.get("resolution")
        == [intrinsics.get("width"), intrinsics.get("height")]
        and len(observed) == 3
        and all(len(row) == 3 for row in observed)
        and all(
            abs(observed[row][column] - expected[row][column]) <= 1e-9
            for row in range(3)
            for column in range(3)
        )
        and all(math.isfinite(value) for value in values)
    )


def validate_observation(item, cameras):
    reasons = []
    if item.get("schema") != "v2x-detection-observation/v2":
        reasons.append("schema")
    if item.get("acceptance_eligible") is not True:
        reasons.append("not_acceptance_eligible")
    camera_id = item.get("camera_id")
    if camera_id not in cameras:
        reasons.append("camera")
    resolution = item.get("native_resolution")
    if (
        not isinstance(resolution, list)
        or len(resolution) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in resolution)
    ):
        reasons.append("native_resolution")
    if not utc_timestamp_valid(item.get("media_timestamp_utc")):
        reasons.append("media_timestamp")
    contact = item.get("ground_contact")
    if not isinstance(contact, dict):
        reasons.append("contact_missing")
    else:
        pixel = contact.get("pixel")
        if (
            contact.get("method") != "reviewed_wheel_road_contact"
            or contact.get("reviewed") is not True
            or contact.get("provenance") != "manually_verified_wheel_contact"
            or not isinstance(pixel, list)
            or len(pixel) != 2
            or not all(finite(value) for value in pixel)
            or not covariance_valid(contact.get("covariance_px2"))
            or contact.get("range_band") not in {"near", "mid", "far"}
            or not sha256_valid(contact.get("frame_sha256"))
        ):
            reasons.append("contact_invalid")
    # Deliberately do not inspect derived_baseline. It is absent from the
    # optimizer's parsed representation and cannot influence preflight/fit.
    return reasons


def validate_inputs(ledger_dir, tracklets_path, associations_path, split_path, cameras_path):
    reasons = []
    ledger_manifest, observations, observations_hash = load_ledger(ledger_dir)
    cameras_file, cameras_raw, cameras_config = load_object(
        cameras_path, "camera config"
    )
    cameras = {
        camera.get("id"): camera
        for camera in cameras_config.get("cameras", [])
        if isinstance(camera, dict)
    }
    for camera_id in CAMERAS:
        camera = cameras.get(camera_id)
        if not valid_measured_intrinsics(camera):
            reasons.append(f"{camera_id}:missing_measured_intrinsics")
            continue
        try:
            verify_intrinsics_files(camera, cameras_file.parent)
        except FactorGraphError:
            reasons.append(f"{camera_id}:intrinsics_artifacts_unbound")

    eligible = {}
    observation_rejections = Counter()
    for event_id, item in observations.items():
        item_reasons = validate_observation(item, cameras)
        if item_reasons:
            observation_rejections.update(item_reasons)
        else:
            eligible[event_id] = {
                "event_id": event_id,
                "camera_id": item["camera_id"],
                "object_id": item.get("object_id"),
                "media_timestamp_utc": item.get("media_timestamp_utc"),
                "native_resolution": list(item["native_resolution"]),
                "pixel": [float(value) for value in item["ground_contact"]["pixel"]],
                "covariance_px2": item["ground_contact"]["covariance_px2"],
                "range_band": item["ground_contact"]["range_band"],
            }
    if not eligible:
        reasons.append("no_eligible_observations")

    tracklets_file, tracklets_raw, tracklets = load_object(
        tracklets_path, "tracklet set"
    )
    if tracklets.get("schema") != "v2x-tracklet-set/v1":
        reasons.append("tracklet_schema")
    if tracklets.get("source_observations_sha256") != observations_hash:
        reasons.append("tracklet_source_hash")
    bound_proposal_index = {}
    bound_review_entry_index = {}
    try:
        _proposals_path, _proposals_raw, bound_proposals = verify_bound_file(
            tracklets_file.parent,
            tracklets,
            "source_proposals_path",
            "source_proposals_sha256",
            "tracklet source proposals",
            require_json=True,
        )
        _review_path, _review_raw, bound_review = verify_bound_file(
            tracklets_file.parent,
            tracklets,
            "review_path",
            "review_sha256",
            "tracklet review",
            require_json=True,
        )
        proposal_values = bound_proposals.get("proposals")
        review_entries = bound_review.get("entries")
        if (
            bound_proposals.get("schema") != "v2x-tracklet-proposals/v1"
            or bound_proposals.get("source_observations_sha256")
            != observations_hash
            or bound_review.get("schema") != "v2x-tracklet-review/v1"
            or bound_review.get("source_proposals_sha256")
            != tracklets.get("source_proposals_sha256")
            or bound_review.get("reviewer") != tracklets.get("reviewer")
            or not isinstance(proposal_values, list)
            or not isinstance(review_entries, list)
        ):
            raise FactorGraphError("tracklet review artifacts do not bind")
        for proposal in proposal_values:
            proposal_id = (
                proposal.get("proposal_id") if isinstance(proposal, dict) else None
            )
            event_ids = (
                proposal.get("event_ids") if isinstance(proposal, dict) else None
            )
            if (
                not isinstance(proposal_id, str)
                or not proposal_id
                or proposal_id.strip() != proposal_id
                or proposal_id in bound_proposal_index
                or not isinstance(event_ids, list)
                or len(event_ids) < 3
                or not all(
                    isinstance(event_id, str) and bool(event_id)
                    for event_id in event_ids
                )
                or len(set(event_ids)) != len(event_ids)
            ):
                raise FactorGraphError("tracklet proposal entries are malformed")
            bound_proposal_index[proposal_id] = proposal
        seen_review_proposal_ids = set()
        for entry in review_entries:
            proposal_id = (
                entry.get("proposal_id") if isinstance(entry, dict) else None
            )
            if (
                not isinstance(proposal_id, str)
                or not proposal_id
                or proposal_id.strip() != proposal_id
                or proposal_id not in bound_proposal_index
                or proposal_id in seen_review_proposal_ids
                or entry.get("decision") not in {"accept", "reject"}
            ):
                raise FactorGraphError("tracklet review entries are malformed")
            if entry["decision"] == "accept" and set(entry) != ACCEPT_REVIEW_ENTRY_KEYS:
                raise FactorGraphError("accepted tracklet review entry is malformed")
            if entry["decision"] == "reject" and (
                not isinstance(entry.get("reason"), str)
                or not entry["reason"].strip()
            ):
                raise FactorGraphError("rejected tracklet review entry is malformed")
            seen_review_proposal_ids.add(proposal_id)
            if entry["decision"] == "accept":
                bound_review_entry_index[proposal_id] = entry
    except (FactorGraphError, TypeError, ValueError):
        reasons.append("tracklet_review_artifacts_unbound")
        bound_proposal_index = {}
        bound_review_entry_index = {}
    tracklet_index = {}
    evidence_groups = defaultdict(list)
    event_owners = {}
    tracklet_values = tracklets.get("tracklets")
    if not isinstance(tracklet_values, list):
        reasons.append("tracklets_not_list")
        tracklet_values = []
    for tracklet in tracklet_values:
        if not isinstance(tracklet, dict):
            reasons.append("tracklet_invalid")
            continue
        tracklet_id = tracklet.get("tracklet_id")
        evidence_group_id = tracklet.get("evidence_group_id")
        camera_id = tracklet.get("camera_id")
        event_ids = tracklet.get("event_ids")
        review = tracklet.get("review")
        valid_review = (
            isinstance(review, dict)
            and review.get("moving") is True
            and review.get("occlusion_free") is True
            and review.get("not_truncated") is True
            and review.get("optical_flow_consistent") is True
            and isinstance(review.get("reviewer"), dict)
            and review["reviewer"].get("kind") == "human"
            and isinstance(review["reviewer"].get("id"), str)
            and bool(review["reviewer"]["id"].strip())
            and sha256_valid(review.get("review_entry_sha256"))
        )
        if (
            not isinstance(tracklet_id, str)
            or not tracklet_id
            or tracklet_id in tracklet_index
            or not isinstance(evidence_group_id, str)
            or not evidence_group_id
            or evidence_group_id.strip() != evidence_group_id
            or camera_id not in CAMERAS
            or not isinstance(event_ids, list)
            or len(event_ids) < 3
            or not all(
                isinstance(event_id, str) and bool(event_id)
                for event_id in event_ids
            )
            or len(set(event_ids)) != len(event_ids)
            or not valid_review
            or not isinstance(tracklet.get("lane_path_id"), str)
            or not finite(tracklet.get("motion_direction_deg"))
        ):
            reasons.append(
                "tracklet_evidence_group"
                if not isinstance(evidence_group_id, str)
                or not evidence_group_id
                or evidence_group_id.strip() != evidence_group_id
                else "tracklet_invalid"
            )
            continue
        proposal = bound_proposal_index.get(tracklet_id)
        review_entry = bound_review_entry_index.get(tracklet_id)
        expected_checks = {
            "moving": True,
            "occlusion_free": True,
            "not_truncated": True,
            "optical_flow_consistent": True,
        }
        if (
            proposal is None
            or review_entry is None
            or proposal.get("camera_id") != camera_id
            or proposal.get("event_ids") != event_ids
            or proposal.get("start_media_timestamp_utc")
            != tracklet.get("start_media_timestamp_utc")
            or proposal.get("end_media_timestamp_utc")
            != tracklet.get("end_media_timestamp_utc")
            or review_entry.get("lane_path_id") != tracklet.get("lane_path_id")
            or review_entry.get("evidence_group_id") != evidence_group_id
            or (review_entry.get("includes_turn") is True)
            != (tracklet.get("includes_turn") is True)
            or not finite(review_entry.get("motion_direction_deg"))
            or float(review_entry["motion_direction_deg"])
            != float(tracklet["motion_direction_deg"])
            or review_entry.get("checks") != expected_checks
            or review.get("reviewer") != tracklets.get("reviewer")
            or review.get("review_entry_sha256")
            != sha256_bytes(
                json.dumps(
                    review_entry, sort_keys=True, separators=(",", ":")
                ).encode()
            )
        ):
            reasons.append("tracklet_review_binding")
            continue
        if any(event_id not in eligible for event_id in event_ids):
            reasons.append("tracklet_references_ineligible_observation")
            continue
        if any(eligible[event_id]["camera_id"] != camera_id for event_id in event_ids):
            reasons.append("tracklet_camera_mismatch")
            continue
        if any(event_id in event_owners for event_id in event_ids):
            reasons.append("observation_reused_across_tracklets")
            continue
        for event_id in event_ids:
            event_owners[event_id] = tracklet_id
        tracklet_index[tracklet_id] = tracklet
        evidence_groups[evidence_group_id].append(tracklet_id)

    split_file, split_raw, split = load_object(split_path, "track split")
    tracklets_hash = sha256_bytes(tracklets_raw)
    if split.get("schema") != "v2x-track-split/v1":
        reasons.append("split_schema")
    if split.get("source_tracklets_sha256") != tracklets_hash:
        reasons.append("split_source_hash")
    assignments = split.get("assignments")
    if not isinstance(assignments, dict) or set(assignments) != set(tracklet_index):
        reasons.append("split_assignments")
        assignments = {}
    elif any(value not in SPLITS for value in assignments.values()):
        reasons.append("split_assignment_value")
    declared_group_assignments = split.get("evidence_group_assignments")
    group_assignments_valid = True
    if (
        not isinstance(declared_group_assignments, dict)
        or set(declared_group_assignments) != set(evidence_groups)
    ):
        reasons.append("split_evidence_group_assignments")
        group_assignments_valid = False
        declared_group_assignments = {}
    elif any(
        value not in SPLITS for value in declared_group_assignments.values()
    ):
        reasons.append("split_evidence_group_assignment_value")
        group_assignments_valid = False

    recomputed_group_assignments = {}
    if assignments:
        for evidence_group_id, tracklet_ids in evidence_groups.items():
            partitions = {assignments.get(tracklet_id) for tracklet_id in tracklet_ids}
            if len(partitions) != 1 or None in partitions:
                reasons.append("evidence_group_crosses_split")
                continue
            partition = next(iter(partitions))
            recomputed_group_assignments[evidence_group_id] = partition
            if (
                group_assignments_valid
                and declared_group_assignments.get(evidence_group_id) != partition
            ):
                reasons.append("split_evidence_group_mismatch")
    holdout_day = split.get("holdout_day_utc")
    try:
        parsed_holdout_day = datetime.fromisoformat(holdout_day).date()
    except (TypeError, ValueError):
        parsed_holdout_day = None
    if parsed_holdout_day is None:
        reasons.append("missing_later_day_holdout")
    if assignments and not SPLITS.issubset(set(assignments.values())):
        reasons.append("split_missing_partition")
    later_day_tracks = 0
    if parsed_holdout_day is not None and assignments:
        for tracklet_id, tracklet in tracklet_index.items():
            event_days = [
                datetime.fromisoformat(
                    eligible[event_id]["media_timestamp_utc"][:-1] + "+00:00"
                ).date()
                for event_id in tracklet["event_ids"]
            ]
            if any(day >= parsed_holdout_day for day in event_days):
                later_day_tracks += 1
                if assignments.get(tracklet_id) != "holdout":
                    reasons.append("later_day_track_not_in_holdout")
        if later_day_tracks == 0:
            reasons.append("missing_later_day_holdout_evidence")

    association_index = []
    association_ids = set()
    clock_evidence = defaultdict(list)
    fit_clock_evidence = defaultdict(list)
    synchronized_pair_keys = set()
    associations_hash = None
    if associations_path is not None:
        _association_file, associations_raw, associations = load_object(
            associations_path, "association set"
        )
        associations_hash = sha256_bytes(associations_raw)
        if associations.get("schema") != "v2x-association-set/v1":
            reasons.append("association_schema")
        if associations.get("source_tracklets_sha256") != tracklets_hash:
            reasons.append("association_source_hash")
        precision = associations.get("precision_evidence")
        association_values = associations.get("associations")
        if not isinstance(association_values, list):
            reasons.append("associations_not_list")
            association_values = []
        association_candidates_sha256 = (
            sha256_bytes(canonical_json_bytes(association_values))
            if association_values
            else sha256_bytes(canonical_json_bytes([]))
        )
        if (
            not isinstance(precision, dict)
            or not finite(precision.get("precision"))
            or float(precision["precision"]) < 0.99
            or not sha256_valid(precision.get("reviewed_subset_sha256"))
        ):
            reasons.append("association_precision")
        else:
            try:
                _review_path, _review_raw, reviewed_subset = verify_bound_file(
                    _association_file.parent,
                    precision,
                    "reviewed_subset_path",
                    "reviewed_subset_sha256",
                    "reviewed association subset",
                    require_json=True,
                )
                true_positives = reviewed_subset.get("true_positives")
                false_positives = reviewed_subset.get("false_positives")
                reviewed_entries = reviewed_subset.get("entries")
                candidate_index = {
                    candidate.get("association_id"): candidate
                    for candidate in association_values
                    if isinstance(candidate, dict)
                    and isinstance(candidate.get("association_id"), str)
                }
                labels = []
                reviewed_ids = set()
                if isinstance(reviewed_entries, list):
                    for entry in reviewed_entries:
                        association_id = (
                            entry.get("association_id")
                            if isinstance(entry, dict)
                            else None
                        )
                        candidate = candidate_index.get(association_id)
                        if (
                            not isinstance(entry, dict)
                            or not isinstance(association_id, str)
                            or not association_id
                            or association_id.strip() != association_id
                            or candidate is None
                            or association_id in reviewed_ids
                            or entry.get("tracklet_ids")
                            != candidate.get("tracklet_ids")
                            or entry.get("label")
                            not in {"true_positive", "false_positive"}
                        ):
                            raise FactorGraphError(
                                "association review entry does not bind a candidate"
                            )
                        reviewed_ids.add(association_id)
                        labels.append(entry["label"])
                if (
                    reviewed_subset.get("schema")
                    != "v2x-reviewed-association-subset/v1"
                    or isinstance(true_positives, bool)
                    or not isinstance(true_positives, int)
                    or isinstance(false_positives, bool)
                    or not isinstance(false_positives, int)
                    or true_positives < 1
                    or false_positives < 0
                    or reviewed_subset.get(
                        "source_association_candidates_sha256"
                    )
                    != association_candidates_sha256
                    or not isinstance(reviewed_entries, list)
                    or len(reviewed_entries) < 100
                    or true_positives != labels.count("true_positive")
                    or false_positives != labels.count("false_positive")
                    or true_positives / (true_positives + false_positives) < 0.99
                    or abs(
                        true_positives / (true_positives + false_positives)
                        - float(precision["precision"])
                    )
                    > 1e-12
                ):
                    raise FactorGraphError("association precision evidence mismatch")
            except FactorGraphError:
                reasons.append("association_precision_artifact")
        for association in association_values:
            ids = association.get("tracklet_ids") if isinstance(association, dict) else None
            evidence = association.get("evidence") if isinstance(association, dict) else None
            association_id = (
                association.get("association_id")
                if isinstance(association, dict)
                else None
            )
            if (
                not isinstance(association_id, str)
                or not association_id
                or association_id.strip() != association_id
                or association_id in association_ids
                or not isinstance(ids, list)
                or len(ids) < 2
                or not all(
                    isinstance(tracklet_id, str) and bool(tracklet_id)
                    for tracklet_id in ids
                )
                or len(set(ids)) != len(ids)
                or any(tracklet_id not in tracklet_index for tracklet_id in ids)
                or len({tracklet_index[tracklet_id]["camera_id"] for tracklet_id in ids}) < 2
                or not isinstance(evidence, dict)
                or evidence.get("reviewed") is not True
                or not finite(evidence.get("appearance_similarity"))
                or float(evidence["appearance_similarity"]) < 0.60
            ):
                reasons.append("association_invalid")
                continue
            association_ids.add(association_id)
            association_splits = {
                assignments.get(tracklet_id) for tracklet_id in ids
            }
            if len(association_splits) != 1 or None in association_splits:
                reasons.append("association_crosses_split")
                continue
            evidence_group_ids = {
                tracklet_index[tracklet_id]["evidence_group_id"]
                for tracklet_id in ids
            }
            if (
                len(evidence_group_ids) != 1
                or association.get("evidence_group_id")
                != next(iter(evidence_group_ids))
            ):
                reasons.append("association_evidence_group_mismatch")
                continue
            synchronized_pairs = association.get("synchronized_pairs", [])
            if not isinstance(synchronized_pairs, list):
                reasons.append("association_synchronized_pairs_not_list")
                continue
            association_events = {
                event_id
                for tracklet_id in ids
                for event_id in tracklet_index[tracklet_id]["event_ids"]
            }
            for pair in synchronized_pairs:
                pair_ids = pair.get("event_ids") if isinstance(pair, dict) else None
                sigma = pair.get("time_sigma_s") if isinstance(pair, dict) else None
                if (
                    not isinstance(pair_ids, list)
                    or len(pair_ids) != 2
                    or not all(
                        isinstance(event_id, str) and bool(event_id)
                        for event_id in pair_ids
                    )
                    or len(set(pair_ids)) != 2
                    or any(event_id not in association_events for event_id in pair_ids)
                    or eligible[pair_ids[0]]["camera_id"] == eligible[pair_ids[1]]["camera_id"]
                    or pair.get("reviewed") is not True
                    or not finite(sigma)
                    or not 0.0 < float(sigma) <= 0.10
                ):
                    reasons.append("association_synchronized_pair_invalid")
                    continue
                pair_key = tuple(sorted(pair_ids))
                if pair_key in synchronized_pair_keys:
                    reasons.append("association_synchronized_pair_duplicated")
                    continue
                synchronized_pair_keys.add(pair_key)
                for event_id in pair_ids:
                    owner = event_owners[event_id]
                    camera_id = eligible[event_id]["camera_id"]
                    clock_evidence[camera_id].append(
                        (
                            pair_key,
                            float(tracklet_index[owner]["motion_direction_deg"]),
                        )
                    )
                    if association_splits == {"fit"}:
                        fit_clock_evidence[camera_id].append(
                            (
                                pair_key,
                                float(
                                    tracklet_index[owner][
                                        "motion_direction_deg"
                                    ]
                                ),
                            )
                        )
            association_index.append(association)
    else:
        reasons.append("reviewed_cross_camera_associations_missing")

    counts_by_camera = Counter(
        tracklet["camera_id"] for tracklet in tracklet_index.values()
    )
    lane_paths = defaultdict(set)
    turns = Counter()
    pixels = defaultdict(list)
    bands = defaultdict(Counter)
    for tracklet in tracklet_index.values():
        camera_id = tracklet["camera_id"]
        lane_paths[camera_id].add(tracklet["lane_path_id"])
        turns[camera_id] += tracklet.get("includes_turn") is True
        for event_id in tracklet["event_ids"]:
            observation = eligible[event_id]
            pixels[camera_id].append(observation["pixel"])
            bands[camera_id][observation["range_band"]] += 1

    camera_gates = {}
    for camera_id in CAMERAS:
        width = float((cameras.get(camera_id) or {}).get("intrinsics", {}).get("width", 1))
        height = float((cameras.get(camera_id) or {}).get("intrinsics", {}).get("height", 1))
        points = pixels[camera_id]
        x_span = (
            (max(point[0] for point in points) - min(point[0] for point in points)) / width
            if points else 0.0
        )
        y_span = (
            (max(point[1] for point in points) - min(point[1] for point in points)) / height
            if points else 0.0
        )
        total_bands = sum(bands[camera_id].values())
        fractions = {
            name: bands[camera_id][name] / total_bands if total_bands else 0.0
            for name in ("near", "mid", "far")
        }
        gate_reasons = []
        if counts_by_camera[camera_id] < 30:
            gate_reasons.append("insufficient_tracklets")
        if len(lane_paths[camera_id]) < 3 or turns[camera_id] < 1:
            gate_reasons.append("insufficient_lane_excitation")
        if fractions["near"] < 0.20 or fractions["mid"] < 0.50 or fractions["far"] < 0.20:
            gate_reasons.append("insufficient_range_excitation")
        if x_span < 0.60 or y_span < 0.40:
            gate_reasons.append("insufficient_image_coverage")
        clock_rows = clock_evidence[camera_id]
        directions = [value[1] for value in clock_rows]
        direction_span = circular_direction_span(directions)
        independent_pairs = len({value[0] for value in clock_rows})
        fit_clock_rows = fit_clock_evidence[camera_id]
        fit_directions = [value[1] for value in fit_clock_rows]
        fit_direction_span = circular_direction_span(fit_directions)
        fit_independent_pairs = len({value[0] for value in fit_clock_rows})
        clock_estimable = (
            fit_independent_pairs >= 5 and fit_direction_span >= 30.0
        )
        camera_gates[camera_id] = {
            "passed": not gate_reasons,
            "reasons": gate_reasons,
            "tracklets": counts_by_camera[camera_id],
            "lane_paths": len(lane_paths[camera_id]),
            "turn_tracklets": turns[camera_id],
            "range_fractions": fractions,
            "horizontal_span": round(x_span, 6),
            "vertical_span": round(y_span, 6),
            "clock_offset": {
                "estimable": clock_estimable,
                "independent_synchronized_pairs": independent_pairs,
                "association_direction_samples": len(directions),
                "direction_span_deg": round(direction_span, 3),
                "fit_independent_synchronized_pairs": fit_independent_pairs,
                "fit_direction_span_deg": round(fit_direction_span, 3),
                "fit_partition_only": True,
                "fallback": None if clock_estimable else "fixed_zero_unobservable",
            },
        }
        reasons.extend(f"{camera_id}:{reason}" for reason in gate_reasons)

    reasons = sorted(set(reasons))
    return {
        "schema": "v2x-detection-factor-graph-report/v1",
        "gate_passed": not reasons,
        "mode": "preflight",
        "reasons": reasons,
        "inputs": {
            "ledger_manifest_sha256": sha256_bytes(
                (Path(ledger_dir).expanduser().resolve() / "manifest.json").read_bytes()
            ),
            "observations_sha256": observations_hash,
            "tracklets_sha256": tracklets_hash,
            "associations_sha256": associations_hash,
            "split_sha256": sha256_bytes(split_raw),
            "cameras_json_sha256": sha256_bytes(cameras_raw),
        },
        "counts": {
            "observations": len(observations),
            "eligible_observations": len(eligible),
            "observation_rejections": dict(sorted(observation_rejections.items())),
            "tracklets": len(tracklet_index),
            "evidence_groups": len(evidence_groups),
            "accepted_associations": len(association_index),
        },
        "cameras": camera_gates,
        "optimizer_contract": {
            "derived_baseline_parsed": False,
            "site_to_map_transform_frozen": True,
            "intrinsics_fixed_to_measured": True,
            "lane_prior_excluded_from_metrics": True,
            "whole_evidence_group_atomic": (
                group_assignments_valid
                and len(recomputed_group_assignments) == len(evidence_groups)
                and recomputed_group_assignments == declared_group_assignments
            ),
            "diagnostic_until_independent_truth": True,
        },
    }


def run_fit(report, ledger_dir, tracklets_path, associations_path, split_path,
            cameras_path, static_solution_path, lane_map_path, multistarts):
    """Load only the reviewed pixel/time representation and run diagnostics."""
    from datetime import timezone
    from detection_trajectory_fit import (
        TrajectoryFitError,
        fit_detection_constraints,
    )

    if not report["gate_passed"]:
        raise FactorGraphError("preflight gate did not pass")
    _manifest, observations, _observations_hash = load_ledger(ledger_dir)
    _camera_file, cameras_raw, camera_config = load_object(cameras_path, "camera config")
    cameras = {
        camera["id"]: camera
        for camera in camera_config.get("cameras", [])
        if isinstance(camera, dict) and camera.get("id") in CAMERAS
    }
    _tracklet_file, _tracklet_raw, tracklet_set = load_object(tracklets_path, "tracklet set")
    _split_file, _split_raw, split = load_object(split_path, "track split")
    assignments = split["assignments"]
    tracks = []
    event_owner = {}
    for tracklet in tracklet_set["tracklets"]:
        event_ids = list(tracklet["event_ids"])
        times = []
        pixels = []
        covariances = []
        for event_id in event_ids:
            item = observations[event_id]
            # This explicit projection is the entire fit input. In particular,
            # derived_baseline/GPS/local-XZ are never copied or parsed.
            timestamp = datetime.fromisoformat(
                item["media_timestamp_utc"][:-1] + "+00:00"
            )
            if timestamp.tzinfo is None or timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
                raise FactorGraphError("fit timestamp is not UTC")
            times.append(timestamp.timestamp())
            pixels.append([float(value) for value in item["ground_contact"]["pixel"]])
            covariances.append(item["ground_contact"]["covariance_px2"])
            event_owner[event_id] = tracklet["tracklet_id"]
        tracks.append({
            "tracklet_id": tracklet["tracklet_id"],
            "camera_id": tracklet["camera_id"],
            "event_ids": event_ids,
            "pixels": pixels,
            "times_epoch": times,
            "covariances_px2": covariances,
            "lane_path_id": tracklet["lane_path_id"],
            "motion_direction_deg": tracklet["motion_direction_deg"],
            "includes_turn": tracklet.get("includes_turn") is True,
            "split": assignments[tracklet["tracklet_id"]],
        })

    synchronized_pairs = []
    if associations_path is not None:
        _association_file, _association_raw, association_set = load_object(
            associations_path, "association set"
        )
        tracklet_index = {
            tracklet["tracklet_id"]: tracklet for tracklet in tracklet_set["tracklets"]
        }
        for association in association_set.get("associations", []):
            allowed_tracklets = set(association["tracklet_ids"])
            if {assignments.get(value) for value in allowed_tracklets} != {"fit"}:
                continue
            for pair in association.get("synchronized_pairs", []):
                if any(event_owner.get(event_id) not in allowed_tracklets for event_id in pair["event_ids"]):
                    raise FactorGraphError("synchronized pair ownership mismatch")
                pair = dict(pair)
                pair_cameras = {
                    observations[event_id]["camera_id"]
                    for event_id in pair["event_ids"]
                }
                pair["estimate_clock_offset"] = all(
                    report["cameras"][camera_id]["clock_offset"]["estimable"]
                    for camera_id in pair_cameras
                )
                synchronized_pairs.append(pair)

    static_file, static_raw, static_solution = load_object(
        static_solution_path, "static camera solution"
    )
    lane_file, lane_raw, lane_map = load_object(
        lane_map_path, "surveyed lane map"
    )
    _truth_path, truth_raw, _truth = verify_bound_file(
        static_file.parent,
        static_solution.get("truth"),
        "manifest_path",
        "manifest_sha256",
        "static survey manifest",
        require_json=True,
    )
    _transform_path, transform_raw, _transform = verify_bound_file(
        static_file.parent,
        static_solution.get("site_to_map_transform"),
        "artifact_path",
        "artifact_sha256",
        "site-to-map transform artifact",
        require_json=True,
    )
    _survey_path, survey_raw, _survey = verify_bound_file(
        lane_file.parent,
        lane_map,
        "survey_manifest_path",
        "survey_manifest_sha256",
        "lane survey manifest",
        require_json=True,
    )
    truth_gate = _truth.get("heldout_gate")
    expected_camera_solutions_hash = sha256_bytes(
        canonical_json_bytes(static_solution.get("cameras"))
    )
    if (
        _truth.get("schema") != "v2x-static-camera-survey-manifest/v1"
        or _truth.get("site_frame") != "surveyed_enu_z_up"
        or sorted(_truth.get("camera_ids", [])) != list(CAMERAS)
        or not isinstance(truth_gate, dict)
        or truth_gate.get("passed") is not True
        or _truth.get("camera_solutions_sha256")
        != expected_camera_solutions_hash
        or truth_gate.get("reference_resolution") != [1280, 960]
    ):
        raise FactorGraphError("static survey manifest did not pass its heldout gate")
    camera_metrics = truth_gate.get("cameras")
    if not isinstance(camera_metrics, dict) or set(camera_metrics) != set(CAMERAS):
        raise FactorGraphError("static survey manifest lacks per-camera holdout metrics")
    for camera_id in CAMERAS:
        metrics = camera_metrics[camera_id]
        try:
            point_count = int(metrics["landmark_count"])
            point_rmse = float(metrics["landmark_rmse_px"])
            point_p95 = float(metrics["landmark_p95_px"])
            point_max = float(metrics["landmark_max_px"])
            road_rmse = float(metrics["road_rmse_px"])
            road_max = float(metrics["road_max_px"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise FactorGraphError(
                f"{camera_id} static heldout metrics are invalid"
            ) from exc
        if (
            point_count < 4
            or point_rmse > 10.0
            or point_p95 > 16.0
            or point_max > 24.0
            or road_rmse > 6.0
            or road_max > 12.0
            or not all(
                math.isfinite(value)
                for value in (point_rmse, point_p95, point_max, road_rmse, road_max)
            )
        ):
            raise FactorGraphError(
                f"{camera_id} static heldout thresholds did not pass"
            )
    transform_residuals = _transform.get("heldout_residuals")
    try:
        transform_p95 = float(transform_residuals["p95_m"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise FactorGraphError("site-to-map transform residuals are invalid") from exc
    if (
        _transform.get("schema") != "v2x-site-to-map-transform/v1"
        or _transform.get("site_frame") != "surveyed_enu_z_up"
        or _transform.get("model") != "se2_fixed_scale"
        or _transform.get("scale") != 1.0
        or not isinstance(_transform.get("translation_m"), list)
        or len(_transform["translation_m"]) != 2
        or not all(finite(value) for value in _transform["translation_m"])
        or not finite(_transform.get("yaw_deg"))
        or not math.isfinite(transform_p95)
        or transform_p95 > 0.25
    ):
        raise FactorGraphError("site-to-map transform artifact is not acceptance grade")
    if (
        _survey.get("schema") != "v2x-lane-survey-manifest/v1"
        or _survey.get("site_frame") != "surveyed_enu_z_up"
        or _survey.get("independent_of_detections") is not True
        or not finite(_survey.get("survey_accuracy_m"))
        or abs(float(_survey["survey_accuracy_m"]) - float(lane_map["survey_accuracy_m"]))
        > 1e-9
        or not set(lane_map["lane_paths"]).issubset(
            set(_survey.get("lane_path_ids", []))
        )
        or _survey.get("lane_paths_sha256")
        != sha256_bytes(canonical_json_bytes(lane_map.get("lane_paths")))
    ):
        raise FactorGraphError("lane survey manifest does not bind the lane map")
    intrinsics_evidence = {
        camera_id: verify_intrinsics_files(camera, Path(cameras_path).resolve().parent)
        for camera_id, camera in cameras.items()
    }
    try:
        result = fit_detection_constraints(
            cameras=cameras,
            static_solution=static_solution,
            lane_map=lane_map,
            tracks=tracks,
            synchronized_pairs=synchronized_pairs,
            cameras_json_sha256=sha256_bytes(cameras_raw),
            multistarts=multistarts,
        )
    except TrajectoryFitError as exc:
        raise FactorGraphError(str(exc)) from exc
    result["artifacts"] = {
        "static_solution_sha256": sha256_bytes(static_raw),
        "static_survey_manifest_sha256": sha256_bytes(truth_raw),
        "site_to_map_transform_sha256": sha256_bytes(transform_raw),
        "lane_map_sha256": sha256_bytes(lane_raw),
        "lane_survey_manifest_sha256": sha256_bytes(survey_raw),
        "intrinsics": intrinsics_evidence,
    }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--tracklets", required=True)
    parser.add_argument("--split", required=True)
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--associations")
    parser.add_argument("--static-solution")
    parser.add_argument("--lane-map")
    parser.add_argument("--multistarts", type=int, default=5)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="validate all inputs and write the contract report without fitting",
    )
    args = parser.parse_args(argv)
    try:
        report = validate_inputs(
            args.ledger,
            args.tracklets,
            args.associations,
            args.split,
            args.cameras_json,
        )
        fit_succeeded = None
        if report["gate_passed"] and not args.preflight_only:
            if not args.static_solution or not args.lane_map:
                raise FactorGraphError(
                    "--static-solution and --lane-map are required for fitting"
                )
            report["mode"] = "diagnostic_fit"
            report["fit"] = run_fit(
                report,
                args.ledger,
                args.tracklets,
                args.associations,
                args.split,
                args.cameras_json,
                args.static_solution,
                args.lane_map,
                args.multistarts,
            )
            report["preflight_gate_passed"] = True
            report["gate_passed"] = report["fit"]["numerical_gate_passed"]
            report["acceptance_eligible"] = False
            fit_succeeded = report["gate_passed"]
        output = Path(args.output).expanduser().resolve()
        if output.exists():
            raise FactorGraphError("output report already exists")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except FactorGraphError as exc:
        print(f"factor-graph preflight failed: {exc}", file=sys.stderr)
        return 1
    return 0 if report["gate_passed"] and fit_succeeded is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
