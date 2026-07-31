# OpenDRIVE topology correspondence implementation plan

## Scope and invariants

- Add a source-only CLI under `apps/bridge/tools/compare_opendrive_topology_correspondence.py` and focused tests.
- Read only an old XODR, a deployed XODR, and an accepted `v2x-map-candidate-lineage-manifest/v1` JSON. Require the manifest to bind both exact XODR byte hashes, the exact accepted candidate names/IDs, blocked reconciliation status, exclusive mutability, and the complete recovered-package inventory/artifact graph.
- Never access CARLA, Unreal, services, evidence/holdout data, or network resources.
- Always emit `acceptance_eligible=false`, `scoring_permitted=false`, and `lineage_resolved=false`. Geometry correspondence cannot resolve provenance.

## Deterministic classifier

1. Use race-safe, no-follow, single-link regular-file reads inherited from the accepted lineage implementation. Re-read the three inputs as a complete snapshot and fail if any bytes or path identity change.
2. Parse OpenDRIVE strictly: unique nonblank road/junction/connection IDs, finite numeric geometry, valid road/junction/link references, nonblank lane-link endpoints, and supported plan-view primitives only.
3. Require identical normalized georeference/header-offset gauges; otherwise fail closed. Flatten every supported road reference line into parameterization-independent world XYZ samples at a frozen arclength interval and quantization, including elevation so stacked roads cannot alias. Record plan-view, lane-structure, road-link degree/contact, and junction-membership signatures. Keep exact signature hashes as evidence and explicitly test forward and reversed reference-line orientation.
4. Build a deterministic bipartite road-overlap graph using asymmetric matched-arclength coverage in both directions, endpoint, elevation, and length evidence. Freeze low/high distance and coverage thresholds: below the low band is no edge, above the high band is a candidate, and the middle band forces the entire component to `ambiguous`. Classify each connected component:
   - one-to-one exact semantic/topology agreement: `unchanged` when IDs match, otherwise `renumbered`;
   - one-to-many with joint terminal coverage, bounded child overlap, and compatible lane/topology evidence: `split`;
   - many-to-one counterpart: `merged`;
   - isolated old/new vertices: `removed`/`added`;
   - every N-to-M, conflicting, multiply plausible, threshold-boundary, stacked, or insufficient-evidence component: `ambiguous` with explicit failed predicates.
5. Build junction incident/connectivity/lane-link signatures from the already frozen road-component mapping. Apply the same one-to-one/split/merge/add/remove/ambiguous component accounting without using IDs as truth.
6. Require terminal accounting: every input road and junction occurs exactly once in one terminal classification and no output record duplicates an ID.

## Report and publication

- Bind schema/algorithm version, Python/XML-parser versions, tool SHA-256, all input paths/hashes/byte counts, lineage candidate IDs, frozen thresholds, signature hashes, every candidate edge metric, every component decision, and aggregate accounting.
- Preserve one-to-one and many-to-one evidence explicitly; sort every collection for permutation-invariant output.
- Exclude only a generated timestamp from snapshot equality. Build twice and require identical reports before publication.
- Publish atomically with no replacement, no symlink following, held parent identity, fsync, and cleanup on failure.

## Verification

- Synthetic tests for unchanged, renumbering, split, merge, add/remove, N-to-M ambiguity, reversed roads, stacked/parallel roads, input permutation, high/low/ambiguous threshold boundaries, malformed references/topology, manifest mismatch, symlink rejection, race detection, publication refusal/cleanup, and terminal-accounting invariants.
- A real Path-PC read-only test creates the accepted lineage manifest in memory from the complete recovered package, compares the exact recovered/deployed XODRs, asserts their hashes/topology counts, verifies total accounting, and confirms lineage remains unresolved and scoring prohibited.
- Run focused tests, relevant lineage tests, compile checks, then Fable high-effort implementation review. Repair all material findings before committing an exact successor.
