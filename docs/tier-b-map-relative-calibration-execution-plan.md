# Tier-B map-relative inverse-rendering execution plan

Status: proposed execution amendment; requires independent and Fable review

Scope: Path PC V2X and the packaged Unreal Engine 5.5 RR/CARLA 0.10 worker.
Unreal Engine 6 is excluded.

## 1. Product claim and non-identifiability boundary

The site owner has confirmed that no new checkerboard/ChArUco capture, licensed
survey, RTK test vehicle, GNSS optical target, or thermal acquisition will be
provided. The absolute-world requirements in
`v2x-calibration-completion-contract.md` remain unchanged as **Tier A** and must
be reported `UNAVAILABLE`, never inferred from traffic detections or from the
model under test.

This plan and the versioned claim matrix in
`v2x-calibration-completion-contract.md` define an additional **Tier B** product
claim. Tier A remains globally incomplete when its physical prerequisites are
unavailable; a Tier-B release never changes a Tier-A row to passing.

> Map-relative visual mirroring: across frozen held-out real frames and vehicle
> tracks, the four UE5 cameras and UE5 actors reproduce the observed road
> geometry and same-car image motion within the fixed pixel, overlap,
> model-conditioned predictive-consistency, temporal-stability, and cleanup
> gates below, conditional on the
> frozen Richmond map, a declared nominal optical gauge, and trusted HLS media
> timestamps.

Tier B does not certify surveyed world coordinates, physical lens parameters,
GNSS exposure time, or RTK vehicle error. It may be deployed only after every
Tier-B gate passes and every UI/API surface labels the calibration version and
claim tier. Tier-A rows remain visible as `UNAVAILABLE` after deployment.

The following parameters are not independently identifiable from the available
mostly planar imagery and are therefore gauges or sensitivity variables:

- physical focal length, principal point, and radial distortion separately
  from camera height/pitch and map scale;
- a common site-to-CARLA SE(2) transform separately from the four camera poses;
- map error separately from camera-pose error when only one camera is used;
- absolute exposure time separately from producer HLS timestamps;
- absolute vehicle coordinates separately from the fitted multi-camera
  trajectory.

The deployable optical gauge freezes the production centered-principal-point,
zero-distortion model. Nominal camera height and coworker transforms are priors
with declared uncertainty, not truth. An unconstrained optical fit is retained
only as a sensitivity diagnostic. A bound hit, rank deficiency, condition
number above `1e8`, or materially better unconstrained fit than deployable fit
fails the candidate rather than authorizing a parameter substitution.

## 2. Evidence and leakage controls

Before optimization:

1. Hash and classify the complete source corpus by camera, capture epoch,
   trusted timestamp provenance, native dimensions, and mount-stability epoch.
2. Split whole time windows and whole vehicle tracks into fit, development, and
   untouched holdout. No frame, resampled feature, track, or adjacent burst may
   cross a split.
3. Use spatially contiguous holdout blocks for static annotations. Interleaved
   points on the same lane/crosswalk line are not independent.
4. Freeze the exact UTC corpus cutoff, pagination roots/cursors, thresholds,
   exclusion reasons, annotation policy, loss policy, model hashes,
   map/OpenDRIVE hashes,
   nominal gauges, optimization bounds, and split manifests before fitting.
5. Publish the canonical manifest to the reviewed write-once evidence path.
   Until the AWS prerequisite and evidence-store gates pass, optimization may
   run only on development data and may not consume the no-view vault.
6. Evaluate each holdout once. Any post-result model, annotation, map, pose,
   threshold, or optimizer change burns it. Keep the existing limit of three
   replacements per phase.
7. Retain every completion-contract mechanism that does not depend on the
   unavailable physical inputs. In particular, recover synthetic injected
   timestamp offsets; reject shared zero-residual producer grids; require at
   least 80% reciprocal one-to-one media-clock matches; and require relative
   pairwise P95/max offset at most `50/75 ms` with relative pairwise drift at
   most `5 ms/hour`. These relative gates do not establish GNSS time, but they must pass
   before any cross-camera trajectory or same-car image-motion evidence is
   eligible.

