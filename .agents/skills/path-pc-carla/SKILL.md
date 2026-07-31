---
name: path-pc-carla
description: Operate and diagnose the Path PC CARLA/V2X stack at path@100.72.252.40, including the production Unreal Engine 5.5 RR/CARLA 0.10 worker container, drive WebSocket bridge, Vite dashboard, perception/HLS pipeline, Cloudflare and Tailscale transport, systemd supervision, and controlled deployment/rollback gates. Use for any work that reads, tests, changes, deploys, or recovers the Path PC V2X environment; exclude Unreal Engine 6 experiments, which belong to a separate task and runtime namespace.
---

# Path PC CARLA/V2X

Treat this file as an operating procedure, not proof of current state. Re-run the read-only baseline before every intervention.

## Newest strict-calibration execution state

Observed through 2026-07-14 17:00 UTC; verify rather than assume. These items
override older calibration candidate, test-total, evidence, and next-gate
statements below.

- The clean integration worktree
  `/home/path/.codex/worktrees/v2x-strict-calibration-integration` is at
  `97cc334`. It includes the reviewed proposal-only static fitter, fail-closed
  recovered/deployed OpenDRIVE topology correspondence, and Tier-B dynamic C0
  evidence contracts. The combined locked suites pass 690/690 under the CARLA
  Python 3.10 environment and 97/97 under the pinned map/LiDAR Python 3.12
  environment; the hostile runner harness also passes. These are source gates,
  not calibration, placement, deployment, or same-car acceptance.
- Map selection remains blocked. The recovered 222-road/29-junction map and
  deployed 208-road/32-junction map are structurally different. Topology
  correspondence is conservative and proposal-only; it cannot establish
  authoring/export provenance, select a candidate, or authorize scoring.
- The static manifest builder candidate is clean and passes its focused and
  locked suites, but it has no exact Fable verdict and is not integrated. Its
  current deterministic deficit records zero independently adjudicated static
  annotations and missing source-inventory/reviewer-ledger contracts. Never
  relabel diagnostic matcher, CSV, or inverse-render artifacts to fill that
  deficit.
- A sanitized 24-hour development snapshot frozen at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260714T164410Z-past24h-development-corpus/20260714T164430Z`
  contains 1,799 detections, including 421 trusted schema-V2 vehicle events
  across ch1-ch4. All 421 nearest retained frames were recovered read-only from
  Kinesis into the sibling `20260714T164410Z-past24h-trusted-vehicle-frames`
  bundle with unique event IDs and one bound camera-config/snapshot identity.
  Only 417/421 are within the 150 ms temporal gate and only 3/421 persisted
  boxes apply to the selected frame under the 50 ms identity tolerance, so the
  persisted boxes are not geometry truth. YOLO11m exact-frame redetection finds
  a matching proposal for 334/421 events; hash-pinned YOLO11x finds 355/421.
  Their conservative 0.60-IoU/full-visibility/contact-agreement gate retains
  consensus on only 210/421. Sixty-five events have no vehicle proposal from
  either model. Twenty-eight of those belong to one stationary ch2 production
  track whose 73-frame/73-second dense window visibly contains no car, proving
  that production event existence cannot be treated as vehicle truth. These
  development results are neither independently reviewed correctness nor
  twin-placement proof; retain the full disagreement and false-positive
  accounting.
- At the read-only live check, all four local perception cameras remain in
  `reconnecting` state because the external Kinesis producers stopped together.
  The local service must not be restarted as a substitute for restoring those
  producers. Historical ON_DEMAND retrieval remains usable for development,
  but live four-feed, recency, and cross-camera same-car gates all fail.
- Claude CLI currently reports `loggedIn=false`; no Anthropic API/OAuth
  credential is present in the process environment or authorized agent vault.
  Any source lane requiring the mandatory exact Fable review must remain
  uncommitted or unintegrated until authentication is restored. Never replace
  that gate with a different model or claim a Fable pass from a session-limit
  or authentication failure.
- The sealed holdout remains no-view. Do not list, hash, decode, sample, or use
  any content beneath
  `/home/path/V2XCarla/v2x-evidence/calibration/20260713T192217Z-untouched-holdout-candidate-vault`
  before parameter freeze and the single authorized evaluation. UE6 remains a
  separate task and runtime namespace and must not enter V2X evidence or gates.

## Newest perception release chronology

Observed through 2026-07-13 18:37 UTC; verify rather than assume. These items
override every older candidate, deployment, and next-gate statement below.

- Canonical `origin/main` and the exact, detached, clean live production tree
  are now PR 54 merge `400c3277452154985096bc251fe65b4be60cef36`.
  PR 54 preserves the accepted PR 52 perception lifecycle and adds bounded,
  deterministic UE5 twin-actor spawn bootstrap retries that always move a
  provisional actor to the exact detection-derived transform before tracking.
  Its controlled bridge deployment evidence is
  `/home/path/V2XCarla/v2x-evidence/bridge/20260713T183135Z-pr54-twin-spawn-deploy/`;
  its verified rollback is
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T183135Z-pr54-twin-spawn/`.
  CARLA, perception, and web retained their fingerprints, Drive restarted once
  into the candidate, all services held `NRestarts=0`, Richmond returned LIVE
  with zero sessions, and all intended timers were restored.
- Production schema-V2 uploads are now enabled and the controlled PR 54
  activation passed. Retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T183416Z-pr54-v2-upload-activation/`
  and rollback
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T183416Z-pr54-v2-upload-activation/`.
  One new trusted schema-V2 row had exact one-for-one DynamoDB, API, and object-
  history parity with identical SHA-256, 0 ms media reconstruction error, and
  seven-day expiry. The observational UE5 check was LIVE with zero sessions
  and exact 3/3 tracked-object-to-present-actor parity. This proves persistence
  and actor lifecycle only; it is not camera calibration, same-car visual
  overlap, or placement-accuracy acceptance.
- The strict static calibration gate remains **0/4 cameras passing**. The 895
  retained inverse-render evaluations are diagnostic only and remain
  `acceptance_eligible=false`; do not relax thresholds, consume a holdout again,
  or promote their poses. The current calibration reconciliation branch is
  source-only in the clean Codex worktree and is not deployed. The dedicated
  V2X UE5 source workspace is `/mnt/v2x-ue5`. UE6 remains excluded from this
  task, its evidence, and every V2X acceptance decision.
- PR 51's diagnostic five-child canary is rejected. Evidence is at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T160437Z-pr51-shutdown-diagnostics-canary/`,
  its bounded diagnostic is
  `bounded-shutdown-diagnostic.jsonl` with SHA-256
  `334433a7078c6ca772dbc31b61acc68e78f94eef8c31145ac3a2d99923095534`,
  and the verified rollback is
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T160437Z-pr51-shutdown-diagnostics/`.
  Exact all-four health, five FFmpeg children, `in_use=1`, and
  `proactive_preparations=1` were established before stop. The diagnostic
  proved the remaining cleanup was one unclaimed, discarded preparation in
  `capture_open`: cleanup age 32.201 seconds, both `reader_timeout` and
  `terminal_cleanup_timeout`, and one live reader. Its sanitized stack stopped
  in `candidate.join` at `live_capture.py:190`; the candidate stopped inside
  `cv2.VideoCapture(...)` at `ffmpeg_capture.py:1862`. The rawvideo partial-
  buffer warning immediately after stop proves the FIFO writer died while the
  native OpenCV constructor was still blocked. Exact PR 35, four feeds/readers,
  zero sessions/actors/tracks, and all intended timers were restored. Do not
  redeploy PR 51 unchanged.
- The installed PR 35 perception unit was also verified to use systemd's
  default `KillMode=control-group`, `KillSignal=SIGTERM`, `SendSIGKILL=yes`,
  `FinalKillSignal=SIGKILL`, and a 60-second stop timeout. That policy sends
  SIGTERM to every FFmpeg child at service stop before Python can own the
  cooperative teardown. Moving the Python cancellation watcher alone cannot
  fix that boundary. Never use `KillMode=process`; it can orphan children.
- PR 52 keeps an `O_RDWR|O_NONBLOCK` FIFO owner guard while OpenCV enters
  its native constructor and runs a bounded monitor that owns deadline/cancel,
  serialized FFmpeg TERM/KILL/reap, guard close, and the EOF wake needed when a
  producer dies before the native reader exists. Normal constructor return
  hands off to the existing cancellation watcher; release joins the monitor,
  kills/reaps the writer first, and then releases OpenCV. The unit uses
  `KillMode=mixed` so SIGTERM first reaches the Python main process, while
  `SendSIGKILL=yes`, `FinalKillSignal=SIGKILL`, and `TimeoutStopSec=60` retain a
  whole-cgroup crash/timeout fail-safe. The launcher still `exec`s Python.
  Real process probes bounded and reaped an immediate-exit producer in 0.012
  seconds, an alive/no-output producer in 0.109 seconds, and partial-writer
  cancellation in 0.054 seconds. All 87 focused and 251 full perception tests
  pass with warnings as errors; compilation, diff, and systemd verification
  pass; independent source review is clear. Its upload-disabled, zero-session
  canary passed at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T163629Z-pr52-open-boundary-canary/`;
  the verified rollback bundle is
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T163629Z-pr52-open-boundary/`.
  The gate caught a real five-child helper at sample 44, stopped in four seconds
  with `Result=success`, an empty cgroup, and no timeout/SIGKILL signature, then
  restarted exact all-four health in five seconds. Killing one live reader
  reached exact successful terminal recovery by sample three. The 90-by-five-
  second soak kept every feed fresh, inference-fresh, and
  `exact_same_session_pts`; ch1-ch4 published 764/756/768/775 new frames, all
  above the unchanged 700 floor. Natural helpers around samples 44-46 and
  87-90 quiesced back to exactly four readers in zero to seven seconds with no
  cleanup failure. Pre/post phase-4 checks retained Richmond live mode, four
  rig cameras, zero sessions/actors/tracks, unchanged CARLA/Drive/web
  fingerprints, and all intended timers. A prior 16:35 UTC harness attempt
  rejected before candidate start because this systemd reports equivalent
  signals as `15`/`9`; it rolled exact PR 35 back cleanly and is not runtime
  evidence. That lifecycle gate remains accepted in live PR 54; the newer V2
  upload/history activation above has also passed. Fable still fails
  authentication before file access; never claim a Fable pass.
- PR 50's first complete five-child stop canary is rejected. Evidence is at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T153621Z-pr50-claimed-handover-shutdown-canary/`
  and the verified rollback is
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T153621Z-pr50-claimed-handover-shutdown/`.
  PR 50 reached exact four-camera readiness in six seconds and the first
  qualifying 0.2-second helper sample captured `in_use=1`,
  `proactive_preparations=1`, five FFmpeg children, zero cleanup failures, and
  every camera fresh, inference-fresh, trusted, and `exact_same_session_pts`.
  Stop at 15:36:42.916 UTC emitted two immediate partial rawvideo buffer
  warnings, then raised `terminal decoder cleanup exceeded its bounded
  deadline` 32.293 seconds later, exactly at the 32.2-second pipeline boundary;
  systemd recorded `Result=exit-code` without SIGKILL. Exact PR 35 was restored
  and healthy by 15:37:28 UTC. CARLA, Drive, web, sessions, actors, tracks, and
  all intended timers remained safe. Do not redeploy PR 50 unchanged.
- PR 49's first complete five-child stop canary is rejected. Evidence is at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T145940Z-pr49-eager-proactive-cancel-canary/`
  and the verified rollback is
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T145940Z-pr49-eager-proactive-cancel/`.
  PR 49 reached exact four-camera readiness in five seconds, then captured a
  real helper with `in_use=1`, `proactive_preparations=1`, five FFmpeg children,
  zero cleanup failures, and every camera fresh, inference-fresh, trusted, and
  `exact_same_session_pts`. Stop at 15:00:02 UTC still exhausted the 21-second
  pipeline deadline and raised `terminal decoder cleanup exceeded its bounded
  deadline`; systemd recorded `Result=exit-code`. Exact PR 35 was restored by
  15:00:37 UTC. The scheduled 08:00 PDT hourly CARLA/Drive restart then ran
  after timers were restored and completed normally; web did not restart. Do
  not redeploy PR 49 unchanged.
- The failure was a deadline-envelope defect across two real five-child states.
  An unclaimed helper may still spend its native-open timeout before decoder
  cleanup, while a claimed helper previously serialized teardown of the old
  and replacement captures. PR 49 budgeted only OpenCV open plus read plus one
  second and omitted explicit FFmpeg TERM/KILL, PTS-sidecar, mediator, watcher,
  HTTP-server, and fragment-executor shutdown reserves.
- PR 50 releases the old and claimed replacement
  captures concurrently only when shutdown intersects proactive handover.
  Normal handover remains topology-silent and retains the replacement decoder
  lease until the old capture is dead. Shutdown promotes the already-running
  reader-owned old cleanup into the process-wide tracked cleanup gate without
  calling release twice; replacement cleanup releases its lease in `finally`.
  The explicit finite-wait reserve is 20.2 seconds, the default pipeline budget
  is 32.2 seconds, and the HTTP/executor/margin reserve is seven seconds, for a
  source-accounted 39.2-second service envelope below the unchanged 45-second
  canary gate and systemd's 60-second timeout. Invalid open/read settings that
  would cross the boundary fail closed. The real-process claimed-handover test
  proves concurrent old/replacement teardown, lease retention, tracked failure
  visibility beyond the reader deadline, zero final topology, and two reaped
  child PIDs; it passed 20/20. All 237 perception tests pass with warnings as
  errors, compilation/diff checks are clean, and independent source review has
  no remaining source blocker. PR 50 is merged but rejected by the newer live
  evidence above and remains undeployed.
- PR 48 merged the source-clear bounded cleanup described below. Its first
  upload-disabled window at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T143815Z-pr48-bounded-cleanup-canary/`
  passed all 24 exact-clock health samples and three changing four-feed rounds,
  but was correctly rejected because the unchanged 700-published-frame floor
  was impossible inside the original 120-second wall: ch1-ch4 advanced only
  212/219/222/217 published frames while inference advanced 559/528/511/513.
  Do not lower 700; extend the soak and explicitly validate the natural
  240-second helper transition.
