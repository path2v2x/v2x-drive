# Detection-constrained V2X calibration and placement plan

## Decision and current state

The retained detections can materially improve camera-to-map registration, but
they cannot independently establish absolute calibration. Each persisted GPS
point is currently generated from the detection's bottom-centre pixel through
the same nominal camera model being evaluated:

`bbox bottom-centre -> nominal K/zero distortion -> pitch/yaw ground ray -> local XZ -> flat-earth GPS -> flat-earth CARLA XY`

Fitting camera parameters to those persisted GPS/CARLA positions would be
circular. Use the raw bbox pixels, trusted media timestamps, same-camera
tracklets, cross-camera appearance, and CARLA road topology as observations;
treat persisted GPS and current actor positions only as baselines to beat.

Observed on 2026-07-11 UTC:

- The preceding 24 hours contain 4,061 detections, 283 grouped events, 276
  trusted schema-v2 events, and 129 vehicle events / 667 vehicle detections.
- Vehicle coverage is ch1 105, ch2 276, ch3 71, ch4 215 detections.
- Persisted records contain bbox, bottom-contact pixel, local XZ, GPS, and a
  model-derived uncertainty, but production records do not persist the required
  independent `identity_association` evidence.
- All four physical cameras still share nominal `fx=fy=1325.4`, centred
  principal point, zero measured distortion, one site origin, and assumed 7 m
  height. The integration config intentionally lacks measured
  `intrinsics_calibration` and `localization` blocks, so its perception startup
  remains fail-closed.
- The ch4 Playwright replay maps the selected white car to a UE5 actor but does
  not show that actor at the corresponding physical image location. Road and
  crosswalk geometry are also visibly misregistered.
- The legacy co-perception point rows are sparse, not independent holdouts, and
  have metric RMSE of about 1.34/4.73/2.58/0.96 m for ch1/ch2/ch3/ch4.

## Model to build

Build a batch factor-graph / robust bundle-adjustment tool that consumes raw
observations rather than stored GPS labels.

### Variables

- Per physical camera: 6-DoF pose in one surveyed site frame.
- Per physical camera: measured K and Brown-Conrady distortion, fixed from the
  independent intrinsics phase during the deployable fit; allow them to vary
  only in a separately reported diagnostic fit.
- Site-to-CARLA transform: bounded SE(2) translation/yaw, with scale fixed by
  the OpenDRIVE georeference unless survey evidence proves otherwise.
- Per vehicle track: latent ground-plane trajectory, lane sequence, velocity,
  heading, and association state.
- Per observation: ground-contact point and covariance. Prefer vehicle
  segmentation/wheel-contact keypoints; retain bbox bottom-centre as a noisier
  fallback with a larger learned covariance.

### Residuals

- Physical pixel reprojection of each latent ground contact through measured
  intrinsics/distortion and camera pose.
- Point-to-lane/road-surface distance without overwriting the raw position.
- Trusted-time velocity, acceleration, turn-rate, and non-holonomic motion.
- Cross-camera trajectory continuity tied only by accepted ConvNeXt appearance,
  time, class, direction, and mutually exclusive association evidence.
- Static lane-edge, curb, crosswalk, horizon, vanishing-point, and unique
  landmark reprojection from independently annotated geometry.
- Survey/RTK priors when available.

Use robust losses and explicit mixture/outlier states. Never force every car to
the nearest lane, let greedy order decide identity, or report lane-snapped
positions as calibration truth.

## Phase 0 — freeze the expiring corpus

Actions:

1. Export every schema-v2 row before DynamoDB TTL removes it, paginated with
   exact API response hashes. Store only sanitized URLs. Run this as a rolling,
   monitored export at least hourly for multiple days; the first dump is urgent
   and must not wait for later reconciliation or curation.
2. Preserve the nearest physical frame for every accepted vehicle observation,
   its bbox crop, HLS program date-time, camera ID, detector/model hash, run ID,
   and source-frame hash.