Clock evidence spans at least six hours and three disjoint capture epochs. Each
observed overlap edge has at least 30 independent passage events, proposed by a
detector that is blind to the timestamp residual. Repeated frames from one
vehicle count as one event. The camera overlap graph must connect all four
cameras. Use reciprocal leave-one-event-out matching, a robust median offset,
Theil-Sen relative-drift estimate, and pre-registered bootstrap 95% confidence
bounds; the upper bound must pass each fixed threshold. Synthetic injection
tests evaluator recovery only and is never called exposure truth.

The current eight CSVs under `apps/perception/calibration` are development-only:
they use one source frame per channel, coarse annotations, mutable inline
train/holdout labels, border points, and CARLA XYZ/depth lifted through an
unverified render pose. They may diagnose scale/framing but may not train or
certify the optimizer. The `ch1_far` exact half-width pattern must be recorded,
but a general `Twin_U = u/2` copying claim is false and must not be asserted.

Detectors, segmenters, dense matchers, UE5 depth, coworker transforms, stored
GPS, lane snapping, and persisted object IDs may propose factors. None may
label its own held-out truth. Static heldouts require manually verified unique
features and manually traced finite geometry. Identity heldouts require blind
review independent of the matcher output.

## 3. Phase A: map and renderer observability gate

Map correction precedes camera fitting because a camera optimizer can absorb a
known map topology error.

- Reconcile the recovered 222-road/29-junction OpenDRIVE lineage with the live
  208-road/32-junction OpenDRIVE and preserve exact package/FBX/GeoJSON/material
  hashes. Select exactly one deterministic map/version/topology using fit and
  development evidence only, then freeze it before any holdout is exposed. If
  lineage cannot be deterministically reconciled, Phase A fails.
- Freeze a candidate-set manifest before scoring. Reject any candidate with a
  topology contradiction, then choose lexicographically by worst-camera road
  max, worst-camera road RMSE, worst-camera point P95, and total robust loss.
  The candidate IDs, class score vector, precedence, and tie rule are immutable.
  If two survivors are within 2% on the first differing metric, or different
  required classes prefer different candidates, declare a competing-map basin
  and fail rather than selecting a map that can absorb camera error.
- Fix and regression-test segmented `roadMark` ranges and stable road, lane,
  range, object, and crosswalk identities in the exporter. Prohibit per-camera
  map edits; every map correction is global, source-controlled, and evaluated
  on all four development cameras. The selected map is a visual model, never a
  claim of corrected physical-world truth.
- Treat `notify.log` as evidence about the recovered authoring package only.
  Determine its producer and whether the `Wrong package tag` failures require
  UE5.5 re-import from FBX or a source-version migration. Do not infer a live
  cooked-map material failure without live read-only/render evidence.
- Use synchronized RGB and metric depth plus OpenDRIVE/map-vector targets. The
  Richmond semantic/instance buffers are not usable because the retained
  diagnostic reports tag 11 and instance sentinel 65535 for all static pixels.
- Run only one UE5 worker during rendering. A separate concurrent GPU worker is
  prohibited by the recorded resource-admission failure. Two exact topologies
  are allowed:
  - Current-map observational capture uses the already-running packaged worker
    on `2000-2002`; prove zero sessions, hold all mutation timers, stop the
    Drive bridge to prevent new sessions, keep CARLA running, create only the
    owned temporary RGB/depth sensors, destroy them in `finally`, and restore
    Drive/timers after a 120-second zero-restart cleanup watch.
  - Candidate-map validation uses a scheduled outage: capture and verify the
    rollback bundle, stop Drive and the production worker, prove ports
    `2000-2002` closed, start exactly one isolated packaged UE5.5 candidate on
    loopback `2300-2302`, enforce a 180-second map-load deadline, collect
    evidence, stop it, prove `2300-2302` closed, restore the production worker
    and Drive, then pass a 120-second zero-restart/listener/WS cleanup watch.
  Every expected outage is recorded; no phase may claim uninterrupted Drive
  service during candidate-map validation.

Exit: reconciled map lineage, deterministic RGB/depth/vector rendering, no
unexplained topology contradiction in the fit geometry, and no live service or
feed regression. Tier-A survey validation remains `UNAVAILABLE`.

