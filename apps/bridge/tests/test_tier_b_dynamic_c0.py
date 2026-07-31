import copy
import hashlib
import json
import math
from pathlib import Path

import pytest

from apps.bridge.tools import tier_b_c0_common as common
from apps.bridge.tools.tier_b_c0_common import C0Error, NON_RELEASE_FLAGS, publish_no_replace
from apps.bridge.tools import build_tier_b_track_split as split_tool
from apps.bridge.tools import build_tier_b_relative_clock as clock_tool
from apps.bridge.tools import build_tier_b_dynamic_feasibility as dynamic_tool


GENERATOR = {"commit": "a" * 40, "worktree_clean": True}


def artifact(path: Path, value):
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def identities(root: Path):
    return {"config": artifact(root / "config.json", b"config"),
            "models": [artifact(root / "model.bin", b"model")],
            "runtime": artifact(root / "runtime.json", b"runtime")}


def flags():
    return dict(NON_RELEASE_FLAGS)


def split_document(root: Path, dense):
    ids = identities(root / "ids")
    groups = []
    for index, camera in enumerate(("ch1", "ch2", "ch3", "ch4")):
        groups.append({
            "group_id": f"g{index}", "split": "development", "camera_id": camera,
            "track_id": f"track{index}", "identity_id": f"identity{index}",
            "clip": artifact(root / f"clip{index}.bin", f"clip{index}".encode()),
            "evidence": [artifact(root / f"evidence{index}.bin", f"evidence{index}".encode())],
            "source_hashes": [hashlib.sha256(f"source{index}".encode()).hexdigest()],
            "capture_epoch": f"capture{index}", "mount_epoch": f"mount{index}",
            "start_ms": index * 10_000, "end_ms": index * 10_000 + 1000,
            "ambiguity_ids": [f"ambiguity{index}"],
            "derived_feature_ids": [f"derived{index}"],
            "terminal_state": "accepted", "terminal_reason": None,
        })
    identity_rows = []
    for a, b in (("ch1", "ch2"), ("ch2", "ch3"), ("ch3", "ch4")):
        for index in range(30):
            identity_rows.append((a, f"{a}-{b}-identity-{index}", f"epoch-{index // 10}"))
        for truth in ("positive", "hard_negative"):
            for index in range(59):
                identity_rows.append((a, f"identity-{a}-{b}-{truth}-{index}", "review-epoch"))
    for offset, (camera, identity_id, epoch) in enumerate(identity_rows, start=len(groups)):
        groups.append({
            "group_id": f"g{offset}", "split": "development", "camera_id": camera,
            "track_id": f"track{offset}", "identity_id": identity_id,
            "clip": artifact(root / f"clip{offset}.bin", f"clip{offset}".encode()),
            "evidence": [artifact(root / f"evidence{offset}.bin", f"evidence{offset}".encode())],
            "source_hashes": [hashlib.sha256(f"source{offset}".encode()).hexdigest()],
            "capture_epoch": epoch, "mount_epoch": f"mount{offset}",
            "start_ms": offset * 10_000, "end_ms": offset * 10_000 + 1000,
            "ambiguity_ids": [f"ambiguity{offset}"], "derived_feature_ids": [f"derived{offset}"],
            "terminal_state": "accepted", "terminal_reason": None,
        })
    return {"schema": split_tool.SCHEMA, **flags(), "generator": GENERATOR,
            "dense_proposal_manifest": dense,
            "corpus": {"cutoff_utc": "2026-07-14T00:00:00Z", "pagination_root": "root",
                       "cursor_sha256": "b" * 64,
                       "exclusion_policy": artifact(root / "exclusions.json", b"exclusions"),
                       "adjacency_buffer_ms": 1000, "holdout_generation": "generation-0",
                       "holdout_consumed": False, "holdout_burned": False},
            "identities": ids, "groups": groups,
            "terminal_accounting": {"recoverable": len(groups), "accepted": len(groups),
                                    "structured_rejected": 0, "authoritative_aged_out": 0}}


