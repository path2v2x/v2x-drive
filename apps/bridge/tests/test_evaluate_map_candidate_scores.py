import hashlib
import json
from pathlib import Path

import pytest

from apps.bridge.tools import build_map_candidate_lineage_manifest as lineage
from apps.bridge.tools import evaluate_map_candidate_scores as scores


METRICS = scores.SCORE_PRECEDENCE


def vector(a, b, c, d):
    return dict(zip(METRICS, (a, b, c, d)))


def document(a_road, a_crosswalk, b_road, b_crosswalk, *, a_block=False, b_block=False):
    return {
        "schema": scores.INPUT_SCHEMA,
        "required_classes": ["crosswalk", "road_edge"],
        "candidates": [
            {"candidate_id": "a", "topology_contradiction": a_block,
             "class_scores": {"road_edge": a_road, "crosswalk": a_crosswalk}},
            {"candidate_id": "b", "topology_contradiction": b_block,
             "class_scores": {"road_edge": b_road, "crosswalk": b_crosswalk}},
        ],
    }


def test_selects_exact_lexicographic_winner_when_all_classes_agree():
    value = document(
        vector(10, 5, 3, 4), vector(8, 4, 2, 3),
        vector(20, 7, 4, 5), vector(18, 6, 3, 4),
    )
    decision = scores.evaluate_score_document(value, {"a", "b"})
    assert decision["policy_status"] == "selected_by_frozen_policy"
    assert decision["selected_candidate_id"] == "a"
    assert decision["metric_precedence"] == METRICS
    assert next(item for item in decision["candidates"] if item["candidate_id"] == "a")[
        "aggregate_score_vector"
    ] == [10.0, 5.0, 3.0, 7.0]


def test_two_percent_first_difference_and_zero_behavior_are_exact():
    near = scores.within_two_percent_at_first_difference((100.0, 1, 1, 1), (101.5, 0, 0, 0))
    assert near == {
        "near_tie": True, "metric": METRICS[0], "difference": 1.5, "denominator": 101.5,
    }
    assert scores.within_two_percent_at_first_difference((0, 1, 0, 0), (0, 2, 0, 0))[
        "near_tie"
    ] is False
    assert scores.within_two_percent_at_first_difference((0, 0, 0, 0), (0, 0, 0, 0)) == {
        "near_tie": True, "metric": None, "difference": 0.0, "denominator": 0.0,
    }
    value = document(
        vector(100, 1, 1, 1), vector(100, 1, 1, 1),
        vector(101.5, 1, 1, 1), vector(101.5, 1, 1, 1),
    )
    decision = scores.evaluate_score_document(value, {"a", "b"})
    assert decision["policy_status"] == "blocked_competing_map_basin"
    assert decision["selected_candidate_id"] is None


def test_required_class_conflict_and_topology_rejection_block_selection():
    conflict = document(
        vector(1, 1, 1, 1), vector(10, 10, 10, 10),
        vector(10, 10, 10, 10), vector(1, 1, 1, 1),
    )
    assert scores.evaluate_score_document(conflict, {"a", "b"})["policy_status"] == (
        "blocked_required_class_conflict"
    )
    blocked = document(
        vector(1, 1, 1, 1), vector(1, 1, 1, 1),
        vector(20, 20, 20, 20), vector(20, 20, 20, 20),
        a_block=True,
    )
    decision = scores.evaluate_score_document(blocked, {"a", "b"})
    assert decision["selected_candidate_id"] == "b"


@pytest.mark.parametrize("bad", [None, "1", True, -1, float("nan"), float("inf")])
def test_invalid_metrics_fail_closed(bad):
    value = document(
        vector(1, 1, 1, 1), vector(1, 1, 1, 1),
        vector(2, 2, 2, 2), vector(2, 2, 2, 2),
    )
    value["candidates"][0]["class_scores"]["road_edge"][METRICS[0]] = bad
    with pytest.raises(scores.ScoreError, match="finite nonnegative"):
        scores.evaluate_score_document(value, {"a", "b"})