3. Preserve current CARLA transforms and renders as the baseline, explicitly
   marked derived/non-truth.

Evidence and gate:

- Counts must reconcile with `/detections/timeline` and pagination must end
  without duplicate event IDs.
- Every retained fit candidate must be schema v2, trusted HLS time, finite bbox,
  finite raw local/GPS diagnostic, and have a decodable hash-bound frame.
- Split complete object tracks—not individual frames—into fit, validation, and
  frozen holdout sets. Reserve at least one later day and every chosen
  same-car camera transition from fitting. The later-day holdout comes from the
  rolling export, not the first 24-hour snapshot.
- Encrypt retained source frames/crops at rest, restrict access, define the
  minimum retention period needed for reproducibility, and redact plates/faces
  in derived review artifacts without altering hash-bound raw evidence.

Safety/rollback: read-only API/video acquisition; no service or config change.

## Phase 1 — make the observation and mapping path non-circular

Implementation:

1. Add a versioned `raw_observation` payload containing native-resolution bbox,
   ground-contact method/pixel/covariance, camera-config hash, detector hash,
   and trusted timestamp provenance.
2. Recompute world positions offline from raw observations for every candidate
   camera model. Do not reuse persisted GPS as an optimizer target.
3. Replace both flat-earth conversions with one tested WGS84 local-ENU / map
   georeference implementation and an explicit site-to-map transform.
4. Replace `placement_planar_error_m`—currently zero when only Z is adopted—with
   independent residuals: raw-to-lane lateral distance, raw-to-actor distance,
   reprojection error, and reference-to-actor error when reference truth exists.
5. Preserve raw XY; use road projection only for surface Z and orientation.
   If probabilistic lane matching is enabled, expose both raw and matched pose
   plus posterior probability and never use matched pose to score calibration.

Tests and gate:

- Pixel -> ray -> ENU -> GPS -> CARLA -> GPS round-trip fixtures at surveyed map
  points, with centimetre-scale numerical round-trip error.
- Mutation tests prove changing K/pose changes recomputed positions and that
  stored GPS cannot enter the fit objective.
- Synthetic lane-offset fixtures prove independent placement metrics are
  non-zero and preserve rejected raw positions.

Rollback: new schema/version flag remains dark; deployed reader continues to
consume the old payload until the complete offline gate passes.

## Phase 2 — extract reliable trajectory constraints

Implementation:

1. Re-run vehicle segmentation/keypoints on retained frames to estimate tire /
   road contact rather than relying only on bbox `y2`. A reviewed wheel/contact
   keypoint is mandatory for fit observations; bbox `y2` is diagnostic only
   because its systematic bumper/shadow bias does not average away.
2. Build same-camera tracklets with optical flow and trusted HLS time; reject
   truncation, occlusion, parked vehicles, shadows, and high contact covariance.
3. Generate cross-camera candidates with pinned ConvNeXt embeddings, plausible
   travel time, direction, lane connectivity, and class compatibility. Retain
   ambiguous hypotheses rather than collapsing them.
4. Require useful spatial excitation: multiple lane paths, turns, distances,
   and image regions per camera. A repeated single lane or clustered pixels is
   underconstrained.
5. Estimate a bounded clock offset per camera in the factor graph, with a tight
   prior from HLS program date-time. Independently verify offsets using shared
   cross-camera events or a synchronized test target; do not let time error be
   absorbed into pose or vehicle velocity.

Gate:

- Human-reviewed precision on a frozen association subset at least 99%; recall
  is secondary and may be lower.
- Contact-pixel uncertainty must be calibrated: at least 90% of reviewed
  contacts fall inside their predicted 95% interval.