def clock_document(root: Path, split_binding, topology_binding, ids):
    edges = []
    for edge_index, (a, b) in enumerate((("ch1", "ch2"), ("ch2", "ch3"), ("ch3", "ch4"))):
        events = []
        for index in range(30):
            time = index * (6 * 3_600_000 / 29)
            residual = 12.0 + ((index % 5) - 2) * .4 + edge_index
            events.append({"event_id": f"{a}-{b}-event-{index}",
                           "identity_id": f"{a}-{b}-identity-{index}",
                           "epoch_id": f"epoch-{index // 10}", "split": "development",
                           "time_a_ms": time, "time_b_ms": time + residual,
                           "reciprocal_a": True, "reciprocal_b": True, "trusted_v2": True})
        raw_log = {"schema": "v2x-tier-b-raw-reciprocal-matches/v1",
                   "camera_a": a, "camera_b": b,
                   "detection_ids_a": [item["event_id"] for item in events],
                   "detection_ids_b": [item["event_id"] for item in events],
                   "events": events}
        edges.append({"camera_a": a, "camera_b": b, "total_a": 30, "total_b": 30,
                      "events": events,
                      "raw_match_log": artifact(root / f"raw-{a}-{b}.json", raw_log)})
    return {"schema": clock_tool.SCHEMA, **flags(), "generator": GENERATOR,
            "claim": "relative_only_not_exposure_or_gnss_truth",
            "track_split_report": split_binding, "topology_report": topology_binding,
            "sources": [artifact(root / "clock-source.json", b"clock-source")],
            "identities": ids, "permitted_splits": ["development"],
            "detector": {"artifact": artifact(root / "detector.bin", b"detector"),
                         "residual_blind": True},
            "trust_predicate": artifact(root / "trust.py", b"trusted-v2"),
            "matcher_artifact": artifact(root / "matcher.py", b"matcher"),
            "matching_method": "reciprocal_leave_one_event_out_one_to_one",
            "bootstrap": {"method": "pre_registered_event_cluster_bootstrap",
                          "replicates": 1000, "seed": 42},
            "injection": {"injector": artifact(root / "injector.py", b"injector"),
                          "recover_injected_ms": 50, "recovered_ms": 50.5,
                          "recovery_error_max_ms": 5, "reject_injected_ms": 300,
                          "reject_detected": True, "evaluator_only": True},
            "edges": edges}