- The second PR 48 window at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T144604Z-pr48-bounded-cleanup-canary/`
  accelerated proactive renewal to 30 seconds to reach the shutdown race
  deterministically. It captured all-four exact health with
  `in_use=1`, `proactive_preparations=1`, zero cleanup failures, four FFmpeg,
  and 4,081 MiB free. The first harness incorrectly required the fifth FFmpeg
  immediately and invoked rollback. That rollback became decisive new source
  evidence: PR 48 avoided SIGKILL but stopped after 21 seconds with
  `terminal decoder cleanup exceeded its bounded deadline`, so systemd recorded
  `Result=exit-code`. Exact PR 35 and all timers were restored by 14:47:05 UTC;
  CARLA, Drive, and web fingerprints did not change. Do not redeploy PR 48
  unchanged.
- PR 49 branch `codex/v2x-eager-proactive-cancel` starts every
  registered proactive cleanup immediately when pipeline shutdown begins,
  before joining any active reader. Preparation captures use a separate discard
  event, so this preserves the full shared deadline instead of waiting up to an
  active read timeout before cancellation. The existing exact incident
  subprocess already proves immediate global cancellation plus a claimed
  handover reaches zero helpers, leases, cleanups, failures, and real child PIDs.
  The new ordering regression proves helper cancellation occurs after every
  reader stop request but before any reader join; all 230 perception tests pass
  with warnings treated as errors. PR 49 is merged but rejected by the newer
  live evidence above and remains undeployed.
- PR 47's upload-disabled canary is rejected at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T133034Z-pr47-affine-discontinuity-canary/`;
  its verified rollback is
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T133034Z-pr47-affine-discontinuity/`.
  PR 47 reached all-four ready in eight seconds. Five complete soak samples
  stayed fresh, inference-fresh, trusted, and `matched`; sample six still had
  exact healthy clocks but exposed one auxiliary decoder and two proactive
  preparations. The unchanged clean-topology gate correctly stopped the
  candidate. During that stop, ch1's blocked read returned after SIGTERM and
  incorrectly entered terminal recovery; a claimed proactive handover then
  blocked while old OpenCV FIFO teardown ran before its FFmpeg writer was
  killed. Unbounded reader/cleanup joins consumed `TimeoutStopSec=60`, so
  systemd killed Python and one surviving FFmpeg. PR 35 was restored by
  13:32:36 UTC; CARLA, Drive, and web did not restart. Do not retry PR 47
  unchanged or relax the zero-helper gate.
- PR 48's merged bounded-cleanup source starts from PR 47. It
  serializes capture release, kills and reaps the FIFO writer before calling
  OpenCV release, leaves mediator/state teardown with the owner rather than the
  cancellation watcher, skips terminal recovery after shutdown is requested,
  and applies one finite reader/helper cleanup deadline inside systemd's stop
  bound. It deliberately keeps a claimed proactive decoder lease registered
  until the old writer is dead and handover adopts the replacement. Focused and
  full perception suites currently pass 152 and 229 tests, including release-
  order, stop-during-failed-read, forced claimed-handover interleaving, active-
  inference shutdown, real-child SIGTERM, and stubborn-reader cases. Twenty
  standalone real Kinesis/NVDEC sessions returned 400 matched frames, reaped
  every child, and returned descriptors/threads exactly to baseline.
  It is merged but rejected by the newer canary evidence above and is not
  deployed. Fable still fails authentication before file access; never claim a
  Fable pass.
- The next live gate is a fresh zero-session perception-only rollback window.
  In addition to unchanged exact-clock, freshness, inference, feed, GPU, and
  fingerprint gates, deliberately stop once while a proactive helper is active
  and require systemd `Result=success`, no SIGKILL/timeout, zero remaining
  FFmpeg/helper/lease/cleanup state, and a clean exact restart before forced
  reader recovery or uploads. Any failure restores exact PR 35 immediately.
- PR 46's first candidate window is rejected before readiness. Evidence is at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T123958Z-pr46-premux-pts-canary/`
  and rollback is
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T123958Z-pr46-premux-pts/`.
  The pre-mux stats parser did not fail; instead all four captures exposed the
  finite reason `discontinuity`. The marker persisted across replacement
  sessions, so the candidate was stopped early and exact PR 35 was restored by
  12:42:28 UTC with every timer active. CARLA, Drive, and web did not restart.
- The PR 46 discontinuity rejection is a false positive proven from retained
  Kinesis media around the canary. ON_DEMAND playlists marked essentially every
  adjacent fragment discontinuous, but for six consecutive fragments on each
  of ch1-ch4, measured `PDT - first packet PTS` had exactly 0.000 ms drift from
  the frozen affine origin. The current source-only branch is
  `codex/v2x-affine-discontinuity` on PR 46 main. It preserves the advisory tag
  for FFmpeg but trusts only the already strict raw-fragment affine validation
  plus exact emitted pre-mux PTS lookup; a true reset still becomes
  `fragment_clock_rejected`. An archived marked-session decode returned strict
  increasing exact PTS with no diagnostics on all four cameras (180 frames on
  ch1-ch3, 155 on ch4); ch4 safely removed 25 replay frames. Full perception
  remains 218 passing tests plus syntax/diff checks. Twenty real live-session
  open/read/close cycles all stayed matched, returned the process to exactly
  four file descriptors with zero new threads, killed every child, exposed no
  remote/signed source in argv, and left production at four FFmpeg readers with
  4,007 MiB GPU free. This follow-up is not
  merged or deployed and requires independent re-review and the unchanged
  rollback-gated canary.
- PR 45 fixed the valid KVS preroll rejection and added finite transport
  diagnostics, but its first real upload-disabled canary is also rejected.
  Evidence is at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T110704Z-pr45-preroll-affine-canary/`
  and rollback is
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T110704Z-pr45-preroll-affine/`.
  All four channels exposed `sidecar_pts_nonmonotonic` within the first live
  fragment roll, so the canary was intentionally stopped early rather than
  waiting 150 seconds. Exact PR 35, its upload environment, and all three
  timers were restored and verified by 11:08:24 UTC. CARLA, Drive, and web did
  not restart; Richmond returned LIVE with zero sessions/actors/tracks and four
  healthy cameras. Do not redeploy PR 45 unchanged.
- The second root cause is now exact. FFmpeg 6.1.1 clamps backward preroll PTS
  at the framecrc/NUT mux boundary: a real ch2 run repeated `16.006` seconds,
  while a pre-mux `showinfo` probe proved the decoded PTS moved from `6.273`
  back to `5.494` seconds. PR 46's merged source replaces the split/2x2/framecrc
  branch with FFmpeg's dedicated numeric-only `-stats_enc_pre` pipe, strictly
  validates output/input frame identity and rational PTS, permits only the
  already affine-validated backward preroll, and drops replay frames until PTS
  is newer before perception sees them. Stderr remains discarded and the
  signed source remains out of argv/disk. A real standalone ch2 capture returned
  700 strict-increasing trusted frames, safely dropped 25 overlap frames, kept
  every diagnostic matched, and retained at least 3,358 MiB free GPU. The
  focused suite passed 119 tests and full perception passed 218 plus syntax and
  diff checks. The newer rejected PR 46 canary and affine-discontinuity
  follow-up above supersede that source-only verdict.
- PR 44's first real zero-overlap NVDEC canary is rejected. Evidence is at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T103200Z-pr44-same-session-pts-canary/`
  and the verified rollback is
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T103200Z-pr44-same-session-pts/`.
  All four channels briefly produced `exact_same_session_pts`, proving the
  host split-output/framecrc/NUT path can establish a real initial mapping.
  That evidence then disappeared at runtime: ch1/ch2/ch4 froze on their last
  trusted frames for 134-141 seconds, while ch3 remained fresh but untrusted.
  The bounded topology reached six FFmpeg children, two admitted auxiliaries,
  and three registered proactive preparations; no terminal recovery ran. The
  unchanged 150-second all-four/clean-topology gate failed before feed, forced-
  reader, upload, or persistence work. The harness automatically restored
  exact PR 35 in 19 seconds with four healthy readers and every timer active.
  CARLA, Drive, and web did not restart inside the candidate window. Do not
  redeploy PR 44 unchanged or call the initial frame matches a release pass.
- The PR 44 runtime failure is now reproduced against current read-only KVS
  transport data. KVS periodically emits a valid 0.78-0.80 second adjacent-
  fragment keyframe/preroll overlap. PR 44 rejects
  `later.first_pts <= earlier.last_pts`, then permanently clears the transport
  clock. Across observed ch1/ch2/ch4 overlap pairs, PDT delta matched first-PTS
  delta; a fresh ch3 pair had 780 ms interval overlap, no duplicate packet PTS,
  and only 0.000238 ms affine-origin difference. A PR 44-equivalent four-
  fragment decode emitted a strictly increasing PTS subset and every emitted
  PTS existed in the probed union. The six-FFmpeg/two-auxiliary topology is the
  downstream replacement response, not the primary fault.
- PR 45's merged source accepts preroll overlap only when every fragment agrees with one
  frozen session-wide PTS/PDT affine origin within max(1 ms, packet ticks),
  while still rejecting backward PTS/PDT, duplicate sequence, changed fragment
  metadata, accumulated drift, and conflicting duplicate-PTS UTC. It also adds
  a finite allowlisted per-camera transport diagnostic and linearizes mediator
  failure state with clock classification; health/logs cannot contain a URL,
  raw exception, packet, credential, or numeric fragment detail. The focused
  diagnostic suite passed 116 tests and the full perception suite passed 215,
  plus syntax/diff checks. The newer rejected PR 45 canary and pre-mux follow-up
  above supersede that source-only verdict. Do not widen the 150-second,
  freshness, topology, or GPU thresholds.
- PR 43's first zero-overlap, upload-disabled startup at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T062453Z-pr43-evidence-controlled-startup/`
  reached all-four trusted clocks and clean topology in 35 seconds: ch1 used
  `anchor_match_frame_count=3`, while ch2-ch4 used a one-frame exact match.
  Two strict feed rounds and a two-second clean stop passed. A required clean
  restart then failed the unchanged 150-second gate: ch1 remained untrusted
  until after the deadline while ch2/ch3 used three-frame matches and ch4 used
  one frame. No forced reader was killed, uploads were never enabled, and the
  exact rollback bundle
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T062453Z-pr43-evidence/`
  restored PR 35, its unit/environment, and every timer. CARLA, Drive, and web
  fingerprints/restart counts were unchanged. Treat this as a rejected release
  gate that proves decoded-pixel uniqueness is not deterministic for an empty
  or visually static road; do not retry PR 43 for luck and do not widen the
  150-second threshold.
- PR 44's merged implementation mediates the exact capture HLS session through a capability-
  scoped loopback server, observes the exact init/fragment bytes actually
  served to FFmpeg, and pairs every OpenCV frame with the same FFmpeg graph's
  source PTS from a bounded `framecrc` sidecar. That sidecar is now proven
  mux-normalized during real preroll and is superseded by the pre-mux follow-up
  above. Exact UTC is
  `fragment PDT + source PTS - first fragment video PTS`; evidence is named
  `exact_same_session_pts` and never carries `anchor_match_frame_count`.
  Signed upstream URLs/config enter only memory or inherited memfds, never
  argv, stderr, disk, health, or persisted records. The mediated FFmpeg child
  protocol set is only `file,http,tcp`; any master URI-bearing tag is rejected.
- The transport path is additive and fail-closed. Unknown URI tags, redirects,
  discontinuities, non-video companion tracks, duplicate/colliding PTS,
  malformed or reordered sidecar records, bounded-fetch/probe failures, and
  PTS/provenance inconsistencies disable and clear transport evidence. The
  exact already-fetched media bytes still reach FFmpeg so the existing exact
  pixel/sequence resolver remains available. A missing current sidecar record
  immediately drops the prior transport clock and starts fallback rather than
  carrying stale evidence. Production network fetches run in a killable helper
  with signed input through memfd, bounded stdout, no redirects, and a hard
  deadline; cleanup scrubs retained state even on failure.
- Read-only ch1 transport observation across three consecutive fragments found
  non-overlapping presentation ranges `0.000-1.967`, `5.536-7.501`, and
  `7.707-9.678` seconds. Their advances matched each playlist PDT, supporting
  the same-session piecewise mapping without pixel change. This is transport
  evidence only, not a four-camera decoder canary or deployment pass. The
  source gate passed 211 perception tests plus syntax/diff checks; an
  independent adversarial review correctly blocked the first draft and cleared
  the merged source. The rejected live transition above is newer, decisive
  evidence and supersedes that source-only verdict. Fable remains unavailable because Claude CLI OAuth
  expires before file access; never claim a Fable pass.
- The next live gate remains perception-only and rollback-first: require exact
  source/rollback fingerprints, Richmond LIVE, zero Drive sessions/actors/
  tracks, exactly four baseline FFmpeg readers, at least 3 GiB GPU headroom,
  unchanged CARLA/Drive/web fingerprints, uploads disabled, all four cameras
  trusted via `exact_same_session_pts` well inside 150 seconds, clean topology,
  two changing feed rounds, and trust across a playlist roll. Any partial result
  or cleanup failure restores PR 35 before uploads or forced-reader testing.

- Canonical source `origin/main` is now
  `7b74103c2811a22464755db40fdaf6d18be58333`, merged PR 42; it is not the
  production deployment. The earlier controlled zero-overlap
  PR 40 replacement at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T050440Z-pr40-controlled-startup/`
  passed upload-disabled and upload-enabled four-reader readiness, strict feed
  samples, clean two-second shutdown, and clean decoder topology, but its fixed
  three-minute upload window contained no eligible road detections, so fresh
  persistence did not pass and the harness restored PR 35. A later forced
  canary at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T051530Z-pr40-forced-terminal-canary-v2/`
  never reached the reader kill because ch2 remained exact-clock unavailable
  for 150 seconds while ch1/ch3/ch4 were trusted; it also restored PR 35.
  Treat both as rejected release gates, not outages and not deployments.
- PR 42 addresses the
  intermittent initial anchor without weakening trust: the first unique exact
  frame attempt remains, but a failed attempt retries only with exactly three
  contiguous decoded frames. CPU and NVDEC fragment paths require one unique
  contiguous match, finite strictly increasing capture/fragment positions,
  at least one changing exact identity, and per-frame capture/fragment cadence
  agreement within 1 ms. Duplicate publication frames remain in clock evidence
  so `A,B,B,C` cannot be relabeled as `A,B,C`. A sequence anchor publishes only
  secret-free `anchor_match_frame_count=3` and terminal evidence
  `exact_fragment_sequence`; signed URLs remain connection-local.
- The same candidate quarantines any active exact-clock resolver superseded by
  proactive or terminal handover. Terminal recovery waits for that resolver
  inside its unchanged deadline or fails at `active_clock_cleanup`; reconnect
  and shutdown wait for every tracked cleanup, including cancellation-insensitive
  transports. A successful retry promotes only its paired clock source, while
  failed/stale clock URLs are dropped. Adversarial duplicate/cadence, active and
  proactive resolver-lifetime, source-promotion, static/invalid sequence, and
  marker-persistence cases pass. Its source gate is 182 perception, 241
  Python-3.10 bridge, 23 recovery-infrastructure, and 132 web tests, zero Svelte
  diagnostics, and a successful web production build. Independent adversarial
  review found no remaining hard blocker and directly confirmed successful
  clock-source promotion plus zero final cleanup/admission counters.
- The first exact PR 42 zero-overlap, upload-disabled startup is rejected. ch1
  and ch2 anchored first, ch3 recovered next, and ch4 became trusted only at
  about 158 seconds, beyond the unchanged 150-second startup gate. Uploads
  stayed disabled, no forced reader was attempted, the candidate stopped
  cleanly in two seconds, and the rollback restored exact PR 35 source, unit,
  environment, and all three timers. CARLA, Drive, and web PIDs/restart counts
  were unchanged; Richmond remained LIVE with zero sessions, actors, tracks,
  or objects and about 4.1 GiB GPU memory free. Evidence and rollback are at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T060850Z-pr42-sequence-controlled-startup/`
  and
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T060850Z-pr42-sequence/`.
- Current unreleased follow-up branch
  `codex/v2x-perception-sequence-evidence` fixes the discovered terminal
  telemetry contract before any forced canary: `exact_fragment_sequence` and
  every bounded internal failure/deadline stage are accepted while arbitrary
  values remain rejected. `/health` now exposes per-camera, secret-free
  `anchor_match_frame_count` as only `1`, `3`, or null, so the next startup can
  prove which anchor path succeeded even with no detections. The local suite is
  184 perception tests; independent review found no hard blocker, exhaustively
  accepted only the finite internal stage/deadline set, rejected adversarial
  URL/token/newline/nested/non-string values, and confirmed no injected secret
  reaches health. Require canonical merge, another
  zero-overlap upload-disabled all-four-clock startup within 150 seconds, clean
  stop, forced-reader recovery, upload-enabled fresh persistence when traffic
  exists, and exact rollback evidence before promotion. Fable still fails
  authentication before file access; never claim a Fable pass.

- Live production remains the verified PR 35 rollback
  `76e561cd41d070a6402c39c98847e646bd81cc9a`. At 03:23 UTC every production
  perception reader had been stuck in `reconnecting` since about 01:34 UTC
  while five FFmpeg children remained alive. A zero-session, perception-only
  restart restored all four trusted feeds and the four-feed verifier at 03:25
  UTC, but PR 35 required its full 60-second stop timeout and SIGKILL cleanup.
  Treat this as a retained terminal-reader and shutdown failure, not a release
  pass. Do not describe PR 37 or any later recovery candidate as deployed.
- Production CARLA had a real Vulkan out-of-memory crash at 03:15:39 UTC and
  systemd recovered it at 03:15:50 UTC; the current CARLA restart counter is
  therefore one. The crash coincided with repeated isolated NVDEC canaries, but
  causality is not proven. Concurrent four-camera GPU canaries are prohibited
  until a separate GPU-budget proof exists, regardless of sampled headroom.
  The only allowed next gate is an in-place, zero-overlap perception replacement
  with a pre/post CARLA fingerprint; abort on any additional restart,
  Vulkan/OOM signature, or less than 3 GiB free.
- PR 39 merged as canonical
  `56199fedae0ffe8ad832f5381840fc26e7b3c495`. Its zero-overlap controlled
  startup passed upload-disabled and upload-enabled readiness, two four-feed
  rounds in each mode, fresh trusted schema-v2 persistence, Richmond/LIVE/zero
  sessions, and a deliberate clean perception stop in two seconds. The first
  post-start pre-hour safety monitor later exited its decoder/headroom gate
  before producing a pass artifact. The exact subcondition was not durably
  retained,
  so do not overstate it as a measured outage; the topology could still own four
  active readers, one proactive replacement reader, and two exact fragment
  decoders. PR 39 was immediately rolled back to PR 35 with four trusted feeds
  and all timers restored. Evidence and rollback are at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T034307Z-pr39-controlled-startup/`
  and
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T034307Z-pr39-terminal-recovery/`.
  Do not redeploy `56199fe` unchanged.
- PR 40 is merged as canonical source release
  `c29f4f4f5533d07fd0621d9f20458fbd96fc0c12` (implementation commit
  `b788faa4e0ae0faa5d468596e79c398790a28803`). It is not deployed; production
  remains the verified PR 35 rollback. The release retains signed capture and
  clock URLs only inside one reader, exposes secret-safe stage/evidence plus
  process-wide decoder-topology telemetry, and never accepts a prior decoder
  cursor or prior clock as a new anchor. A same-session restart must match a
  unique contiguous sequence of three distinct exact full-frame identities
  with decoder-time delta agreement within 1 ms, or obtain one unique exact
  match from the bounded HLS fragment window. Missing, duplicate, cancelled,
  late, or validator-rejected evidence fails closed.
- All proactive captures and normal/urgent fragment decoders now share one
  priority-aware two-permit auxiliary budget. A proactive capture holds its
  permit through old-reader teardown and handover; terminal recovery closes the
  failed active reader first, blocks new proactive registration, cancels the
  current global proactive set, and holds urgent priority across its complete
  fragment batch. At most four active readers plus two auxiliary decoders may
  exist. A slow old capture, candidate, nested clock resolver, or FFmpeg child
  is asynchronously quarantined and remains counted; the same reader cannot
  reopen until its non-admitted decoder is confirmed dead. Failover outcome is
  reported at the fixed deadline rather than waiting for slow cleanup.
- FFmpeg teardown no longer drops a live child handle or reports success after
  TERM and KILL both time out. It retains the process and temporary resources,
  records a cleanup failure, and fails closed; OpenCV teardown errors do not
  skip child termination. Pipeline shutdown unbounded-joins any reader that
  outlives the soft stop deadline and waits for every tracked cleanup, leaving
  systemd's service timeout as the final cgroup kill boundary rather than
  allowing an orphaned decoder. No freshness, clock, inference, duplicate-
  frame, or zero-reconnect threshold is weakened.
- The last four-camera NVDEC recovery proof ran on functional failover commit
  `791676c`: two sequential forced-reader cycles recovered ch1-ch4 eight times
  with zero not-ready samples. Six recoveries used the exact three-frame
  sequence and two used exact fragment matching; durations were
  1.466-7.635 seconds, maximum decode latency was 4480.641 ms, capture and
  inference advanced, and final readiness/media-clock readiness were true.
  That transient service did not stop cleanly because it predates the final
  cooperative shutdown changes, so it is recovery evidence only. Candidate
  `c29f4f4` passes 173 perception tests; its inherited base passes 241
  Python-3.10 bridge, 23 recovery-infrastructure, and 132 web tests plus zero
  Svelte diagnostics and the web production build. The strict deadline,
  cancellation-insensitive resolver, claimed-handover, urgent-batch,
  four-reader/six-decoder, same-reader quarantine, shutdown, and stubborn-child
  adversarial cases all pass; an independent final-code review found no
  remaining hard blocker. Fable review is still unavailable because Claude CLI
  authentication expires before file access, so do not claim a Fable pass. A
  one-camera runtime attempt correctly exited because the production pipeline
  requires one detector per configured source; do not weaken that invariant or
  count the attempt as canary proof.
- Candidate `49ac21b` and its PR49 single-frame ring proof are superseded and
  rejected for release: one exact frame can be ambiguous in static imagery and
  the prior-clock cursor shortcut was not independent re-anchor evidence. Keep
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T023600Z-pr49-exact-ring-terminal-canary/`
  as historical evidence only. Before live replacement, require independent
  final-code review, full regression, a fresh rollback bundle, zero Drive
  sessions, bounded timer hold, and a GPU-safe replacement rather than a
  concurrent four-camera canary. Then require upload-disabled and enabled
  startup, clean shutdown rehearsal, ten-minute and 30-minute watches, a
  natural hourly restart, and a fresh 24-hour monitor. Any failure restores PR
  35 perception only.
