# Twin Mirroring Roadmap

Date: 2026-07-16

## Goal

A person watching the digital twin sees every significant real event at the
site (vehicle, pedestrian, emergency vehicle) appear in the right place, with
the right class and heading, within a stated latency budget, with stable
identity — with recorded evidence quantifying all of that, sustained over
days, and alerting when it breaks.

## Postmortem: 2026-07-14 feed outage

- All four KVS streams (`v2x-backend-cam-ch1..4`) stopped receiving
  fragments at **2026-07-14 14:13 UTC**. The streams still exist; the
  session API returns `video_session_unavailable` /
  `ResourceNotFoundException` (no live media), and the coverage API shows
  zero fragments since the onset.
- ROOT CAUSE (confirmed 2026-07-16 via CloudTrail + on-site probes): the
  producer is a separate **PeMS camera server at 128.32.234.154**
  (campus subnet, no reverse DNS), pushing as IAM user
  `pems-rfs-camera-server`. Its last KVS call was **2026-07-14T14:01Z**
  with no auth errors and its access key still Active — then silence.
  The host is now completely dark (no ping, no open ports) even from
  inside the campus network, so **the machine itself is down** and needs
  an on-site power cycle or campus IT intervention. Note `path-rfs-1`
  (fixed-128-32-129-4.path.berkeley.edu) is a drive-stack host, not the
  producer; it stays healthy.
- The perception service handled the outage exactly as designed: no crash,
  no restart, bounded reconnects (6,500+ on ch1 by 07-16), fail-closed
  health, sanitized errors. This validates the PR 48–52 lifecycle work.
- The outage ran **2+ days unnoticed** — there was no alerting. Fixed
  2026-07-16 (see M1).
- Separately, the PR 54 24-hour upload watch (2026-07-13) failed at phase-4
  round 67 only because a twin client switched to replay mode mid-sample
  (`twin_status.mode=="replay"`; round 66 was identical and clean). The
  watch treated operator presence as a system failure. Fixed 2026-07-16
  (see M3).

## Milestones

### Phase 0 — Restore signal and never lose it silently

- **M0 — Cameras back online.** BLOCKED-EXTERNAL: needs a login on
  `path-rfs-1` to restart/re-credential the producer. Exit:
  `/video/session/ch1..4` return 200; perception `/health` shows all four
  cameras fresh with `exact_same_session_pts`; `verify_live_feeds.py`
  passes locally and through the public tunnel.
- **M1 — Feed-loss alerting.** DONE (local layer, 2026-07-16): debounced
  `v2x-alert@` notifier units + OnFailure/OnSuccess drop-ins on
  `v2x-perception-link-health.service` and `v2x-perception.service`,
  installed via `scripts/install-feed-alerts.sh`, verified end-to-end
  against the live outage. Optional webhook via `/etc/v2x-alerts.env`.
  REMAINING: run `infra/aws-cli/provision-feed-alarms.sh` with an
  authorized principal (rfs-v2x-service cannot create SNS topics /
  CloudWatch alarms) so producer silence alarms independently of the
  Path PC.

### Phase 1 — Close out the July 13 campaign

- **M2 — PR 54 twin-spawn parity canary passes.** Blocked by M0 (needs
  live detections to spawn twin actors).
- **M3 — 24-hour upload watch passes.** Harness fixed (2026-07-16):
  `scripts/upload-watch.sh` classifies operator activity (active session
  or twin replay) as bounded "occupied" rounds instead of failures;
  initial/final samples get a grace window; health/feed/fingerprint
  strictness unchanged. Classification verified against the archived
  July 13 evidence (round 66 → clean, round 67 → occupied). Blocked by M0
  to actually run.
- **M4 — Latency baseline.** TOOLING DONE (2026-07-16):
  `apps/perception/tools/latency_baseline.py` reports p50/p95/max for
  decode / ingest / end-to-end from trusted schema-v2 records plus health
  decode latency; unit-tested. It runs automatically on feed recovery via
  `v2x-feed-recovery.timer` (`scripts/check-feed-recovery.sh`), which
  also re-runs the live-feed gate and an observational phase-4 probe and
  archives evidence — so M4's first capture and M2's observational
  precheck execute unattended the moment M0 lands. Mutating gates (the
  PR 54 parity canary `--apply` run, the long upload watch) remain
  operator-initiated per the controlled deployment doctrine.
  **FIRST BASELINE CAPTURED (2026-07-16, retroactively)** from 1,561
  persisted schema-v2 records of the Jul 13→14 live-upload window via
  `--historical` mode (`/detections/range` pagination): decode p50/p95/max
  = 4.55/6.22/9.92 s; ingest ≈ 0 s (second-resolution timestamps); end
  to end = 4.27/6.15/10.95 s. Evidence:
  `v2x-evidence/perception/20260716T231007Z-historical-latency-baseline/`.
  Consequence for M10: decode/transport alone exceeds a 5 s p95 twin
  budget — the target needs transport work (shorter fragments or a
  different consumer path), not just replacing the 5 s poller.