## 4. Phase B: four-camera static inverse rendering

For each mount-stability epoch and camera:

1. Build temporal-median real targets for curb/road boundaries, lane paint,
   crosswalk/stop paint, horizon, vanishing directions, and unique stable
   landmarks. Exclude vehicles, people, vegetation, reflections, shadows, and
   unmatched map classes.
2. Capture hash-bound real/twin pairs with the `twin_frame` protocol and exact
   camera/config/map fingerprints. Resolve twin pixels through retained metric
   depth only after the pixel annotations are frozen.
3. Fit camera 6-DoF and FOV under the nominal optical gauge with class-aware
   symmetric distance transforms, oriented-edge, horizon/vanishing, finite
   landmark, and spatial-coverage losses. Use Huber or Tukey robust losses.
4. Search multiple basins with a deterministic Sobol/grid initialization and
   CMA-ES or Powell refinement. Cache by full source/map/config/candidate hash.
5. Use frozen CARLA map coordinates as the exact gauge: there is no fitted
   global site SE(2) parameter. If map reauthoring applies a global SE(2), bake
   it once into the frozen map artifact before fitting. Jointly refine the four
   camera transforms in that gauge. Add only
   independently annotated shared-feature and cross-camera epipolar/ground
   consistency terms. Coworker co-perception transforms enter as covariance-
   weighted priors.
6. Run principal-point, distortion, height, and focal sensitivity sweeps around
   the deployable solution. Report their effect; do not copy optimizer radial
   coefficients into CARLA lens fields.

Each camera holdout contains at least four unique held-out points and two finite
held-out road polylines, with point coverage spanning at least 50% of native
image width and 30% of height. Fit and holdout geometry must not be duplicate,
resampled, or adjacent portions of the same annotation.
Each fit split contains at least eight unique points and three finite road
polylines per camera across at least three disjoint capture epochs. Temporal
medians are built wholly inside one split. Polyline vertices are clustered by
physical feature for confidence intervals and never counted as independent
samples. In addition to the contract minima, pre-register the largest feasible
independent landmark denominator before annotation and report `INSUFFICIENT`
rather than shrinking it after results are known.

Unchanged held-out gates, reported on a 1280x960 reference canvas with explicit
per-axis native transforms:

- point RMSE/P95/max at most `10/16/24 px`;
- finite road-geometry RMSE/max at most `6/12 px`;
- every required semantic class and spatial quadrant passes without regression;
- horizon, vanishing directions, road edge, lane/crosswalk topology, and stable
  landmarks pass the retained visual veto;
- full data Jacobian rank, condition number at most `1e8`, no parameter bound
  hit, no unresolved competing basin, and stable results across capture epochs.

For rank and conditioning, optimize dimensionless parameters normalized by
pre-registered physical/search scales (meters, radians, and focal/FOV scale).
Freeze one map gauge as above. Define a competing basin as a solution outside
the pre-registered parameter-neighborhood whose development loss is within 2%
of the best. Define material unconstrained improvement as more than 10% on any
required development metric. Across epochs, every parameter must remain within
its development-derived 95% interval and every epoch must independently pass
the fixed image-space gates.

At 2560 native width the corresponding x-scaled point gates are `20/32/48 px`
and road gates `12/24 px`; y is scaled independently. These are the existing
gates, not relaxed replacements. The physical-vs-renderer dense-ray gate stays
Tier-A `UNAVAILABLE` because physical intrinsics are unavailable.

Exit: all four cameras pass independently and jointly on development data, then
one frozen static holdout passes once. Otherwise no camera configuration is
deployed.

## 5. Phase C: dynamic localization and same-car placement

After the static candidate is frozen:

- Recover exact trusted schema-v2 frames and run dense tracking. Keep YOLO11m
  and future models as proposal generators with immutable model/frame/mask
  fingerprints and terminal accounting.
- Require reviewed visible road-contact/footprint points for fit factors. The
  bbox bottom-center remains diagnostic; silhouette midpoint and extent may be
  low-weight nuisance cues when the UE5 blueprint differs from the real class.
