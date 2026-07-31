#!/usr/bin/env python3
"""Compose Phase C0 reports into a non-releasing dynamic feasibility verdict."""

from __future__ import annotations

import argparse
from collections import Counter

from apps.bridge.tools.tier_b_c0_common import (
    CAMERAS, C0Error, NON_RELEASE_FLAGS, bind_file, exact_keys, finite_number,
    generator, identity_bindings, load_bound_json, nonblank, publish_no_replace,
    read_input_json, require_non_release,
)
from apps.bridge.tools.build_tier_b_relative_clock import SCHEMA as CLOCK_SCHEMA
from apps.bridge.tools.build_tier_b_track_split import SCHEMA as SPLIT_SCHEMA

SCHEMA = "v2x-tier-b-dynamic-feasibility/v1"
STATIC_SCHEMA = "v2x-tier-b-static-development-fit/v2"
MAP_SCHEMA = "v2x-map-candidate-lineage-manifest/v1"
TOPOLOGY_SCHEMA = "v2x-tier-b-topology-report/v1"
DENSE_SCHEMA = "v2x-dense-vehicle-track-proposals/v1"
MIN_PAIR_CLASS = 59
MIN_KAPPA = .80
MIN_SIMILARITY = .60
MIN_ELIGIBLE = .80
MIN_TRACKLETS_TOTAL = 30
MIN_TRACKLETS_CAMERA = 5
MIN_CONTACTS_TOTAL = 59
MIN_CONTACTS_CAMERA = 12
MIN_TRANSITIONS_TOTAL = 20
MIN_TRANSITIONS_CAMERA = 5
MIN_TRANSITIONS_PAIR = 2


def _kappa(left, right):
    if len(left) != len(right) or not left:
        raise C0Error("reviewer labels are incomplete")
    agreement = sum(a == b for a, b in zip(left, right)) / len(left)
    left_true = sum(left) / len(left); right_true = sum(right) / len(right)
    if not (0 < left_true < 1) or not (0 < right_true < 1):
        raise C0Error("reviewer labels are degenerate and have no class variance")
    expected = left_true * right_true + (1 - left_true) * (1 - right_true)
    return (agreement - expected) / (1 - expected)


def _pair(value):
    if (not isinstance(value, list) or len(value) != 2 or value[0] not in CAMERAS
            or value[1] not in CAMERAS or value[0] >= value[1]):
        raise C0Error("camera pair is invalid")
    return tuple(value)