- No fit track may share an object or frame with validation/holdout tracks.
- Before optimization, require per camera at least 30 accepted moving vehicle
  tracklets, at least three connected lane paths including one turn, at least
  20%/50%/20% of contacts in near/mid/far range bands, and accepted contacts
  spanning at least 60% of image width and 40% of height. If bootstrap pose
  spread still exceeds the later gate, increase whole-track counts rather than
  relaxing it. The current 667 detections—especially ch3's 71—are expected to
  be an initial diagnostic corpus, not sufficient acceptance evidence.

## Phase 3 — acquire independent absolute truth

Detection-only optimization remains diagnostic until this phase passes.

Required evidence:

1. Measure each pole/camera optical-centre height and pole/site position; use
   survey uncertainty as a prior to break height/pitch/focal degeneracy.
2. Measure per-camera intrinsics/distortion using at least ten accepted
   ChArUco/checkerboard fit images and two untouched holdouts per channel.
   First prove a sufficiently large target is visible from a safe location.
   If pole geometry makes in-situ capture infeasible, use a dimensioned larger
   target or a documented bench calibration of the exact fixed-focus unit;
   dense static-landmark self-calibration remains diagnostic, not equivalent.
3. Collect at least eight fit and four untouched globally identified static
   landmarks per camera, plus finite road/crosswalk polylines.
4. Initiate one controlled test-vehicle pass carrying RTK-GNSS or a surveyed
   trajectory target in parallel with Phases 0–2. Synchronize it to HLS time
   and reserve part of the pass entirely for holdout.
5. Test the site-to-CARLA model explicitly: compare surveyed points against
   fixed-scale SE(2), bounded similarity, and planar/vertical residual models.
   Deploy the simplest model that passes held-out survey residuals; unexplained
   scale, roll, pitch, or terrain bias blocks camera fitting.

Gate:

- Intrinsics fit/holdout RMS at most 2 px and held-out corner max at most 5 px.
- RTK/reference actor planar error: median at most 0.5 m, P95 at most 1.5 m,
  correct-lane assignment at least 95%, without scoring a lane-snapped pose.
- Cross-camera estimates of the same car at matched time agree within 1.0 m at
  P95 and preserve identity.

## Phase 4 — fit diagnostic and deployable models

Implementation:

1. First fit camera poses/site transform using static geometry and measured
   intrinsics only.
2. Add latent vehicle trajectories and lane/kinematic residuals, then compare
   the solution against the static-only solution. Large disagreement is a
   failure, not an average to deploy.
3. Use deterministic multi-start robust optimization, observability/Jacobian
   rank tests, Hessian condition limits, bootstrap across entire tracks/days,
   and leave-one-camera/lane-out sensitivity analysis.
4. Produce two reports:
   - unconstrained diagnostic optical model;
   - exact production-representable model using the shared UE5/verifier path.

Gate:

- No fitted parameter may sit on a search bound or remain weakly observable.
- Bootstrap pose spread must remain within 0.2 degrees and 0.10 m; otherwise
  acquire more diverse tracks/static truth.
- Detection-assisted fitting must improve frozen validation and holdout errors,
  not merely training loss, and must not degrade any camera.
- Keep existing held-out geometry gates: at 1280x960 landmark RMSE/P95/max at
  most 10/16/24 px and road-polyline RMSE/max at most 6/12 px, scaled by native
  width/1280. Retained images must also pass visible topology review.

## Phase 5 — shadow replay and controlled UE5 release

1. Run the new mapper in shadow mode against new traffic for at least 24 hours;
   publish no candidate locations and spawn no candidate actors.
2. Compare old/new map residuals, trajectory smoothness, ambiguity, coverage,
   and frozen static holdouts. Alert on drift; do not auto-update calibration.
3. In a zero-session maintenance gate, capture rollback, pause mutation-capable
   timers, canary one camera, and rerun the exact Playwright same-car replay.
4. Require the matched physical car and projected UE5 actor over at least three
   frames per transition: stable actor ID, centroid error at most 16 px at
   1280-wide, bbox IoU at least 0.50, world error at most 2.0 m, correct motion,
   no competing actor, and visible road geometry pass.
