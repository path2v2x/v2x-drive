#!/usr/bin/env python3
"""Execute the frozen Tier-B Phase-A map candidate score policy."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

from apps.bridge.tools.build_map_candidate_lineage_manifest import (
    SCORE_PRECEDENCE,
    LineageError,
    publish_no_replace,
    read_input,
    utc_now,
)


INPUT_SCHEMA = "v2x-map-candidate-class-scores/v1"
OUTPUT_SCHEMA = "v2x-map-candidate-score-decision/v1"
MANIFEST_SCHEMA = "v2x-map-candidate-lineage-manifest/v1"
NEAR_TIE_FRACTION = 0.02


class ScoreError(ValueError):
    pass


def _metric(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoreError(f"{label} must be a finite nonnegative number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ScoreError(f"{label} must be a finite nonnegative number") from exc
    if not math.isfinite(result) or result < 0:
        raise ScoreError(f"{label} must be a finite nonnegative number")
    return result


def _vector(value: dict, label: str) -> tuple[float, float, float, float]:
    if not isinstance(value, dict) or set(value) != set(SCORE_PRECEDENCE):
        raise ScoreError(f"{label} must contain the exact frozen score metrics")
    return tuple(_metric(value[name], f"{label}.{name}") for name in SCORE_PRECEDENCE)


def first_difference(left: tuple[float, ...], right: tuple[float, ...]) -> int | None:
    return next((index for index, pair in enumerate(zip(left, right)) if pair[0] != pair[1]), None)


def within_two_percent_at_first_difference(
    left: tuple[float, ...], right: tuple[float, ...]
) -> dict:
    index = first_difference(left, right)
    if index is None:
        return {"near_tie": True, "metric": None, "difference": 0.0, "denominator": 0.0}
    difference = abs(left[index] - right[index])
    denominator = max(abs(left[index]), abs(right[index]))
    # Unequal values cannot have a zero denominator. Keeping this branch explicit
    # freezes zero behavior and prevents division-based policy drift.
    near_tie = denominator == 0.0 or difference <= NEAR_TIE_FRACTION * denominator
    return {
        "near_tie": near_tie,
        "metric": SCORE_PRECEDENCE[index],
        "difference": difference,
        "denominator": denominator,
    }


def evaluate_score_document(document: dict, expected_candidate_ids: set[str]) -> dict:
    if not isinstance(document, dict) or document.get("schema") != INPUT_SCHEMA:
        raise ScoreError("score document schema is invalid")
    classes = document.get("required_classes")
    if (
        not isinstance(classes, list) or not classes
        or any(not isinstance(value, str) or not value.strip() for value in classes)
        or len(set(classes)) != len(classes) or classes != sorted(classes)
    ):
        raise ScoreError("required_classes must be unique, nonblank, and sorted")
    candidates = document.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ScoreError("candidate score list is empty")
    normalized = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ScoreError("candidate score entry is invalid")
        candidate_id = candidate.get("candidate_id")
        if candidate_id not in expected_candidate_ids or candidate_id in seen:
            raise ScoreError("candidate score IDs do not exactly match the manifest")
        seen.add(candidate_id)
        contradiction = candidate.get("topology_contradiction")
        if not isinstance(contradiction, bool):
            raise ScoreError("topology_contradiction must be boolean")
        class_scores = candidate.get("class_scores")
        if not isinstance(class_scores, dict) or set(class_scores) != set(classes):
            raise ScoreError("candidate must score every and only required class")
        vectors = {name: _vector(class_scores[name], f"{candidate_id}:{name}") for name in classes}
        aggregate = (
            max(value[0] for value in vectors.values()),
            max(value[1] for value in vectors.values()),
            max(value[2] for value in vectors.values()),
            sum(value[3] for value in vectors.values()),
        )
        normalized.append({
            "candidate_id": candidate_id,
            "topology_contradiction": contradiction,
            "class_score_vectors": vectors,
            "aggregate_score_vector": aggregate,
        })
    if seen != expected_candidate_ids:
        raise ScoreError("candidate score IDs do not exactly match the manifest")
    survivors = [item for item in normalized if not item["topology_contradiction"]]
    if not survivors:
        status, winner = "blocked_all_topology_contradictions", None
        class_winners, comparison = {}, None
    else:
        class_winners = {}
        tied_class = None
        for class_name in classes:
            ordered = sorted(
                survivors,
                key=lambda item: (item["class_score_vectors"][class_name], item["candidate_id"]),
            )
            best_vector = ordered[0]["class_score_vectors"][class_name]
            tied = [item["candidate_id"] for item in ordered if item["class_score_vectors"][class_name] == best_vector]
            if len(tied) != 1:
                tied_class = class_name
            class_winners[class_name] = tied
        unique_winners = {values[0] for values in class_winners.values() if len(values) == 1}
        if tied_class is not None:
            status, winner, comparison = "blocked_required_class_tie", None, None
        elif len(unique_winners) != 1:
            status, winner, comparison = "blocked_required_class_conflict", None, None
        else:
            ordered = sorted(
                survivors, key=lambda item: (item["aggregate_score_vector"], item["candidate_id"])
            )
            if len(ordered) > 1:
                comparison = within_two_percent_at_first_difference(
                    ordered[0]["aggregate_score_vector"], ordered[1]["aggregate_score_vector"]
                )
            else:
                comparison = None
            if comparison is not None and comparison["near_tie"]:
                status, winner = "blocked_competing_map_basin", None
            else:
                status, winner = "selected_by_frozen_policy", ordered[0]["candidate_id"]
                unanimous_class_winner = next(iter(unique_winners))
                if winner != unanimous_class_winner:
                    status, winner = "blocked_aggregate_class_conflict", None
    return {
        "policy_status": status,
        "selected_candidate_id": winner,
        "required_classes": classes,
        "metric_precedence": SCORE_PRECEDENCE,
        "aggregate_rule": "max_first_three_metrics_across_required_classes_and_sum_total_robust_loss",
        "near_tie_fraction": NEAR_TIE_FRACTION,
        "near_tie_comparison": comparison,
        "required_class_winners": class_winners,
        "candidates": [
            {
                **item,
                "class_score_vectors": {key: list(value) for key, value in item["class_score_vectors"].items()},
                "aggregate_score_vector": list(item["aggregate_score_vector"]),
            }
            for item in sorted(normalized, key=lambda value: value["candidate_id"])
        ],
    }


def load_json(path: str, label: str) -> tuple[dict, dict]:
    raw, identity = read_input(path, label)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScoreError(f"{label} is not valid JSON") from exc
    return value, identity


def run(manifest_path: str, scores_path: str) -> dict:
    manifest, manifest_identity = load_json(manifest_path, "lineage_manifest")
    scores, scores_identity = load_json(scores_path, "candidate_scores")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ScoreError("lineage manifest schema is invalid")
    policy = manifest.get("selection_policy") or {}
    if (
        policy.get("lexicographic_score_precedence") != SCORE_PRECEDENCE
        or policy.get("policy_mutable_after_manifest") is not False
        or policy.get("topology_contradiction_precedence") != "reject_before_scoring"
        or policy.get("metric_direction") != "ascending"
        or policy.get("tie_rule")
        != "fail_competing_map_basin_when_first_differing_metric_within_2_percent"
        or policy.get("class_conflict_rule")
        != "fail_when_different_required_classes_prefer_different_candidates"
    ):
        raise ScoreError("lineage manifest does not contain the frozen executable policy")
    expected_ids = {
        item.get("candidate_id") for item in manifest.get("candidates", [])
        if isinstance(item, dict)
    }
    if not expected_ids or None in expected_ids or len(expected_ids) != len(manifest.get("candidates", [])):
        raise ScoreError("lineage manifest candidate IDs are invalid")
    if scores.get("lineage_manifest_sha256") != manifest_identity["sha256"]:
        raise ScoreError("scores are not bound to the exact lineage manifest")
    decision = evaluate_score_document(scores, expected_ids)
    unresolved = (manifest.get("lineage_reconciliation") or {}).get("status") != "resolved"
    if unresolved:
        decision["selected_candidate_id"] = None
        decision["policy_status"] = "blocked_unresolved_opendrive_lineage"
    return {
        "schema": OUTPUT_SCHEMA,
        "created_at_utc": utc_now(),
        "acceptance_eligible": False,
        "lineage_manifest": manifest_identity,
        "candidate_scores": scores_identity,
        "lineage_status": (manifest.get("lineage_reconciliation") or {}).get("status"),
        "decision": decision,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lineage-manifest", required=True)
    parser.add_argument("--scores", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        report = run(args.lineage_manifest, args.scores)
        publish_no_replace(args.output, report)
    except (LineageError, ScoreError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
