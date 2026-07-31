# Semantic inverse-rendering calibration plan

Status: implementation plan; supersedes the sparse-point and bbox-bottom plan.
Scope: Path PC V2X UE5.5 worker only. No UE6 resources.

## 1. Freeze inputs and safety state

- Preserve exact hashes, timestamps, camera IDs, and native resolution for real
  frames. Split entire physical time windows into fit, development, and untouched
  holdout sets before optimization.
- Record the UE5 image, binary, map, OpenDRIVE, camera config, service restart,
  timer, and zero-session fingerprints before every rendering window.
- Run the packaged UE5.5 image as a separate calibration worker on loopback-only
  ports, with restart disabled and a bounded lifetime. Never point the optimizer
  at production ports 2000-2002. Use only temporary owned sensors and actors and
  destroy them in `finally`.

Exit: immutable source corpus, disjoint splits, and a clean safety baseline.

## 2. Build class-aware static targets and a UE5 render harness

- Real frames: produce masks and sub-pixel polylines for road boundary/curb,
  lane center paint, crosswalk/stop paint, horizon, and unique stable landmarks.
  Use temporal median frames to suppress vehicles and moving foliage. Retain raw
  model proposals and Codex-reviewed corrections as diagnostic provenance.
- UE5: render RGB, semantic segmentation, instance IDs, and depth from one
  temporary camera for an explicit pose/FOV candidate. Bind all buffers to one
  CARLA frame and candidate hash.
- Normalize real and twin masks at the same native aspect ratio. Exclude dynamic
  objects, vegetation, shadows, reflections, and map classes that visibly do not
  exist in both domains.

Exit: deterministic candidate rendering plus hash-bound paired semantic targets.

## 3. Optimize each fixed camera from static geometry

- Fix the global site/map gauge with surveyed anchors (or one jointly fitted,
  frozen site SE(2) transform) before interpreting per-camera XY/yaw. Search
  absolute x/y/z, pitch/yaw/roll, and FOV coarse-to-fine. Keep principal
  point and distortion as sensitivity variables until physically measured.
- Score class-specific symmetric distance transforms, oriented edge agreement,
  horizon/vanishing-direction error, landmark reprojection, and visible-area
  coverage. Use robust losses and per-class uncertainty; never score generic
  image edges indiscriminately.
- Start with bounded Sobol/grid candidates, keep multiple basins, refine with
  Powell or CMA-ES, then render full-resolution finalists. Cache candidates by
  full pose/config/map hash.
- Estimate parameter covariance and reject boundary-hitting, multimodal,
  underconstrained, or class-incompatible solutions.

Exit: a diagnostic candidate only when it improves every required static class
and does not regress any held-out view.

## 4. Static held-out acceptance gate

- On untouched frames and annotations require road-boundary P95/max <= 6/12 px
  at 1280 width (scaled at native resolution), point RMSE/P95/max <= 10/16/24 px,
  stable horizon/vanishing directions, and retained four-camera overlays.
- Report errors per semantic class, spatial quadrant, time window, and camera.
  A visually contradictory overlay, map topology mismatch, or missing measured
  intrinsics keeps deployment closed even if aggregate loss is low.

Exit: freeze each passing camera independently; otherwise return the exact map,
intrinsics, or annotation deficit without weakening thresholds.

## 5. Fit vehicle trajectories after cameras are frozen

- Redetect, segment, and track exact dense archived frames. Associate the same
  car across cameras using time, appearance, mutually exclusive geometry, and
  uncertainty; never trust stored GPS derived from the old camera model.
- Optimize one shared ground-plane trajectory, heading, velocity, and dimensions
  across time/cameras. Treat blueprint family and length/width/height as nuisance
  variables selected from a bounded library.
- Score silhouette IoU, symmetric contour distance, visible wheel/road contacts,
  projected 3-D box, optical flow, road/lane legality, and temporal acceleration.
  When the real vehicle class has no close UE5 mesh, use the robust silhouette
  centroid/midpoint and longitudinal extent as lower-weight cues rather than
  forcing a sedan contour onto a truck.

Exit: fit windows and untouched tracks are disjoint; actor placement is stable,
physically plausible, and jointly explains every available camera.

## 6. UE5 replay, regression, and deployment

- In a bounded maintenance window, replay unseen vehicles and require exact media
  clock provenance, one stable UE5 actor, same-car appearance proof, projected
  overlap in every matched view, directional motion agreement, cleanup, and
  multi-session isolation.
- Require no UE5/Drive restart increment or crash signature. Restore the prior
  bundle immediately on any gate failure.
- Deploy camera/source/config only after four-camera static and dynamic gates pass;
  preserve the rollback bundle and leave no source only in the live checkout.

Exit: documented deployed hashes, retained evidence, rollback command, remaining
debt, and an explicit fail-closed result for every camera and held-out track.