- Fit a robust factor graph over latent smooth ground trajectories, heading,
  velocity, dimensions/blueprint family, per-observation contact uncertainty,
  and bounded per-camera time offsets. Do not lane-snap inside the objective.
- Use ConvNeXt appearance, plausible transit, mutual exclusion, and geometry
  for cross-camera association. Ambiguous candidates produce separate tracks
  and retained ambiguity evidence; input order may not decide identity.
- Establish identity truth with two reviewers who label time-disjoint clips
  while blind to matcher output. Require Cohen's kappa at least `0.80` before
  adjudication. Validate the `0.60` similarity floor separately for every
  camera pair against independent positive and hard-negative development
  identities, then freeze each pair's floor before holdout. A pair-specific
  floor may only tighten the global `0.60` minimum. For a one-sided 95% upper
  error bound at most 5%, require at least 59 independent positives and 59
  independent hard negatives with zero errors per observed pair; repeated
  frames from one identity do not increase the denominator. Otherwise report
  `INSUFFICIENT`, never pool pairs or weaken the confidence target.
- Keep zero identity switches for accepted tracks. The legacy `<=2 m` field is
  an operational association cutoff only and is not called calibrated or world
  uncertainty in Tier B. Prevent covariance gaming with pre-registered
  leave-one-camera-out and leave-one-clip-out prediction: fit without the held-
  out camera/clip, project into its image, and measure predictive contact,
  centroid, and contour coverage against blind annotations. Report this as
  model-conditioned relative predictive consistency, not NEES or independent
  position truth. Require at least 30 independent held-out tracklets total and
  at least five per camera. Use a nominal 95% predictive image-space region,
  cluster repeated frames by vehicle track, and require empirical coverage from
  90% through 99% with a Wilson 95% lower confidence bound at least 85%; also
  require the covered observations to pass the fixed centroid,
  contour, and contact residual gates so an inflated region cannot pass.
  Blindly human-audit a pre-seeded random sample of
  at least 59 independent held-out contact observations, at least 12 per
  camera, for a one-sided 95% upper error bound at most 5% when zero errors are
  observed. An audit error is a contact placed outside the visible vehicle-road
  footprint or more than `6 px` from the blind reviewed contact at 1280 width,
  scaled per axis natively. A failed predictive or contact audit rejects the
  model.
- Optimize on fit tracks, select on development tracks, and evaluate entire
  untouched tracks. Dynamic refinement must improve every camera without
  regressing the frozen static gates; otherwise retain the static solution.
- Per-camera time offsets are bounded nuisance parameters under the passed
  relative media-clock gate. A fitted offset at its bound, a failed injected-
  offset recovery, or violation of the fixed offset/drift gates fails the
  dynamic candidate.

Held-out same-car visual gates remain:

- at least five transitions per camera, 20 total, and two for each observed
  overlapping camera pair;
- at least three distinct frames over at least two seconds and at least `0.25
  m` actor motion per transition;
- projected centroid error at most `16 px` at 1280 width, bbox IoU at least
  `0.50`, actor coverage at least `0.50`, raw actor visibility at least `75%`,
  compatible detection confidence at least `0.50`, consistent motion, stable
  actor ID, no neighboring-actor explanation, zero identity switches, zero
  stale actors, and zero unexplained eligible failures;
- a stable source-controlled blueprint digest; requested transform and optical
  values agree with an actor/sensor snapshot bound to the same `twin_frame`:
  planar and z readback error at most `0.05 m`, pitch/yaw/roll error at most
  `0.05 deg`, FOV error at most `0.01 deg`, and each serialized lens attribute
  either byte-identical or within `1e-6`; one actor ID persists across
  all samples and the required bridge restart; every projected vehicle/walker
  confounder is retained and the target wins the source-controlled
  `verify_phase4_live.py` exclusivity gate with minimum target-IoU margin `0.10`.
  Bind the verifier SHA-256 and prohibit a CLI override that weakens the margin;
- every recoverable observation reaches exactly one accepted, structured-
  rejected, or authoritative-aged-out terminal state, and at least `80%` remain
  eligible under pre-registered occlusion/truncation rules.

