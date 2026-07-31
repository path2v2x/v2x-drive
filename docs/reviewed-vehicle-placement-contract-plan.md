# Reviewed Vehicle Localization and UE5 Placement Contract

Status: source-only design and tests. This work does **not** establish that the
static camera calibration gate has passed and must not be deployed without the
separate controlled live gate.

Fable review status (2026-07-13): the earlier OAuth-blocked attempts were retried
after re-authentication. A high-effort, read-only Fable review completed and its
governance, canonicalization, clock, numeric-boundary, monitoring, and schema
evolution findings were incorporated. The final high-effort re-review completed
successfully and reported no remaining substantive blocker.

Fable implementation review status (2026-07-13): a separate high-effort,
read-only review inspected the producer, attachment tool, runtime validator,
UE5 synchronization, schemas, fixtures, and adversarial tests. It reported no
substantive blocker in the required risk classes. Its non-blocking sample-zero,
yaw-normalization, and short-array observations were tightened before the final
test run.

Independent implementation review status: changes required after commit
`e986ab6`. The remediation must add authority verification, semantic evidence
linkage, measured optical/static gates, trajectory dynamics and appearance gates,
eigenvalue-correct covariance bounds, transaction-safe actor updates/cleanup,
strict-live freshness, and exact blueprint/geometry binding. The prior commit is
not an acceptance candidate.

Current remediation verification (2026-07-13): the isolated source worktree
passes 505 bridge tests and 501 perception tests with warnings treated as errors.
The implementation now binds a producer-persisted lossless inference frame,
native detector instance mask, exact detector output and same-session PTS;
authenticates every upstream semantic artifact by allowlisted role; replays the
retained four-camera calibration manifests, images, annotations, intrinsics
sources, depth buffers, survey registry and exact held-out denominators; and
quarantines any UE5 actor whose full pose/dimensions or rollback cannot be proved.
Both JSON schemas parse and the complete suites exercise the new contracts. This
is source evidence only; it does not claim that a real site calibration or live
same-car UE5 replay has passed.

## Safety and migration boundary

- Keep `DTB_TWIN_REVIEWED_PLACEMENT=off` as the default. Off mode remains covered
  by the complete pre-existing bridge regression suite; this plan does not claim
  a byte-for-byte golden comparison that has not been performed.
- Strict mode accepts only a versioned, self-hashed reviewed localization sample
  whose event, identity, exact native frame, mask, detector, camera config,
  intrinsics, map, timing, consensus, reviewer, covariance, and trajectory
  fingerprints match the running bridge and detection record.
- Strict rejection is terminal for that sample: never fall back to baseline GPS,
  bbox-bottom-centre diagnostics, a lane-snapped position, or an earlier identity.
- No service, AWS, UE worker, future holdout, or production checkout mutation is
  part of this change.
- The exact authority registry selected by `DTB_TWIN_REVIEW_AUTHORITY_KEYS` is the
  local trust anchor. It must be root/operator controlled and changed atomically.
  Removing a key or removing its role revokes every previously signed artifact
  at the next load/restart; no grandfathering or dual acceptance is implicit.
  Rotation requires overlapping active key IDs only during an explicitly reviewed
  re-signing window, followed by removal of the old key and a strict rejection
  audit. A later schema version must use a new schema ID and explicit migration;
  v1 never accepts added fields or silently upgrades old signatures.
- Strict-live freshness compares signed/trusted media time with the Path host's
  UTC wall clock (`time.time`). Deployment must prove NTP synchronization before
  enabling strict mode; a stale/future signed sample still fails closed.
- Held-out residual recomputation uses Python scalar IEEE-754 binary64 `math`
  operations and a fixed nearest-rank P95, with no NumPy/BLAS dependency. Exact
  boundary and just-over-boundary tests prevent platform-tuned epsilon changes.

## Implementation phases

1. Add a dependency-light reviewed-localization validator and canonical hashing
   helper. Require finite PSD pixel/world covariance, <=2 m uncertainty, reviewed
   footprint midpoint (not bbox centre), exact-same-session PTS provenance,
   unambiguous global identity, dimensions, heading, blueprint family, and complete
   SHA-256 bindings. Reject unknown keys that could create unsigned semantics.
2. Add an offline attachment tool for already-reviewed consensus/trajectory
   artifacts. It must verify every bound file and source hash before embedding a
   self-hashed per-event contract; diagnostic factor-graph or baseline GPS output
   remains ineligible.
3. Add the opt-in bridge feature gate. In strict mode, validate against the active
   map name/OpenDRIVE hash and exact cameras JSON/per-camera hashes, place the
   reviewed UE5 actor-centre coordinates directly, use the reviewed heading, and
   avoid all lateral/vertical lane snapping. Use a stable SHA-256 placement key for
   deterministic blueprint-family selection across process restarts.