5. Repeat all four cameras, multi-session isolation, stale cleanup, four-feed
   freshness, HLS rotation, persistence, LIVE restoration, zero sessions, and
   rollback rehearsal before release.

Rollback: revert the source/config fingerprint and redeploy the prior exact
version; calibration artifacts are immutable evidence and never edited in
place.

## Prioritized next executable actions

1. Immediately start the rolling export and hash the current 24-hour raw corpus
   before TTL expiry; operational owner: backend/data, same day.
2. In parallel, initiate site-access, safe target placement, pole-height survey,
   and RTK test-pass scheduling; field owner: site/calibration, expected longest
   lead-time workstream.
3. Implement an offline observation-ledger builder and non-circular replay of
   candidate camera models; no production write path.
4. Replace flat-earth duplication and vacuous placement metrics behind tests.
5. Build the robust trajectory/map factor graph and run synthetic observability
   tests before using real detections.
6. Acquire measured intrinsics/static landmarks and the RTK pass; calibration
   owner and independent reviewer must freeze holdouts before fitting. Until
   then, detection-assisted results remain diagnostic.

## Implementation checkpoint — 2026-07-11

Implemented offline on `codex/v2x-calibration-current`, not deployed:

- bounded hourly corpus exporter with pagination/timeline reconciliation,
  all-string URL sanitization, userinfo removal, exact echoed-window checks,
  private output, free-space floor, 72-snapshot retention, and hardened opt-in
  systemd templates;
- pixel-only schema-v2 observation ledger that prefers emitted
  `raw_observation` fingerprints and permanently quarantines older rows lacking
  raw/model/config provenance;
- archived HLS frame verifier binding event ID, camera, object/type, bbox,
  native dimensions, exact bytes, trusted media time, and nearest-frame error;
- named-human frame-bound wheel/contact review, proposal-only object grouping,
  named-human optical-flow/occlusion/motion/lane track review, and atomic
  evidence-group/later-day split freezing;
- one shared WGS84/OpenDRIVE transverse-Mercator projection across perception,
  bridge, road export, and verification, with map-georeference cache keys bound
  to exact content;
- raw-to-target/actor metrics that no longer claim zero placement accuracy when
  independent reference truth is absent;
- artifact-bound fit preflight and bounded robust multi-start diagnostic fit.
  It fixes measured intrinsics/site transform, propagates reviewed 2x2 contact
  covariance through the pixel-to-ground Jacobian, uses reviewed direction,
  motion, weak unsnapped lane, and cross-camera trajectory residuals, estimates
  clock offsets only with sufficient independent synchronized evidence, tests
  the data-only Jacobian, and rejects any per-camera validation/holdout
  degradation. Output remains structurally `acceptance_eligible=false`.

The first real frozen corpus contains 4,061 rows and 642 trusted vehicle
observations. It yields only 56 proposal tracklets (ch1/ch2/ch3/ch4 =
15/9/13/19; 211 observations), all blocked pending frame-bound contact review.
It also predates emitted raw provenance and measured optics. Even perfect review
cannot meet the unchanged minimum of 30 diverse tracklets per camera, so it is
diagnostic only.

The next executable work is evidence acquisition, not another fit: keep rolling
exports, retain exact source frames, collect measured per-camera intrinsics plus
two untouched holdouts, survey static/lane/site geometry and camera centres,
run a controlled RTK pass, and obtain named-human contact/track/association
reviews. Only then run the preflight/fit, locked evaluator, bootstrap, 24-hour
shadow, Playwright same-car replay, and controlled UE5 deployment gate.

## Non-goals

- Do not infer measured lens distortion from vehicle tracks alone.
- Do not train on current GPS/CARLA actor positions as labels.
- Do not use lane snapping to manufacture low placement error.
- Do not deploy an online self-calibrator or automatically update production
  camera poses.
- Do not lower held-out thresholds because the corpus is noisy.
