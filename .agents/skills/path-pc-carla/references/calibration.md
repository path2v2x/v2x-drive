# Calibration, archived detections, and same-car evidence

Use this workflow for camera calibration, detection localization, mapping, or
same-car twin proof. It is intentionally offline until every deployment gate
passes.

## Current acceptance state

- Canonical `origin/main` and the clean live V2X tree are PR 54 merge
  `400c3277452154985096bc251fe65b4be60cef36`. PR 52's bounded perception
  lifecycle is accepted and remains part of that live revision.
- Schema-V2 uploads are enabled and passed the controlled activation at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T183416Z-pr54-v2-upload-activation/`.
  The retained row has exact DynamoDB/API/object-history parity and the live
  UE5 observation has 3/3 tracked-object-to-present-actor parity. Roll back with
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T183416Z-pr54-v2-upload-activation/`;
  the preceding PR 54 bridge rollback is
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T183135Z-pr54-twin-spawn/`.
- Those runtime results do not pass calibration. The strict static gate is 0/4
  cameras; all 895 retained diagnostic evaluations remain
  `acceptance_eligible=false`. The calibrated reconciliation branch is
  source-only and not deployed. Do not modify its holdouts or optimizer, relax
  a threshold, or promote a diagnostic pose.
- Measured physical intrinsics remain absent. Live config repeats assumed
  `fx=fy=1325.4`, `cx=1280`, `cy=960`, while perception consumes missing/zero
  distortion. The acquisition tool does not yet bind channel/device, exact
  board SVG plus physical measurement/photo, capture UTC, encoder/crop/focus/
  zoom state, or before/after mount stability. Those fail-closed bindings and
  distortion consumption are implementation/acquisition blockers; never call
  the current K or zero coefficients measured.
- The isolated V2X UE5 source workspace is `/mnt/v2x-ue5`. UE6 is a separate
  task and is excluded from this workflow, its evidence, and its gates.
- The paginated persistence gate is row-complete and fail-closed: any unknown
  camera, rejected row, missing/blank `event_id`, or duplicate `event_id`
  anywhere in the queried window fails the global result. Duplicate page items
  never count toward a camera's span or recency evidence. Leading/trailing
  event-ID whitespace is invalid. Every accepted row must retain
  `exact_same_session_pts`, reconstruct media UTC within 5 ms, keep ISO and
  epoch decode receipt within 5 ms, ingest within five integer seconds, and
  expire at exactly seven days from media time.
  Run row-content verification before the selected evidence expires. Score TTL
  as the exact producer delta of 604800 seconds, independent of verifier/read
  time; DynamoDB may purge later and the long watch must not require a purged
  item to remain readable.

## Non-circular evidence contract

- Never fit to persisted GPS, camera-local XZ, current CARLA actor positions,
  lane-snapped poses, or model-generated object IDs. They were derived from the
  nominal camera model and are baselines only.
- Fit inputs are exact archived frames, manually reviewed wheel/road contact
  pixels with covariance, trusted schema-v2 HLS media time, measured per-camera
  intrinsics/distortion, surveyed ENU static/lane geometry, and reviewed
  whole-object track/association evidence.
- Keep the OpenDRIVE site transform fixed unless independent survey evidence
  selects a different model. Use `v2x_common/geodesy.py`; do not reintroduce
  degree-per-meter constants.
- Detection-assisted camera changes remain diagnostic even when their numerical
  objective improves. Do not write `config/cameras.json`, restart perception,
  or spawn candidate actors from that output.

## Freeze and curate detections

1. Export a rolling 24-hour corpus with
   `apps/perception/tools/export_detection_corpus.py`. Reconcile every page with
   `/detections/timeline`, preserve `SHA256SUMS`, and never persist signed URL
   queries. The systemd service/timer templates are opt-in deployment artifacts;
   do not enable them during observational work.
   Assign every trusted vehicle observation a terminal accepted, rejected, or
   exact-frame-unavailable state; an unaccounted row fails the phase.
2. Build the pixel-only ledger with
   `apps/perception/tools/build_detection_observation_ledger.py`.
   `derived_baseline` must remain quarantined and forbidden as an optimizer
   target.
3. Recover each source frame with
   `apps/perception/tools/verify_historical_correlation.py` using the exact
   persisted detection JSON. Require trusted HLS time, at most 100 ms nearest
   frame error, a decodable native-resolution frame, hash-bound report, and no
   signed URL in output.
   Build proposal-only per-camera static composites with
   `build_temporal_static_targets.py`. Use schema v2: whole source windows stay
   in one split, event boxes are expanded only to exclude dynamic pixels, and
   the canonical median requires explicit unmasked-sample coverage. Window IDs
   are content-bound and path-independent. The default requires at least three
   valid samples; a one-sample override is diagnostic only and must never be
   presented as temporal stability. Raw medians, bbox masks, and temporal
   stability are never annotation truth.
4. Apply wheel/road contacts with
   `apps/perception/tools/apply_ground_contact_reviews.py`. Acceptance requires
   a named human and the exact retained frame plus verifier-report hashes.
   Model-only contact proposals can guide review but can never pass this tool.
5. Generate proposals with `propose_detection_tracklets.py`, apply named-human
   motion/occlusion/optical-flow/lane review with
   `apply_tracklet_reviews.py`, and freeze whole evidence groups with
   `freeze_track_split.py`. A later UTC day must be holdout. Never split frames
   from one physical car across partitions.

## Fit and evaluate

Run `fit_detection_factor_graph.py --preflight-only` first. Per camera require
at least 30 reviewed moving tracklets, three lane paths including a turn,
near/mid/far fractions of at least 20/50/20 percent, 60 percent image-width and
40 percent image-height coverage, valid measured intrinsics, and no association
split leakage.

The diagnostic fit additionally requires:

- `v2x-static-camera-solution/v1` bound to `cameras.json`, surveyed ENU truth,
  a passing independent static holdout, tight pose priors/bounds, and a frozen
  site-to-map transform;
- `v2x-surveyed-lane-map/v1` independent of detections and no worse than 0.25 m
  survey accuracy;
- reviewed simultaneous cross-camera pairs before estimating clock offsets.
  Otherwise fix each offset to zero/unobservable.
- at least four non-overlapping clock windows spanning 12 hours, pairwise P95
  phase at most 75 ms, max at most 125 ms, and drift at most 10 ms/hour. KVS
  producer-time agreement remains diagnostic until an independent timing target
  measures sensor exposure and replay/CARLA clock offset. Require at least 80%
  reciprocal one-to-one matches per camera side. Reject exact/shared
  zero-residual timestamp grids as likely common ingest stamping rather than
  physical exposure synchronization; retain an injected-offset recovery test.

Cross-model segmentation contact consensus uses schema v2. It must bind and
load the exact capture report, account for every event in that frozen
denominator, verify each mask against native frame dimensions, validate finite
symmetric positive-semidefinite 2x2 covariance, and compare x/y disagreement
independently after native 1280x960 axis scaling. Euclidean distance or
width-only scaling is forbidden. Even a passing proposal remains
`acceptance_eligible=false` until independent contact review.
Segmentation masks and overlays must be staged outside the destination and
published as one atomic no-replace directory; a partially written artifact
tree is not valid evidence.

The optimizer holds measured intrinsics and the site transform fixed, uses weak
lane distance without snapping, excludes pose priors from its Jacobian rank
test, and always reports `acceptance_eligible=false`. A successful synthetic or
diagnostic fit is not production proof.

Before any four-camera fit, bind exactly one manifest per channel to one
surveyed site registry with
`apps/bridge/tools/aggregate_twin_calibration_manifests.py`. The registry and
all four manifests must be SHA-256-recorded by the aggregation report; one
canonical `global_landmark_id` maps to one frozen split, surveyed XYZ, and
external survey-record path/hash/size site-wide. Every input annotation,
real/twin frame, cameras file, intrinsics artifact/source image, depth buffer,
and survey record must exist and be re-hashed by both aggregation and optimizer.
Every camera must participate in shared landmark identities and those identities
must form one connected four-camera graph; zero-sharing and disconnected camera
islands fail. Reuse across cameras is valid only with that exact identity.
Different IDs below 0.25 m, cross-camera split changes,
inconsistent depth-resolved map coordinates, mixed map/OpenDRIVE fingerprints,
incomplete builder contracts, malformed entries, missing cameras, or
inconsistent survey identity fail closed. The aggregation remains
`acceptance_eligible=false` and does not authorize deployment by itself. Every
optimizer invocation must pass the retained aggregation report; the optimizer
re-hashes all four manifests and the registry and recomputes the report before
any UE5 runtime probe.

RR/CARLA 0.10 live camera acceptance must recompute the tracked transform in
strict projection mode and retain `projection.source="opendrive_georeference"`.
`origin_centered_fallback` is diagnostic only. A missing/malformed declaration
or a syntactically valid projection that disagrees with CARLA's map origin is a
hard failure. Retain the exact map name, full OpenDRIVE SHA-256, and extracted
georeference SHA-256 in every builder manifest and runtime comparison.

## Richmond map-correction gate

- Do not use the historical UE4.26 road-marking package as a loose mount,
  runtime injection, or package transplant. The loose asset and a separately
  UE5.5-resaved/cooked minimal-project transplant both caused isolated worker
  exit 139; the latter failed only when Richmond loaded.
- A future road-marking correction must originate in the actual CARLA UE5.5
  source project, preserve the complete map/package dependency graph, and be
  cooked as a complete fingerprinted map image. A successful standalone asset
  cook is insufficient.
- Require a zero-session fail-safe window for its first isolated boot. Reject
  immediately on map-load timeout, exit 139, OpenDRIVE drift, or any production
  restart-counter change. Restore production and timers before diagnosis.
- Do not resume inverse-render fitting until the corrected map passes Richmond
  load, exact OpenDRIVE hash, fit/dev road-topology renders for all four
  cameras, and a fresh untouched held-out static-geometry gate.
- The current deployed map has failed a camera-independent planar-consistency
  diagnostic. Evidence at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T103000Z-ch4-crosswalk-planar-consistency-v1/`
  fits one crosswalk on the common road plane and shows other visible physical
  crosswalks disagreeing by tens to hundreds of pixels. Do not reinterpret this
  as a camera-pose residual: a single projective camera cannot make mutually
  inconsistent coplanar correspondences agree.