- Historical experiments `f76d493`, `d0802cc`, `b515e4b`, `7127779`,
  `ec1cd2a`, `2e27521`, `4162e96`, `75c1cda`, and `0c73261` are rejected or
  intermediate evidence, not deployment targets. Their forced canaries exposed
  respectively late capture-open or clock-resolution paths, and some rejected
  transient units required a process-group timeout during cleanup. Never cite
  those partial successes as release proof.

- PR 37 merged as canonical
  `80db3de34870379ddaa6984497607726a563a17d`. Its terminal-FIFO
  hot failover preserves the last trusted frame for at most five seconds while
  one fresh signed session decodes and obtains an exact trusted media clock;
  otherwise the unchanged reconnect/staleness path fails closed. Fast failures
  cannot spin session minting, and discarded preparations cannot retain hidden
  FFmpeg captures. The 15-second freshness, ten-second capture/inference
  progress, -1/+10-second clock, duplicate-frame, and zero-reconnect gates are
  unchanged. Verification passed 139 perception, 241 Python-3.10 bridge, and 23
  generated read-API tests.
- Controlled upload-disabled and upload-enabled startup passed twice on that
  exact target, most recently at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T002100Z-pr37-canonical-startup/`,
  using verified rollback bundle
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260713T002100Z-pr37-hot-failover`.
  Its independent ten-minute gate passed 600/600 strict samples and 10/10 feed
  rounds at
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T233414Z-pr37-live-watch-10m/`.
  The unattended 30-minute product portion passed 1,800/1,800 strict samples and
  30/30 feed rounds, including the prior ch3 minute-24 failure point, at
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T234651Z-pr37-live-watch-30m/`.
  Its post-hold harness probed phase 4 about one second before the Drive
  WebSocket finished opening, conservatively rolled perception back, and must
  not be called a complete release-gate pass. The same canonical fingerprint
  was then redeployed through the fresh startup above. A supervised 24-hour
  service `v2x-pr37-24h-monitor.service` is active with minute heartbeats,
  five-minute feed/phase-4 checks, explicit hourly Richmond recovery, and
  perception-only rollback at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T002445Z-pr37-live-monitor-24h/`.
  Do not call PR 37 accepted until that monitor reaches its terminal pass.
  That first watch stopped at the natural 01:00 UTC hourly restart because its
  harness treated systemd's normal `activating` state as inactive. Perception
  remained healthy, the hourly restart completed Richmond/LIVE/zero-session
  recovery, and no rollback occurred; this is rejected orchestration evidence.
  After that harness check was corrected, a fresh watch at
  `/home/path/V2XCarla/v2x-evidence/perception/20260713T010831Z-pr37-live-monitor-24h/`
  found a real ch1 terminal `frame read failed` at sample 353. PR 37 did not
  produce a trusted replacement inside its five-second bound, entered the
  unchanged reconnect path, and the monitor automatically restored verified
  PR 35 `76e561cd41d070a6402c39c98847e646bd81cc9a`. CARLA, Drive, web,
  timers, and restart counters were preserved; the 01:14 UTC live state is
  therefore PR 35, not PR 37. Do not restart the PR 37 acceptance clock.
- Candidate `f76d493ebc2d38e8c4c0f70cac091f9c8024a377` increases only the
  terminal replacement preparation bound from five to eight seconds, still
  below the unchanged ten-second inference and 15-second freshness gates, and
  exposes per-camera terminal-failover attempts, successes, failures, outcome,
  and duration in `/health`. Missing or late trusted media clocks still fall
  through to reconnect/staleness; no clock, freshness, decode-latency, or
  zero-reconnect threshold is weakened. Verification passes 140 perception,
  241 Python-3.10 bridge, and 23 recovery-infrastructure tests. Require an
  upload-disabled isolated canary with forced terminal reader loss before any
  controlled live deployment, followed by the full startup, ten-minute,
  30-minute, natural-hourly, and fresh 24-hour gates.
- PR 36 merged as canonical
  `edaae29e9c00b411137ba40b0fd546f4b7d3c33d`. It contains the
  fail-closed vehicle identity behavior described below. Controlled startup
  passed both upload modes, its ten-minute watch passed 600/600 strict samples
  plus ten feed rounds, and its attended watch passed 1,440 strict samples plus
  25 feed rounds before ch3 had one terminal raw-reader failure and entered
  reconnecting. The unchanged zero-reconnect gate correctly rejected the
  release and automatically restored proven PR 35
  `76e561cd41d070a6402c39c98847e646bd81cc9a`; retain evidence at
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T225200Z-pr36-live-watch-30m/`
  and rollback bundle
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T223703Z-pr36-identity`.
  Restoring the persistent hourly timer immediately replayed its missed restart;
  that restart completed successfully with Richmond, LIVE, four twin cameras,
  zero sessions, perception, and all three safety timers healthy. Do not call
  PR 36 deployed or accepted, and do not attribute the reader failure to the
  identity-only change without stronger evidence.
- PR 35 merged as canonical
  `76e561cd41d070a6402c39c98847e646bd81cc9a`. Controlled startup
  passed both upload modes. Its new ten-minute watch passed 600/600 strict
  samples and ten feed rounds. Its attended 30-minute watch passed 1,800/1,800
  strict samples, 30/30 feed rounds, complete decoder turnover, zero
  reconnects, inference-age maxima 7.226–8.767 seconds, decode-latency maxima
  4.839–6.108 seconds, Richmond/LIVE/zero sessions, unchanged service
  fingerprints, and timer restoration. The bounded hourly hold replayed the
  missed restart after the gate; the restart completed successfully and all
  four twin counters advanced. Evidence is
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T215500Z-pr35-live-watch-30m/`.
  This closes the attended perception/HLS gate only; the 24-hour monitor and
  vehicle identity/calibration gates remain open.
- Fresh object `global_car_b0678022_4` produced nine strict schema-v2 rows on
  ch1, ch3, and ch2. Exact historical frame checks passed ch1 at 9 ms / YOLO
  IoU 0.939 and ch3 at 16 ms / IoU 0.798. Three later ch2 local-track-53 rows
  passed at 1–16 ms with IoU 0.986/0.867/0.888. Ch2 local-track-49 is rejected:
  one row had zero bbox overlap at 13 ms and one row fell outside returned HLS
  coverage. Evidence is
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T223000Z-object-b0678022-4-exact/`.
  Do not use the shared global ID as same-car proof.
- The root identity defect is the legacy vehicle slow path: vehicles have no
  appearance embedding, yet any new local track could inherit a global vehicle
  ID within 30 m for 40 seconds, including a different track on the same
  camera. Candidate `745bae0ec04c868573d1f853dbfb4c9539d62c18`
  rejects different same-camera track IDs and disables uncalibrated
  cross-camera vehicle deduplication/tracking by default. It intentionally
  splits uncertain identities until a held-out vehicle-ReID/geometric linker
  exists. Generic MobileNet similarities on the exact views were only
  0.24–0.53 for accepted same-car proposals, so no arbitrary appearance
  threshold was deployed. All 136 perception tests pass. Require isolated and
  live gates before deployment; this is not same-car acceptance.

- PR 34 merged as canonical
  `b64f1f81e8d455c197cb5ac09a42ce4ec2a2b432`. After an unrelated
  San Ramon/replay preflight block and the normal hourly Richmond reset, its
  controlled upload-disabled and upload-enabled startup passed five strict
  samples plus five complete feed checks in each mode. The uninterrupted
  ten-minute watch then passed 600/600 strict samples, ten feed rounds, full
  decoder turnover, fresh schema-v2 persistence, Richmond/LIVE/zero sessions,
  and unchanged service fingerprints. Evidence is
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T210400Z-pr34-live-watch-10m/`.
- The attended 30-minute watch passed 1,310 strict samples and 22 feed rounds,
  then feed round 23 rejected ch2 because its raw frame counter did not advance
  inside the five-second deadline. The retained one-second trace shows normal
  updates followed by one frame held for 4.943 seconds; media remained trusted,
  decode latency was 984.688 ms, inference age was 4.783 seconds, and reconnect
  count stayed zero. The verifier's inherited rollback trap stopped perception,
  so the concurrent sampler's connection refusal was a consequence, not an
  independent crash. Automatic rollback restored `d54f5df`, the old
  unit/environment, perception, and all timers. Retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T211600Z-pr34-live-watch-30m/`.
  Do not deploy PR 34 unchanged.
- Candidate `6ab99a8f7fb6d7028c225c2abef656cb8997f0f3` sets raw progress
  to the same explicit ten-second deadline as inference. This remains stricter
  than the unchanged 15-second freshness gate and matches the existing
  +10-second trusted media-clock budget; timestamp/counter regression still
  fails immediately. Runtime service code is unchanged from the passing
  canary. All 133 perception tests pass. Require canonical merge and repeat the
  full controlled startup, uninterrupted ten-minute, and attended 30-minute
  gates before starting the automated 24-hour watch.

- PR 33 merged as canonical
  `001bc6a0401752b0cae1e9cebc5bd03c83c670ec`. Its controlled
  upload-disabled startup passed inference-aware readiness, five strict
  samples, five complete feed checks, and LIVE/zero sessions. After uploads
  were enabled, readiness passed but the first fixed three-second feed pair
  caught ch1 on the same completed-fragment boundary. The unchanged freshness,
  trust, latency, and inference gates were still healthy. Automatic rollback
  restored `d54f5df`, the old unit/environment, perception, and all timers;
  retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T204500Z-pr33-canonical-startup/`.
  Do not redeploy PR 33 with a fixed raw-capture sleep.
- Candidate `eaba2d9ef78aead6a80627f0875924caddc049b4` also treats raw
  progress as an explicit deadline. It keeps the initial three-second sample,
  then polls the raw frame counter for at most five seconds—slightly more than
  two measured 2.002-second fragments—while polling inference for at most ten.
  It still rejects regression immediately and preserves the 15-second
  timestamp, trusted-clock, and -1,000/+10,000 ms latency gates. All 133
  perception tests pass. Require canonical merge and repeat the full controlled
  startup/watch sequence; prior runtime canary evidence covers the unchanged
  service code but does not replace a live verifier gate for this commit.

- PR 32 merged as canonical
  `21554f18f523fdc577c8524623534a60b0ebf500`. A controlled live startup at
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T200300Z-pr32-cadence-aware-startup/`
  passed uploads disabled and enabled, five strict health samples plus five
  four-feed checks in each mode, LIVE/zero sessions, unchanged CARLA/Drive/web
  fingerprints, and timer restoration. A repeated startup at
  `20260712T200830Z-pr32-cadence-aware-startup/` also passed. Two subsequent
  ten-minute harnesses were invalidated by harness-only PID/schema errors and
  correctly rolled back; retain them as rejected orchestration evidence, not
  product failures or partial acceptance.
- A fresh third startup then found a real, intermittent semantic mismatch in
  the fourth upload-disabled feed round. Raw frames and strict media clocks
  remained healthy, but ch1's `/detections/latest.updated_at` did not advance
  across one three-second pair. The first three rounds already showed normal
  per-camera inference-summary gaps up to 4.421 seconds, and the retained
  corpus contains a 7.516-second gap. Four fixed-order camera jobs share two
  workers and the next batch waits at the global barrier, so a three-second
  paired sample is not an inference liveness deadline. The automatic rollback
  restored `d54f5df`, the old unit/environment, perception, and all timers.
  Evidence is
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T202300Z-pr32-final-startup/`;
  rollback bundle
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T195908Z-pr32-cadence-retry/`
  verifies cleanly. Do not redeploy PR 32 unchanged.
- Candidate `40673cbcc890c628f07e422aa06c943933bb34cc` makes inference
  liveness explicit rather than relaxing the existing gates. `/health` now
  exposes a monotonic per-camera inference count, source timestamp, completion
  age, and freshness; overall readiness fails closed when any inference result
  is older than ten seconds. The feed verifier still requires raw capture to
  advance across three seconds, two distinct JPEGs, timestamp ages at most 15
  seconds, trusted matched media clocks, and decode latency in the unchanged
  -1,000/+10,000 ms range. It polls each inference counter for at most ten
  seconds, rejects regression immediately, and fails if any counter does not
  advance. All 131 perception tests pass. A production-like upload-disabled
  canary then passed 120/120 strict one-second samples, ten inference-aware
  four-feed checks, repeated accelerated 30-second decoder renewals, complete
  initial-decoder turnover, and cleanup back to the baseline service. Evidence
  is
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T203800Z-inference-health-canary/`.
  Require canonical merge, a fresh rollback bundle, both live startup modes,
  the uninterrupted 600-sample watch with decoder turnover, then attended
  30-minute and automated 24-hour gates before acceptance.

Observed through 2026-07-12 19:38 UTC; verify rather than assume. This section
overrides older perception candidate and deployment statements below.

