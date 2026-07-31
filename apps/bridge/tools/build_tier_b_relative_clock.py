#!/usr/bin/env python3
"""Build a relative-only four-camera clock feasibility report."""

from __future__ import annotations

import argparse
import math

import numpy as np

from apps.bridge.tools.tier_b_c0_common import (
    CAMERAS, C0Error, NON_RELEASE_FLAGS, bind_file, exact_keys, finite_number,
    generator, identity_bindings, load_bound_json, nonblank, publish_no_replace,
    read_input_json, require_non_release,
)
from apps.bridge.tools.build_tier_b_track_split import SCHEMA as SPLIT_SCHEMA

SCHEMA = "v2x-tier-b-relative-clock/v1"
MIN_EVENTS = 30
MIN_EPOCHS = 3
MIN_SPAN_HOURS = 6.0
MIN_RECIPROCAL = 0.80
P95_MAX_MS = 50.0
ABS_MAX_MS = 75.0
DRIFT_MAX_MS_PER_HOUR = 5.0


def _theil_sen(times_ms, residual_ms):
    slopes = []
    for i in range(len(times_ms)):
        for j in range(i + 1, len(times_ms)):
            delta = times_ms[j] - times_ms[i]
            if delta != 0:
                slopes.append((residual_ms[j] - residual_ms[i]) / delta * 3_600_000.0)
    if not slopes:
        raise C0Error("clock events cannot estimate drift")
    return float(np.median(slopes))


def _metrics(events):
    times = np.asarray([(item["time_a_ms"] + item["time_b_ms"]) / 2 for item in events])
    residual = np.asarray([item["time_b_ms"] - item["time_a_ms"] for item in events])
    absolute = np.abs(residual - np.median(residual))
    return {"median_offset_ms": float(np.median(residual)),
            "p95_absolute_residual_ms": float(np.quantile(absolute, .95)),
            "max_absolute_residual_ms": float(np.max(absolute)),
            "absolute_drift_ms_per_hour": abs(_theil_sen(times, residual))}


def _bootstrap(events, seed, replicates):
    rng = np.random.default_rng(seed)
    clusters = {}
    for event in events:
        clusters.setdefault(event["epoch_id"], []).append(event)
    names = sorted(clusters)
    rows = []
    for _ in range(replicates):
        selected = []
        for index in rng.integers(0, len(names), len(names)):
            selected.extend(clusters[names[index]])
        try:
            rows.append(_metrics(selected))
        except C0Error:
            rows.append({"p95_absolute_residual_ms": math.inf,
                         "max_absolute_residual_ms": math.inf,
                         "absolute_drift_ms_per_hour": math.inf})
    return {name: float(np.quantile([row[name] for row in rows], .95)) for name in (
        "p95_absolute_residual_ms", "max_absolute_residual_ms",
        "absolute_drift_ms_per_hour")}