- The shared projection chain has about 0.015 mm retained anchor error, but one
  global SE(2) still fails approach-held-out stability and cannot cure topology.
  The current `export_map_calibration_geometry.py` overwrites waypoint marking
  state and keeps only the final left/right marking for a lane, while
  enumeration-only crosswalk IDs are unstable. Require segmented OpenDRIVE
  `roadMark` ranges with stable road/lane/s/t identities and stable road/object
  crosswalk IDs. QL2 vertical RMSE/P95 of about 0.044/0.087 m lacks horizontal
  residual and current-paint truth and is not a correction gate.
- The recovered raw bundle OpenDRIVE is the older SHA-256
  `ed2e44492616901fbb20b89191ab03d666c0217620d0247e55235c116f5cf2b1`
  with 222 roads/29 junctions. The deployed UE5.5 map is
  `0737f3d9f9f344c06b2c63fe669afa8a15f814568ee9c16046795338f56f5ee1`
  with 208 roads/32 junctions. They are not byte-identical; keep map-source
  lineage open until that topology difference is explained.
- Runtime environment-object inspection is not a correction mechanism. The
  deployed worker exposes eight aggregate `RoadLines` objects containing the
  road-paint layer; individual crosswalks cannot be disabled, and disabling the
  aggregates removes the whole layer. Debug-drawn lines do not satisfy the
  complete-map, fingerprint, collision, replay, or rollback contract.