- PR 31 merged as canonical
  `d17ed1bf690e9874d72813c73a87e04a7751be8d`. Its controlled
  upload-disabled startup and five strict samples passed, but the first exact
  feed round rejected ch2 because its detection-event timestamp did not
  advance. The inherited ERR trap automatically restored verified bundle
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T192801Z/`,
  `d54f5df`, the prior unit/environment, perception, and all timers. This proves
  raw-reader publication alone is insufficient; do not redeploy PR 31
  unchanged.
- Candidate `9a1b66bb1c5db80f2951aa5e1aebcedae88dbf44` runs the four
  camera-local YOLO model/tracker calls through a persistent two-worker
  executor. Each camera already owns an independent model/tracker; embeddings,
  cross-camera deduplication, uploads, and event publication remain ordered
  after inference. The systemd unit explicitly fixes the worker count at two.
  A barrier regression fails if the calls become sequential. All 98 perception
  tests pass. An upload-disabled canary passed 30/30 exact capture-and-event
  feed verifiers plus 120/120 strict one-second samples across accelerated
  renewals, with zero reconnects/errors/stale samples, latency maxima
  ch1/ch2/ch3/ch4 = 9.237/9.257/7.603/9.059 seconds, and publication-age
  maxima = 2.043/3.995/1.693/3.776 seconds. Cleanup passed. Evidence is
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T193315Z-parallel-inference-canary/`.
  This remains isolated evidence. Require canonical merge, a fresh verified
  rollback bundle, upload-disabled and upload-enabled startup, repeated exact
  feed verifiers, LIVE/zero sessions, and the unchanged ten-minute, 30-minute,
  and 24-hour watches before perception acceptance.

- PR 30 merged as canonical
  `ec3cd60f639e7d591607474d2302b48f73f2fcfe`. Its controlled deployment
  used verified rollback bundle
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T191545Z/`.
  Upload-disabled startup, five strict samples, three exact feed verifiers, and
  LIVE/zero-session observation passed. Upload-enabled startup and five more
  strict samples also passed, followed by two exact feed verifiers; the third
  rejected ch1 because its capture timestamp did not advance across two
  seconds. The candidate was explicitly rolled back to `d54f5df`; the prior
  unit/environment and all timers are restored, while CARLA/Drive/web remain
  unchanged. Do not redeploy PR 30 unchanged.
- The remaining feed failure was an architecture seam: MJPEG publication
  waited behind four sequential YOLO calls even when the trusted reader was
  healthy. Candidate `7193bfd8ad1162a33afb7cf535ca8b56bad5f952`
  publishes a rate-limited raw physical frame directly from each accepted
  reader callback, with unchanged media-clock assessment, while detection and
  annotated/offline outputs stay separate. Callback failures cannot reconnect
  a trusted reader; health becomes stale naturally if publication actually
  stops. All 97 perception tests pass. An upload-disabled four-camera canary
  passed 30/30 consecutive exact feed verifiers and 120/120 strict one-second
  samples across 30-second renewals, with zero reconnects/errors/stale samples,
  latency maxima ch1/ch2/ch3/ch4 = 7.055/8.376/9.275/8.553 seconds, and
  publication-age maxima = 3.075/6.296/6.368/5.774 seconds. Cleanup passed.
  Evidence is
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T192226Z-raw-feed-decoupling-canary/`.
  This is not live acceptance. Require canonical merge, another fresh verified
  rollback bundle, upload-disabled then upload-enabled startup, repeated exact
  feed verifiers, LIVE/zero sessions, a ten-minute renewal watch, and the
  attended 30-minute plus automated 24-hour gates. Roll back immediately on
  any unchanged gate.

- PR 29 merged as canonical
  `9d541d2cdc2dfd3fcf86dfbb867129c94c38d12b`. Its controlled deployment
  used the verified rollback bundle
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T184900Z/`
  and started with uploads disabled. Five strict all-camera health samples
  passed, but the independent four-feed verifier rejected ch1 because its
  capture timestamp did not advance across the fixed two-sample interval. The
  deployment automatically restored `d54f5df`, the prior unit and environment,
  perception, and all timers; CARLA/Drive/web were unchanged. Do not redeploy
  PR 29 unchanged.
- A live-loop defect then proved that reader sequences were advanced before a
  global every-other-iteration inference throttle. A camera frame arriving on
  a throttled iteration was permanently discarded and repeated phase alignment
  could starve one channel. Commit `36e0f065da7ab3346cc66a507bca4518fa8c8b35`
  preserves those sequences until an inference iteration actually consumes
  them. Ten consecutive feed verifiers passed, but the stronger accelerated
  watch still failed ch3 stale at sample 47; retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T185552Z-live-throttle-sequence-canary/`.
- Reducing the exact-clock fragment-match pool from three to two capped peak
  decoder sessions at seven and improved normal cadence. That intermediate
  candidate still failed at sample 96 when ch4 reported
  `proactive capture preparation failed` and reconnected while ch2 aged to
  14.95 seconds. Retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T190257Z-nvdec-pool2-five-minute-canary/`.
- Candidate `28c223006bdca95b826a60dca8f1686f63565547` combines the sequence
  preservation, seven-session cap, immediate hot preparation on the first
  invalid trusted-clock sample, and retryable off-path preparation failure.
  A failed replacement no longer tears down a still-readable active session;
  real active-reader failures remain reconnecting/stale and the -1/+10-second
  clock plus 15-second freshness thresholds are unchanged. All 96 perception
  tests pass. The upload-disabled four-camera canary passed ten consecutive
  exact feed verifiers and 300/300 strict one-second samples across repeated
  30-second accelerated renewals, with zero reconnects/errors/stale samples,
  latency maxima ch1/ch2/ch3/ch4 = 7.989/8.324/8.023/9.662 seconds, and
  publication-age maxima = 4.935/6.474/3.072/3.602 seconds. Every owned process
  was cleaned up. Evidence is
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T190843Z-prep-retry-five-minute-canary/`.
  This remains isolated candidate evidence. Require a canonical merge, a fresh
  verified rollback bundle, controlled upload-disabled then upload-enabled
  startup gates, four changing feeds, LIVE/zero sessions, a full ten-minute
  renewal watch, then attended 30-minute and automated 24-hour watches. Roll
  back immediately on any unchanged gate.
- The normal 12:00 PDT hourly UE5/Drive restart completed successfully at
  19:01:16 UTC. It produced fresh CARLA/Drive PIDs with `NRestarts=0`, LIVE
  mode, zero sessions, and zero actors. The current live perception checkout
  remains the deliberate rolled-back `d54f5df` baseline until a newer canonical
  candidate passes its controlled deployment.

- PR 28 merged as canonical
  `0bb596b227cad420c77e12516fb4dc77a11af5e0`. Its read Lambda code-only
  deployment is backed up at
  `/home/path/V2XCarla/v2x-backend-backups/read-api-code-only/v2x-backend-read-20260712T182245Z-0bb596b2/`.
  The real API reports `DIRECT_KINESIS` plus `ON_DISCONTINUITY` for direct
  perception sessions and `SAME_ORIGIN_PROXY` plus `ALWAYS` for browser
  sessions; the browser response does not expose the signed Kinesis origin.
- The controlled PR 28 perception deployment passed five strict startup
  samples, the independent four-feed verifier, LIVE/zero-session twin
  observation, and preserved CARLA/Drive/web fingerprints. Its ten-minute
  watch then failed at sample 266: ch3's FFmpeg child stayed alive but stopped
  publishing frames, so the last trusted frame aged from 8.43 to 15.43 seconds
  and health correctly changed to `stale`. No reconnect or sanitized error was
  surfaced. Retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T183001Z-continuous-pts-live-watch-10m/`.
  The live checkout and installed perception unit were immediately restored to
  `d54f5dfaec90e791af83105ff048e5dd3c6506a2`; only perception restarted, all
  three timers were restored, and CARLA/Drive/web PIDs and restart counters
  remained unchanged. Do not redeploy PR 28 unchanged.
- Candidate `7dafc4e361f1a55f8512c5ffff283dcd2bb93b69` adds a seven-second
  FFmpeg network-I/O timeout, a three-refresh nonadvancing-HLS bound, and a
  renewable-session-only hot replacement when a previously trusted clock
  becomes persistently invalid. It does not change the fixed -1/+10-second
  clock or 15-second stale thresholds. All 94 perception tests pass. An
  upload-disabled four-camera canary in an owned systemd cgroup passed 120
  strict one-second samples with 30-second accelerated renewals, zero stale or
  reconnect samples, and maxima ch1/ch2/ch3/ch4 =
  9.134/8.614/8.771/9.999 seconds; every canary process was cleaned up.
  Evidence is
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T184344Z-stalled-reader-recovery-canary/`.
  The ch4 margin is only about 1 ms, so this is not a live pass. Require a
  canonical merge, a new verified rollback bundle, a controlled zero-session
  deployment, five strict startup samples, four changing feeds, LIVE/zero
  sessions, a full ten-minute renewal watch, then attended 30-minute and
  automated 24-hour watches. Roll back immediately on any unchanged gate.
- The current rolled-back CPU-decoder service can again show missing clock
  readiness and latency above ten seconds. This is retained baseline debt, not
  evidence that the rejected PR 28 deployment should remain live. Do not claim
  perception acceptance until a newer candidate passes every watch and a fresh
  schema-v2 persisted vehicle is proven.

## Current deployed state and integration hold

Observed through 2026-07-12 17:35 UTC; verify rather than assume. The following
newest chronology overrides older candidate/deployment statements later in this
section:

- Canonical `origin/main` is
  `e64b9abe559853bfe6b186822996d47778c655c9` after merged PRs 22-25. The public
  browser release was independently proven at merged PR 22 commit
  `d9a6ad8e7d83acad25c315b1f41e7b80cbb4f2d8`: Amplify job 203 succeeded at that
  exact commit and refreshed Playwright CLI evidence at
  `/home/path/V2XCarla/v2x-evidence/playwright/20260712T155049Z-public-release/`
  shows HTTP 200 on `/drive`, `/live`, and `/timeline`, four playing native
  2560x1920 timeline videos, four current live snapshots, and zero browser
  warnings or errors. This is browser-release evidence, not static calibration
  or same-car placement acceptance.
- PR 23 (`5513c8e890d8836c1b142fa6d7d31664c851c4e5`) separated the short live
  capture playlist from the five-fragment exact-clock playlist and restored
  proactive 240-second renewal. Its isolated canary passed, but the strict live
  watch failed immediately at 10.127 seconds on ch2 and 10.585 seconds on ch3.
  The live checkout and perception unit were rolled back without restarting
  CARLA, Drive, or web. Retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T162000Z-live-edge-renewal-watch/failure.json`.
- PR 24 (`828bd341d3b700bd9186587317a4589233e43aad`) reduced only the direct
  perception capture session to one fragment while keeping the exact-clock
  session at five. The Lambda code-only apply is backed up at
  `/home/path/V2XCarla/v2x-backend-backups/read-api-code-only/v2x-backend-read-20260712T163322Z-148b13bbdcad/`;
  its isolated four-camera canary stayed below 7.64 seconds. The subsequent
  strict live watch nevertheless failed at sample 132 when ch1 reached
  10.075919 seconds. It was rolled back exactly to
  `d54f5dfaec90e791af83105ff048e5dd3c6506a2`; all three timers were restored and
  the CARLA/Drive/web PIDs and restart counters remained unchanged. The current
  clean live checkout is therefore deliberately behind canonical main at
  `d54f5df`; do not redeploy PR 23 or PR 24 unchanged. Retain the failure at
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T164000Z-one-fragment-renewal-watch/failure.json`
  and rollback bundle at
  `/home/path/V2XCarla/v2x-backend-backups/perception-one-fragment/20260712T163710Z-828bd341d3b7/`.