def validate(document):
    exact_keys(document, {
        "schema", *NON_RELEASE_FLAGS, "generator", "claim", "track_split_report",
        "topology_report", "sources", "identities", "permitted_splits", "detector",
        "trust_predicate", "matcher_artifact", "matching_method", "bootstrap", "injection", "edges",
    }, "relative clock")
    if document["schema"] != SCHEMA:
        raise C0Error("relative clock schema is invalid")
    require_non_release(document, "relative clock")
    if document["claim"] != "relative_only_not_exposure_or_gnss_truth":
        raise C0Error("relative clock claim is invalid")
    generated = generator(document["generator"])
    split_doc, split = load_bound_json(document["track_split_report"], "track split report")
    if split_doc.get("schema") != SPLIT_SCHEMA:
        raise C0Error("track split report schema is invalid")
    require_non_release(split_doc, "track split report")
    development = {}
    for group in split_doc.get("groups", []):
        if group.get("split") == "development":
            identity_id = group.get("identity_id")
            if identity_id in development:
                raise C0Error("development split identity is duplicated")
            development[identity_id] = group.get("capture_epoch")
    topology = bind_file(document["topology_report"], "topology report")
    sources_value = document["sources"]
    if not isinstance(sources_value, list) or not sources_value:
        raise C0Error("clock sources must be nonempty")
    sources = [bind_file(item, f"clock source[{i}]") for i, item in enumerate(sources_value)]
    identities = identity_bindings(document["identities"])
    if document["permitted_splits"] != ["development"]:
        raise C0Error("clock C0 may consume only development split")
    detector_value = exact_keys(document["detector"], {"artifact", "residual_blind"}, "detector")
    detector = bind_file(detector_value["artifact"], "clock detector")
    if detector_value["residual_blind"] is not True:
        raise C0Error("clock detector must be residual-blind")
    trust = bind_file(document["trust_predicate"], "trusted-v2 predicate")
    matcher = bind_file(document["matcher_artifact"], "clock matcher")
    if document["matching_method"] != "reciprocal_leave_one_event_out_one_to_one":
        raise C0Error("clock matching method is invalid")
    boot = exact_keys(document["bootstrap"], {"method", "replicates", "seed"}, "bootstrap")
    if boot["method"] != "pre_registered_event_cluster_bootstrap":
        raise C0Error("clock bootstrap method is invalid")
    if not isinstance(boot["replicates"], int) or boot["replicates"] < 1000:
        raise C0Error("clock bootstrap requires at least 1000 replicates")
    if not isinstance(boot["seed"], int):
        raise C0Error("clock bootstrap seed is invalid")
    injection = exact_keys(document["injection"], {
        "injector", "recover_injected_ms", "recovered_ms", "recovery_error_max_ms",
        "reject_injected_ms", "reject_detected", "evaluator_only",
    }, "injection")
    injector = bind_file(injection["injector"], "clock injector")
    recover = finite_number(injection["recover_injected_ms"], "recover injection")
    recovered = finite_number(injection["recovered_ms"], "recovered injection")
    recovery_max = finite_number(injection["recovery_error_max_ms"], "recovery error", minimum=0)
    reject = finite_number(injection["reject_injected_ms"], "reject injection")
    if (abs(recover) != 50 or abs(reject) != 300 or abs(recovered - recover) > recovery_max
            or recovery_max > 5 or injection["reject_detected"] is not True
            or injection["evaluator_only"] is not True):
        raise C0Error("synthetic injection recovery/rejection gate failed")
    edges_value = document["edges"]
    if not isinstance(edges_value, list) or not edges_value:
        raise C0Error("clock edges are missing")
    normalized, pairs, graph = [], set(), {camera: set() for camera in CAMERAS}
    for edge_index, edge in enumerate(edges_value):
        exact_keys(edge, {"camera_a", "camera_b", "total_a", "total_b", "events", "raw_match_log"},
                   f"edge[{edge_index}]")
        a, b = edge["camera_a"], edge["camera_b"]
        if a not in CAMERAS or b not in CAMERAS or a >= b or (a, b) in pairs:
            raise C0Error("clock edge camera pair is invalid or duplicated")
        pairs.add((a, b)); graph[a].add(b); graph[b].add(a)
        events_value = edge["events"]
        if not isinstance(events_value, list) or len(events_value) < MIN_EVENTS:
            raise C0Error("clock edge has fewer than 30 events")
        events, event_ids, identity_ids = [], set(), set()
        for i, event in enumerate(events_value):
            exact_keys(event, {"event_id", "identity_id", "epoch_id", "split",
                               "time_a_ms", "time_b_ms", "reciprocal_a", "reciprocal_b",
                               "trusted_v2"}, f"clock event {i}")
            event_id = nonblank(event["event_id"], "clock event ID")
            identity_id = nonblank(event["identity_id"], "clock identity ID")
            if event_id in event_ids or identity_id in identity_ids:
                raise C0Error("clock events must be independent passage identities")
            event_ids.add(event_id); identity_ids.add(identity_id)
            if event["split"] != "development" or event["trusted_v2"] is not True:
                raise C0Error("clock event is not trusted development evidence")
            if development.get(identity_id) != event["epoch_id"]:
                raise C0Error("clock event does not resolve to its bound development split epoch")
            normalized_event = dict(event)
            normalized_event["time_a_ms"] = finite_number(event["time_a_ms"], "time_a", minimum=0)
            normalized_event["time_b_ms"] = finite_number(event["time_b_ms"], "time_b", minimum=0)
            if not isinstance(event["reciprocal_a"], bool) or not isinstance(event["reciprocal_b"], bool):
                raise C0Error("clock reciprocal flags must be boolean")
            events.append(normalized_event)
        raw_log, raw_binding = load_bound_json(edge["raw_match_log"], f"edge[{edge_index}] raw match log")
        exact_keys(raw_log, {"schema", "camera_a", "camera_b", "detection_ids_a",
                             "detection_ids_b", "events"}, "raw match log")
        if (raw_log["schema"] != "v2x-tier-b-raw-reciprocal-matches/v1"
                or raw_log["camera_a"] != a or raw_log["camera_b"] != b
                or raw_log["events"] != events_value):
            raise C0Error("raw match log disagrees with edge")
        detections_a, detections_b = raw_log["detection_ids_a"], raw_log["detection_ids_b"]
        if (not isinstance(detections_a, list) or not isinstance(detections_b, list)
                or len(detections_a) != len(set(detections_a))
                or len(detections_b) != len(set(detections_b))
                or any(not isinstance(item, str) or not item for item in detections_a + detections_b)):
            raise C0Error("raw match log detection IDs are invalid")
        total_a, total_b = len(detections_a), len(detections_b)
        if edge["total_a"] != total_a or edge["total_b"] != total_b:
            raise C0Error("clock side denominators disagree with raw match log")
        if any((item["reciprocal_a"] and item["event_id"] not in detections_a)
               or (item["reciprocal_b"] and item["event_id"] not in detections_b) for item in events):
            raise C0Error("reciprocal event is absent from raw detections")
        matched_a = sum(item["reciprocal_a"] for item in events)
        matched_b = sum(item["reciprocal_b"] for item in events)
        if matched_a / total_a < MIN_RECIPROCAL or matched_b / total_b < MIN_RECIPROCAL:
            raise C0Error("clock reciprocal match floor failed")
        matched_events = [item for item in events
                          if item["reciprocal_a"] and item["reciprocal_b"]]
        if len(matched_events) < MIN_EVENTS:
            raise C0Error("clock edge has fewer than 30 reciprocal one-to-one events")
        epochs = {item["epoch_id"] for item in matched_events}
        if len(epochs) < MIN_EPOCHS:
            raise C0Error("clock edge requires three epochs")
        epoch_ranges = {
            epoch: (
                min(min(item["time_a_ms"], item["time_b_ms"])
                    for item in matched_events if item["epoch_id"] == epoch),
                max(max(item["time_a_ms"], item["time_b_ms"])
                    for item in matched_events if item["epoch_id"] == epoch),
            )
            for epoch in epochs
        }
        ordered_ranges = sorted((start, end, epoch) for epoch, (start, end) in epoch_ranges.items())
        if any(ordered_ranges[index][1] >= ordered_ranges[index + 1][0]
               for index in range(len(ordered_ranges) - 1)):
            raise C0Error("clock capture epochs must be time-disjoint")
        all_times = ([item["time_a_ms"] for item in matched_events]
                     + [item["time_b_ms"] for item in matched_events])
        span_hours = (max(all_times) - min(all_times)) / 3_600_000.0
        if span_hours < MIN_SPAN_HOURS:
            raise C0Error("clock edge span is shorter than six hours")
        metrics = _metrics(matched_events)
        if metrics["p95_absolute_residual_ms"] == 0 and metrics["max_absolute_residual_ms"] == 0:
            raise C0Error("shared zero-residual clock grid is forbidden")
        upper = _bootstrap(matched_events, boot["seed"] + edge_index, boot["replicates"])
        if (upper["p95_absolute_residual_ms"] > P95_MAX_MS
                or upper["max_absolute_residual_ms"] > ABS_MAX_MS
                or upper["absolute_drift_ms_per_hour"] > DRIFT_MAX_MS_PER_HOUR):
            raise C0Error("clock bootstrap upper bound failed")
        normalized.append({**edge, "raw_match_log": raw_binding,
                           "events": events, "epochs": sorted(epochs),
                           "epoch_ranges_ms": {key: list(value) for key, value in sorted(epoch_ranges.items())},
                           "span_hours": span_hours, "metrics": metrics,
                           "bootstrap_95_upper": upper,
                           "reciprocal_fraction_a": matched_a / total_a,
                           "reciprocal_fraction_b": matched_b / total_b})
    reached, queue = set(), [CAMERAS[0]]
    while queue:
        node = queue.pop()
        if node in reached: continue
        reached.add(node); queue.extend(graph[node] - reached)
    if reached != set(CAMERAS):
        raise C0Error("clock overlap graph is disconnected")
    return {
        "schema": SCHEMA, **NON_RELEASE_FLAGS, "status": "PASS",
        "claim": document["claim"], "generator": generated,
        "track_split_report": split, "topology_report": topology,
        "sources": sources, "identities": identities,
        "detector": {"artifact": detector, "residual_blind": True},
        "trust_predicate": trust, "matcher_artifact": matcher,
        "matching_method": document["matching_method"],
        "bootstrap": dict(boot), "injection": {**injection, "injector": injector},
        "thresholds": {"minimum_events_per_edge": MIN_EVENTS, "minimum_epochs_per_edge": MIN_EPOCHS,
                       "minimum_span_hours_per_edge": MIN_SPAN_HOURS,
                       "minimum_reciprocal_fraction_per_side": MIN_RECIPROCAL,
                       "p95_upper_ms": P95_MAX_MS, "max_upper_ms": ABS_MAX_MS,
                       "drift_upper_ms_per_hour": DRIFT_MAX_MS_PER_HOUR},
        "edges": normalized, "observed_pairs": [list(pair) for pair in sorted(pairs)],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input"); parser.add_argument("--output", required=True)
    args = parser.parse_args(argv); document, input_sha256 = read_input_json(args.input)
    report = validate(document); report["input_sha256"] = input_sha256
    publish_no_replace(args.output, report); print(args.output); return 0


if __name__ == "__main__":
    raise SystemExit(main())
