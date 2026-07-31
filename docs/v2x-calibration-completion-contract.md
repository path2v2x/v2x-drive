# V2X calibration and same-car placement completion contract

Status: active execution contract, claim matrix version 2

Scope: Path PC production V2X stack and the packaged Unreal Engine 5.5
RR/CARLA 0.10 worker only. Unreal Engine 6 is excluded.

This contract supersedes diagnostic-only calibration plans. A phase is complete
only when its retained evidence passes the fixed gates below. Aggregate visual
improvement, an actor existing in CARLA, or coordinates derived by the model
under test are not acceptance evidence.

## Claim matrix version 2

| Claim | Required evidence | Release state when physical prerequisites are unavailable |
|---|---|---|
| Tier A — absolute-world calibrated twin | Every requirement in this contract, including measured intrinsics, authenticated survey, GNSS optical timing, RTK vehicle truth, thermal envelope, dense-ray optical consistency, and direct world-centroid error | `UNAVAILABLE`; globally incomplete; never inferred or marked passing |
| Tier B — map-relative visual mirroring | Every non-physical mechanism in this contract plus every gate in `tier-b-map-relative-calibration-execution-plan.md`, including frozen nominal gauges, relative-clock proof, static image-space geometry, blind identity/contact evidence, predictive cross-camera consistency, same-car replay, rollback, and claim metadata | May release only as `calibration_claim_tier=map_relative_visual_mirroring`; no absolute camera, map, time, uncertainty, or world-coordinate claim |

Tier B is a versioned amendment, not a waiver. Numeric image-space, identity,
terminal-accounting, persistence, cleanup, isolation, and service gates in this
contract remain binding and may only be tightened. A Tier-B release report must
show every Tier-A physical row as `UNAVAILABLE`, name the frozen map and optical
gauge, state the observed operating envelope, and disclose common-mode map,
time, and coordinate bias as unobservable. Tier-B deployment is prohibited
unless the amendment plan passes exact Fable and independent review and all of
its implementation/evidence gates pass from the integrated release commit.
Tier-B UI/API schemas may expose model-conditioned predictive regions and the
legacy `<=2 m` association cutoff only when labeled as operational diagnostics;
they must never name either value calibrated uncertainty, accuracy, or world
error.

## 0. Durable baseline and corpus

- Make each source change in a dedicated clean `codex/v2x-*` worktree based on
  the exact production candidate. Integrate only independently reviewed commits
  into a newly created clean integration worktree based on the then-current
  `origin/main`; never use the dirty/superseded `v2x-calibration-current` or
  `v2x-calibration-integration` worktrees as integration truth.
- Freeze production source/config, UE5 image and binary, map/OpenDRIVE, service
  restart counters, timers, mode, session count, and rollback bundle before a
  mutation window.
- Export the complete paginated 24-hour detection corpus. Preserve all trusted
  schema-v2 records and assign every vehicle observation one terminal audit
  state: accepted, rejected with a machine-readable reason, or unavailable
  because the exact physical frame aged out.
- Freeze exact dense physical frame windows before fitting. Split entire time
  windows and vehicle tracks into fit, development, and untouched holdout sets
  before optimization; no frame or resampled feature may cross splits.
- Measure physical-camera pairwise clock offset and drift, and physical-camera
  to replay/CARLA-clock offset, from the trusted media clocks. At the fastest
  accepted road speed, require P95 pairwise offset <= 50 ms, max <= 75 ms, and
  absolute drift <= 5 ms/hour. These tightened bounds reserve no more than
  0.75 m P95 and 1.125 m max for timing at 15 m/s; slower scenes do not relax
  them.
  Require at least 80% reciprocal one-to-one timestamp matches per camera side.
  Exact/shared zero-residual producer grids fail as likely common ingest
  stamping; the evaluator must recover a synthetic injected offset. Producer
  timestamps remain diagnostic until an independent exposure/UTC target passes.
- A holdout may be evaluated exactly once. Any threshold, model, map, pose,
  annotation, or optimizer decision made after seeing its result burns that
  holdout. Replacement holdouts must be newly frozen, time-disjoint windows and
  tracks; old failures remain in the permanent audit history.
- Cap replacement holdouts at three frozen replacements per phase. A fourth
  attempt requires a new pre-registered model generation and external review;
  it cannot be another tuning iteration against fresh data.
