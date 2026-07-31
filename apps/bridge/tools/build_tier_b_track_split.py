#!/usr/bin/env python3
"""Build the write-once whole-group Tier-B track split contract."""

from __future__ import annotations

import argparse
from collections import defaultdict
from apps.bridge.tools.tier_b_c0_common import (
    CAMERAS, C0Error, NON_RELEASE_FLAGS, SPLITS, bind_file, exact_keys,
    finite_number, generator, identity_bindings, nonblank, publish_no_replace,
    read_input_json, require_non_release,
)

SCHEMA = "v2x-tier-b-track-split/v1"
GROUP_KEYS = {
    "group_id", "split", "camera_id", "track_id", "identity_id", "clip",
    "evidence", "source_hashes", "capture_epoch", "mount_epoch", "start_ms",
    "end_ms", "ambiguity_ids", "derived_feature_ids", "terminal_state",
    "terminal_reason",
}
TERMINAL = ("accepted", "structured_rejected", "authoritative_aged_out")
SEALED_KEYS = {"artifact_id", "sha256", "bytes", "seal_generation", "never_open"}


def sealed_binding(value, label):
    """Validate holdout inventory metadata without opening the sealed artifact."""
    exact_keys(value, SEALED_KEYS, label)
    artifact_id = nonblank(value["artifact_id"], f"{label}.artifact_id")
    digest = value["sha256"]
    if (not isinstance(digest, str) or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)):
        raise C0Error(f"{label}.sha256 is invalid")
    size = value["bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise C0Error(f"{label}.bytes must be a positive integer")
    generation = nonblank(value["seal_generation"], f"{label}.seal_generation")
    if value["never_open"] is not True:
        raise C0Error(f"{label}.never_open must be true")
    return {"artifact_id": artifact_id, "sha256": digest, "bytes": size,
            "seal_generation": generation, "never_open": True}


def validate(document):
    exact_keys(document, {
        "schema", *NON_RELEASE_FLAGS, "generator", "dense_proposal_manifest",
        "corpus", "identities", "groups", "terminal_accounting",
    }, "track split")
    if document["schema"] != SCHEMA:
        raise C0Error("track split schema is invalid")
    require_non_release(document, "track split")
    generated = generator(document["generator"])
    dense = bind_file(document["dense_proposal_manifest"], "dense proposal manifest")
    corpus = exact_keys(document["corpus"], {
        "cutoff_utc", "pagination_root", "cursor_sha256", "exclusion_policy",
        "adjacency_buffer_ms", "holdout_generation", "holdout_consumed",
        "holdout_burned",
    }, "corpus")
    nonblank(corpus["cutoff_utc"], "corpus.cutoff_utc")
    nonblank(corpus["pagination_root"], "corpus.pagination_root")
    cursor = corpus["cursor_sha256"]
    if not isinstance(cursor, str) or len(cursor) != 64:
        raise C0Error("corpus cursor SHA-256 is invalid")
    exclusion = bind_file(corpus["exclusion_policy"], "exclusion policy")
    adjacency = finite_number(corpus["adjacency_buffer_ms"], "adjacency buffer", minimum=1)
    nonblank(corpus["holdout_generation"], "holdout generation")
    if not isinstance(corpus["holdout_consumed"], bool) or not isinstance(corpus["holdout_burned"], bool):
        raise C0Error("holdout state must be boolean")
    if corpus["holdout_consumed"] or corpus["holdout_burned"]:
        raise C0Error("C0 requires an unconsumed, unburned holdout")
    identities = identity_bindings(document["identities"])
    groups = document["groups"]
    if not isinstance(groups, list) or not groups:
        raise C0Error("track groups must be nonempty")
    normalized, ids = [], set()
    key_splits = defaultdict(set)
    hash_splits = defaultdict(set)
    cameras = set()
    windows = []
    for index, raw in enumerate(groups):
        exact_keys(raw, GROUP_KEYS, f"group[{index}]")
        group_id = nonblank(raw["group_id"], "group ID")
        if group_id in ids:
            raise C0Error("group IDs must be unique")
        ids.add(group_id)
        split = raw["split"]
        if split not in SPLITS:
            raise C0Error("group split is invalid")
        camera = raw["camera_id"]
        if camera not in CAMERAS:
            raise C0Error("group camera is invalid")
        cameras.add(camera)
        start = finite_number(raw["start_ms"], "group start", minimum=0)
        end = finite_number(raw["end_ms"], "group end", minimum=0)
        if end <= start:
            raise C0Error("group time window is invalid")
        binding = sealed_binding if split == "untouched_holdout" else bind_file
        clip = binding(raw["clip"], f"group {group_id} clip")
        evidence_value = raw["evidence"]
        if not isinstance(evidence_value, list) or not evidence_value:
            raise C0Error("group evidence must be nonempty")
        evidence = [binding(item, f"group {group_id} evidence[{i}]")
                    for i, item in enumerate(evidence_value)]
        hashes = raw["source_hashes"]
        if (not isinstance(hashes, list) or not hashes
                or any(not isinstance(item, str) or len(item) != 64
                       or any(char not in "0123456789abcdef" for char in item) for item in hashes)
                or len(hashes) != len(set(hashes))):
            raise C0Error("group source hashes are invalid")
        ambiguity = raw["ambiguity_ids"]
        derived = raw["derived_feature_ids"]
        if any(not isinstance(value, list) for value in (ambiguity, derived)):
            raise C0Error("group ambiguity/derived IDs must be lists")
        scalar_keys = [
            ("track", nonblank(raw["track_id"], "track ID")),
            ("identity", nonblank(raw["identity_id"], "identity ID")),
            ("clip", clip["sha256"]),
            ("capture_epoch", nonblank(raw["capture_epoch"], "capture epoch")),
            ("mount_epoch", nonblank(raw["mount_epoch"], "mount epoch")),
        ]
        for kind, value in scalar_keys:
            key_splits[(kind, value)].add(split)
        for kind, values in (("ambiguity", ambiguity), ("derived", derived)):
            if len(values) != len(set(values)) or any(not isinstance(item, str) or not item for item in values):
                raise C0Error(f"group {kind} IDs are invalid")
            for value in values: key_splits[(kind, value)].add(split)
        for digest in [clip["sha256"], *(item["sha256"] for item in evidence), *hashes]:
            hash_splits[digest].add(split)
        terminal = raw["terminal_state"]
        if terminal not in TERMINAL:
            raise C0Error("group terminal state is invalid")
        reason = raw["terminal_reason"]
        if terminal == "accepted":
            if reason is not None:
                raise C0Error("accepted group cannot have terminal reason")
        else:
            nonblank(reason, "terminal reason")
        windows.append((camera, split, start, end, group_id))
        normalized.append({**raw, "clip": clip, "evidence": evidence,
                           "start_ms": start, "end_ms": end})
    if cameras != set(CAMERAS):
        raise C0Error("track split must cover all four cameras")
    if any(len(value) != 1 for value in key_splits.values()) or any(
            len(value) != 1 for value in hash_splits.values()):
        raise C0Error("whole track/identity/clip/evidence group crosses splits")
    for i, (camera, split, start, end, group_id) in enumerate(windows):
        for other_camera, other_split, other_start, other_end, other_id in windows[i + 1:]:
            if split != other_split and (
                    start <= other_end + adjacency and other_start <= end + adjacency):
                raise C0Error(f"adjacent windows cross splits: {group_id}/{other_id}")
    accounting = exact_keys(document["terminal_accounting"], {
        "recoverable", "accepted", "structured_rejected", "authoritative_aged_out"
    }, "terminal accounting")
    counts = {name: sum(item["terminal_state"] == name for item in normalized)
              for name in TERMINAL}
    if (accounting["recoverable"] != len(normalized)
            or any(accounting[name] != counts[name] for name in TERMINAL)
            or sum(counts.values()) != len(normalized)):
        raise C0Error("terminal accounting is not exact")
    return {
        "schema": SCHEMA, **NON_RELEASE_FLAGS, "generator": generated,
        "dense_proposal_manifest": dense,
        "corpus": {**corpus, "exclusion_policy": exclusion,
                   "adjacency_buffer_ms": adjacency},
        "identities": identities, "groups": normalized,
        "terminal_accounting": dict(accounting),
        "split_counts": {split: sum(item["split"] == split for item in normalized)
                         for split in SPLITS},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input"); parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    document, input_sha256 = read_input_json(args.input)
    report = validate(document)
    report["input_sha256"] = input_sha256
    publish_no_replace(args.output, report); print(args.output); return 0


if __name__ == "__main__":
    raise SystemExit(main())