- Independent per-camera XYZ fitting is diagnostic overfit, not a workaround.
  If visual fitting is continued before a corrected map exists, use one shared
  camera-cluster world translation with bounded per-camera orientation/FOV,
  report Jacobian rank/condition and bound hits, and keep its output
  `acceptance_eligible=false`.
- The current shared-cluster diagnostic implementation is
  `apps/bridge/tools/fit_joint_diagnostic_visual_calibration.py`. It exposes
  only one XYZ delta for the whole four-camera cluster and per-camera
  pitch/yaw/roll/FOV deltas, excludes priors from the numerical Jacobian, binds
  the map-consistency report, and always emits production gate false. Its first
  retained result fails bound and holdout gates; do not expand bounds merely to
  remove the reported hits.
- The required source/capacity acquisition and full-cook sequence is
  `docs/v2x-map-correction-recovery-plan.md`. UE4.26 editor assets, cooked
  packages, another task's linked content, and an unlabelled runtime image are
  not equivalent to a dedicated complete CARLA UE5.5 source graph.

## Exact archived same-car proof status

- `global_car_4db7ffc8_138` has an exact 0 ms fMP4 source-frame binding for one
  representative event on each of ch1/ch2/ch3/ch4 at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T100128Z-object-138-exact/`.
  The independent detector overlaps are strong, and the views support the same
  white Toyota Camry, but neither model object IDs nor visual similarity are
  blind identity truth.
- Segmentation ground-contact consensus accepts proposal contacts for ch1,
  ch2, and ch4 and rejects the clipped ch3 view. Proposal agreement never
  replaces named independent contact review. Do not place or score a UE5 actor
  from this sample until static calibration and identity gates pass.
- A nearby KVS `GetImages` frame within 150 ms is not interchangeable with the
  exact archived fMP4 frame. Persisted detection boxes must bind to their exact
  source time/frame; a visually similar neighboring frame is ineligible.

## Production acceptance

Do not promote a candidate until all of these pass without relaxed thresholds:

- per-camera checkerboard/ChArUco fit RMS at most 2 px and untouched holdout max
  at most 5 px;
- independent map validation from at least six surveyed stable landmarks and
  ten non-collinear distances, horizontal RMSE at most 0.25 m and max at most
  0.50 m, with datum/elevation uncertainty retained;
- held-out static geometry at 1280x960: landmark RMSE/P95/max at most 10/16/24
  px and road-polyline RMSE/max at most 6/12 px, transformed per native x/y
  axis rather than by width alone;
- whole-track bootstrap pose spread at most 0.2 degrees and 0.10 m, full data
  Jacobian rank, condition at most 1e8, no parameter at a bound, and no camera
  degraded on validation/holdout;
- independent RTK/reference actor median/P95 planar error at most 0.5/1.5 m,
  correct lane at least 95 percent, cross-camera same-car P95 agreement at most
  1.0 m, with raw unsnapped positions used for scoring;
- 24-hour shadow replay, then zero-session canary proof for all four cameras:
  stable same actor across at least three frames, centroid at most 16 px at
  1280-wide, bbox IoU at least 0.50, world error at most 2.0 m, correct motion,
  visible road geometry, cleanup to LIVE and zero sessions.
- at least five held-out same-car transitions per camera, at least 20 total,
  at least two for every observed overlap pair, zero identity switches, and no
  hidden eligible failure. At least 80 percent of recoverable trusted vehicle
  observations must remain eligible under frozen occlusion/truncation rules.
- label identity independently of matcher output with two blind reviews or
  review pipelines plus adjudication; validate the fixed appearance threshold
  separately for every camera pair.

Evaluate a holdout once. Seeing its result burns it for all later optimizer,
map, threshold, model, or annotation choices; replace it only with a newly
frozen time-disjoint window/track and retain the original failure. After deploy,
watch actively for 30 minutes and unattended for 24 hours, then revalidate
stable landmarks weekly and after any mount, focus, crop, firmware, thermal, or
map change.

If physical target images, surveyed landmarks/lane geometry, or RTK truth do not
exist, record the exact acquisition deficit and stop at diagnostic status. Do
not fabricate labels or lower a threshold to turn missing evidence into a pass.