- Store frames, raw buffers, manifests, splits, tool/model snapshots, reviews,
  and failures under the append-only calibration evidence namespace. Before
  optimization, publish a canonical hash/split manifest to a versioned
  write-once remote object store and verify its retention/version ID. Local
  ownership, permissions, exclusive publication, immutable-bit state, and the
  remote version binding are evidence fields; a mutable directory is not a
  frozen holdout.
- Define `trusted schema-v2` by the exact source-controlled `is_trusted_v2`
  implementation/commit. Require schema version 2, a matched HLS
  program-date-time source/schema, exact media/timestamp equality, and all raw
  producer fingerprints required by that implementation; caller booleans are
  not trust evidence.
- Validate exposure time with a GPS-disciplined LED/flasher or equivalent
  GNSS-timed optical target visible to every camera. Retain controller logs,
  UTC uncertainty, frame hashes, and independently recovered injected offsets.
  Producer timestamps remain diagnostic until this passes.

### Physical prerequisites and error budget

The site owner must authorize and supply the physical observations below;
Codex prepares acquisition tools/manifests and validates returned evidence.
None may be inferred from ordinary traffic detections.

- Four native-resolution board datasets, board measurement, focus/zoom/crop
  state, mount-stability frames, and roadside safety authorization.
- A current licensed survey with horizontal and vertical datum/epoch plus
  independently authenticated surveyor/instrument provenance.
- A GNSS-disciplined optical timing run across all four cameras.
- An independently logged RTK-GNSS test vehicle driven through every accepted
  camera/pair transition, with raw fixes, covariance, antenna/vehicle lever arm,
  heading, controller clock, and calibration retained.
- Camera/environment temperature telemetry spanning the claimed operating
  range. Until measured, the envelope is limited to acquired conditions and an
  excursion reopens calibration.

The P95 root-sum-square development budget at 15 m/s is fixed before holdout:
timing <= 0.75 m, static road-plane projection <= 0.50 m, reviewed contact <=
0.35 m, map/survey <= 0.25 m, and actor/readback <= 0.05 m (combined <= 1.0 m,
leaving >= 1.0 m to the 2 m end-to-end gate). Report covariance and signed
residual correlations; correlated terms are summed, not hidden by RSS. Every
held-out RTK comparison must independently pass the direct <= 2 m world gate.

Exit: hashes reconcile, production remains healthy, every corpus row is
accounted for, clocks pass the fixed offset/drift gate, and all optimization
inputs and split assignments are immutable.

## 1. Four-camera static inverse rendering

- Obtain per-camera native-resolution measured intrinsics from at least ten
  unique checkerboard/ChArUco fit images and two untouched holdouts. Require fit
  and holdout RMS <= 2 px and held-out corner max <= 5 px. Bind the physical
  board measurement, focus/zoom/crop state, and mount-stability proof.
- Before extrinsic fitting, prove whether the measured Brown-Conrady model can
  be represented by the deployed UE5/CARLA optical path. If not, implement and
  hash-bind either physical-feed undistortion to the deployed pinhole model or
  a source-controlled render-distortion path, then re-run dense-ray and scene
  holdouts. Never substitute CARLA lens coefficients by similar names.
- Build temporally stable, class-aware real targets for finite road/curb edges,
  lane paint, crosswalk/stop paint, horizon, vanishing directions, and unique
  stable landmarks. Exclude vehicles, people, vegetation, shadows, reflections,
  and unmatched semantic classes.
- Correct or explicitly replace missing/misplaced Richmond road-paint topology
  in source-controlled map/config assets. Camera optimization must not compensate
  for a known map topology error.
- Validate the map independently before camera fitting using at least six
  surveyed stable landmarks spanning the site and ten non-collinear pairwise
  distances. Require horizontal RMSE <= 0.25 m and max <= 0.50 m, record survey
  uncertainty and datum, and separately validate elevation/road grade where it
  affects contact projection. Camera agreement with the same map is not this
  independent proof.
- Render synchronized UE5 RGB and metric depth for explicit 6-DoF pose, FOV,
  principal-point, and distortion candidates. Bind every buffer to the map,
  camera config, CARLA frame, candidate, and exact input hashes.
- Fit at least eight globally identified points and three finite road polylines;
  reserve at least four points and two road polylines as untouched holdouts per
  camera. Require point coverage >= 50% of width and 30% of height.