def test_executable_binds_manifest_and_keeps_unresolved_lineage_blocked(tmp_path):
    manifest = {
        "schema": scores.MANIFEST_SCHEMA,
        "selection_policy": {
            "lexicographic_score_precedence": METRICS,
            "policy_mutable_after_manifest": False,
            "topology_contradiction_precedence": "reject_before_scoring",
            "metric_direction": "ascending",
            "tie_rule": "fail_competing_map_basin_when_first_differing_metric_within_2_percent",
            "class_conflict_rule": "fail_when_different_required_classes_prefer_different_candidates",
        },
        "lineage_reconciliation": {"status": "unresolved_blocking"},
        "candidates": [{"candidate_id": "a"}, {"candidate_id": "b"}],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    value = document(
        vector(1, 1, 1, 1), vector(1, 1, 1, 1),
        vector(20, 20, 20, 20), vector(20, 20, 20, 20),
    )
    value["lineage_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    score_path = tmp_path / "scores.json"
    score_path.write_text(json.dumps(value))
    output = tmp_path / "decision.json"
    assert scores.main([
        "--lineage-manifest", str(manifest_path), "--scores", str(score_path),
        "--output", str(output),
    ]) == 0
    result = json.loads(output.read_text())
    assert result["decision"]["policy_status"] == "blocked_unresolved_opendrive_lineage"
    assert result["decision"]["selected_candidate_id"] is None
    with pytest.raises(SystemExit, match="refusing to replace"):
        scores.main([
            "--lineage-manifest", str(manifest_path), "--scores", str(score_path),
            "--output", str(output),
        ])


def test_evaluator_negative_bindings_and_terminal_blocks(tmp_path):
    base = document(
        vector(1, 1, 1, 1), vector(1, 1, 1, 1),
        vector(2, 2, 2, 2), vector(2, 2, 2, 2),
    )
    tied = document(
        vector(1, 1, 1, 1), vector(1, 1, 1, 1),
        vector(1, 1, 1, 1), vector(1, 1, 1, 1),
    )
    assert scores.evaluate_score_document(tied, {"a", "b"})["policy_status"] == (
        "blocked_required_class_tie"
    )
    both_blocked = json.loads(json.dumps(base))
    for candidate in both_blocked["candidates"]:
        candidate["topology_contradiction"] = True
    assert scores.evaluate_score_document(both_blocked, {"a", "b"})["policy_status"] == (
        "blocked_all_topology_contradictions"
    )
    with pytest.raises(scores.ScoreError, match="exactly match"):
        scores.evaluate_score_document(base, {"a", "missing"})

    manifest = {
        "schema": scores.MANIFEST_SCHEMA,
        "selection_policy": {
            "lexicographic_score_precedence": METRICS,
            "policy_mutable_after_manifest": False,
            "topology_contradiction_precedence": "reject_before_scoring",
            "metric_direction": "ascending",
            "tie_rule": "fail_competing_map_basin_when_first_differing_metric_within_2_percent",
            "class_conflict_rule": "fail_when_different_required_classes_prefer_different_candidates",
        },
        "lineage_reconciliation": {"status": "resolved"},
        "candidates": [{"candidate_id": "a"}, {"candidate_id": "b"}],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    score_path = tmp_path / "scores.json"
    base["lineage_manifest_sha256"] = "0" * 64
    score_path.write_text(json.dumps(base))
    with pytest.raises(scores.ScoreError, match="exact lineage manifest"):
        scores.run(str(manifest_path), str(score_path))
    manifest["selection_policy"]["tie_rule"] = "weakened"
    manifest_path.write_text(json.dumps(manifest))
    base["lineage_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    score_path.write_text(json.dumps(base))
    with pytest.raises(scores.ScoreError, match="frozen executable policy"):
        scores.run(str(manifest_path), str(score_path))