### Phase 2 — Localization accuracy ("confidently")

Calibration is the critical path: the operating skill declares current
per-camera calibration diagnostic-only (4–7 inconsistent points). Until
this phase lands, twin positions are unproven.

- **M5 — Per-camera intrinsics.** ≥10 accepted ChArUco/checkerboard images
  + 2 untouched holdouts per camera. Needs a site visit.
- **M6 — Surveyed extrinsics.** ≥12 globally identified correspondences
  per channel (8 fit + 4 holdout, 50% width / 30% height coverage);
  held-out RMSE/P95/max ≤ 10/16/24 px; road-geometry RMSE/max ≤ 6/12 px;
  correct topology/horizon. Needs a site visit (batch with M5).
- **M7 — Land the calibration campaign.** Reconcile open PR #21 and the
  ~40 commits in the codex calibration worktrees into reviewable PRs;
  deploy fitted poses behind the fail-closed gate. Exit: a staged static
  target at a measured location reported within ≤ 1.5 m at ≤ 50 m range on
  all four cameras.
- **M8 — Dynamic ground-truth run.** RTK-GPS-logged vehicle driven through
  the site; compare uploaded tracks to the GPS log. Exit: position RMSE
  ≤ ~2 m, heading error ≤ ~15°, no track-ID splits over a pass, recorded
  as an evidence directory.

### Phase 3 — Twin mirroring quality ("clearly")

- **M9 — Twin actor fidelity.** Correct blueprint per class, heading from
  track velocity, smoothing between updates, despawn hysteresis. (Done
  2026-07-16: poller "0 locations resolved" root-caused as expected
  actor-owned steady state; log line now reports the actor-owned count.)
  Exit: strict same-object twin gate (`verify_phase4_live.py --apply`)
  passes against live data.
- **M10 — Latency to target.** Budget first (proposed: event visible in
  twin ≤ 5 s p95), then cheapest levers: push detections over WebSocket
  instead of 5 s polling; shorter KVS fragments; only then transport
  changes. Exit: measured p95 ≤ target over a continuous hour vs the M4
  baseline.
- **M11 — Acceptance demo.** Staged real events (vehicle pass, pedestrian
  crossing, crossing paths) recorded side-by-side: annotated camera MJPEG
  vs twin render, plus replay correlation through the schema-v2 clocks.
  Exit: every staged event visibly mirrored within budget with stable
  identity; archived evidence bundle + screen recording.
  NOTE (2026-07-16): KVS retention defaults to 24 h
  (`provision-video-streams.sh`), which already expired the Jul 13–14
  footage and blocked replay-correlation evidence for that window
  (detections outlive video: DynamoDB TTL ≈ 7 days). Before M11, raise
  `RETENTION_HOURS` (e.g. 168) so demo footage survives review — small
  storage cost, run by an authorized principal.

### Phase 4 — Make it boring

- **M12 — 7-day green soak.** Uploads on, twin live, M1 alerting armed,
  daily automated verifier runs. Exit: 7 consecutive green days, zero
  silent outages, evidence auto-archived. Alongside: clear failed
  transient units, triage the ~21 unmerged codex branches, operator
  runbook (feed outage, CARLA restart, rollback).

## Sequencing

M0 blocks Phases 1 and 3 and M7's live exit. Independent of M0: M1's AWS
alarm provisioning, M7's code reconciliation, M9 fidelity work against
recorded data, and all hygiene items. M5/M6/M8 each need physical site
access — batch them into as few visits as possible; they are the long
pole. Biggest risks: field-site access, a possible hard latency floor in
KVS-HLS above the M10 target (decide budget vs transport early), and the
calibration bar, which may take more than one survey visit to pass.