- Optimize with class-aware symmetric distance transforms, edge orientation,
  horizon/vanishing error, point reprojection, spatial coverage, and robust
  losses. Preserve multiple basins and reject boundary-hitting, multimodal, or
  underconstrained results.
- Use lighting-invariant geometry targets for the primary loss. If RGB edges
  are used, bind real and UE5 time-of-day/exposure conditions and prove that
  shadows, reflections, and seasonal vegetation cannot improve the score.

Exit for every camera, reported on a 1280x960 reference canvas with explicit
per-axis native-coordinate transforms:

- held-out point RMSE/P95/max <= 10/16/24 px;
- held-out road-geometry RMSE/max <= 6/12 px;
- measured physical-vs-deployed optical-model dense-ray RMS mismatch <= 0.25 px
  and max <= 1 px on a 41x31 native-image grid. This is an implementation
  consistency check between the measured lens model and the deployed renderer,
  not the looser scene-to-map reprojection-error gate;
- every required semantic class and spatial quadrant passes without regression;
- retained overlay has correct road-edge, lane/crosswalk topology, horizon,
  vanishing points, and stable landmarks.

All pixel gates are evaluated in native coordinates. When reporting at 1280
width, transform x and y independently by their native-axis scales and transform
the principal point and distortion domain explicitly; never assume the aspect
ratio. No camera configuration is deployed until all four cameras pass
independently.

## 2. Exact-frame vehicle observation and localization

- Redetect and segment every recoverable trusted vehicle observation from its
  exact media-clock frame. Preserve detector/model hashes, bbox, mask, class,
  confidence, occlusion, truncation, and uncertainty.
- Estimate the visible road-contact/footprint midpoint from segmentation,
  wheel/contact evidence, local road geometry, and adjacent frames. The bbox
  bottom-center remains diagnostic only. The bbox geometric center may be a
  low-weight silhouette cue but is never projected directly onto the road.
- Cross-model contact proposals must cover the complete frozen capture
  denominator, bind exact frame and mask hashes, validate native mask dimensions
  and finite symmetric positive-semidefinite covariance, and pass independent x
  and y disagreement limits scaled from 1280x960. Width-only or Euclidean
  thresholding is forbidden. Proposal consensus is not independent contact truth.
- Track dense frame windows with appearance, motion, mutual exclusion, and
  calibrated geometry. Persist ambiguity rather than greedily merging cars.
- Associate cross-camera identity only with trusted schema-v2 clocks, plausible
  transit, finite <= 2 m combined localization uncertainty, and pinned ConvNeXt
  similarity >= 0.60. Zero identity switches are permitted in accepted tracks.
- Establish identity truth independently: two reviewers or independent review
  pipelines label time-disjoint clips while blind to matcher output, disagreements
  are adjudicated, and the 0.60 similarity floor is validated separately for
  every camera pair against positive and hard-negative examples. Matcher output
  never labels its own truth.
- Set every camera-pair appearance floor on the development split only and
  freeze it before holdout. Require Cohen's kappa >= 0.80 between blind identity
  reviewers before adjudication; lower agreement fails the truth-labeling gate.
- Fit one shared ground-plane trajectory, heading, velocity, dimensions, and
  bounded blueprint family per vehicle. Optimize across all available cameras
  and time using silhouette, contour, footprint/contact, projected 3-D box,
  optical flow, lane legality, occlusion, and temporal smoothness.

Exit:

- every recoverable corpus observation has reproducible exact-frame evidence;
- accepted observations have finite uncertainty <= 2 m and no circular use of
  legacy detector-derived GPS as truth;
- fit/development/holdout tracks remain disjoint;
- at least 80% of recoverable trusted vehicle observations remain acceptance
  eligible after fixed occlusion/truncation rules, so the denominator cannot be
  reduced adaptively to hide difficult cases;
- every rejected or unavailable observation has an explicit retained reason.

## 3. Held-out UE5 same-car proof

- Replay only untouched held-out tracks after all four static cameras pass.
- Establish absolute vehicle truth with the independent RTK test vehicle, not
  the fitted trajectory. Align the controller clock through the passed optical
  timing model, transform antenna fixes through the surveyed lever arm and map
  datum, and keep the RTK trajectory outside fitting. A self-fitted multi-camera
  trajectory is diagnostic consistency evidence only.
