#!/usr/bin/env python3
"""Freeze whole evidence groups into fit, validation, and later-day holdout."""

import argparse
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sys


class SplitError(RuntimeError):
    pass


def parse_day(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise SplitError("holdout day must be YYYY-MM-DD") from exc


def timestamp_day(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SplitError("tracklet timestamp is invalid")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").date()
    except ValueError as exc:
        raise SplitError("tracklet timestamp is invalid") from exc


def freeze_split(tracklets_path, output_path, holdout_day_utc, seed):
    tracklets_path = Path(tracklets_path).expanduser().resolve()
    raw = tracklets_path.read_bytes()
    try:
        tracklet_set = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SplitError("tracklet set is invalid") from exc
    if not isinstance(tracklet_set, dict) or tracklet_set.get("schema") != "v2x-tracklet-set/v1":
        raise SplitError("tracklet set schema is unsupported")
    values = tracklet_set.get("tracklets")
    if not isinstance(values, list) or not values:
        raise SplitError("tracklet set is empty")
    holdout_day = parse_day(holdout_day_utc)
    if not isinstance(seed, str) or len(seed) < 8:
        raise SplitError("split seed must contain at least 8 characters")
    groups = {}
    ids = set()
    for tracklet in values:
        tracklet_id = tracklet.get("tracklet_id") if isinstance(tracklet, dict) else None
        group_id = tracklet.get("evidence_group_id") if isinstance(tracklet, dict) else None
        if not isinstance(tracklet_id, str) or not tracklet_id or tracklet_id in ids or not isinstance(group_id, str) or not group_id:
            raise SplitError("tracklet IDs/evidence groups are invalid")
        ids.add(tracklet_id)
        groups.setdefault(group_id, []).append(tracklet)
    assignments = {}
    group_assignments = {}
    for group_id, tracks in sorted(groups.items()):
        later = any(
            timestamp_day(track[boundary]) >= holdout_day
            for track in tracks
            for boundary in (
                "start_media_timestamp_utc",
                "end_media_timestamp_utc",
            )
        )
        if later:
            partition = "holdout"
        else:
            bucket = int.from_bytes(
                hashlib.sha256((seed + "\0" + group_id).encode()).digest()[:8],
                "big",
            ) % 100
            partition = "validation" if bucket < 20 else "fit"
        group_assignments[group_id] = partition
        for track in tracks:
            assignments[track["tracklet_id"]] = partition
    missing = {"fit", "validation", "holdout"} - set(assignments.values())
    if missing:
        raise SplitError("split is missing required partitions: " + ", ".join(sorted(missing)))
    output_path = Path(output_path).expanduser().resolve()
    if output_path.exists():
        raise SplitError("split output already exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "schema": "v2x-track-split/v1",
        "source_tracklets_sha256": hashlib.sha256(raw).hexdigest(),
        "holdout_day_utc": holdout_day_utc,
        "seed_sha256": hashlib.sha256(seed.encode()).hexdigest(),
        "assignments": dict(sorted(assignments.items())),
        "evidence_group_assignments": dict(sorted(group_assignments.items())),
        "contract": {
            "whole_evidence_group_atomic": True,
            "later_day_forced_to_holdout": True,
            "row_level_randomization_forbidden": True,
        },
    }
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    return output_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tracklets")
    parser.add_argument("output")
    parser.add_argument("--holdout-day-utc", required=True)
    parser.add_argument("--seed", required=True)
    args = parser.parse_args(argv)
    try:
        freeze_split(args.tracklets, args.output, args.holdout_day_utc, args.seed)
    except (SplitError, OSError) as exc:
        print(f"track split failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