def validate(document):
    exact_keys(document, {
        "schema", *NON_RELEASE_FLAGS, "generator", "bindings", "identities",
        "identity_reviews", "predictive_tracklets", "contact_audits", "transitions",
        "terminal_accounting",
    }, "dynamic feasibility")
    if document["schema"] != SCHEMA:
        raise C0Error("dynamic feasibility schema is invalid")
    require_non_release(document, "dynamic feasibility")
    generated = generator(document["generator"])
    bindings_value = exact_keys(document["bindings"], {
        "static_report", "map_lineage_manifest", "topology_report", "clock_report",
        "track_split_report", "dense_proposal_manifest",
    }, "feasibility bindings")
    parsed, bindings = {}, {}
    for name, value in bindings_value.items():
        parsed[name], bindings[name] = load_bound_json(value, name)
    expected_schemas = {
        "static_report": STATIC_SCHEMA, "map_lineage_manifest": MAP_SCHEMA,
        "topology_report": TOPOLOGY_SCHEMA, "clock_report": CLOCK_SCHEMA,
        "track_split_report": SPLIT_SCHEMA, "dense_proposal_manifest": DENSE_SCHEMA,
    }
    for name, schema in expected_schemas.items():
        if parsed[name].get("schema") != schema:
            raise C0Error(f"{name} schema is invalid")
    for name in ("clock_report", "track_split_report"):
        require_non_release(parsed[name], name)
    if (parsed["static_report"].get("acceptance_eligible") is not False
            or parsed["static_report"].get("proposal_only") is not True
            or parsed["static_report"].get("release_eligible") is not False
            or parsed["static_report"].get("holdout_consumed") is not False
            or parsed["static_report"].get("development_gate_passed") is not True):
        raise C0Error("accepted static development report is required")
    if any(parsed["dense_proposal_manifest"].get(key) is not value
           for key, value in NON_RELEASE_FLAGS.items()):
        raise C0Error("dense proposal manifest must remain non-acceptance evidence")
    if parsed["clock_report"].get("status") != "PASS":
        raise C0Error("relative clock report did not pass")
    topology = parsed["topology_report"]
    exact_keys(topology, {"schema", "map_candidate_id", "observed_camera_pairs",
                          "corpus_cutoff_utc", "config_sha256"}, "topology report")
    observed_rows = [_pair(item) for item in topology["observed_camera_pairs"]]
    if len(observed_rows) != len(set(observed_rows)):
        raise C0Error("topology observed camera pairs are duplicated")
    observed_pairs = set(observed_rows)
    if not observed_pairs:
        raise C0Error("topology has no observed overlap pairs")
    clock_pairs = {_pair(item) for item in parsed["clock_report"].get("observed_pairs", [])}
    if clock_pairs != observed_pairs:
        raise C0Error("clock observed pairs disagree with topology")
    split_report = parsed["track_split_report"]
    if split_report.get("dense_proposal_manifest", {}).get("sha256") != bindings["dense_proposal_manifest"]["sha256"]:
        raise C0Error("track split and feasibility dense manifest disagree")
    if parsed["clock_report"].get("track_split_report", {}).get("sha256") != bindings["track_split_report"]["sha256"]:
        raise C0Error("clock and feasibility track split disagree")
    if parsed["clock_report"].get("topology_report", {}).get("sha256") != bindings["topology_report"]["sha256"]:
        raise C0Error("clock and feasibility topology disagree")
    corpus = split_report.get("corpus") or {}
    if corpus.get("holdout_consumed") is not False or corpus.get("holdout_burned") is not False:
        raise C0Error("track split holdout state is not pristine")
    if corpus.get("cutoff_utc") != topology["corpus_cutoff_utc"]:
        raise C0Error("corpus cutoff disagrees across reports")
    config_hashes = {
        parsed["static_report"].get("cameras_json", {}).get("sha256"),
        split_report.get("identities", {}).get("config", {}).get("sha256"),
        parsed["clock_report"].get("identities", {}).get("config", {}).get("sha256"),
        topology["config_sha256"],
    }
    identities = identity_bindings(document["identities"])
    config_hashes.add(identities["config"]["sha256"])
    if None in config_hashes or len(config_hashes) != 1:
        raise C0Error("config identity disagrees across reports")
    map_candidate = topology["map_candidate_id"]
    if parsed["static_report"].get("map", {}).get("candidate_id") != map_candidate:
        raise C0Error("static/topology map candidate disagrees")
    lineage_ids = {item.get("candidate_id") for item in parsed["map_lineage_manifest"].get("candidates", [])}
    if map_candidate not in lineage_ids:
        raise C0Error("topology candidate is absent from map lineage")
    development_identities = {
        group.get("identity_id") for group in split_report.get("groups", [])
        if group.get("split") == "development"
    }

    reviews_value = document["identity_reviews"]
    if not isinstance(reviews_value, list):
        raise C0Error("identity reviews must be a list")
    review_pairs, review_reports, identity_insufficient = set(), [], False
    for pair_index, review in enumerate(reviews_value):
        exact_keys(review, {"camera_pair", "similarity_floor", "floor_split", "frozen_before_holdout",
                            "hard_negative_policy", "reviewer_a", "reviewer_b", "cases"},
                   f"identity review[{pair_index}]")
        pair = _pair(review["camera_pair"])
        if pair in review_pairs:
            raise C0Error("identity review pair is duplicated")
        review_pairs.add(pair)
        floor = finite_number(review["similarity_floor"], "similarity floor", minimum=0)
        if floor < MIN_SIMILARITY or review["floor_split"] != "development" or review["frozen_before_holdout"] is not True:
            raise C0Error("identity floor was not safely frozen")
        hard_policy = bind_file(review["hard_negative_policy"], "hard-negative policy")
        reviewer_a = nonblank(review["reviewer_a"], "reviewer A")
        reviewer_b = nonblank(review["reviewer_b"], "reviewer B")
        if reviewer_a == reviewer_b:
            raise C0Error("identity reviewers must be distinct")
        cases = review["cases"]
        if not isinstance(cases, list):
            raise C0Error("identity review cases must be a list")
        ids, identities_seen, labels_a, labels_b, counts, errors = set(), set(), [], [], Counter(), 0
        for case in cases:
            exact_keys(case, {"case_id", "identity_id", "truth_class", "reviewer_a_match",
                              "reviewer_b_match", "adjudicated_match", "matcher_match", "clip_a",
                              "clip_b", "reviewers_blind", "time_disjoint", "split"}, "identity case")
            case_id = nonblank(case["case_id"], "identity case ID")
            identity_id = nonblank(case["identity_id"], "identity case identity")
            if case_id in ids or identity_id in identities_seen:
                raise C0Error("identity cases must be independent identities")
            ids.add(case_id); identities_seen.add(identity_id)
            truth_class = case["truth_class"]
            if truth_class not in {"positive", "hard_negative"}:
                raise C0Error("identity truth class is invalid")
            expected_truth = truth_class == "positive"
            for field in ("reviewer_a_match", "reviewer_b_match", "adjudicated_match", "matcher_match"):
                if not isinstance(case[field], bool):
                    raise C0Error("identity labels must be boolean")
            if case["adjudicated_match"] != expected_truth:
                raise C0Error("adjudication disagrees with registered truth class")
            if case["reviewers_blind"] is not True or case["time_disjoint"] is not True or case["split"] != "development":
                raise C0Error("identity review was not blind time-disjoint development evidence")
            if identity_id not in development_identities:
                raise C0Error("identity case does not resolve to the bound development split")
            bind_file(case["clip_a"], f"identity {case_id} clip A")
            bind_file(case["clip_b"], f"identity {case_id} clip B")
            labels_a.append(case["reviewer_a_match"]); labels_b.append(case["reviewer_b_match"])
            counts[truth_class] += 1; errors += case["matcher_match"] != expected_truth
        kappa = _kappa(labels_a, labels_b)
        if kappa < MIN_KAPPA:
            raise C0Error("identity reviewer kappa is below 0.80")
        if counts["positive"] < MIN_PAIR_CLASS or counts["hard_negative"] < MIN_PAIR_CLASS:
            identity_insufficient = True
        if errors:
            raise C0Error("identity pair has adjudicated matcher errors")
        review_reports.append({"camera_pair": list(pair), "positive": counts["positive"],
                               "hard_negative": counts["hard_negative"], "errors": errors,
                               "cohen_kappa": kappa, "similarity_floor": floor,
                               "hard_negative_policy": hard_policy})
    if review_pairs != observed_pairs:
        raise C0Error("identity review pair set disagrees with topology")

    def unique_records(value, required, label):
        if not isinstance(value, list): raise C0Error(f"{label} must be a list")
        seen = set(); rows = []
        for item in value:
            exact_keys(item, required, label)
            item_id = nonblank(item["id"], f"{label} ID")
            if item_id in seen: raise C0Error(f"{label} IDs must be unique")
            seen.add(item_id); rows.append(item)
        return rows
    tracklets = unique_records(document["predictive_tracklets"], {"id", "cameras"}, "tracklet")
    contacts = unique_records(document["contact_audits"], {"id", "camera_id", "error"}, "contact")
    transitions = unique_records(document["transitions"], {"id", "camera_id", "camera_pair"}, "transition")
    tracklet_camera = Counter()
    for row in tracklets:
        if not isinstance(row["cameras"], list) or not row["cameras"]:
            raise C0Error("tracklet cameras are invalid")
        for camera in set(row["cameras"]):
            if camera not in CAMERAS: raise C0Error("tracklet camera is invalid")
            tracklet_camera[camera] += 1
    contact_camera = Counter()
    for row in contacts:
        if row["camera_id"] not in CAMERAS or not isinstance(row["error"], bool):
            raise C0Error("contact audit is invalid")
        if row["error"]: raise C0Error("contact audit contains an error")
        contact_camera[row["camera_id"]] += 1
    transition_camera, transition_pair = Counter(), Counter()
    for row in transitions:
        if row["camera_id"] not in CAMERAS: raise C0Error("transition camera is invalid")
        pair = _pair(row["camera_pair"])
        if pair not in observed_pairs: raise C0Error("transition pair is unobserved")
        if row["camera_id"] not in pair: raise C0Error("transition camera is absent from its pair")
        transition_camera[row["camera_id"]] += 1; transition_pair[pair] += 1
    insufficient = (
        len(tracklets) < MIN_TRACKLETS_TOTAL or any(tracklet_camera[c] < MIN_TRACKLETS_CAMERA for c in CAMERAS)
        or len(contacts) < MIN_CONTACTS_TOTAL or any(contact_camera[c] < MIN_CONTACTS_CAMERA for c in CAMERAS)
        or len(transitions) < MIN_TRANSITIONS_TOTAL or any(transition_camera[c] < MIN_TRANSITIONS_CAMERA for c in CAMERAS)
        or any(transition_pair[p] < MIN_TRANSITIONS_PAIR for p in observed_pairs)
    )
    accounting = exact_keys(document["terminal_accounting"], {
        "recoverable", "eligible", "structured_rejected", "authoritative_aged_out"
    }, "feasibility terminal accounting")
    for key, value in accounting.items():
        if not isinstance(value, int) or value < 0: raise C0Error("terminal counts must be nonnegative integers")
    if accounting["eligible"] + accounting["structured_rejected"] + accounting["authoritative_aged_out"] != accounting["recoverable"]:
        raise C0Error("feasibility terminal accounting is not exact")
    fraction = accounting["eligible"] / accounting["recoverable"] if accounting["recoverable"] else 0
    if fraction < MIN_ELIGIBLE:
        raise C0Error("eligible denominator is below 80 percent")
    status = "INSUFFICIENT" if insufficient or identity_insufficient else "PASS"
    return {
        "schema": SCHEMA, **NON_RELEASE_FLAGS, "status": status,
        "generator": generated, "bindings": bindings, "identities": identities,
        "observed_pairs": [list(pair) for pair in sorted(observed_pairs)],
        "identity_reviews": review_reports,
        "coverage": {"predictive_tracklets_total": len(tracklets), "tracklets_per_camera": dict(tracklet_camera),
                     "contact_audits_total": len(contacts), "contacts_per_camera": dict(contact_camera),
                     "transitions_total": len(transitions), "transitions_per_camera": dict(transition_camera),
                     "transitions_per_pair": {"-".join(pair): transition_pair[pair] for pair in observed_pairs}},
        "terminal_accounting": dict(accounting), "eligible_fraction": fraction,
        "thresholds": {"identity_positive_per_pair": MIN_PAIR_CLASS,
                       "identity_hard_negative_per_pair": MIN_PAIR_CLASS,
                       "minimum_kappa": MIN_KAPPA, "minimum_similarity": MIN_SIMILARITY,
                       "minimum_eligible_fraction": MIN_ELIGIBLE,
                       "predictive_tracklets_total": MIN_TRACKLETS_TOTAL,
                       "predictive_tracklets_per_camera": MIN_TRACKLETS_CAMERA,
                       "contact_audits_total": MIN_CONTACTS_TOTAL,
                       "contact_audits_per_camera": MIN_CONTACTS_CAMERA,
                       "transitions_total": MIN_TRANSITIONS_TOTAL,
                       "transitions_per_camera": MIN_TRANSITIONS_CAMERA,
                       "transitions_per_pair": MIN_TRANSITIONS_PAIR},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input"); parser.add_argument("--output", required=True)
    args = parser.parse_args(argv); document, input_sha256 = read_input_json(args.input)
    report = validate(document); report["input_sha256"] = input_sha256
    publish_no_replace(args.output, report); print(args.output); return 0


if __name__ == "__main__":
    raise SystemExit(main())