@pytest.fixture(scope="module")
def foundation(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("tier-b-c0")
    config_ids = identities(tmp_path / "shared-ids")
    dense_doc = {"schema": dynamic_tool.DENSE_SCHEMA, **flags()}
    dense = artifact(tmp_path / "dense.json", dense_doc)
    split_doc = split_document(tmp_path / "split-input", dense)
    # Force all three reports to share the exact config identity.
    split_doc["identities"] = config_ids
    split_report = split_tool.validate(split_doc)
    split_binding = artifact(tmp_path / "split-report.json", split_report)
    topology_doc = {"schema": dynamic_tool.TOPOLOGY_SCHEMA, "map_candidate_id": "map-A",
                    "observed_camera_pairs": [["ch1", "ch2"], ["ch2", "ch3"], ["ch3", "ch4"]],
                    "corpus_cutoff_utc": "2026-07-14T00:00:00Z",
                    "config_sha256": config_ids["config"]["sha256"]}
    topology = artifact(tmp_path / "topology.json", topology_doc)
    clock_doc = clock_document(tmp_path / "clock-input", split_binding, topology, config_ids)
    clock_report = clock_tool.validate(clock_doc)
    clock_binding = artifact(tmp_path / "clock-report.json", clock_report)
    static_doc = {"schema": dynamic_tool.STATIC_SCHEMA, "acceptance_eligible": False,
                  "proposal_only": True, "release_eligible": False,
                  "holdout_consumed": False, "development_gate_passed": True,
                  "map": {"candidate_id": "map-A"},
                  "cameras_json": {"sha256": config_ids["config"]["sha256"]}}
    lineage_doc = {"schema": dynamic_tool.MAP_SCHEMA,
                   "candidates": [{"candidate_id": "map-A"}]}
    bindings = {
        "static_report": artifact(tmp_path / "static.json", static_doc),
        "map_lineage_manifest": artifact(tmp_path / "lineage.json", lineage_doc),
        "topology_report": topology, "clock_report": clock_binding,
        "track_split_report": split_binding, "dense_proposal_manifest": dense,
    }
    return {"root": tmp_path, "ids": config_ids, "dense": dense,
            "split_doc": split_doc, "clock_doc": clock_doc, "bindings": bindings}


def dynamic_document(foundation):
    root = foundation["root"] / "dynamic-input"
    observed = (("ch1", "ch2"), ("ch2", "ch3"), ("ch3", "ch4"))
    reviews = []
    clip_a = artifact(root / "review-a.bin", b"review-a")
    clip_b = artifact(root / "review-b.bin", b"review-b")
    hard_policy = artifact(root / "hard-policy.json", b"hard-policy")
    for a, b in observed:
        cases = []
        for truth, count in (("positive", 59), ("hard_negative", 59)):
            match = truth == "positive"
            for index in range(count):
                cases.append({"case_id": f"{a}-{b}-{truth}-{index}",
                              "identity_id": f"identity-{a}-{b}-{truth}-{index}",
                              "truth_class": truth, "reviewer_a_match": match,
                              "reviewer_b_match": match, "adjudicated_match": match,
                              "matcher_match": match, "clip_a": clip_a, "clip_b": clip_b,
                              "reviewers_blind": True, "time_disjoint": True,
                              "split": "development"})
        reviews.append({"camera_pair": [a, b], "similarity_floor": .60,
                        "floor_split": "development", "frozen_before_holdout": True,
                        "hard_negative_policy": hard_policy, "reviewer_a": "reviewer-A",
                        "reviewer_b": "reviewer-B", "cases": cases})
    tracklets = [{"id": f"tracklet-{i}", "cameras": [f"ch{i % 4 + 1}"]} for i in range(32)]
    contacts = [{"id": f"contact-{i}", "camera_id": f"ch{i % 4 + 1}", "error": False}
                for i in range(60)]
    transitions = []; pair_occurrence = {pair: 0 for pair in observed}
    for i in range(24):
        pair = observed[i % len(observed)]
        occurrence = pair_occurrence[pair]; pair_occurrence[pair] += 1
        a_limits = {observed[0]: 5, observed[1]: 4, observed[2]: 3}
        camera = pair[0] if occurrence < a_limits[pair] else pair[1]
        transitions.append({"id": f"transition-{i}", "camera_id": camera,
                            "camera_pair": list(pair)})
    return {"schema": dynamic_tool.SCHEMA, **flags(), "generator": GENERATOR,
            "bindings": foundation["bindings"], "identities": foundation["ids"],
            "identity_reviews": reviews, "predictive_tracklets": tracklets,
            "contact_audits": contacts, "transitions": transitions,
            "terminal_accounting": {"recoverable": 100, "eligible": 80,
                                    "structured_rejected": 15, "authoritative_aged_out": 5}}


def test_track_split_binds_whole_groups_and_terminal_accounting(foundation):
    report = split_tool.validate(foundation["split_doc"])
    assert report["schema"] == split_tool.SCHEMA and report["release_eligible"] is False
    assert report["terminal_accounting"]["recoverable"] == len(foundation["split_doc"]["groups"])


@pytest.mark.parametrize("member", ["track_id", "identity_id", "capture_epoch", "mount_epoch",
                                     "ambiguity_ids", "derived_feature_ids"])
def test_track_split_rejects_member_laundering(foundation, member):
    doc = copy.deepcopy(foundation["split_doc"])
    doc["groups"][1]["split"] = "fit"
    if member.endswith("_ids"):
        doc["groups"][1][member] = copy.deepcopy(doc["groups"][0][member])
    else:
        doc["groups"][1][member] = doc["groups"][0][member]
    with pytest.raises(C0Error, match="crosses splits"):
        split_tool.validate(doc)


@pytest.mark.parametrize("member", ["clip", "evidence", "source_hashes"])
def test_track_split_rejects_artifact_laundering(foundation, member):
    doc = copy.deepcopy(foundation["split_doc"])
    doc["groups"][1]["split"] = "fit"
    doc["groups"][1]["start_ms"] = 20_000_000
    doc["groups"][1]["end_ms"] = 20_001_000
    doc["groups"][1][member] = copy.deepcopy(doc["groups"][0][member])
    with pytest.raises(C0Error, match="crosses splits"):
        split_tool.validate(doc)


def test_track_split_rejects_consumed_or_burned_holdout(foundation):
    for key in ("holdout_consumed", "holdout_burned"):
        doc = copy.deepcopy(foundation["split_doc"]); doc["corpus"][key] = True
        with pytest.raises(C0Error, match="unconsumed, unburned"):
            split_tool.validate(doc)


def test_track_split_rejects_adjacent_windows_and_bad_accounting(foundation):
    doc = copy.deepcopy(foundation["split_doc"])
    doc["groups"][1]["split"] = "fit"; doc["groups"][1]["start_ms"] = 1500
    doc["groups"][1]["end_ms"] = 2500
    with pytest.raises(C0Error, match="adjacent windows"):
        split_tool.validate(doc)


def test_track_split_holdout_is_metadata_only_and_never_opened(foundation):
    doc = copy.deepcopy(foundation["split_doc"])
    group = doc["groups"][0]
    group["split"] = "untouched_holdout"
    group["start_ms"] = 10_000_000
    group["end_ms"] = 10_001_000
    group["clip"] = {"artifact_id": "sealed-clip-0", "sha256": "c" * 64,
                     "bytes": 123, "seal_generation": "generation-0", "never_open": True}
    group["evidence"] = [{"artifact_id": "sealed-evidence-0", "sha256": "d" * 64,
                          "bytes": 456, "seal_generation": "generation-0", "never_open": True}]
    report = split_tool.validate(doc)
    assert report["groups"][0]["clip"]["never_open"] is True


def test_track_split_holdout_rejects_path_binding(foundation):
    doc = copy.deepcopy(foundation["split_doc"])
    doc["groups"][0]["split"] = "untouched_holdout"
    with pytest.raises(C0Error, match="exact fields"):
        split_tool.validate(doc)
    doc = copy.deepcopy(foundation["split_doc"]); doc["terminal_accounting"]["recoverable"] = 3
    with pytest.raises(C0Error, match="accounting"):
        split_tool.validate(doc)


def test_relative_clock_passes_strict_raw_and_bootstrap_gates(foundation):
    report = clock_tool.validate(foundation["clock_doc"])
    assert report["status"] == "PASS" and len(report["edges"]) == 3
    assert all(edge["bootstrap_95_upper"]["p95_absolute_residual_ms"] <= 50
               for edge in report["edges"])


@pytest.mark.parametrize("mutation,error", [
    ("disconnect", "disconnected"), ("reciprocal", "reciprocal"),
    ("zero", "zero-residual"), ("drift", "upper bound"),
    ("duplicate_identity", "independent"), ("untrusted", "trusted"),
])
def test_relative_clock_false_greens_fail_closed(foundation, mutation, error):
    doc = copy.deepcopy(foundation["clock_doc"])
    if mutation == "disconnect": doc["edges"] = doc["edges"][:2]
    elif mutation == "reciprocal":
        for event in doc["edges"][0]["events"][:7]: event["reciprocal_a"] = False
    elif mutation == "zero":
        for event in doc["edges"][0]["events"]: event["time_b_ms"] = event["time_a_ms"]
    elif mutation == "drift":
        for i, event in enumerate(doc["edges"][0]["events"]): event["time_b_ms"] += i * 100
    elif mutation == "duplicate_identity":
        doc["edges"][0]["events"][1]["identity_id"] = doc["edges"][0]["events"][0]["identity_id"]
    else: doc["edges"][0]["events"][0]["trusted_v2"] = False
    if mutation != "disconnect":
        edge = doc["edges"][0]
        raw = {"schema": "v2x-tier-b-raw-reciprocal-matches/v1",
               "camera_a": edge["camera_a"], "camera_b": edge["camera_b"],
               "detection_ids_a": [item["event_id"] for item in edge["events"]],
               "detection_ids_b": [item["event_id"] for item in edge["events"]],
               "events": edge["events"]}
        edge["raw_match_log"] = artifact(foundation["root"] / f"raw-mutation-{mutation}.json", raw)
        with pytest.raises(C0Error, match=error): clock_tool.validate(doc)


@pytest.mark.parametrize("mutation", ["recover", "reject"])
def test_relative_clock_rejects_failed_injection(foundation, mutation):
    doc = copy.deepcopy(foundation["clock_doc"])
    if mutation == "recover": doc["injection"]["recovered_ms"] = 56
    else: doc["injection"]["reject_detected"] = False
    with pytest.raises(C0Error, match="injection"):
        clock_tool.validate(doc)


def test_relative_clock_rejects_duplicate_event_id(foundation):
    doc = copy.deepcopy(foundation["clock_doc"])
    doc["edges"][0]["events"][1]["event_id"] = doc["edges"][0]["events"][0]["event_id"]
    with pytest.raises(C0Error, match="independent"):
        clock_tool.validate(doc)


def test_dynamic_feasibility_passes_unpooled_pair_and_coverage_gates(foundation):
    report = dynamic_tool.validate(dynamic_document(foundation))
    assert report["status"] == "PASS" and report["eligible_fraction"] == .8
    assert all(row["positive"] == row["hard_negative"] == 59 for row in report["identity_reviews"])


@pytest.mark.parametrize("mutation,error", [
    ("pair_shrink", "pair set"), ("floor", "floor"), ("pooled", "59 positive"),
    ("matcher_error", "matcher errors"), ("kappa", "kappa"),
    ("eligibility", "80 percent"), ("config", "config identity"),
])
def test_dynamic_feasibility_composition_false_greens_fail(foundation, mutation, error):
    doc = dynamic_document(foundation)
    if mutation == "pair_shrink": doc["identity_reviews"] = doc["identity_reviews"][:-1]
    elif mutation == "floor": doc["identity_reviews"][0]["similarity_floor"] = .59
    elif mutation == "pooled": doc["identity_reviews"][0]["cases"] = doc["identity_reviews"][0]["cases"][:-1]
    elif mutation == "matcher_error": doc["identity_reviews"][0]["cases"][0]["matcher_match"] = False
    elif mutation == "kappa":
        for case in doc["identity_reviews"][0]["cases"][:30]: case["reviewer_b_match"] = not case["reviewer_b_match"]
    elif mutation == "eligibility": doc["terminal_accounting"].update(eligible=79, structured_rejected=16)
    else:
        other = artifact(foundation["root"] / "other-config.json", b"other-config")
        doc["identities"] = copy.deepcopy(doc["identities"]); doc["identities"]["config"] = other
    if mutation == "pooled":
        assert dynamic_tool.validate(doc)["status"] == "INSUFFICIENT"
    else:
        with pytest.raises(C0Error, match=error): dynamic_tool.validate(doc)


def test_dynamic_insufficient_is_explicit_nonrelease(foundation):
    doc = dynamic_document(foundation); doc["predictive_tracklets"] = doc["predictive_tracklets"][:20]
    report = dynamic_tool.validate(doc)
    assert report["status"] == "INSUFFICIENT"
    assert report["release_eligible"] is False


@pytest.mark.parametrize("mutation", ["same_reviewer", "not_blind", "not_disjoint"])
def test_dynamic_rejects_reviewer_leakage(foundation, mutation):
    doc = dynamic_document(foundation)
    if mutation == "same_reviewer": doc["identity_reviews"][0]["reviewer_b"] = "reviewer-A"
    elif mutation == "not_blind": doc["identity_reviews"][0]["cases"][0]["reviewers_blind"] = False
    else: doc["identity_reviews"][0]["cases"][0]["time_disjoint"] = False
    with pytest.raises(C0Error, match="reviewers|blind"):
        dynamic_tool.validate(doc)


def test_dynamic_rejects_degenerate_constant_reviewer_labels(foundation):
    doc = dynamic_document(foundation)
    for case in doc["identity_reviews"][0]["cases"]:
        case["reviewer_a_match"] = case["reviewer_b_match"] = True
    with pytest.raises(C0Error, match="degenerate"):
        dynamic_tool.validate(doc)


def test_dynamic_rejects_duplicate_case_identity_and_incoherent_transition(foundation):
    doc = dynamic_document(foundation)
    doc["identity_reviews"][0]["cases"][1]["identity_id"] = doc["identity_reviews"][0]["cases"][0]["identity_id"]
    with pytest.raises(C0Error, match="independent"):
        dynamic_tool.validate(doc)
    doc = dynamic_document(foundation)
    doc["transitions"][0]["camera_id"] = "ch4"
    with pytest.raises(C0Error, match="absent from its pair"):
        dynamic_tool.validate(doc)


def test_dynamic_rejects_duplicate_topology_pair(foundation):
    topology_path = Path(foundation["bindings"]["topology_report"]["path"])
    topology = json.loads(topology_path.read_text())
    topology["observed_camera_pairs"].append(topology["observed_camera_pairs"][0])
    doc = dynamic_document(foundation)
    doc["bindings"] = copy.deepcopy(doc["bindings"])
    new_topology = artifact(
        foundation["root"] / "topology-duplicate.json", topology)
    doc["bindings"]["topology_report"] = new_topology
    clock = json.loads(Path(doc["bindings"]["clock_report"]["path"]).read_text())
    clock["topology_report"]["sha256"] = new_topology["sha256"]
    doc["bindings"]["clock_report"] = artifact(
        foundation["root"] / "clock-duplicate-topology.json", clock)
    with pytest.raises(C0Error, match="duplicated"):
        dynamic_tool.validate(doc)


def test_bound_file_rejects_hash_drift_and_symlink(tmp_path):
    binding = artifact(tmp_path / "source.bin", b"before")
    Path(binding["path"]).write_bytes(b"after")
    with pytest.raises(C0Error, match="hash mismatch"):
        common.bind_file(binding, "source")
    target = tmp_path / "target.bin"; target.write_bytes(b"target")
    link = tmp_path / "link.bin"; link.symlink_to(target)
    with pytest.raises(C0Error, match="without following links"):
        common.bind_file({"path": str(link), "sha256": hashlib.sha256(b"target").hexdigest()}, "link")


def test_input_and_publication_fail_closed_on_symlink_or_release(tmp_path):
    target = tmp_path / "input.json"; target.write_text('{"safe": true}')
    link = tmp_path / "input-link.json"; link.symlink_to(target)
    with pytest.raises(C0Error, match="without following links"):
        common.read_input_json(link)
    with pytest.raises(C0Error, match="acceptance_eligible"):
        publish_no_replace(str(tmp_path / "bad.json"), {"generator": GENERATOR})


def test_exclusive_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "_derived_generator", lambda: GENERATOR)
    report = {"schema": "test/v1", **flags(), "generator": GENERATOR}
    output = tmp_path / "report.json"; publish_no_replace(str(output), report)
    with pytest.raises(C0Error, match="refusing to replace"):
        publish_no_replace(str(output), report)