- Playlist timestamps prove Kinesis is normally within roughly one completed
  two-second fragment of wall time; the remaining 10-second tail is local CPU
  decode backlog from four 2560x1920 streams plus inference. A source candidate
  uses host `/usr/bin/ffmpeg` NVDEC through an anonymous memfd master playlist
  and timestamped local NUT FIFO, keeps signed URLs out of command lines and
  disk, and uses the same NVDEC pixels for exact fragment matching. A real
  upload-disabled four-camera diagnostic ran 90 seconds with zero reconnects
  and maxima ch1/ch2/ch3/ch4 = 5.838/7.559/8.024/6.673 seconds. A second
  120-second diagnostic completed one staggered, pre-clocked handover per
  camera with zero untrusted/stale samples after initial acquisition, zero
  reconnects, and maxima 5.936/5.665/5.939/8.494 seconds. Treat this as a
  full detector rerun then passed 120 consecutive strict samples across all
  four staggered handovers with maxima 8.950/7.431/6.240/5.608 seconds and
  seven upload-disabled objects. PR 25 merged that candidate as canonical
  `e64b9abe`.
  Its controlled live deployment passed startup and the four-feed/zero-session
  gates, but the strict watch rejected one ch3 frame whose exact media mapping
  was 3.044 seconds ahead of receipt, outside the existing -1 second trust
  floor. The deployment was immediately rolled back to `d54f5df`; only
  perception restarted and CARLA/Drive/web fingerprints plus all timers were
  preserved. Do not redeploy `e64b9abe` unchanged. Retain the failure at
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T172900Z-nvdec-live-renewal-watch/failure.json`
  and rollback bundle at
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T172427Z-perception-nvdec-e64b9abe/`.
  Kinesis `ListFragments` over 30 minutes showed producer timestamps 0.65-0.76
  seconds behind AWS server time, while host chrony offset was below 0.3 ms;
  the rejected -3 second value is therefore an intermittent decoder PTS mapping
  fault, not a stable physical-camera clock offset. The next candidate must
  validate every resolved clock before publication/handover, discard and
  re-anchor an out-of-window mapping, and still pass full detector, controlled
  live, 30-minute, and 24-hour gates without weakening -1/+10 second bounds.
  PR 26 merged that validation as
  `f9a966ddc5f07411969666efda328647dbdc0e3b`. Its controlled deployment again
  passed startup/four-feed/LIVE gates, then the strict watch proved a second
  fail-closed defect: after ch1 rejected a bad PTS mapping, ordinary unclocked
  frames were still published during re-anchor, dropping global clock readiness
  at sample 5. It was rolled back exactly; retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T174500Z-clock-validated-live-watch/failure.json`
  and
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T174335Z-perception-clock-gate-f9a966dd/`.
  The next candidate freezes publication on the last trusted frame during
  re-anchor; that frame still ages normally, so a recovery longer than 15
  seconds fails stale rather than hiding an outage. Its upload-disabled full
  detector canary passed 120 strict accelerated-renewal samples with accepted
  minima 2.778/2.674/1.499/1.739 seconds and maxima
  7.028/7.771/8.221/7.654 seconds. This is not live acceptance; require a new
  canonical commit and repeat the controlled watches.
  PR 27 merged the publication freeze as
  `f2499bfd28a6cb9a9171798afdb768f4f087790c`. Its live watch kept clocks trusted
  but correctly failed when ch2 could not re-anchor before the last trusted
  frame became stale at 15.12 seconds. Retain
  `/home/path/V2XCarla/v2x-evidence/perception/20260712T175400Z-clock-hold-live-watch/failure.json`
  and
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260712T175248Z-perception-clock-hold-f2499bfd/`;
  live was again restored to `d54f5df` without CARLA/Drive/web changes.
  The root cause is the direct HLS session's `DiscontinuityMode=ALWAYS`, which
  inserts a synthetic discontinuity at every normal two-second fragment. A
  read-only 180-second ch1 session with `ON_DISCONTINUITY` stayed trusted at
  3.19-9.57 seconds. A four-camera upload-disabled full detector canary using
  direct AWS `ON_DISCONTINUITY` sessions then passed 120 strict accelerated
  renewal samples with minima 1.932/2.579/0.657/2.457 seconds and maxima
  8.865/7.905/7.025/8.690 seconds. The next release must change only direct
  perception sessions to `ON_DISCONTINUITY`, leave the already-proven browser
  proxy on `ALWAYS`, expose and verify the selected mode, and repeat API,
  detector, controlled live, 30-minute, and 24-hour gates.

- The post-hourly clean HLS watch is complete at
  `/home/path/V2XCarla/v2x-evidence/playwright/20260712T150759Z-post-hourly-clean/`.
  It ran from 15:07:59.841 through 15:38:48 UTC with 36,964 50-ms samples,
  7/7/7/6 completed staggered session handoffs for ch1-ch4, a 1.75-second
  worst availability gap, no active final outage, all four selected videos at
  2560x1920/`readyState=4`/playing with no media error, and zero console
  warnings or errors. A second final sample proved every video clock advanced.
  The sanitized resource ledger retained only HTTP 200 for ten browser-session,
  fifteen coverage, and 1,744 proxy responses, plus five status-0 proxy reads
  bounded within successful double-buffer handoffs; the latter are retained as
  retired-session abort candidates, not deleted. Direct and opaque browser
  master canaries returned 200 for all four channels without persisting signed
  or opaque URLs. This closes the attended browser-HLS seam only.
- Do not promote that HLS result to the complete perception/product gate. The
  final four-feed verifier proved two advancing trusted frames and distinct
  hashes for every camera, but ch3 decode latency was 13.94/10.84 seconds,
  exceeding the fixed 10-second limit; a later health sample had ch4 at 12.39
  seconds. The inspected timeline visibly reported `Objects DB STALE` and its
  newest persisted schema-v2 car was 14:33:40.087 UTC, while current live
  frames at 15:42 UTC were fresh but contained zero detections. This may be an
  empty-road interval rather than an upload failure, but a new eligible
  schema-v2 persistence proof is still required before end-to-end acceptance.
- Draft PR 22 now includes the tested HLS/API release and least-privilege
  observability policy at `69b1f3a`. The policy adds read-only CloudWatch
  metrics and read-only access to the exact read-Lambda log group. Its
  state-hash bootstrap refused before mutation because the only V2X-account
  source credential lacks `iam:GetUser`; do not bypass the administrator gate
  or claim those observability permissions are deployed.
- The isolated UE5.5 recovery workspace remains outside production. The
  official engine dependency fetch and official CARLA `ue5-dev` content/LFS
  materialization are in progress on `/mnt/v2x-ue5`. The exact 29-object
  Richmond road core was hash-gated into the isolated CARLA project with
  rollback evidence at
  `/mnt/v2x-ue5/evidence/april-road-core-dependencies/migration-20260712T151559Z/`;
  this is staging only, not conversion, render, cook, or calibration proof.

- Canonical `origin/main`, the clean live checkout, the Amplify mirror, and
  successful production Amplify job 202 are exact commit
  `d54f5dfaec90e791af83105ff048e5dd3c6506a2`.
- Live CARLA/Drive use the packaged UE5.5 RR/CARLA 0.10 worker. After the
  scheduled 23:08 PDT restart both services held `NRestarts=0`, all expected
  listeners remained bound, and the all-channel metadata plus local/public
  four-feed verifiers passed.
- A fresh read-only audit at 12:14–12:15 UTC found every V2X service and all
  three mutation-capable timers active with `NRestarts=0`; the expected image,
  shipping-binary, UE5 marker, Richmond OpenDRIVE, six listeners, LIVE twin
  mode, zero active sessions, and four advancing twin camera counters matched.
  The corrected local four-feed verifier passed with two advancing timestamps
  and two distinct JPEG hashes for ch1–ch4. Re-run rather than inheriting this
  result after any service, config, image, or source change.
- Replay synchronization, tick-bound scene snapshots, exact actor-observed
  default lens acceptance, and cleanup are deployed. A bounded replay for
  `global_car_4db7ffc8_2` remained crash-free and returned to LIVE with zero
  sessions, but failed the unchanged final visual gate: no compatible visual
  detection overlapped the projected UE5 actor. Treat this as a genuine
  calibration/localization failure; do not lower thresholds or rerun replay
  before a new accepted candidate exists.
- Durable replay evidence is at
  `/home/path/V2XCarla/v2x-evidence/twin-replay/20260711T0546Z-default-lens-canary/`.
  The verified rollback bundle is
  `/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-20260711T054633Z-default-lens`.
- The HLS producer proactively rotates signed sessions at 240 seconds. The
  deployed candidate passed 660 one-second samples across two rotations with
  no health outage and per-channel maximum latency below 5.75 seconds. Re-run
  that complete gate after any merged perception deployment; old evidence does
  not transfer to a new fingerprint.
- Newest browser/API state overrides older HLS chronology below. The read
  Lambda was updated at 14:28 UTC from state hash
  `5958dd663b35bc3dbaf9ffd9d64181892efa957f695b3bb69e053fc2e1cdcb6e`;
  its narrow upstream retry covers only HTTP 429/5xx and network timeouts,
  while 4xx, invalid content, redirects, bounds, and auth/config failures still
  fail immediately. The verified rollback bundle is
  `/home/path/V2XCarla/v2x-backend-backups/read-api-reconciliation/v2x-backend-read-20260712T142822Z-5958dd663b35/`.
  A real playlist 502 and a measured 6.70-second all-camera simultaneous-renew
  outage remain retained failures at
  `/home/path/V2XCarla/v2x-evidence/playwright/20260712T142114Z-seamless-failed-upstream502/`
  and
  `/home/path/V2XCarla/v2x-evidence/playwright/20260712T142909Z-post-retry-failed-simultaneous-renewal/`.
  The current web candidate double-buffers each HLS session and staggers ch1–ch4
  renewals by ten seconds. By 14:59 UTC its attended Playwright probe had five
  renewal rounds per camera, zero console errors, all session calls 200, and a
  worst measured availability gap of 1.45 seconds. Do not call the run complete
  before its 30-minute endpoint or before retaining final network/screenshot
  evidence. The candidate is isolated in commit `748d02e` on
  `codex/v2x-hls-release`, draft PR 22; 132 web tests, clean build/type checks,
  22 generated-Lambda tests, and recovery-infrastructure tests pass.
  Direct Amplify attachment to `path2v2x/v2x-backend` failed before metadata
  mutation because the organization disables deploy keys; do not weaken that
  policy. The current owner-controlled fallback
  `michaelvu1207/v2x-backend-amplify` is proven exact to canonical `main`, has
  an active push webhook and sync workflow, and Amplify jobs 193–202 succeeded.
  Repository-apply rollback metadata is
  `/home/path/V2XCarla/v2x-backend-backups/amplify-repository/d1ugco1rmb7yjj-20260712T145830Z-289164f0714ea21fb4b5a419fd11c60218dec0498eb7b487345db2a3b7530a1c.json`;
  the failed canonical update left Amplify repository metadata unchanged.
- Public `/timeline` is not currently an acceptance pass. The earlier
  Playwright evidence at
  `/home/path/V2XCarla/v2x-evidence/playwright/20260712T063328Z-current-baseline/`
  shows only CH2 visible while CH1/CH3/CH4 remain black and the header reports
  zero cameras/FPS. A first proxy canary then broke perception when the legacy
  `/video/session/{camera_id}` contract was changed in place; it was rolled back
  exactly and perception recovered without a restart. Commit `141a317` fixes
  the contract split: backend perception retains direct signed Kinesis delivery
  at `/video/session/{camera_id}`, while browsers use opaque same-origin delivery
  at `/video/browser-session/{camera_id}` and child resources remain under
  `/video/proxy/{token}/{resource_id}`. The split Lambda/routes, prefix-scoped
  IAM, one-day state expiry, and independent route throttles were deployed at
  12:40 UTC from reviewed state hash
  `8aa9f567c48dcc4c3bc708d89040e3ef25b50a320cee5b78828f5b49f67b5396`.
  Rollback evidence is
  `/home/path/V2XCarla/v2x-backend-backups/read-api-reconciliation/v2x-backend-read-20260712T124017Z-8aa9f567c48d/`.
  Real API proof passed direct ch1 plus opaque proxy master/media playlist reads
  for ch1-ch4. A local production build passed Playwright with four simultaneous
  2560x1920 videos at `readyState=4`, no media errors, all four browser-session
  calls returning 200, and 138 events / 3,806 detections in 24 hours; evidence
  is `/home/path/V2XCarla/v2x-evidence/playwright/20260712T124500Z-local-hls-split/`.
  At 12:46 UTC, more than six minutes after the API apply and therefore beyond
  the producer's 240-second renewal interval, the local four-feed verifier again
  passed with advancing timestamps and distinct JPEGs on every channel;
  `v2x-perception.service` remained active with `NRestarts=0` and current schema-v2
  uploads continued. This specifically closes the regression that forced the
  first proxy canary rollback, but it does not replace the 30-minute/24-hour
  production watch gates. The same browser watch later exposed two transient
  CH2 `/video/coverage` 502s because the page issued six ListFragments windows
  concurrently for each stream. A follow-up watch proved the connection limit
  is account-wide when overlapping ch2/ch3 calls still returned 502. The next
  source candidate serializes all coverage chunks globally and refuses
  overlapping refreshes. Also retain the deployment-compatibility fix: new HLS
  proxy settings have safe defaults and existing Lambda configuration is
  reconciled before new code. The 12:40 apply briefly produced two
  `HLS_PROXY_PREFIX` import failures while code preceded environment; do not
  call that canary a clean 30-minute pass or repeat the unsafe order.
  A subsequent local watch found Chromium 145 claiming native HLS support but
  leaving ch1 paused with `DEMUXER_ERROR_COULD_NOT_PARSE` after an opaque media
  playlist was blocked by ORB. The accepted browser candidate must prefer
  hls.js whenever Media Source Extensions are supported and reserve native HLS
  for Safari/fallback. Require all four video elements to remain playing with
  no media error across the renewed clean watch.
  The globally serialized browser candidate later exposed two isolated
  `/video/coverage` 502s at 13:31 UTC. Both responses had the exact 100-byte
  `video_coverage_unavailable` shape for
  `ClientLimitExceededException`, after 4.81/3.74-second requests. Commit
  `fda0d41` adds a narrow, jittered three-attempt retry for only transient
  Kinesis conditions; authorization/configuration errors still fail
  immediately. It was deployed at 13:40 UTC from reviewed state hash
  `dff605961d94b4b7616f752f41a74baee66451dc1f12f6c96420f1998c6f04de`.
  Rollback evidence, including the prior Lambda zip and prior IAM/API state, is
  `/home/path/V2XCarla/v2x-backend-backups/read-api-reconciliation/v2x-backend-read-20260712T134008Z-dff605961d94/`.
  Immediate post-apply canaries passed all four direct sessions, all four
  browser sessions, and all four four-hour coverage calls. Commit `b25720c`
  removes the underlying continuous-load pattern by aligning four-hour windows,
  caching complete historical chunks, re-fetching only the live-edge chunk,
  and refreshing optional coverage every five minutes. Web tests are 130/130,
  the production build is clean, and `svelte-check` reports zero errors and
  zero warnings. A fresh Playwright CLI watch started at 13:44:42 UTC; its
  initial 28 aligned historical/live-edge calls all returned 200 and all four
  2560x1920 videos played without media error. Do not call it a 30-minute pass
  until the full interval and renewal/refresh cycles complete, and still require
  the separate 24-hour watch.
  This is a candidate-browser pass, not a public-production pass: Amplify is
  still connected to `michaelvu1207/v2x-backend-amplify` at main commit
  `d54f5df`, so the public app has not yet received the browser-route change.
  Require a clean source-controlled Amplify release and repeat the public gate.
- The clean integration worktree is
  `/home/path/.codex/worktrees/v2x-calibration-integration` on
  `codex/v2x-calibration-integration`. It layers the fail-closed calibration,
  physical-intrinsics, identity, persistence, rollback, and GPS-planar-placement
  gates onto current `origin/main` while preserving the newer replay protocol.
  It is not deployed. Never deploy from the dirty recovery worktree.
- The integration candidate also contains a read-only rolling detection-corpus
  exporter, hash-bound observation/contact/tracklet curation, a shared WGS84
  OpenDRIVE projection, honest independent placement metrics, and a bounded
  detection-assisted trajectory fit. The fit is diagnostic by construction;
  it cannot authorize deployment without measured per-camera intrinsics,
  surveyed static/lane evidence, locked whole-track holdouts, bootstrap, RTK,
  and UE5 visual proof. Read
  [references/calibration.md](references/calibration.md) before calibration,
  historical-frame, mapping, or same-car acceptance work.
- The active completion contract is
  `docs/v2x-calibration-completion-contract.md`. Its Fable-reviewed additions
  include clock drift, independent map survey, one-use holdouts, fixed eligible
  denominators/minimum sample counts, blind identity adjudication, per-axis
  pixel scaling, and 30-minute plus 24-hour deployment watches. Do not use an
  older plan as authority.
  Fresh high-effort Fable review attempts after the 13:48 UTC map-plan update
  still fail before reading the file because Claude CLI reports an expired,
  non-refreshable OAuth session. Do not describe the updated map plan as newly
  Fable-reviewed until that external authentication works; retain the fixed
  contract and continue only work that cannot weaken its gates.
- Current static evidence still fails. Clean, vehicle-resistant fit/dev/holdout
  composites from three independent KVS windows per camera are retained at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T072000Z-temporal-static-targets-v3/`.
  They are proposal-only, not annotation truth. `build_temporal_static_targets.py`
  now makes window IDs path-independent and defaults to at least three valid
  samples; never opt down to one sample for an acceptance-labelled workflow.
  The latest completed bounded isolated UE5 search is
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T104045Z-inverse-render-search-v6/`.
  It retained 895 diagnostic evaluations across broad
  pose/FOV ranges. Every selected candidate fails the fixed geometry gate and
  visual review; ch1/ch2/ch3/ch4 road-surface scores are approximately
  0.702/0.540/0.470/0.508 and all remain below the contract. A subsequent cold
  Richmond load did not become ready within ten minutes, so no v7 render corpus
  exists. Do not re-enter a maintenance window until the map loader has a true
  outer process deadline and rollback remains independently armed.
- The decisive static-topology diagnostic is
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T103000Z-ch4-crosswalk-planar-consistency-v1/report.json`.
  A homography fitted to one physical/map crosswalk reproduces that crosswalk,
  but projects the other visible Richmond crosswalks tens to hundreds of pixels
  away. CARLA exposes the paint as eight large aggregate `RoadLines` objects,
  not independently controllable crosswalk objects, so hiding one bad marking
  at runtime is not available and hiding all eight removes the full road-marking
  layer. Camera pose alone cannot repair this map inconsistency. Require the
  actual complete UE5.5 Richmond source map/dependency graph (or an authorized,
  independently surveyed complete road-marking replacement), a fingerprinted
  full cook, and fresh untouched holdouts before production calibration.
- The shared projection chain itself is accurate to about 0.015 mm at the
  retained anchor, but one global SE(2) fails approach-held-out stability and
  cannot repair topology. The diagnostic map exporter currently overwrites
  per-waypoint marking metadata, retaining only the final left/right marking
  for a lane, and uses unstable enumeration-only crosswalk IDs. Correction work
  must preserve segmented OpenDRIVE `roadMark` s/t ranges under stable
  road/lane/range identities and stable road/object crosswalk IDs. QL2 vertical
  RMSE/P95 of about 0.044/0.087 m has no horizontal residual or current-paint
  truth and is not an alignment acceptance gate.
- A joint-rig diagnostic that forbids independent per-camera translation is at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T121500Z-joint-visual-selfcal-v1/`.
  The 19 visual parameters are full-rank with condition about 1.5e3, but five
  axes hit bounds and frozen ch1/ch2 road holdouts remain about 98/541 px at
  640-wide. All four overlays were visually inspected; local fits break other
  roads or landmarks. The candidate is rejected and must not be rendered,
  deployed, or used for actor placement.
- The map-source/capacity audit and accepted recovery routes are frozen in
  `docs/v2x-map-correction-recovery-plan.md`. The private
  `SimForgeinc/RFS_Reconstruction` main revision contains April Richmond
  editor assets but remains UE4.26 and has no raw RoadRunner/FBX/OBJ/USD/GIS
  source in that repository. The raw authoring export has now been recovered
  at `/home/path/Downloads/entire scene-20260211T002439Z-1-001 (2)/entire scene/`:
  `Richmond.fbx` is 163,879,392 bytes with SHA-256
  `68e889cf8d2ab17cc2005c5e7364fd64608723b819df747c102d95a53757e3e0`,
  and `Richmond.xodr` has SHA-256
  `ed2e44492616901fbb20b89191ab03d666c0217620d0247e55235c116f5cf2b1`,
  with 222 roads/29 junctions. It is byte-identical only to the older local
  CARLA cache, not to the deployed UE5.5 OpenDRIVE. Live evidence reports
  SHA-256 `0737f3d9f9f344c06b2c63fe669afa8a15f814568ee9c16046795338f56f5ee1`
  with 208 roads/32 junctions; the exact retained comparison is
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T104500Z-opendrive-source-audit/compare-old-richmond.json`.
  GeoJSON, RoadRunner metadata, and materials are present. A source candidate
  and dedicated V2X capacity are therefore available, but OpenDRIVE lineage,
  independent survey, measured intrinsics, static calibration, full dependency
  validation, cook, and untouched holdouts remain open. The production image
  is cooked-only. The only local comparison workspace with UE5.5 source belongs
  to the separate UE6 comparison task and is ineligible for V2X. Do not delete,
  inspect, or reuse it. A dedicated clean
  V2X UE5.5 migration workspace now exists at `/mnt/v2x-ue5` on a 500 GB
  loop-backed ext4 image stored as the single removable file
  `/mnt/v2x-capacity/v2x-ue5-build.ext4` on the secondary Windows volume. The
  outer NTFS mount intentionally uses `ntfs-3g`; the kernel `ntfs3` driver
  materialized the whole sparse file during the first bounded attempt, which
  was stopped and removed before retrying. A second `largefile4` format was
  also rejected after clean checkout proved its low inode count unsuitable for
  UE's 183,000+ source files; only partial V2X clones existed and were discarded.
  The current normal ext4 image exposes about 32 million inodes, initially used
  about 10 MB physical space, and left existing Windows data unchanged. Clean
  CARLA `ue5-dev` commit `6279162d1836024488474d3e5b2a5737ce57bb63`
  is complete. The engine checkout has been corrected from stock Epic 5.5.4 to
  CARLA's required `CarlaUnreal/UnrealEngine` `ue5-dev-carla` commit
  `2ac0528831e08e80784df2759db9a2c592d3bd4d`; fork dependencies are downloading
  at background CPU/disk priority in
  `/mnt/v2x-ue5/src/UnrealEngine-5.5.4`; do not rename or duplicate the
  in-progress tree. The clean Richmond source checkout is
  fixed at `d14da5b57bbe4356930a2b9a926a675692e18547`. The complete 29-file April
  road-core subset—level, scene, road/curb/gutter/sidewalk and both marking
  layers plus their primary materials/textures—matches every recorded LFS SHA
  and has zero missing material/texture imports in UE Viewer. Retained evidence
  is `/mnt/v2x-ue5/evidence/april-road-core-dependencies/`. Thousands of
  unrelated prop assets remain pointers and are not acceptance-ready; do not
  mislabel the road-core subset as a complete final map package. Engine fork
  dependencies completed and `Setup.sh` is now downloading the bundled Linux
  clang toolchain at background priority; `GenerateProjectFiles.sh` and the
  UnrealEditor build have not run yet. A probe proved the latest retained
  `Richmond_NR.umap` references
  `New_RFS/Richmond_Field_Station_Richmond_CA.uasset`; that scene asset retains
  a distinct newer source path,
  `C:/Users/123/Downloads/Unreal_Exports/Richmond_Field_Station_Richmond_CA.fbx`,
  plus `Terrain_Marking` and `Roads_Marking` scene hierarchies. This is a real
  candidate map revision, not accepted geometry and not a substitute for the
  independent survey gate. A clean 64-bit UE Viewer build at source commit
  `a0bfb468d42be831b126632fd8a0ae6b3614f981` can enumerate the UE4.26 assets,
  bounds, imports, and source models, but reports zero cooked/render LODs for
  the editor-only road meshes and therefore cannot export their vertices. Do
  not mislabel that as an empty map; it proves the CARLA UE5.5 editor migration
  remains necessary. All inputs and build products remain only in this
  V2X mount. The root filesystem and all UE6/comparison paths
  remain excluded. After any reboot, verify both mounts and the image allocation
  before resuming; no persistent mount entry has been installed yet. The UE4
  import metadata names the historical authoring path
  `D:/Work/Simforge/Berkley/Road Runner/28012026/Richmond.fbx`. A Drive
  inventory records a 158 GB Richmond export dated 2026-03-30, but its linked
  folder still returns 404; retain that as historical provenance, not as the
  current raw-source blocker. Do not import or deploy the recovered package
  until its no-replace inventory, dependency graph, coordinate conventions,
  independent survey, and controlled UE5.5 cook plan pass review.