The absolute world-centroid/RTK row remains Tier-A `UNAVAILABLE`. Tier B adds no
self-referential meter threshold as a substitute; it reports cross-camera
trajectory disagreement and covariance as diagnostics alongside the unchanged
image-space acceptance gates.

## 6. Integration, deployment, and rollback

Component commits are integrated only after exact source, independent, and
Fable reviews pass. A green unit suite alone is insufficient. Rejected parent
commits never become eligible merely because a successor exists.

Tier-B deployment is allowed only under completion-contract claim-matrix
version 2, and only when:

1. the map, camera, tracker, placement, evidence, and terminal-accounting
   commits are cleanly integrated on the current production base;
2. all relevant bridge, perception, web, schema, type, AWS plan-only, rollback,
   and adversarial tests pass from that exact commit;
3. the write-once split manifest exists before the one-shot holdout evaluation;
4. every Phase A-C Tier-B gate passes without changing a threshold;
5. the UI/API reports `calibration_claim_tier=map_relative_visual_mirroring`,
   exact hashes/version, and Tier-A unavailable rows;
6. a zero-session maintenance gate holds all mutation timers and captures plus
   rehearses the exact rollback bundle;
7. one layer is deployed at a time with refreshed CLI/API and browser evidence.

Replay-clock eligibility requires at least 30 independently selected samples
per camera across at least five accepted transitions and three tracks. Each
physical frame has trusted schema-v2 HLS media time; each binary twin JPEG is
preceded by hash-matching `twin_frame` metadata with an advancing UE5 frame ID;
the replay clock is nonnegative and at most `250 ms` after the sampled object
clock. The evaluator must recover injected `+50 ms` and reject injected
`+300 ms`, stale frame IDs, reordered metadata, and shared producer grids.

The release report defines the exact lighting, weather, focus,
zoom/crop, firmware, and capture-epoch envelope represented by the accepted
corpus. Temperature is explicitly `UNKNOWN/UNCLAIMED` because telemetry is
absent; the product must not imply a thermal envelope or performance outside
the other observed acquired conditions.

Rollback immediately on a UE5/Drive/perception restart increment, listener or
feed loss, more than five seconds of four-feed readiness loss, calibration or
same-car replay regression, actor/session/socket leak, or incorrect claim
metadata. Restore LIVE mode, zero actors/sessions, the prior config/map/source
bundle, current healthy tunnel endpoints, and all timers.

After deployment require an attended 30-minute watch and a supervised quiet
24-hour watch with automatic rollback on fixed triggers. The source-controlled
`systemd/v2x-calibration-release-watchdog.service`,
`systemd/v2x-calibration-release-watchdog.timer`, and
`scripts/v2x-calibration-release-watchdog.sh` must bind the candidate commit,
config/map/model hashes, verified rollback-bundle hash, and an exclusive `flock`.
The service samples service counters/listeners, `/health`, sessions, actors, and
claim metadata every second. Restart increments, UE5 faults, listener loss,
actor/session leaks, hash/metadata mismatch, or unreadable evidence have zero
debounce and invoke rollback immediately. Only feed readiness may accumulate a
bounded transient; five consecutive one-second failed samples invoke rollback,
so an outage may never exceed five seconds.

The installed root-owned executable is
`/usr/local/libexec/v2x-calibration-rollback`, built byte-for-byte from tracked
`scripts/restore-v2x-rollback.sh`. Its only interface is
`--bundle /home/path/V2XCarla/v2x-backend-backups/CLAIMED_BUNDLE --manifest-sha256
EXPECTED --candidate-sha EXPECTED`; the root-owned unit environment supplies
those exact immutable values and the service user receives sudo permission for
that exact command only. The restore script verifies `MANIFEST.sha256`, bundle
mode/ownership, candidate binding, and an exclusive deployment lock; restores
source/config/map/models/units; daemon-reloads; restores CARLA, perception,
Drive, web, and tunnels in dependency order; proves LIVE/Richmond, zero sessions
and actors, expected container/source/config hashes, listeners, four feeds, and
claim metadata; then restores the snapshotted timer states. A failed readback
keeps mutation timers stopped, records the failed step, and escalates rather
than claiming rollback success. Evidence is exclusively published under
`/home/path/V2XCarla/v2x-evidence/calibration-release-watchdog/<release-id>/`.
The script is idempotent, fails closed if evidence cannot be read, and restores
timers only after the readback sequence passes.
It must be proven in a non-production rollback rehearsal and name the on-call
owner plus escalation path before the unattended watch. Retain weekly stable-landmark
reprojection with an owner; reopen static calibration when any landmark exceeds
its point gate or aggregate RMS regresses by more than 20%, and after any mount
impact, focus/zoom/crop, firmware/image-pipeline, map, or out-of-envelope
environmental change. Record deployed hashes, corpus outcome counts, UI
screenshots/console/network/WebSocket evidence, rollback command, and remaining
Tier-A debt. Leave no live-only source.