- Use one stable-digest UE5 blueprint and actor ID for each track across bridge
  restarts. Render the projected 3-D actor bbox, centroid, silhouette, and
  visible ground footprint in every matched view.
- Require at least three distinct frames spanning at least two seconds per
  accepted transition, at least 0.25 m of physical/actor movement, and consistent
  projected-vs-detected direction and displacement.
- Require world centroid error <= 2 m, projected centroid error <= 16 px at
  1280 width, bbox IoU >= 0.50, actor coverage >= 0.50, raw actor visibility >=
  75%, and compatible visual detection confidence >= 0.50 on every required
  frame. Reject foreground occlusion or a neighboring actor that explains the
  detection.
- Report success over all acceptance-eligible held-out observations and tracks;
  no failed eligible event may be hidden by an average. Ineligible events remain
  visible with fixed reasons such as severe occlusion, truncation, or missing
  exact source pixels.

Exit: at least five independently held-out same-car transitions per camera,
at least 20 total, and at least two for every camera pair with observed overlap
pass. Every available eligible multi-camera transition also passes, with zero
identity switches, zero stale actors, and zero unexplained eligible failures.
The minimum sample prevents a single favorable event from establishing product
accuracy; it does not permit hiding any other eligible failure.

Eligibility and adjudication reasons are pre-registered before viewing the
holdout. Two blind reviewers may adjudicate whether a fixed occlusion or
truncation rule applies, but an adjudicated eligible failure remains a failed
event. The zero-unexplained-eligible-failure gate is not replaced by an average
or pass-rate threshold.

## 4. Regression, deployment, and product proof

- Pass bridge, perception, web, type/protocol, AWS-route, rollback, and
  deterministic calibration tests from the exact candidate commit.
- In a zero-session maintenance gate, hold mutation-capable timers, capture and
  rehearse rollback, deploy one layer at a time, and restore immediately on a
  restart increment, UE5 fault, listener loss, or acceptance regression.
- Prove LIVE restoration, zero replay actors, multi-session isolation, and no
  CARLA/Drive restart increments through the post-deployment watch.
- Prove all four physical feeds fresh and changing, trusted media clocks,
  proactive HLS rotations without >5 s outage, <=10 s decode latency, fresh
  schema-v2 persistence, and no socket accumulation.
- Prove local and public `/drive`, `/live`, and `/timeline` with refreshed
  screenshots plus console, network, WebSocket, and exact same-car replay
  evidence. The browser-selected endpoint must match the live published config.
- Record deployed source/config/model/map hashes, UI/API evidence, rollback
  command, corpus outcome counts, and remaining non-acceptance debt. Leave no
  source or configuration only in the live checkout.
- Run a 30-minute attended post-deployment watch and a 24-hour unattended watch.
  Any service restart, listener loss, stale feed beyond the limits above,
  session/socket accumulation, actor leak, or calibration regression triggers
  the rehearsed rollback.
- Supervise the unattended watch with a source-controlled systemd watchdog that
  records every sample and automatically invokes the exact rehearsed rollback
  bundle on a fixed trigger. Prove watchdog and rollback in a non-production
  rehearsal, record timer/unit hashes, and name the on-call owner; an unattended
  prose instruction is not a rollback mechanism.
- Re-run stable-landmark projection weekly and after a mount impact, refocus,
  zoom/crop change, firmware/image-pipeline change, thermal shift outside the
  validated range, or map update. Reopen Phase 1 if any landmark exceeds the
  held-out point gate or the aggregate RMS regresses by more than 20%.
- Install the weekly check as a tracked timer with retained reports and an
  explicit owner/escalation target. Establish the validated thermal interval
  from Phase 0 telemetry; an unmeasured interval cannot suppress recalibration.
- Validate shadow/exposure robustness on time-disjoint development and holdout
  conditions spanning the claimed operating envelope. If only one day/weather
  regime exists, state that narrower envelope and do not claim more.

Exit: every gate above is green on the deployed version through both watch
windows and rollback evidence is independently restorable.

## Stop and escalation rules

Do not weaken a threshold, fabricate measured intrinsics, promote matcher
proposals to held-out truth, relabel circular coordinates, or alter unrelated
live sessions. Continue autonomously through code, data, rendering, testing,
and rollback-safe deployment work. Report a blocker only after three repeated
goal turns establish that the same required physical input, site authorization,
credential, or external state cannot be produced safely from the Path PC.
