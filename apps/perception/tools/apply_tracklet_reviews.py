#!/usr/bin/env python3
"""Turn proposals into accepted tracklets only through named human review."""

import argparse
import json
import math
from pathlib import Path
import sys

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
from apply_ground_contact_reviews import ReviewError, load_ledger, load_object  # noqa: E402
from export_detection_corpus import sha256_bytes  # noqa: E402


class TrackletReviewError(RuntimeError):
    pass


ACCEPT_REVIEW_ENTRY_KEYS = {
    "proposal_id",
    "decision",
    "lane_path_id",
    "evidence_group_id",
    "includes_turn",
    "motion_direction_deg",
    "checks",
}


def review_entry_sha256(entry):
    return sha256_bytes(
        json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
    )


def apply_tracklet_reviews(ledger_dir, proposals_path, review_path, output_path):
    _ledger_dir, _manifest, observations, observations_hash = load_ledger(ledger_dir)
    proposals_path = Path(proposals_path).expanduser().resolve()
    review_path = Path(review_path).expanduser().resolve()
    proposals_raw = proposals_path.read_bytes()
    proposals = load_object(proposals_path, "tracklet proposals")
    review = load_object(review_path, "tracklet review")
    if proposals.get("schema") != "v2x-tracklet-proposals/v1":
        raise TrackletReviewError("proposal schema is unsupported")
    if proposals.get("source_observations_sha256") != observations_hash:
        raise TrackletReviewError("proposal source hash mismatch")
    if review.get("schema") != "v2x-tracklet-review/v1":
        raise TrackletReviewError("review schema is unsupported")
    if review.get("source_proposals_sha256") != sha256_bytes(proposals_raw):
        raise TrackletReviewError("review proposal hash mismatch")
    reviewer = review.get("reviewer")
    if (
        not isinstance(reviewer, dict)
        or reviewer.get("kind") != "human"
        or not isinstance(reviewer.get("id"), str)
        or not reviewer["id"].strip()
    ):
        raise TrackletReviewError("tracklet review requires a named human")
    proposal_values = proposals.get("proposals")
    if not isinstance(proposal_values, list):
        raise TrackletReviewError("proposal list is malformed")
    proposal_index = {}
    for value in proposal_values:
        proposal_id = value.get("proposal_id") if isinstance(value, dict) else None
        event_ids = value.get("event_ids") if isinstance(value, dict) else None
        if (
            not isinstance(proposal_id, str)
            or not proposal_id.strip()
            or proposal_id != proposal_id.strip()
            or proposal_id in proposal_index
            or not isinstance(event_ids, list)
            or len(event_ids) < 3
            or not all(isinstance(event_id, str) and event_id for event_id in event_ids)
            or len(set(event_ids)) != len(event_ids)
        ):
            raise TrackletReviewError("proposal IDs/event IDs are invalid or duplicated")
        proposal_index[proposal_id] = value
    if len(proposal_index) != len(proposal_values):
        raise TrackletReviewError("proposal IDs are invalid or duplicated")
    observation_index = {value["event_id"]: value for value in observations}
    entries = review.get("entries")
    if not isinstance(entries, list) or not entries:
        raise TrackletReviewError("tracklet review has no entries")
    seen = set()
    accepted = []
    for entry in entries:
        proposal_id = entry.get("proposal_id") if isinstance(entry, dict) else None
        if proposal_id not in proposal_index or proposal_id in seen:
            raise TrackletReviewError("review proposal IDs are unknown or duplicated")
        seen.add(proposal_id)
        decision = entry.get("decision")
        if decision not in {"accept", "reject"}:
            raise TrackletReviewError("tracklet decision must be accept or reject")
        if decision == "reject":
            if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
                raise TrackletReviewError("rejected tracklet requires a reason")
            continue
        if set(entry) != ACCEPT_REVIEW_ENTRY_KEYS:
            raise TrackletReviewError("accepted tracklet review entry is malformed")
        proposal = proposal_index[proposal_id]
        rows = [observation_index[event_id] for event_id in proposal["event_ids"]]
        if proposal.get("status") != "ready_for_human_track_review" or any(
            row.get("acceptance_eligible") is not True for row in rows
        ):
            raise TrackletReviewError("accepted tracklet has ineligible observations")
        direction = entry.get("motion_direction_deg")
        if isinstance(direction, bool) or not isinstance(direction, (int, float)) or not math.isfinite(float(direction)):
            raise TrackletReviewError("accepted tracklet direction is invalid")
        lane_id = entry.get("lane_path_id")
        evidence_group_id = entry.get("evidence_group_id")
        if not isinstance(lane_id, str) or not lane_id or not isinstance(evidence_group_id, str) or not evidence_group_id:
            raise TrackletReviewError("accepted tracklet lacks lane/evidence group")
        checks = entry.get("checks")
        if not isinstance(checks, dict) or any(
            checks.get(key) is not True
            for key in ("moving", "occlusion_free", "not_truncated", "optical_flow_consistent")
        ):
            raise TrackletReviewError("accepted tracklet review checks did not pass")
        accepted.append({
            "tracklet_id": proposal_id,
            "camera_id": proposal["camera_id"],
            "event_ids": proposal["event_ids"],
            "start_media_timestamp_utc": proposal["start_media_timestamp_utc"],
            "end_media_timestamp_utc": proposal["end_media_timestamp_utc"],
            "lane_path_id": lane_id,
            "includes_turn": entry.get("includes_turn") is True,
            "motion_direction_deg": float(direction),
            "evidence_group_id": evidence_group_id,
            "review": {
                "moving": True,
                "occlusion_free": True,
                "not_truncated": True,
                "optical_flow_consistent": True,
                "reviewer": reviewer,
                "review_entry_sha256": review_entry_sha256(entry),
            },
        })
    if not accepted:
        raise TrackletReviewError("review accepted no tracklets")
    output_path = Path(output_path).expanduser().resolve()
    if output_path.exists():
        raise TrackletReviewError("tracklet output already exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "schema": "v2x-tracklet-set/v1",
        "source_observations_sha256": observations_hash,
        "source_proposals_path": str(proposals_path),
        "source_proposals_sha256": sha256_bytes(proposals_raw),
        "review_path": str(review_path),
        "review_sha256": sha256_bytes(review_path.read_bytes()),
        "reviewer": reviewer,
        "tracklets": sorted(accepted, key=lambda value: value["tracklet_id"]),
    }
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    return output_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger_dir")
    parser.add_argument("proposals")
    parser.add_argument("review")
    parser.add_argument("output")
    args = parser.parse_args(argv)
    try:
        apply_tracklet_reviews(args.ledger_dir, args.proposals, args.review, args.output)
    except (TrackletReviewError, ReviewError, OSError) as exc:
        print(f"tracklet review failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
