#!/usr/bin/env python3
"""Propose same-camera tracklets without promoting model identity to truth."""

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import sys

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
from apply_ground_contact_reviews import ReviewError, load_ledger  # noqa: E402
from export_detection_corpus import sha256_bytes  # noqa: E402


class TrackletProposalError(RuntimeError):
    pass


def epoch(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TrackletProposalError("observation timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise TrackletProposalError("observation timestamp is invalid") from exc
    return parsed.timestamp()


def propose_tracklets(ledger_dir, output_path, *, max_gap_seconds=2.5,
                      minimum_observations=3):
    if not 0.05 <= float(max_gap_seconds) <= 30.0:
        raise TrackletProposalError("max gap must be between 0.05 and 30 seconds")
    if not isinstance(minimum_observations, int) or not 3 <= minimum_observations <= 1000:
        raise TrackletProposalError("minimum observations must be between 3 and 1000")
    ledger_dir, _manifest, observations, observations_hash = load_ledger(ledger_dir)
    groups = defaultdict(list)
    rejected = 0
    for observation in observations:
        object_id = observation.get("object_id")
        camera_id = observation.get("camera_id")
        if not isinstance(object_id, str) or not object_id or not isinstance(camera_id, str):
            rejected += 1
            continue
        groups[(camera_id, object_id)].append((epoch(observation.get("media_timestamp_utc")), observation))
    proposals = []
    for (camera_id, object_id), values in sorted(groups.items()):
        values.sort(key=lambda value: (value[0], value[1]["event_id"]))
        runs = []
        run = []
        for value in values:
            if run and value[0] - run[-1][0] > float(max_gap_seconds):
                runs.append(run)
                run = []
            run.append(value)
        if run:
            runs.append(run)
        for run_index, candidate in enumerate(runs):
            if len(candidate) < minimum_observations:
                continue
            rows = [value[1] for value in candidate]
            proposal_id = sha256_bytes(
                (camera_id + "\0" + object_id + "\0" + "\0".join(row["event_id"] for row in rows)).encode()
            )[:24]
            eligible = sum(row.get("acceptance_eligible") is True for row in rows)
            proposals.append({
                "proposal_id": f"tracklet-{proposal_id}",
                "camera_id": camera_id,
                "model_object_id": object_id,
                "model_identity_is_truth": False,
                "event_ids": [row["event_id"] for row in rows],
                "start_media_timestamp_utc": rows[0]["media_timestamp_utc"],
                "end_media_timestamp_utc": rows[-1]["media_timestamp_utc"],
                "observation_count": len(rows),
                "eligible_contact_count": eligible,
                "status": (
                    "ready_for_human_track_review"
                    if eligible == len(rows)
                    else "blocked_pending_contact_review"
                ),
            })
    output_path = Path(output_path).expanduser().resolve()
    if output_path.exists():
        raise TrackletProposalError("proposal output already exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "v2x-tracklet-proposals/v1",
        "source_ledger": str(ledger_dir),
        "source_observations_sha256": observations_hash,
        "parameters": {
            "max_gap_seconds": float(max_gap_seconds),
            "minimum_observations": minimum_observations,
        },
        "contract": {
            "model_object_id_is_proposal_only": True,
            "human_review_required": True,
            "optical_flow_review_required": True,
        },
        "counts": {
            "observations": len(observations),
            "proposals": len(proposals),
            "observations_without_group_identity": rejected,
        },
        "proposals": proposals,
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return output_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger_dir")
    parser.add_argument("output")
    parser.add_argument("--max-gap-seconds", type=float, default=2.5)
    parser.add_argument("--minimum-observations", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        propose_tracklets(
            args.ledger_dir,
            args.output,
            max_gap_seconds=args.max_gap_seconds,
            minimum_observations=args.minimum_observations,
        )
    except (TrackletProposalError, ReviewError, OSError) as exc:
        print(f"tracklet proposal failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