4. Expose strict-mode status, accepted provenance hashes, exact reviewed target,
   raw-to-actor placement error, blueprint-selection digest, and bounded rejection
   diagnostics. Preserve existing cleanup, replay generation isolation, actor
   ownership, and despawn behavior.
5. Add adversarial tests for artifact tampering/spoofing, stale/missing hashes,
   map/config mismatch, ambiguous identity, non-finite/non-PSD covariance,
   excessive uncertainty, timestamp/session violations, exact footprint midpoint
   versus bbox centre, multi-camera trajectory identity, strict off/on/no-fallback,
   exact coordinates, cleanup, and deterministic blueprint selection.
6. Run the intended bridge and perception suites in their pinned environments,
   inspect the source-only diff, and commit the clean isolated branch. Any missing
   surveyed static truth or accepted trajectory artifact remains an explicit
   deployment blocker, never a test threshold adjustment.
7. Remediate independent-review findings without relaxing gates: verify an
   allowlisted keyed reviewer authority; semantically bind every accepted event,
   contact, mask, factor-graph placement, identity pair, appearance result,
   measured intrinsics, and passing static holdout result; enforce strict
   trajectory time/dynamics/covariance/transit constraints; make UE5 actor updates
   and cleanup transactional; gate strict live freshness separately from replay;
   and bind the selected blueprint catalog/pool/item plus measured actor dimensions
   to reviewed geometry and independent placement-error evidence.
8. Close the second independent-review findings: accept only inference-time
   producer evidence with a native instance segmentation output; recompute the
   pinned appearance embedding from those exact pixels; require separately signed
   raw RTK/total-station measurements from a non-camera authority; recompute the
   four-camera static gate from every retained raw artifact and exact holdout
   point/road residual; require at least one genuine cross-camera transition;
   leave acceleration unknown until three positions or a signed prior velocity
   exist; and destroy/quarantine actors after rollback, tick-readback, full-pose,
   or dimension failure.
9. Close the Fable governance/verifiability findings: test canonical key ordering,
   intentional numeric and Unicode byte distinctions, and authority-algorithm
   confusion; test exact and just-over residual boundaries; document the authority
   registry lifecycle, Path-host clock authority, deterministic numeric method,
   rejection/quarantine monitoring, explicit re-signing, and v2 migration rule;
   then require a successful final Fable re-review before source acceptance.

## Exit criteria

- Default/off mode passes the pre-existing bridge tests without behavior changes.
- Strict mode accepts a fully bound reviewed sample and places its exact CARLA
  world coordinates; every listed malformed or mismatched variant is rejected with
  no GPS fallback and a visible reason.
- Identical global identity plus blueprint family yields the same selection digest
  and blueprint across fresh `TwinSync` instances; different/ambiguous identities
  cannot alias silently.
- A multi-camera trajectory keeps one global track and actor while timestamps and
  sample order advance monotonically; at least two cameras and one bound
  cross-camera transition are mandatory. Two positions establish speed but not
  acceleration; the third position establishes the first acceleration sample.
- Offline tooling round-trips a valid reviewed trajectory and detects any mutation
  of detections, producer inference manifest/frame/native instance mask/detector
  output, consensus, factor-graph, appearance, raw independent measurement,
  config, intrinsics source image, retained calibration input, or map binding.
- Static calibration cannot pass from a `passed: true` summary. Exactly ch1-ch4
  must replay through the approved aggregation validator, preserve every train
  and holdout denominator, and independently recompute held-out point RMSE/P95/max
  <=10/16/24 px and road RMSE/max <=6/12 px at 1280-wide scale.
- A strict UE5 update commits metadata only after exact full XYZ/yaw/pitch/roll and
  blueprint-dimension readback. Failed rollback or later tick drift makes the
  actor absent, quarantined and cleanup-owned; it is never reported as present.
- Full bridge and perception suites pass in their intended Python environments
  with `-W error`; JSON schemas parse; `git diff --check` is clean.
- Canonical JSON sorts object keys but deliberately does not equate integer/float
  spellings or Unicode normalization forms. Exact retained bytes and schema tags
  are security semantics; alternate HMAC/hash algorithm tags are rejected even
  when the same key material is used.
- Deployment/runbook work must alert on sustained strict-rejection rate and any
  nonzero quarantine/cleanup-failure count. Legitimate camera/map/model changes
  require a coordinated artifact regeneration/review/re-signing runbook; stale
  hashes never receive an availability fallback. Unbounded quarantine growth is
  an incident and closes deployment. Clock synchronization health must remain
  monitored after enablement; loss of NTP synchronization closes strict mode.
- The recorded 505 bridge and 501 perception totals are independently re-run from
  separate commands in their pinned environments.
- Fable high-effort read-only plan re-review completed successfully after the
  findings above with no remaining substantive blocker.
- No runtime/deployment files outside this worktree are changed, and no statement
  claims static calibration acceptance.