- Exact source-frame evidence for `global_car_4db7ffc8_138` is retained at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T100128Z-object-138-exact/`.
  One representative persisted event per camera is bound to the exact fMP4
  frame at 0 ms media-time error; independent YOLO detection IoU is about
  0.951/0.963/0.870/0.943 for ch1/ch2/ch3/ch4. The four views visually support
  the same white Toyota Camry, but this remains identity proposal evidence, not
  blind-adjudicated identity or world-placement acceptance.
- Cross-model segmentation contact consensus for that exact-frame sample has
  three accepted proposals (ch1/ch2/ch4) with median mask IoU about 0.983 and
  maximum native contact disagreement 1.5 px x / 1.75 px y. Ch3 is correctly
  rejected as clipped. The report is
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T100128Z-object-138-exact/contact-consensus.json`.
  It is useful proposal evidence but fails four-camera coverage and independent
  contact review. The full frozen observation ledger contains 369 trusted
  vehicle rows and zero acceptance-eligible rows because reviewed contacts,
  static calibration, and independently adjudicated identity are absent.
  Consensus schema v2 must load the
  hash-bound capture report, cover its entire fixed denominator, validate masks
  and covariance, and apply native x/y disagreement limits independently.
  Producer-time samples that formerly appeared to show 0 ms phase/drift are
  now rejected as a shared zero-residual ingest timestamp grid. The fail-closed
  report is
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T101000Z-kvs-timestamp-drift-v3/report.json`;
  it also has only two windows/4.43 hours versus four windows/12 hours and lacks
  independent exposure/UTC truth.
- The recovery worktree contains rejected exploratory camera CSVs and a dirty
  `config/cameras.json`. Preserve them as user-owned diagnostics, but never
  stage, glob, fit, promote, or deploy them.
- The integration candidate now shares one complete actor-observed CARLA
  default lens tuple across rig, manifest builder, optimizer, and replay
  verifier. Configured lens overrides remain a hard safety hold and no lens
  attributes are written at runtime.
- Proposal-only SIFT diagnostics for the retained source pairs found distributed
  proposal counts ch1/ch2/ch3/ch4 = 1/1/6/1. None reaches the 12-point manual
  evidence minimum. Outputs at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260711T064950Z-acquisition-deficit/proposals/`
  are `acceptance_eligible=false` and must not be promoted.
- The clean `path2v2x/co-perception` reference commit
  `c4ec4730bbabd915d62fad7f4acecc8488be4533` has been re-audited with every
  preserved channel CSV at
  `/home/path/V2XCarla/v2x-evidence/calibration/20260712T103000Z-legacy-co-perception-audit-v2/audit.json`.
  It contains only 7/5/4/4 camera-local points, no measured-intrinsics artifact,
  frame hashes, global landmark IDs, survey provenance, or frozen holdouts.
  Leave-one-out RMSE is about 1.49/7.60/4.84/1.85 m for ch1/ch2/ch3/ch4;
  ch1/ch2/ch4 geometry is collinear, and the active script's “Channel 4” comment
  actually matches the ch1 CSV. Use its nominal K and transforms only as a
  derived diagnostic baseline, never as physical or held-out calibration truth.
- Playwright CLI evidence at
  `/home/path/V2XCarla/v2x-evidence/playwright/20260711T073035Z/` proves the
  `/timeline` archive workflow, four HTTP-200 video sessions, replay control,
  a visible physical ch4 car, and cleanup to LIVE/zero sessions. It also proves
  the strict geometric gate fails: the corresponding twin actor is not visibly
  placed and road/crosswalk geometry is misregistered. Treat this as
  counter-evidence, not acceptance. Refresh the browser evidence after any
  deployed candidate; prior screenshots never transfer to a new fingerprint.
- The superseded local Playwright CLI baseline at
  `/home/path/V2XCarla/v2x-evidence/playwright/20260712T122000Z-calibration-baseline/`
  shows all four `/live` physical feeds visible, but `/timeline` still renders
  only ch2 while ch1/ch3/ch4 are black, the global header reports zero cameras
  and zero FPS, and the object table is stale. Infrastructure HTTP success and
  the passing four-feed CLI verifier did not make that timeline UI an
  acceptance pass. Use the newer 13:44 UTC candidate watch described above;
  retain this black-frame result as before-state evidence.

## Safety boundaries

- Work locally when already on `path-B860I-AORUS-PRO-ICE`; do not SSH back into the same host.
- From another host, use the configured SSH/Tailscale connection to `path@100.72.252.40`. Do not embed credentials in commands or source.
- Make source changes only in a clean Codex worktree. Treat `/home/path/V2XCarla/v2x-backend-dev` as a reference candidate, not as proof that it is clean; verify its status and fail the deployment gate if it is dirty.
- Do not overwrite `/home/path/V2XCarla/v2x-backend` until a controlled deployment gate. It may run active services and contain live-only work.
- Preserve `/home/path/V2XCarla/v2x-backend-backups/` and take a fresh rollback snapshot before deployment.
- Use only the packaged Unreal Engine 5.5 worker container `carla-rr-maps` for production V2X simulator work. The accepted image is RR/CARLA 0.10 and its runtime reports `5.5.0-0+UE5`.
- Calibration batch rendering may use the same approved image in the tracked,
  non-restarting `v2x-calibration-ue5` container on loopback ports 2300-2302.
  This Path PC has a 16 GiB GPU and cannot safely run that worker alongside the
  production worker: a dual-worker Richmond load produced a bounded Vulkan OOM
  while production fingerprints remained stable. Use a rollback-captured,
  zero-session maintenance window, hold all three mutation-capable timers, stop
  Drive and the production UE5 worker, run one isolated batch, then stop the
  owned calibration container and fully restore production/timers. Never point
  an optimizer at production ports 2000-2002.
- Never use the retired `carla-rfs`/CARLA 0.9.16 restart recipe.
- Do not build, launch, debug, authorize, retry, coordinate, or accept evidence from `/home/path/V2XCarla/CarlaUE6`, `/home/path/V2XCarla/UnrealEngine_6`, `ue6-*` user units, or ports `2100-2102` in a V2X task. A separate UE6 task owns those paths, processes, changes, and acceptance criteria.
- UE6 work must not stop, hold, delay, restart, or reconfigure V2X services or timers. V2X work must likewise remain independent: do not inspect, poll, gate on, coordinate, or operate UE6 paths, units, processes, listeners, or evidence. Validate only the V2X-owned UE5.5 resources below. Any cross-runtime contention is owned by the separate UE6 task, which must stop itself rather than asking V2X to change state.
- Before service or tunnel changes, stop every mutation-capable timer in the maintenance window, snapshot its state, and restore it only after validation:

```bash
sudo systemctl stop \
  v2x-drive-link-health.timer \
  v2x-perception-link-health.timer \
  v2x-hourly-drive-restart.timer
```

The link-health units can repair/publish public runtime configuration when their independent repair and release gates are enabled. The hourly unit restarts CARLA/drive and may publish tunnel configuration when explicitly enabled.

## Revalidate the live topology

Observed on 2026-07-10 UTC; verify rather than assume:

| Layer | Expected live value |
|---|---|
| Simulator engine | packaged Unreal Engine `5.5.0-0+UE5`; never UE6 |
| CARLA container | `carla-rr-maps` |
| CARLA image | `ghcr.io/simforgeinc/carla-rr-maps:0.10.0` |
| CARLA image ID | `sha256:8e7c7152f86a9e26878de5f280514f224290f70aad7b28d00d5087709504118e` |
| Shipping binary SHA-256 | `d9d8cafc10def42557cdfc2897f9581da45c4900dc82c3ff37f2c5e2e7b98b23` |
| CARLA command | `./CarlaUnreal.sh -RenderOffScreen -vulkan -nosound -carla-rpc-port=2000` |
| CARLA runtime/network | NVIDIA runtime, Docker bridge, host ports `2000-2002` |
| Map | `Richmond_Field_Station_Richmond_CA` |
| CARLA Python | `/home/path/V2XCarla/carla-venv-310/bin/python` |
| Drive WebSocket | `0.0.0.0:8765`, `v2x-drive.service` |
| Frontend | Vite on `0.0.0.0:5173`, `v2x-web.service`; do not inject browser-local `VITE_DRIVE_WS_URL` |
| Perception | `0.0.0.0:8090`, `v2x-perception.service` |
| Perception Python | `/home/path/V2XCarla/perception-venv/bin/python` (observed Python 3.12.3) |
| Perception assets | ignored live `apps/perception/yolov8n.pt` plus pinned `~/.cache/torch/hub/checkpoints/mobilenet_v3_small-047dcff4.pth` and `convnext_base-6075fbad.pth`; hash and preserve all three |
| Drive tunnel | `v2x-cloudflared-drive.service`; currently Quick Tunnel unless a named-tunnel gate has completed |
| Perception tunnel | `v2x-cloudflared-perception.service`; currently Quick Tunnel unless a named-tunnel gate has completed |
| Public API | `https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com` |
| AWS deploy caller | `arn:aws:iam::147229569658:user/rfs-v2x-service`; API writes require the dedicated least-privilege deploy role |
| Amplify repository | reconcile stale `michaelvu1207/v2x-backend` to canonical `path2v2x/v2x-backend` with a fresh token and explicit release gate |

Collect a non-mutating baseline:

```bash
hostname
date -u +%Y-%m-%dT%H:%M:%SZ
git -C /home/path/V2XCarla/v2x-backend status --short --branch
git -C /home/path/V2XCarla/v2x-backend-dev status --short --branch
test -z "$(git -C /home/path/V2XCarla/v2x-backend-dev status --porcelain=v1)" || {
  echo "Clean-reference candidate is dirty; stop and reconcile it." >&2
  exit 1
}
docker ps -a --filter name=carla-rr-maps --no-trunc
docker inspect carla-rr-maps --format \
  'image={{.Config.Image}} runtime={{.HostConfig.Runtime}} network={{.HostConfig.NetworkMode}} restart={{.HostConfig.RestartPolicy.Name}} ports={{json .HostConfig.PortBindings}} cmd={{json .Config.Cmd}}'
expected_image_id='sha256:8e7c7152f86a9e26878de5f280514f224290f70aad7b28d00d5087709504118e'
actual_image_id="$(docker inspect -f '{{.Image}}' carla-rr-maps)"
test "$actual_image_id" = "$expected_image_id" || {
  echo "Production CARLA image ID drifted: $actual_image_id" >&2
  exit 1
}
container_pid="$(docker inspect -f '{{.State.Pid}}' carla-rr-maps)"
ue5_binary="/proc/$container_pid/root/home/carla/CarlaUnreal/Binaries/Linux/CarlaUnreal-Linux-Shipping"
expected_binary_sha256='d9d8cafc10def42557cdfc2897f9581da45c4900dc82c3ff37f2c5e2e7b98b23'
actual_binary_sha256="$(sudo sha256sum "$ue5_binary" | awk '{print $1}')"
test "$actual_binary_sha256" = "$expected_binary_sha256" || {
  echo "Production CARLA UE5 worker binary drifted: $actual_binary_sha256" >&2
  exit 1
}
sudo strings -a "$ue5_binary" \
  | awk 'index($0, "/UnrealEngine5/") {found=1} END {exit !found}' || {
  echo 'Production CARLA binary lacks the UnrealEngine5 marker; stop.' >&2
  exit 1
}
if systemctl cat v2x-carla-rr.service \
  | grep -Eqi 'CarlaUE6|UnrealEngine_6|carla-rpc-port=2100'; then
  echo 'Production V2X service references the separate UE6 runtime; stop.' >&2
  exit 1
fi
if find /home/path/V2XCarla/v2x-backend \
  \( -path '*/.git' -o -path '*/node_modules' -o -path '*/.svelte-kit' \) \
  -prune -o -iname '*ue6*' -print -quit | grep -q .; then
  echo 'A UE6 artifact exists inside the production V2X checkout; stop.' >&2
  exit 1
fi
ss -ltnp | awk 'NR==1 || /:(2000|2001|2002|8765|5173|8090)( |$)/'
ps -eo pid=,ppid=,lstart=,args= | awk '/[c]loudflared/'
systemctl show \
  v2x-carla-rr.service \
  v2x-drive.service \
  v2x-perception.service \
  v2x-web.service \
  v2x-cloudflared-drive.service \
  v2x-cloudflared-perception.service \
  v2x-drive-link-health.timer \
  v2x-perception-link-health.timer \
  v2x-hourly-drive-restart.timer \
  --property=Id,ActiveState,SubState,UnitFileState,FragmentPath,MainPID,ExecMainStartTimestamp,NextElapseUSecRealtime
for unit in \
  v2x-carla-rr.service v2x-drive.service v2x-perception.service \
  v2x-web.service v2x-cloudflared-drive.service \
  v2x-cloudflared-perception.service v2x-drive-link-health.timer \
  v2x-perception-link-health.timer v2x-hourly-drive-restart.timer; do
  printf '%s=' "$unit"
  systemctl is-enabled "$unit" 2>&1 || true
done
```

Inspect installed definitions before trusting tracked units:

```bash
systemctl cat \
  v2x-carla-rr.service \
  v2x-drive.service \
  v2x-perception.service \
  v2x-web.service \
  v2x-cloudflared-drive.service \
  v2x-cloudflared-perception.service \
  v2x-drive-link-health.service \
  v2x-drive-link-health.timer \
  v2x-perception-link-health.service \
  v2x-perception-link-health.timer \
  v2x-hourly-drive-restart.service \
  v2x-hourly-drive-restart.timer
```

Print only allowlisted, non-secret safety gates. Calculate the installed
last-declaration-wins values, then compare active services with their actual
process environments; a mismatch means the process has not consumed the new
configuration. Never dump a complete unit or process environment.