## 7. Prioritized executable actions

1. Resolve the recovery worktree's dirty schema-only `cameras.json` change in a
   reviewed branch; preserve the deployed pose values and do not add nonzero
   lens fields.
2. Publish the eight CSVs as rejected/development diagnostics and hard-block
   them from accepted optimizer input.
3. Finish independent and Fable reviews of the terminal, dense publication,
   placement, map-registration, and AWS prerequisite candidates; integrate only
   exact accepted successors.
4. Reconcile Richmond OpenDRIVE/package lineage and fix exporter stable IDs and
   segmented road-mark ranges.
5. Characterize the recovered package asset incompatibility and prove the live
   renderer's actual RGB/depth/vector behavior before choosing a re-import path.
6. Build the multi-epoch temporal-median annotation and mount-stability tools.
7. Implement the per-camera and joint-static optimizer with nominal gauges,
   multi-start search, robust semantic losses, covariance, and degeneracy tests.
8. Implement the dynamic factor graph, track-level split enforcement, identity
   ambiguity, contact uncertainty, and exact UE5 placement/readback path.
9. Freeze the integrated candidate and write-once manifest, then consume one
   untouched static/dynamic holdout exactly once.
10. Deploy only a passing Tier-B candidate through the rollback gate and prove
    local/public behavior, cleanup/isolation, and both watches.

## 8. Exact release evidence matrix

Every command exits zero and its retained output is hash-bound to the release:

- four local feeds:
  `/home/path/V2XCarla/perception-venv/bin/python apps/perception/tools/verify_live_feeds.py http://127.0.0.1:8090`;
- public-selected perception origin: run the same verifier against the exact
  origin read by the refreshed browser without retaining signed HLS URLs;
- observational Drive state:
  `PYTHONPATH=apps/bridge /home/path/V2XCarla/carla-venv-310/bin/python apps/bridge/tools/verify_phase4_live.py`, requiring LIVE/Richmond, zero sessions,
  zero actors, and zero poll failures;
- exact held-out object replay in the authorized window: run the same verifier
  with `--apply --skip-drive`, the exact run-scoped object ID/start/camera, and
  the allowlisted model hash; require all Phase-C gates, then rerun the
  observational command for cleanup;
- API: `GET /drive-config` returns 200 with fresh nondecreasing version and the
  active tunnel URL; `GET /health` and two `/detections/latest` samples prove
  four trusted advancing media clocks; the paginated persistence verifier proves
  the exact schema-v2 record and claim metadata;
- Playwright CLI at 1440x1000 loads local and public `/live`, `/timeline`, and
  `/drive` in fresh contexts after a hard refresh; retain screenshots and HARs;
  require HTTP 200 documents, zero console/page/request/HTTP>=400 failures,
  four visibly distinct advancing feeds, the exact timeline/replay object, a
  WebSocket 101 to the endpoint selected from `/drive-config`, and visible
  Tier-B/version/hash plus Tier-A `UNAVAILABLE` labels. Do not click Start during
  observational evidence. The authorized replay flow must show the same real
  car and stable UE5 actor across the required frames.

After all browser contexts close, require the observational Drive command,
CARLA actor inventory, WebSocket connections, and service counters to prove
LIVE, zero sessions/actors, no socket accumulation, and no restart increment.
Repeat this matrix after rollback rehearsal; the expected hashes/claim metadata
must be the prior release values.
