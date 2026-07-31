# V2X Operator Runbook

Date: 2026-07-16. Covers the Path PC deployment (perception + drive/twin +
web) and the field producer. Companion docs:
`docs/twin-mirroring-roadmap.md` (milestones),
`.agents/skills/path-pc-carla/SKILL.md` (deployment doctrine).

## 1. Feed outage (all or some cameras stale)

Symptoms: `v2x-feed-stalled-*` CloudWatch alarms; `ALERT:
v2x-perception-link-health.service failed` in the journal (or webhook);
`/health` shows `status: degraded`, cameras `reconnecting`.

Checks, in order:

```bash
curl -s http://127.0.0.1:8090/health | jq '.status, .cameras[].state'
curl -s -o /dev/null -w '%{http_code}\n' \
  https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com/video/session/ch1
```

- Session API 200 but local health degraded → problem is on the Path PC:
  `journalctl -u v2x-perception -n 100`; restart only if wedged
  (`sudo systemctl restart v2x-perception` — KillMode=mixed, bounded stop).
- Session API 404 `video_session_unavailable` / `ResourceNotFoundException`
  → **the producer stopped pushing**. The producer runs on `path-rfs-1`
  (100.126.56.83, Tailscale). Log in there (Tailscale SSH; approval by the
  tailnet owner may be required), check the producer service and its AWS
  credentials, and restart it. All four channels stopping simultaneously
  usually means the producer process died or its credentials expired.
- After the producer returns, **everything downstream is automatic**:
  perception reconnects (≤ ~30 s), the link-health check passes and sends
  a one-time recovery notice, CloudWatch alarms clear, and
  `v2x-feed-recovery.timer` runs the post-recovery validation (feed gate,
  observational phase-4, 10-minute latency baseline) archiving evidence
  under `v2x-evidence/perception/*-post-recovery-validation/`.

## 2. Alerting reference

- Local: `v2x-alert@<unit>.service` fires from OnFailure drop-ins on
  `v2x-perception-link-health.service`, `v2x-perception.service`,
  `v2x-feed-recovery.service`, `v2x-daily-verify.service`. Debounce 1 h
  per unit (state in `/var/lib/v2x-alerts/`); one-shot recovery notice on
  next success. Optional webhook: `V2X_ALERT_WEBHOOK_URL` in
  `/etc/v2x-alerts.env` (Slack-compatible JSON).
- AWS: SNS topic `v2x-feed-alerts` (us-west-2), four alarms
  `v2x-feed-stalled-v2x-backend-cam-ch1..4` — Sum of
  `PutMedia.IncomingFragments` < 1 for 3×5 min, missing data breaching.
  Manage subscriptions with an authorized principal;
  re-provision via `infra/aws-cli/provision-feed-alarms.sh`.
- Install/repair the local layer: `scripts/install-feed-alerts.sh`
  (idempotent; backs up anything it replaces).

## 3. Perception service

```bash
systemctl status v2x-perception
journalctl -u v2x-perception -n 200 --no-pager
curl -s http://127.0.0.1:8090/health | jq .
```

- Config: `/etc/v2x-perception.env` (uploads gated by
  `V2X_PERCEPTION_UPLOAD`). Unit uses `KillMode=mixed` — never change to
  `control-group` (SIGTERMs FFmpeg children before Python teardown) or
  `process` (orphans children).
- Four-feed acceptance gate:
  `perception-venv/bin/python apps/perception/tools/verify_live_feeds.py http://127.0.0.1:8090`
  (run again through the public tunnel before declaring healthy).

## 4. Drive / CARLA stack

- `v2x-carla-rr.service` supervises the UE5.5 `carla-rr-maps` container
  (ports 2000-2002) by adoption — do not restart or recreate a healthy
  container just to add supervision (`docker wait` adoption per doctrine).
- `v2x-hourly-drive-restart.timer` restarts the drive stack hourly; the
  drive server is `v2x-drive.service` (WS :8765).
- Bounded observational regression:
  `carla-venv-310/bin/python apps/bridge/tools/verify_phase4_live.py`
  (never pass `--apply` outside an authorized mutation window with zero
  active sessions — it creates sessions and actors).

## 5. Long watches and baselines

- Upload watch (operator-initiated, needs healthy feeds):
  `scripts/upload-watch.sh` — 24 h by default; operator activity counts
  as bounded "occupied" rounds, not failures. Smoke run:
  `WATCH_DURATION_SECONDS=1800 WATCH_LABEL=upload-smoke scripts/upload-watch.sh`.
- Latency baseline:
  `perception-venv/bin/python apps/perception/tools/latency_baseline.py --duration 600`
  (also runs automatically once per feed recovery).
- Daily green check: `v2x-daily-verify.timer` archives a dated feed +
  phase-4 evidence directory every day at 15:00 UTC and alerts on failure
  — this is the M12 soak evidence stream.

## 6. Rollback

Per the controlled deployment gate in the operating skill: stop the three
repair/restart timers first; restore the previous artifact immediately
when an acceptance gate fails. Rollback bundles live under
`/home/path/V2XCarla/v2x-backend-backups/` (unit files replaced by the
alert installer are backed up there too, under `feed-alerts-<stamp>/`).
For Quick Tunnels, restore variables around the currently healthy
endpoint — never a dead saved URL. Re-enable timers only after the final
public/runtime checks pass.