```bash
gate_keys='^(ALLOW_CARLA_CONFIG_DRIFT|ALLOW_CARLA_CREATE|ALLOW_CARLA_RECREATE|AMPLIFY_RELEASE_ENABLED|DRIVE_CONFIG_REQUIRED|DRIVE_LINK_HEALTH_REPAIR|DRIVE_TUNNEL_MODE|DRIVE_WS_INSECURE_SSL|PERCEPTION_LINK_HEALTH_REPAIR|PUBLISH_DRIVE_FRONTEND_CONFIG|PUBLISH_DRIVE_FRONTEND_CONFIG_REQUIRED|SKIP_RESTART_IF_ACTIVE_SESSION|V2X_PERCEPTION_UPLOAD)$'
show_declared_gates() {
  unit="$1"; shift
  {
    systemctl show "$unit" --property=Environment --value | tr ' ' '\n'
    for file in "$@"; do
      sudo awk -F= -v keys="$gate_keys" '$1 ~ keys {print}' "$file" 2>/dev/null || true
    done
  } | awk -F= -v keys="$gate_keys" '$1 ~ keys {value[$1]=$2} END {for (key in value) print key "=" value[key]}' | sort
}
show_declared_gates v2x-carla-rr.service /etc/v2x-carla-rr.env
show_declared_gates v2x-perception.service /etc/v2x-perception.env
show_declared_gates v2x-cloudflared-drive.service /etc/v2x-drive-tunnel.env
show_declared_gates v2x-cloudflared-perception.service /etc/v2x-perception-tunnel.env
show_declared_gates v2x-drive-link-health.service /etc/v2x-drive-tunnel.env /etc/v2x-drive-link-health.env
show_declared_gates v2x-perception-link-health.service /etc/v2x-perception-tunnel.env /etc/v2x-perception-link-health.env
show_declared_gates v2x-hourly-drive-restart.service /etc/v2x-drive-restart.env

for unit in v2x-carla-rr.service v2x-perception.service \
  v2x-cloudflared-drive.service v2x-cloudflared-perception.service; do
  pid="$(systemctl show "$unit" --property=MainPID --value)"
  printf '[%s pid=%s effective]\n' "$unit" "$pid"
  if [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then
    sudo sh -c 'tr "\0" "\n" < "/proc/$1/environ"' sh "$pid" \
      | awk -F= -v keys="$gate_keys" '$1 ~ keys {print}' | sort
  else
    echo inactive
  fi
done
```

## Mental model

Keep these layers separate:

1. The UE5.5 `carla-rr-maps` worker container and CARLA RPC on `2000`.
2. Drive WebSocket bridge on `8765`.
3. Supervised frontend dev server on `5173`.
4. Perception health/MJPEG on `8090`.
5. Independently supervised Drive and perception tunnels.
6. Cloudflare or Tailscale transport.
7. Public runtime configuration and API routes.

A healthy CARLA container does not prove a healthy bridge, tunnel, frontend, or perception pipeline.

## Computer Use companion

- Use CLI/API probes for infrastructure facts and Computer Use for visible `/drive`, `/live`, and `/timeline` behavior, screenshots, browser console, network requests, and WebSocket frames. Hard-refresh after each state-changing action before recording evidence.
- If `node_repl` with `@oai/sky` is unavailable on the Path PC task, continue the stable companion task on `remote-ssh-codex-managed:simforgelaptop` with `send_message_to_thread`; create a new task only when explicitly requested. Do not ask the user to operate the browser.
- Include the public URL, expected deployed commit/config version, exact page flows, read-only or mutation boundary, cleanup requirement, and a request to debug within scope until each acceptance check works. Require `node_repl`/`@oai/sky`, refreshed screenshots, console and network evidence, and explicit cleanup of any Drive session.
- Immediately create an `automation_update` heartbeat that calls `read_thread` every minute, reports terminal completion, and disables itself after success/failure. If that runtime is unavailable, dedicate a collaboration agent to poll the task every 30 seconds with bounded waits. Preserve the task when the selected account is usage-limited and resume it when capacity returns.
- Record the companion task ID and heartbeat ID in the phase evidence. Stop/archive the heartbeat only after consuming the final result; do not infer completion from silence.

## Drive diagnosis order

1. Confirm `carla-rr-maps` is running with the expected image/command and reports `5.5.0-0+UE5`.
2. Confirm RR/CARLA 0.10 in that UE5.5 worker accepts a client and has the Richmond map loaded.
3. Confirm `v2x-drive.service` and listener `8765`.
4. Perform a WebSocket handshake/protocol health check.
5. Inspect the tunnel process and its local origin.
6. Compare public `/config.json` and `/drive-config` with the active tunnel.
7. Refresh `/drive` and capture visible state, console, network, and WebSocket evidence.

CARLA client probe:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python - <<'PY'
import carla

client = carla.Client("127.0.0.1", 2000)
client.set_timeout(20.0)
print("client", client.get_client_version())
print("server", client.get_server_version())
print("map", client.get_world().get_map().name)
PY
```

WebSocket handshake probe:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python - <<'PY'
import asyncio
import websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8765", open_timeout=10):
        print("WS_OK")

asyncio.run(main())
PY
```

Useful logs:

```bash
journalctl -u v2x-drive.service --utc -n 200 --no-pager
journalctl -u v2x-cloudflared-drive.service --utc -n 200 --no-pager
journalctl -u v2x-drive-link-health.service --utc -n 200 --no-pager
tail -n 200 /tmp/v2x-cloudflared.log
```

## Tunnel and runtime configuration

- The current installed drive unit may launch a process-scoped Quick Tunnel to `http://localhost:8765`.
- A named hostname such as `wss://drive.path2v2x.net` is valid only after its credential, DNS, unit, and WebSocket handshake are independently proven.
- Never hardcode a newly observed `*.trycloudflare.com` URL in source.
- Never roll back to a saved Quick-Tunnel URL after that process has stopped; it is dead. Preserve the old tunnel during a blue/green cutover, publish the newly proven endpoint, verify public convergence, and only then stop the old process.
- Treat Tailscale and Cloudflare as separate transports. Validate the endpoint the browser actually selected.
- Treat an enabled-but-inactive `v2x-cloudflared-perception.service` and a `cloudflared` process with PPID 1 as separate facts. `enable` does not adopt that unmanaged process; starting the unit creates a second tunnel. Record both PID/PPID/command/URL tuples, keep the PPID-1 tunnel alive during blue/green validation, and stop only the exact old PID after public convergence.

Read-only checks:

```bash
pgrep -af cloudflared
curl -fsS https://path2v2x.net/config.json | jq .
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com/drive-config
```

Accept `/drive-config` only when it returns HTTP `200`; `version` is a positive,
nondecreasing integer; `updatedAt`/`expiresAt` are fresh and within the browser's
24-hour TTL bound; and the selected WebSocket URL equals the endpoint of the
still-running tunnel. For a Quick Tunnel, compare it directly:

```bash
(
body="$(mktemp)"; trap 'rm -f "$body"' EXIT
code="$(curl -sS -o "$body" -w '%{http_code}' \
  https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com/drive-config)"
test "$code" = 200
: "${PREVIOUS_DRIVE_CONFIG_VERSION:=0}"
jq -e --argjson previous "$PREVIOUS_DRIVE_CONFIG_VERSION" \
  --argjson now "$(date -u +%s)" '
  .version as $v
  | (.updatedAt | fromdateiso8601) as $updated
  | (.expiresAt | fromdateiso8601) as $expires
  | ($v | type == "number") and ($v >= 1) and ($v == ($v | floor))
    and ($v >= $previous) and ($updated <= ($now + 300))
    and ($expires > $now) and (($expires - $updated) > 0)
    and (($expires - $updated) <= 86400)' "$body"
pgrep -af 'cloudflared.*(localhost|127\.0\.0\.1):8765'
active_drive_ws="$(grep -Eo 'https://[A-Za-z0-9-]+\.trycloudflare\.com' \
  /tmp/v2x-cloudflared.log | tail -n 1 | sed 's#^https:#wss:#')"
published_drive_ws="$(jq -er '.cloudflareDriveWsUrl' "$body")"
test "$published_drive_ws" = "$active_drive_ws"
DRIVE_WS_URL="$published_drive_ws" /home/path/V2XCarla/carla-venv-310/bin/python - <<'PY'
import asyncio, os, websockets
async def main():
    async with websockets.connect(os.environ["DRIVE_WS_URL"], open_timeout=10):
        print("PUBLIC_WS_OK")
asyncio.run(main())
PY
)
```

Also require Computer Use network evidence that `/drive-config` returned that
version and `/drive` opened its WebSocket against the same endpoint after a
hard refresh. An HTTP `426` from the tunnel root can be expected for a reachable
WebSocket-only origin; require a real WebSocket `101`/handshake for acceptance.

## Perception diagnosis

Do not use HTTP `200`, MJPEG byte flow, or a rising republished-frame counter as freshness proof. The service can replay `last_valid_frames` while an upstream camera is frozen. Legacy detections created before timestamp schema v2 used Path-PC decode-receipt time and are not valid archive-correlation evidence. Accept a new record for replay proof only when `timestamp_schema_version=2`, `media_time_trusted=true`, `timestamp_utc == media_timestamp_utc`, and `media_clock.source=hls_ext_x_program_date_time` with schema version 1. `decode_received_at_utc` and `decode_latency_ms` must remain separate diagnostics.

Check producer timestamps twice and require all four channels to advance:

```bash
curl -fsS http://127.0.0.1:8090/health | jq .
curl -fsS http://127.0.0.1:8090/detections/latest | jq .
sleep 5
curl -fsS http://127.0.0.1:8090/detections/latest | jq .
```

Expected endpoints:

- `/health`
- `/detections/latest`
- `/streams/ch1.mjpg` through `/streams/ch4.mjpg`

Validate upstream Kinesis separately through the read API:

```bash
for camera in ch1 ch2 ch3 ch4; do
  curl -fsS \
    "https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com/video/session/${camera}" \
    | jq '{cameraId,playbackMode,expiresIn,hlsUrlPresent:(.hlsUrl|length>0)}'
done
```

Never print or retain signed HLS query strings. For acceptance, require:

- ch1-ch4 decoded-frame capture timestamps remain recent and advance;
- real frames change, not only response bytes;
- `/health.media_clock_ready` is true and every channel reports a trusted matched media clock with bounded decode latency;
- event timestamps are monotonic and close to DynamoDB ingestion time;
- forced HLS expiry/reconnect recovers within the agreed bound;
- socket counts do not accumulate `CLOSE_WAIT`;
- a new DynamoDB record proves current media, decode-receipt, and ingestion timestamps plus schema-v2 provenance.

For an archived vehicle/bbox acceptance gate, use the tracked read-only verifier
with one exact persisted detection JSON and the local model. It keeps signed HLS
URLs internal, requires trusted persisted provenance, selects the nearest actual
fMP4 frame, and exits nonzero for timing, bbox, or semantic mismatch:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/perception/tools/verify_historical_correlation.py \
  https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com \
  --detection-json /path/to/one-sanitized-detection.json \
  --output /tmp/v2x-correlation-frame.jpg \
  --yolo-model /home/path/V2XCarla/v2x-backend/apps/perception/yolov8n.pt \
  --require-yolo
```

Do not treat a legacy row, a CLI-supplied timestamp, a merely nonblank bbox, or
a changed twin JPEG as same-object proof. Require the selected `object_id` in a
`twin_status` response to carry the strict schema-v2 HLS clock provenance and
map to an `actor_present=true` UE5 CARLA `actor_id`, type, role, and transform.
Require three status samples spanning at least two replay seconds, one stable
actor ID, and at least 0.25 m of movement; validate the actor directly in CARLA.

A shared database `object_id` across cameras is not itself identity proof. For
vehicles, require two independently passing historical reports, exact archived
frame hashes, different cameras, bounded transit time, and the pinned ConvNeXt
appearance gate before replay acceptance:

```bash
/home/path/V2XCarla/perception-venv/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/perception/tools/verify_cross_camera_identity.py \
  --left-report /tmp/ch4-report.json --left-frame /tmp/ch4-frame.jpg \
  --right-report /tmp/ch1-report.json --right-frame /tmp/ch1-frame.jpg \
  --output /tmp/cross-camera-identity.json --device cuda
```

Production association must compute pinned ConvNeXt embeddings for vehicles,
require similarity at least `0.60` for every slow-path vehicle reattachment
(same or cross camera), never share a cache entry when `track_id` is absent,
and persist the association method, similarity, threshold, devices, time, and
distance. Missing appearance evidence fails closed rather than falling back to
proximity alone.
Live vehicle association must also require exact trusted schema-v2 HLS media
time and reject missing, non-finite, or combined localization uncertainty above
2.0 m. Never clamp a large uncertainty into the accepted association radius,
and record rather than silently overwrite a car/truck/bus class conflict.
When two vehicle candidates have insufficient spatial/appearance separation,
reject association, persist bounded ambiguity evidence, and start a distinct
track. Never let greedy input order choose between adjacent plausible cars.
Every tracked camera must provide finite measured `localization.pixel_sigma`
and `localization.calibration_uncertainty_m` no greater than 2.0 m. Missing
values block perception startup; never fill them from rejected exploratory or
matcher-generated calibration rows.

The 24-hour persistence gate is paginated and fail-closed. Require every
camera to have trusted schema-v2 events spanning at least 23 hours and a recent
upload; a query over a 24-hour window is not itself proof of 24-hour history.
Every accepted row must use `exact_same_session_pts`, reconstruct media time
within 5 ms, keep decode ISO/epoch and latency within 5 ms, ingest within five
integer seconds, expire exactly seven days from media time, and carry an event
ID with no leading/trailing whitespace:

Run the row-content verifier while the target rows are still within that
seven-day lifetime. Its TTL gate is the exact producer delta
`expires_at - int(media_timestamp) == 604800`; it intentionally does not compare
expiry with verifier wall time, because API/read latency and DynamoDB's
asynchronous post-expiry purge are not producer-schema failures. A later long
watch validates purge/availability behavior separately and must not require an
already purged item to remain readable.

```bash
/home/path/V2XCarla/perception-venv/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/perception/tools/verify_detection_persistence.py \
  https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com
