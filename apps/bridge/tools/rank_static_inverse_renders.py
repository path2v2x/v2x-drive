#!/usr/bin/env python3
"""Rank static inverse-render candidates without hiding metric regressions."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path


INPUT_SCHEMA = "v2x-static-inverse-render-score/v1"
OUTPUT_SCHEMA = "v2x-static-inverse-render-ranking/v1"


class RankingError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def load_score(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    score = json.loads(raw)
    if (
        score.get("schema") != INPUT_SCHEMA
        or score.get("acceptance_eligible") is not False
    ):
        raise RankingError(f"score lacks diagnostic contract: {path}")
    metrics = score.get("metrics") or {}
    required = {
        "optimization_loss",
        "symmetric_p95_px",
        "tolerance_f1",
    }
    try:
        normalized = {key: float(metrics[key]) for key in required}
    except (KeyError, TypeError, ValueError) as exc:
        raise RankingError(f"score metrics are incomplete: {path}") from exc
    if not all(value >= 0.0 for value in normalized.values()):
        raise RankingError(f"score metrics are invalid: {path}")
    score["_path"] = str(path)
    score["_sha256"] = hashlib.sha256(raw).hexdigest()
    score["_normalized_metrics"] = normalized
    return score


def candidate_result(score, baseline):
    metrics = score["_normalized_metrics"]
    baseline_metrics = baseline["_normalized_metrics"]
    checks = {
        "loss_improves_by_two_percent": (
            metrics["optimization_loss"]
            <= 0.98 * baseline_metrics["optimization_loss"]
        ),
        "p95_does_not_regress": (
            metrics["symmetric_p95_px"]
            <= baseline_metrics["symmetric_p95_px"]
        ),
        "tolerance_f1_does_not_regress": (
            metrics["tolerance_f1"] >= baseline_metrics["tolerance_f1"]
        ),
    }
    return {
        "candidate_id": score.get("candidate_id"),
        "score_path": score["_path"],
        "score_sha256": score["_sha256"],
        "twin_pose": score.get("twin_pose"),
        "fov_deg": score.get("fov_deg"),
        "metrics": metrics,
        "promotion_checks": checks,
        "promotable_for_visual_review": all(checks.values()),
    }


def rank(baseline, candidates):
    if not candidates:
        raise RankingError("candidate score list is empty")
    common_camera = baseline.get("camera_id")
    common_annotations = baseline.get("annotations_sha256")
    seen = {baseline.get("candidate_id")}
    if not all(
        score.get("camera_id") == common_camera
        and score.get("annotations_sha256") == common_annotations
        for score in candidates
    ):
        raise RankingError("scores do not share camera and annotation evidence")
    results = []
    for score in candidates:
        candidate_id = score.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in seen:
            raise RankingError("candidate IDs are blank or duplicated")
        seen.add(candidate_id)
        results.append(candidate_result(score, baseline))
    results.sort(
        key=lambda item: (
            not item["promotable_for_visual_review"],
            item["metrics"]["optimization_loss"],
            item["candidate_id"],
        )
    )
    promoted = [
        item["candidate_id"]
        for item in results
        if item["promotable_for_visual_review"]
    ]
    return {
        "schema": OUTPUT_SCHEMA,
        "acceptance_eligible": False,
        "created_at_utc": utc_now(),
        "camera_id": common_camera,
        "annotations_sha256": common_annotations,
        "baseline": {
            "candidate_id": baseline.get("candidate_id"),
            "score_path": baseline["_path"],
            "score_sha256": baseline["_sha256"],
            "metrics": baseline["_normalized_metrics"],
        },
        "ranking": [item["candidate_id"] for item in results],
        "promoted_for_visual_review": promoted,
        "recommendation": (
            "review_promoted_candidates" if promoted else "refine_search"
        ),
        "candidates": results,
        "limitations": [
            "ranking_is_diagnostic_not_camera_calibration_acceptance",
            "promotion_requires_no_p95_or_tolerance_f1_regression",
            "heldout_static_geometry_is_not_evaluated_by_this_ranking",
        ],
    }


def write_exclusive(path, report):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise RankingError("refusing to overwrite ranking output") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-score", required=True)
    parser.add_argument("--candidate-score", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        report = rank(
            load_score(args.baseline_score),
            [load_score(value) for value in args.candidate_score],
        )
        write_exclusive(args.output, report)
    except (OSError, ValueError, RankingError) as exc:
        raise SystemExit(str(exc)) from exc
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
