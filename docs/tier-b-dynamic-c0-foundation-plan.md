# Tier-B dynamic Phase C0 foundation

Scope: source-only schema/build/validation tools and adversarial tests. No corpus,
holdout, live service, simulator, deployment, or evidence-store access.

1. Add one shared fail-closed binding/publication module. Every input is opened
   once through held `O_NOFOLLOW` descriptors, `fstat`-checked as a single-link
   regular file, and hashed from that descriptor. Record owner/mode and exact
   generator commit/clean state. Outputs are exclusive/no-replace, always
   `acceptance_eligible=false`, `proposal_only=true`, and `release_eligible=false`.
2. Implement `v2x-tier-b-track-split/v1`. Assign an immutable split to whole
   track groups containing identity, clip, evidence, and time-window keys.
   Enumerate exactly fit/development/untouched-holdout and freeze a nonzero
   adjacency buffer. Reject any key reuse or overlapping/adjacent time window
   across splits, including the transitive closure of ambiguity/derived-feature
   links, any frame or
   evidence hash crossing splits, incomplete ch1-ch4 coverage, and non-terminal
   accounting. Bind the dense proposal manifest, source/config/model/runtime
   identities, capture/mount epochs, corpus cutoff/cursors/exclusion policy,
   holdout generation/burn state, and every clip/evidence artifact.
3. Implement `v2x-tier-b-relative-clock/v1`. Recompute each observed camera
   edge from raw reciprocal one-to-one event matches. Require a connected ch1-
   ch4 graph; at least six hours, three epochs, and 30 independent passage events
   per edge; at least 80% reciprocal matches on both camera sides; P95/max
   bootstrap 95% upper bounds for absolute residual P95/max at most 50/75 ms;
   bootstrap 95% upper absolute Theil-Sen drift at most 5 ms/hour; no shared
   zero-residual grid; reciprocal leave-one-event-out matching; a residual-blind
   bound detector; trusted-schema-v2 predicate identity; development-only split
   membership; and an independently supplied
   pre-registered synthetic-injection recovery/rejection check. Bind source,
   track-split, topology, config/runtime identities and label claims
   `relative_only` (never GNSS/exposure truth).
4. Implement `v2x-tier-b-dynamic-feasibility/v1`. Bind exact accepted static,
   map-lineage, topology, relative-clock, track-split, and dense-proposal
   reports plus config/model/runtime identities. Require exact schemas with no
   unknown fields, consistent shared map/corpus/split/config hashes, observed
   pair set derived from topology, every non-release flag, terminal denominator
   equality and at least 80% eligibility, at least 30 predictive tracklets total
   and five per camera, 59 contact audits total and 12 per camera, 20 transitions
   total/five per camera/two per observed pair,
   every observed camera pair to have at least 59 independent positive and 59
   hard-negative identities with zero adjudicated errors, two matcher-blind
   time-disjoint reviewers, adjudication fields, Cohen's kappa >=0.80, and pair-
   specific similarity floors >=0.60 frozen on development data before holdout,
   and pre-registered hard-negative criteria. Never pool camera pairs; missing
   counts yield explicit non-releasing `INSUFFICIENT`.
5. Add warnings-as-errors tests for happy paths and false greens: path/hash
   drift, split laundering through any group member, overlapping windows,
   duplicate identities/events, denominator reduction, disconnected clocks,
   weak reciprocal matching, offset/drift boundaries, shared-zero grids,
   injection failure, pooled identity denominators, reviewer leakage,
   insufficient pair/camera counts, and replace attempts.
6. Run focused tests, both locked bridge lanes, exact independent review, and an
   exact high-effort Fable review. Repair every substantive finding before the
   clean commit is eligible for integration.