```

Twin camera alignment is a separate gate from channel wiring. The existing
perception CSVs contain only 4-7 local-XZ points per channel, no independent
holdouts, no global landmark IDs, and internally inconsistent shared points;
treat the current camera verifier as diagnostic only. Do not deploy pose, pole,
FOV, or lens changes fitted from those rows. To create acceptance evidence,
measure each physical camera's intrinsics/distortion from at least ten accepted
checkerboard/ChArUco fit images plus two untouched holdouts, independently
survey the common map/site truth, and retain at least 12 globally identified
correspondences per channel split into at least eight fit points and four
untouched holdouts spanning 50% of image width and 30% of height. Record source
frame, board, measurement, map, and render hashes. At 1280x960 require held-out
point RMSE/P95/max at most 10/16/24 pixels and finite road-geometry RMSE/max at
most 6/12 pixels for every camera, plus correct road/lane/crosswalk topology,
horizon, vanishing directions, stable landmarks, and all four retained renders.
Scale these limits by `manifest.width/1280` when residuals use the native
real-camera pixel space; do not apply 1280-wide limits unchanged to 2560-wide
evidence.
The former 75/125/175 point limits were framing diagnostics and must never be
used as calibration acceptance.

Fit, deploy, and verify must all call the same tracked camera-transform and
optical-model functions. A missing translation offset means zero; never hide a
default pole displacement in one path. Resolve candidate landmarks directly
from the UE5 map/depth buffer with `build_twin_camera_landmarks.py`; legacy
camera-local XZ converted through the heading under test is circular evidence.
Reject sparse, collinear, clustered, or non-global datasets before fitting.
Aggregate the four per-camera manifests against one hash-bound surveyed site
registry with `aggregate_twin_calibration_manifests.py` before any joint fit.
Require one canonical landmark ID to retain one split and surveyed world
identity across every camera; reject renamed near-duplicates and keep the
aggregation `acceptance_eligible=false` until every independent gate passes.
Every camera must participate in at least one genuinely shared survey identity,
and shared identities must connect all four cameras rather than form isolated
pairs. The report must state shared-landmark counts per camera and graph edges.
Hash strings alone are not evidence: aggregation and optimization must each
re-open and SHA-256-check the exact annotation, real/twin frames, cameras file,
intrinsics artifact and source images, raw depth buffer, and external survey
records from canonical retained paths.
For RR/CARLA 0.10 live acceptance, the shared transform must report strict
`opendrive_georeference` provenance; `origin_centered_fallback` is never an
accepted live projection.

Feature matchers (SIFT, LoFTR, RoMa, or successors) may propose landmarks but
cannot themselves certify held-out truth. Repeated lane/crosswalk markings can
produce a low numerical loss for the wrong correspondence. Retain the real and
twin source frames, manually/geometrically identify each held-out landmark, and
require an independent road-geometry gate for road edges, lane markings,
horizon, vanishing points, curb/crosswalk topology, and stable map landmarks.
If the retained render visibly contradicts the real view, fail the candidate
even when a point-only threshold passes; do not weaken thresholds or relabel
matcher-generated points to make it green.

Semantic inverse rendering is the preferred diagnostic search before manual
acceptance annotation. Use `scripts/v2x-calibration-worker.sh` and
`render_semantic_calibration_candidate.py` to produce hash-bound, synchronized
RGB, raw semantic, raw instance, and metric-depth buffers for one explicit
candidate. The renderer must reject the production container/port, verify the
approved image and Richmond OpenDRIVE fingerprints, use one CARLA frame for all
buffers, destroy all owned sensors, and keep every result
`acceptance_eligible=false`.

The current Richmond RR/UE5 build was observed to emit semantic tag 11 for 100%
of static pixels and instance sentinel 65535 for 100% of static pixels, even
while RGB and depth were valid. Retain those buffers as failure evidence; never
claim class-aware alignment from them. Build diagnostic road-paint/curb targets
from retained RGB, depth, reviewed topology, and class-specific masks instead.
Score robust symmetric contour distance, P95/max, tolerance precision/recall,
and topology separately. A lower clipped mean does not promote a candidate when
P95, tolerance F1, a required semantic class, or the retained visual overlay
regresses. Missing/mismatched map paint is a map blocker, not a reason to mask a
required held-out class or weaken the road-geometry gate.

Freeze the static camera before using vehicles. For a real vehicle with no close
UE5 mesh, treat blueprint family and dimensions as nuisance variables and use a
robust silhouette centroid/midpoint only as a lower-weight cue alongside visible
contour, wheel/road contact, projected 3-D extent, multi-camera timing, road
legality, and temporal smoothness. Never let midpoint agreement alone recalibrate
the camera or certify same-car placement.

Manual four-camera evidence must use one JSON annotation artifact per channel.
Capture observational pairs with `capture_twin_calibration_pairs.py` only
against a twin protocol that sends a `twin_frame` JSON packet immediately
before each binary JPEG. Pass the exact active `--cameras-json`; the capture
must bind the JPEG hash, channel ID, LIVE mode, CARLA frame/timestamp, capture
gap, whole config hash, and selected camera hash. The older binary-only/twin
clock sequence is diagnostic and must fail this capture gate.
Points require `provenance="manually_verified_unique"`, at least eight frozen
train and four untouched holdouts. Road edges, lane markings, and crosswalk
geometry require `provenance="manually_traced_geometry"`, at least three train
and two holdout polylines; infinite-line evidence is not accepted. Include the
exact real/twin frame SHA-256 values and decode both retained images to verify
their actual dimensions. Also freeze the exact `cameras.json` SHA-256 that
produced the annotated twin render and reject a manifest build against any
other config. Reject duplicate real/twin point pixels across train and holdout,
zero-length or duplicate/resampled train/holdout polylines, collinear/clustered
point sets, holdouts copied from fit geometry, and blank/duplicate semantic
landmark descriptions. Reject annotated twin pixels whose 3x3 depth
neighborhood crosses a geometry discontinuity; a plausible center depth alone
does not establish frozen world truth. Retain the numeric 3x3 neighborhood
range/deviation evidence for every point and polyline vertex. Never translate
`manual_verified_static`, matcher proposals, or vague repeated line points into
the accepted provenance labels.

Do not treat repeated nominal `fx/fy/cx/cy` values in `cameras.json` as
measured intrinsics. Each camera requires an `intrinsics_calibration` block
backed by a retained checkerboard or ChArUco JSON result artifact: exact
SHA-256, one unique SHA-256 per accepted source image, at least 10 accepted
calibration images, no more than 2 px calibration RMS, matching image
resolution and camera matrix, and finite Brown-Conrady `k1/k2/p1/p2/k3`. Pass
the actual result with `--intrinsics-artifact` and repeat
`--intrinsics-source-image` for every declared source hash; the manifest
builder must decode and hash every retained calibration image, then parse and
compare the normalized result to `cameras.json` before it connects to CARLA or
spawns a depth sensor. Quantify the full measured physical
model against the deployed UE5 centered-pinhole render over the image; an
optical mismatch above 0.25 px keeps deployment closed until a shared render
distortion or physical-feed undistortion path is implemented and verified.
The live repeated K (`fx=fy=1325.4`, `cx=1280`, `cy=960`) remains assumed, and
perception currently consumes missing/zero distortion. The acquisition tool
does not yet bind camera channel/device, exact board SVG and physical
measurement/photo, capture timestamp, encoder/crop/focus/zoom state, or
before/after mount stability. Add and test those fail-closed bindings and wire
measured distortion consumption before calling any physical intrinsics measured.

Generate a dimensioned board with the tracked acquisition tool, print it at
100% scale, and verify one square with a physical ruler before capture:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  apps/bridge/tools/calibrate_camera_intrinsics.py generate-board \
  --output /path/to/checkerboard-9x6-25mm.svg \
  --inner-columns 9 --inner-rows 6 --square-mm 25
```

For each fixed physical camera, acquire at least 10 sharp, unique fit images
and two untouched holdouts at its native resolution. Bind the camera/channel,
resolution, crop, focus/zoom state, board hash, capture times, and source
hashes. Move and tilt the board across every image edge/corner with at least 15
degrees of tilt spread and 1.3x distance variation; do not crop, resize,
digitally warp, or reuse frames. Obtain site-access and traffic-safety
authorization before roadside capture, and re-capture frozen landmarks after
the session to prove the camera mount did not move. Calibrate with one
`--image` argument per fit image and one `--holdout-image` per holdout:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  apps/bridge/tools/calibrate_camera_intrinsics.py calibrate \
  --image /path/to/ch1-board-01.png \
  --image /path/to/ch1-board-02.png \
  `# repeat for at least 10 unique accepted images` \
  --holdout-image /path/to/ch1-board-holdout-01.png \
  --holdout-image /path/to/ch1-board-holdout-02.png \
  --output /path/to/ch1-intrinsics.json \
  --report /path/to/ch1-intrinsics-report.json \
  --inner-columns 9 --inner-rows 6 --square-mm 25
```

The tool must reject decode failures, mixed resolutions, duplicate source
hashes, fewer than 10 accepted fits or two disjoint holdouts, poor edge/corner,
tilt, or distance coverage, non-finite output, fit or holdout RMS above 2 px,
and held-out per-corner max error above 5 px. Preserve the board hash, every
source image, artifact, report, and the artifact SHA-256 copied into
`cameras.json`.

Only in an authorized mutation window with zero Drive sessions, resolve those
twin pixels through a temporary UE5 depth sensor into one optimizer manifest:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/bridge/tools/build_twin_calibration_manifest.py \
  /path/to/ch1-annotations.json /tmp/ch1-calibration-manifest.json \
  --camera ch1 \
  --real-frame /path/to/real-ch1.jpg \
  --twin-frame /path/to/twin-ch1.jpg \
  --intrinsics-artifact /path/to/ch1-intrinsics.json \
  --intrinsics-source-image /path/to/ch1-charuco-01.png \
  --intrinsics-source-image /path/to/ch1-charuco-02.png \
  --depth-frame-output /path/to/ch1-depth.bgra \
  --cameras-json /home/path/V2XCarla/v2x-backend/config/cameras.json
```

The builder must destroy its owned depth sensor in `finally`. Preserve the
manifest's annotation, camera-file, per-camera, real-frame, twin-frame, depth
frame, map/OpenDRIVE/georeference projection provenance, and deployment-model
fingerprints. The deployment model must freeze
the surveyed anchor, unadjusted pitch/yaw/roll/FOV, and all six UE5 lens
attributes so the fitted absolute camera can be translated back into tracked
`twin_pose` fields without relying on live state. Run
`optimize_twin_road_geometry.py` only on this generated manifest; a
hand-converted CSV is not acceptance evidence.
Retain the exact raw BGRA depth buffer, including its SHA-256 and byte count;
the manifest alone is insufficient evidence for depth-derived world points.

At optimization time, pass the exact retained annotations, real frame, twin
frame, cameras file, intrinsics artifact, and every calibration source image
again. The optimizer must re-hash all inputs, decode and match every declared
source image, re-hash the selected canonical camera object, and compare the
calibration block with both `cameras.json` and the parsed artifact; a direct
`optimize_manifest()` call without this binding is non-acceptable. First build
one report from all four complete manifests and the surveyed registry; distinct
landmark IDs below 0.25 m, inconsistent cross-camera resolved world identity,
mixed map fingerprints, or incomplete builder counts fail closed:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  apps/bridge/tools/aggregate_twin_calibration_manifests.py \
  --registry /path/to/site-landmark-registry.json \
  --manifest /path/to/ch1-calibration-manifest.json \
  --manifest /path/to/ch2-calibration-manifest.json \
  --manifest /path/to/ch3-calibration-manifest.json \
  --manifest /path/to/ch4-calibration-manifest.json \
  --output /path/to/four-camera-aggregation.json
```

The optimizer re-hashes the registry and all four referenced manifests and
recomputes that report before connecting to UE5:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  apps/bridge/tools/optimize_twin_road_geometry.py \
  /path/to/ch1-calibration-manifest.json \
  --output /path/to/ch1-calibration-report.json \
  --site-aggregation-report /path/to/four-camera-aggregation.json \
  --annotations /path/to/ch1-annotations.json \
  --real-frame /path/to/real-ch1.jpg \
  --twin-frame /path/to/twin-ch1.jpg \
  --cameras-json /path/to/cameras.json \
  --intrinsics-artifact /path/to/ch1-intrinsics.json \
  --intrinsics-source-image /path/to/ch1-charuco-01.png \
  --intrinsics-source-image /path/to/ch1-charuco-02.png \
  --depth-frame /path/to/ch1-depth.bgra
```

The optimizer must reconnect read-only to the UE5.5 worker, verify the active
map name and OpenDRIVE SHA-256, record the host/port endpoint, and recompute the
absolute camera anchor from the verified config. In the same authorized
zero-session mutation window, spawn one temporary depth sensor at that exact
transform, compare fresh and retained depth at every annotated pixel, derive
world truth from the fresh render, and destroy the sensor in `finally`. Any
baseline, deployment-model, map-content, retained-depth, or feature-world
mismatch is a hard failure. No other actor or service mutation is allowed.

The optimizer must fit true 6-DoF extrinsics (CARLA x/y/z plus
pitch/yaw/roll), FOV, principal point, and radial distortion. Preserve the
fully unconstrained solution as diagnostic optical evidence, but never deploy
it directly: pose, principal point, and distortion are partly degenerate. Run
a second bounded optimization through the exact production UE5/verifier model
(currently centered principal point and zero modeled radial distortion), emit
the candidate `twin_pose`, and prove a sub-pixel optical plus exact transform
round trip. If the independently measured principal point or distortion cannot
be represented by that shared model, keep deployment closed; never copy the
optimizer's `k1` into CARLA `lens_k`, because those coefficients are not known
to be equivalent. A parameter at its search bound, an underconstrained fit, or
a green unconstrained fit paired with a failing deployable held-out fit is a
failure, not a calibration result.

Actor visual proof must also be reproducible across bridge restarts: choose UE5
blueprints with a stable digest rather than Python's randomized `hash()`. For a
same-car gate, require the projected actor bbox/centroid in the matched twin
camera over multiple replay timestamps, not merely `actor_present=true`.

A shared persisted object ID across cameras is diagnostic, not identity proof.
Run `apps/perception/tools/verify_cross_camera_persistence.py` against the
public API and require trusted schema-v2 media clocks, matching perception run,
finite GPS/bbox, localization uncertainty no greater than 2 m, plausible
transit time/speed, and a persisted
`identity_association.method="cross_camera_spatiotemporal_convnext"` with the
previous device, ConvNeXt similarity at least 0.60, and consistent distance.
Current production records that omit this association evidence must fail even
when their object IDs happen to match; visual same-car proof remains a separate
required gate.

Useful logs:

```bash
journalctl -u v2x-perception.service --utc -n 300 --no-pager
journalctl -u v2x-cloudflared-perception.service --utc -n 200 --no-pager
journalctl -u v2x-perception-link-health.service --utc -n 200 --no-pager
ss -tanp | awk 'NR==1 || /python/ || /CLOSE-WAIT/'
```

Use the tracked dependency-light verifier for the four-feed gate, locally and
again through the browser-selected public perception origin. It requires two
advancing health/detection samples and two different complete JPEG hashes from
each feed, and rejects query-bearing endpoint input:

```bash
/home/path/V2XCarla/perception-venv/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/perception/tools/verify_live_feeds.py \
  http://127.0.0.1:8090
```

For a bounded Drive/twin/replay regression, run the tracked verifier
observationally first. Never pass `--apply` during planning, read-only diagnosis,
or observational validation: it mutates simulator state by creating sessions
and actors. Omit it entirely from read-only evidence.

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/bridge/tools/verify_phase4_live.py
```

Only inside an authorized mutation window, after the observational command
reports zero active sessions, run apply mode. It creates two isolated sessions,
verifies correlated Teleport, exercises replay, restores live mode, and cleans
up owned actors in `finally`:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/bridge/tools/verify_phase4_live.py --apply
```

After one post-schema-v2 persisted detection has passed the historical frame
verifier, use its exact run-scoped object, replay start, and camera for the
same-object twin gate without creating a Drive session:

```bash
/home/path/V2XCarla/perception-venv/bin/python \
  /home/path/V2XCarla/v2x-backend/apps/bridge/tools/verify_phase4_live.py \
  --apply --skip-drive \
  --twin-object-id global_car_RUN_ID_TRACK \
  --twin-replay-start 2026-07-10T00:00:00.000Z \
  --twin-camera ch1 \
  --twin-yolo-model \
  /home/path/V2XCarla/v2x-backend/apps/perception/yolov8n.pt
```

The exact-object gate must retain one CARLA actor ID over at least three replay
samples and require a compatible YOLO detection to overlap that actor's
projected 3-D bounding box in each corresponding twin JPEG. The stream's
`twin_hello` must carry the exact UE5 camera actor ID, transform, dimensions,
FOV, lens values, and camera-config SHA-256 used for projection. Until the
tracked projection model supports a measured nonzero CARLA `lens_k` or
`lens_kcube`, fail closed rather than treating pinhole projection as equivalent.
Each JPEG must also be preceded by hash-matching `twin_frame` metadata with an
advancing UE5 frame ID and a replay clock no more than 250 ms after the sampled
object clock. Pin the stream fingerprint to the tracked channel config and the
advertised camera actor to the live `sensor.camera.*` transform and optical
attributes. Require before/after capture projection overlap with the same YOLO
bbox, at least 0.50 matched confidence, 0.15 IoU, 0.50 actor coverage, 75% of
the raw actor projection in frame, and an allowlisted YOLO model hash. Project
all live vehicles/walkers and reject foreground occlusion or a neighboring
actor that explains the detection within the fixed exclusivity margin. Across
the three samples, require distinct JPEGs and image-space detection motion that
agrees with the target actor's projected direction and displacement. CLI
overrides may tighten these floors but must never weaken them.

## Controlled deployment gate

Before changing live services:

1. Confirm the clean worktree commit and successful web/bridge/perception tests.
2. Confirm all simulator operations target only the UE5.5 `carla-rr-maps` worker on ports `2000-2002`. Do not inspect or depend on UE6 paths, units, ports, processes, or evidence.
3. Confirm no active drive session.
4. Stop both repair timers and the hourly restart timer.
5. Capture installed unit hashes, process commands, container image ID, live Git status, tunnel/runtime config, ignored-model/cache hashes, perception Python/pip state, and service logs.
6. Preserve rollback copies of installed units, ignored runtime assets, and the live repository changes.
   Use `scripts/capture-v2x-rollback.sh` in its default plan mode first. After
   the timers are stopped, run `ACTION=capture`, then require `ACTION=verify`
   against the new bundle; verification rehearses tracked, staged, unstaged,
   and untracked repository restoration in an isolated clone without changing
   the live checkout.
7. Let `v2x-carla-rr.service` adopt an already-running validated container through `docker wait`; do not restart or recreate it merely to add supervision.
8. Install one layer at a time and refresh UI/API evidence after each action.
9. Start perception with `/etc/v2x-perception.env` keeping `V2X_PERCEPTION_UPLOAD=false`; require four fresh/changing feeds before enabling production uploads and proving a current DynamoDB record.
10. Restore the previous artifact immediately when its acceptance gate fails. For Quick Tunnels, restore variables around the currently healthy endpoint, never a dead saved URL.
11. Re-enable timers only after the final public/runtime checks pass.

API route reconciliation is plan-first and exact-resource only. The normal service user cannot write API Gateway directly. A separately authorized principal must apply `infra/aws-cli/bootstrap-v2x-deploy-role.sh`; then assume `V2XBackendDeployRole` and run `provision-read-api.sh` with the reviewed API ID, `RECONCILE_LAMBDA=false`, IAM attachment disabled, explicit `PLAN_ONLY=false`, and the plan's `EXPECTED_CURRENT_STATE_HASH`. Do not add API privileges to the Amplify service role.

Prefer source-controlled scripts and systemd units over one-off `nohup` or manual Docker commands. Do not leave source that exists only in the live checkout.
