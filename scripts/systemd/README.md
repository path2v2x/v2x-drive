# Path PC V2X service deployment

These definitions describe the accepted Path PC runtime. They are deployment
artifacts, not evidence that the installed units already match them.

The production simulator is the packaged Unreal Engine 5.5 worker container
`carla-rr-maps`; its runtime reports `5.5.0-0+UE5`. Unreal Engine 6 work is a
separate task and must not supply code, processes, ports, or acceptance evidence
to this V2X deployment. UE6 work must never stop, hold, delay, restart, or
reconfigure V2X services or timers; if it cannot coexist, stop the UE6 task and
use a separately authorized maintenance task.

Until the image is published with an immutable registry digest and engine OCI
labels, the Path PC gate pins local image ID
`sha256:8e7c7152f86a9e26878de5f280514f224290f70aad7b28d00d5087709504118e`
and shipping-binary SHA-256
`d9d8cafc10def42557cdfc2897f9581da45c4900dc82c3ff37f2c5e2e7b98b23`.

| Unit | Responsibility | Accepted origin/runtime |
|---|---|---|
| `v2x-carla-rr.service` | supervises the pre-provisioned UE5.5 simulator worker | `carla-rr-maps`, `ghcr.io/simforgeinc/carla-rr-maps:0.10.0`, `5.5.0-0+UE5`, NVIDIA runtime, bridge ports 2000-2002 |
| `v2x-drive.service` | Drive WebSocket bridge | CARLA Python 3.10, `0.0.0.0:8765` |
| `v2x-web.service` | Vite dashboard | `0.0.0.0:5173`; runtime config only, no browser-local Drive override |
| `v2x-perception.service` | four-camera HLS inference and MJPEG/health API | `0.0.0.0:8090` |
| `v2x-cloudflared-drive.service` | Drive transport | `http://localhost:8765` |
| `v2x-cloudflared-perception.service` | perception transport | `http://localhost:8090` |
| `v2x-drive-link-health.timer` | public config-overlay and real WebSocket handshake check | read-only unless repair is explicitly enabled |
| `v2x-perception-link-health.timer` | four-feed health and public perception endpoint parity | release repair is double-gated and rate-limited |
| `v2x-hourly-drive-restart.timer` | guarded simulator/bridge restart | skips healthy active sessions; never creates/replaces the container by default |

Do not use the retired `carla-custommaps`, `carla-rfs`, CARLA 0.9.16,
`CarlaUE4.sh`, or `/home/path/V2XCarla/carla-venv` definitions. Do not use
`/home/path/V2XCarla/CarlaUE6`, `/home/path/V2XCarla/UnrealEngine_6`, `ue6-*`
user units, or ports 2100-2102 from this deployment workflow.

## Controlled deployment gate

Work from a tested commit in a clean worktree. The live checkout
`/home/path/V2XCarla/v2x-backend` may contain active Teleport work; capture and
reconcile it before copying any source. Do not leave a script or unit that
exists only in the live checkout.

Before installing anything, confirm that no Drive session is active, then stop
all mutation-capable timers:

```bash
sudo systemctl stop \
  v2x-drive-link-health.timer \
  v2x-perception-link-health.timer \
  v2x-hourly-drive-restart.timer
```

Capture a new rollback bundle in addition to the existing repository backup.
The tracked helper is plan-only by default, refuses capture while any
mutation-capable timer is active, records all three perception model assets,
and can rehearse repository restoration in an isolated clone:

```bash
scripts/capture-v2x-rollback.sh
ACTION=capture scripts/capture-v2x-rollback.sh
ACTION=verify BUNDLE=/home/path/V2XCarla/v2x-backend-backups/v2x-rollback-<UTC> \
  scripts/capture-v2x-rollback.sh
```

The commands below document the bundle contents and remain useful for manual
inspection. Do not stash the live checkout until the tracked bundle has passed
`ACTION=verify`:

```bash
set -euo pipefail
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
rollback="/home/path/V2XCarla/v2x-backend-backups/systemd-${stamp}"
install -d -m 0700 "$rollback"
git -C /home/path/V2XCarla/v2x-backend status --short --branch >"$rollback/live-git-status.txt"
git -C /home/path/V2XCarla/v2x-backend rev-parse HEAD >"$rollback/live-head.txt"
git -C /home/path/V2XCarla/v2x-backend diff --binary >"$rollback/live-unstaged.patch"
git -C /home/path/V2XCarla/v2x-backend diff --cached --binary >"$rollback/live-staged.patch"
git -C /home/path/V2XCarla/v2x-backend ls-files --others --exclude-standard -z \
  >"$rollback/untracked-files.zlist"
tar -C /home/path/V2XCarla/v2x-backend \
  --null --verbatim-files-from --no-recursion \
  --files-from="$rollback/untracked-files.zlist" \
  -cpf "$rollback/untracked-files.tar"
docker inspect carla-rr-maps >"$rollback/carla-rr-maps.inspect.json"
sha256sum /home/path/V2XCarla/v2x-backend/apps/perception/yolov8n.pt \
  >"$rollback/perception-model-sha256.txt"
sha256sum /home/path/.cache/torch/hub/checkpoints/mobilenet_v3_small-047dcff4.pth \
  >"$rollback/perception-mobilenet-sha256.txt"
install -m 0600 /home/path/V2XCarla/v2x-backend/apps/perception/yolov8n.pt \
  "$rollback/yolov8n.pt"
/home/path/V2XCarla/perception-venv/bin/python --version \
  >"$rollback/perception-python-version.txt" 2>&1
/home/path/V2XCarla/perception-venv/bin/python -m pip freeze \
  >"$rollback/perception-pip-freeze.txt"
/home/path/V2XCarla/perception-venv/bin/python -m pip check \
  >"$rollback/perception-pip-check.txt"
systemctl cat \
  v2x-carla-rr.service v2x-drive.service v2x-web.service v2x-perception.service \
  v2x-cloudflared-drive.service v2x-cloudflared-perception.service \
  v2x-drive-link-health.service v2x-drive-link-health.timer \
  v2x-perception-link-health.service v2x-perception-link-health.timer \
  v2x-hourly-drive-restart.service v2x-hourly-drive-restart.timer \
  >"$rollback/installed-units.txt" 2>&1 || true
sha256sum /etc/systemd/system/v2x-* >"$rollback/installed-unit-sha256.txt" 2>/dev/null || true
cp -a /etc/v2x-drive-tunnel.env /etc/v2x-perception-tunnel.env \
  /etc/v2x-drive-link-health.env /etc/v2x-perception-link-health.env \
  /etc/v2x-drive-restart.env \
  "$rollback/" 2>/dev/null || true

# The patch/tar files above are the authoritative content backup. Stash the
# tracked and untracked live checkout only after they exist, then prove that
# reconciliation starts clean. Never reset or discard the live-only work.
git -C /home/path/V2XCarla/v2x-backend stash push --include-untracked \
  -m "v2x-controlled-deploy-${stamp}"
git -C /home/path/V2XCarla/v2x-backend stash list --format='%H %gd %s' \
  >"$rollback/stash-list-after.txt"
git -C /home/path/V2XCarla/v2x-backend status --porcelain=v1 \
  >"$rollback/live-status-after-stash.txt"
test ! -s "$rollback/live-status-after-stash.txt"
```

Static validation before install:

```bash
bash -n infra/aws-cli/provision-read-api.sh infra/amplify/deploy.sh scripts/*.sh
shellcheck -x infra/aws-cli/provision-read-api.sh infra/amplify/deploy.sh scripts/*.sh
scripts/tests/test-recovery-infra.sh
scripts/tests/test-rollback-bundle.sh
scripts/run-carla-rr.sh validate
```

`run-carla-rr.sh validate` is read-only. The service refuses an absent,
wrong-image, non-NVIDIA, wrong-command, wrong-network, or wrong-port container.
Creation and replacement are intentionally unavailable at boot. Only a
controlled deployment may use `ALLOW_CARLA_CREATE=true` or
`ALLOW_CARLA_RECREATE=true` with `restart-drive-stack.sh` after the rollback
capture.

The recovered Vite process on `:5173` was an unmanaged `setsid` process with
`VITE_DRIVE_WS_URL=ws://localhost:8765`. That value resolves inside each
browser, not on the Path PC, and must not survive deployment. Record the old
PID/start time, stop it only at the `v2x-web.service` cutover, and require the
new unit to bind `:5173`. The tracked unit uses `UnsetEnvironment` for both
legacy Drive build overrides; `/config.json` and `/drive-config` choose the
browser endpoint.

`apps/perception/yolov8n.pt` is an intentionally ignored runtime asset. Preserve
it across source reconciliation and compare it with the captured hash. Preserve
and hash the cached MobileNetV3 tracking weights too. The tracked perception
unit refuses to start unless the Python 3.12 runtime, YOLO model, and MobileNet
cache are readable/executable; restore the captured model and matching Python
environment evidence before rolling back the service.

The recovered perception Cloudflare process is also unmanaged. Use a
blue/green Quick Tunnel cutover while the perception repair timer is stopped:
record the old PID/URL, start and validate the supervised tunnel alongside it,
publish the new URL, refresh public `/live`, and only then stop the old process.
Do not kill the old tunnel before public config switches. After cutover require
exactly one cloudflared process targeting `localhost:8090`; stopping either
Quick Tunnel invalidates that process's hostname.

The perception unit deliberately uses `KillMode=mixed`, not `process` or the
default `control-group`. On a normal stop or restart, systemd sends SIGTERM only
to the Python main process so its bounded owner cleanup can terminate and reap
each FFmpeg FIFO writer before releasing the corresponding OpenCV capture. If
Python crashes, exits with a surviving child, or fails to finish within
`TimeoutStopSec=60`, systemd's subsequent SIGKILL covers the entire service
cgroup before `Restart=always` starts a replacement. This preserves cooperative
writer-first teardown without allowing decoder children to escape the unit.

Install the source-controlled scripts into the reconciled live checkout first,
then install units:

```bash
sudo install -m 0644 scripts/systemd/v2x-carla-rr.service /etc/systemd/system/
sudo install -m 0644 scripts/systemd/v2x-drive.service /etc/systemd/system/
sudo install -m 0644 scripts/systemd/v2x-web.service /etc/systemd/system/
sudo install -m 0644 scripts/systemd/v2x-perception.service /etc/systemd/system/
sudo install -m 0644 scripts/systemd/v2x-perception.env.example /etc/v2x-perception.env
sudo install -m 0644 scripts/systemd/v2x-cloudflared-drive.service /etc/systemd/system/
sudo install -m 0644 scripts/systemd/v2x-cloudflared-perception.service /etc/systemd/system/
sudo install -m 0644 scripts/systemd/v2x-drive-link-health.service /etc/systemd/system/
sudo install -m 0644 scripts/systemd/v2x-drive-link-health.timer /etc/systemd/system/
sudo install -m 0644 scripts/systemd/v2x-perception-link-health.service /etc/systemd/system/
sudo install -m 0644 scripts/systemd/v2x-perception-link-health.timer /etc/systemd/system/
sudo install -m 0644 scripts/systemd/v2x-hourly-drive-restart.service /etc/systemd/system/
sudo install -m 0644 scripts/systemd/v2x-hourly-drive-restart.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable \
  v2x-carla-rr.service v2x-drive.service v2x-perception.service v2x-web.service \
  v2x-cloudflared-drive.service v2x-cloudflared-perception.service

# 1. Adopt the already-running, validated RR container without restarting it.
sudo systemctl start v2x-carla-rr.service
scripts/run-carla-rr.sh validate

# 2. Active services do not consume new unit/source merely because they were
# enabled. Restart Drive and staged perception explicitly and gate each layer.
sudo systemctl restart v2x-drive.service
systemctl is-active --quiet v2x-drive.service
sudo systemctl restart v2x-perception.service
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8090/health \
      | jq -e '.status == "ok" and .ready == true' >/dev/null; then
    break
  fi
  sleep 5
done
curl -fsS http://127.0.0.1:8090/health \
  | jq -e '.status == "ok" and .ready == true'

# 3. Stop only the exact unmanaged Vite PID captured in the rollback bundle,
# then start the strict-port supervised web service.
test -n "${RECOVERED_VITE_PID:?set the captured unmanaged Vite PID}"
sudo kill -TERM "$RECOVERED_VITE_PID"
for _ in $(seq 1 30); do
  ! kill -0 "$RECOVERED_VITE_PID" 2>/dev/null && break
  sleep 1
done
! kill -0 "$RECOVERED_VITE_PID" 2>/dev/null
sudo systemctl start v2x-web.service
systemctl is-active --quiet v2x-web.service
```

Do not restart either recovered Quick Tunnel in the service cutover. The
existing Drive unit/process keeps its current public hostname until
`/drive-config` can publish a replacement atomically. In particular, do not run
`systemctl restart v2x-cloudflared-drive.service` during adoption.

For perception, keep the captured unmanaged PID and URL alive, then start the
new supervised process in parallel:

```bash
sudo systemctl start v2x-cloudflared-perception.service
systemctl is-active --quiet v2x-cloudflared-perception.service
new_perception_url="$(grep -Eo 'https://[A-Za-z0-9-]+\.trycloudflare\.com' \
  /tmp/v2x-perception-cloudflared.log | tail -n 1)"
test -n "$new_perception_url"
/home/path/V2XCarla/perception-venv/bin/python \
  apps/perception/tools/verify_live_feeds.py "$new_perception_url"
```

Publish that URL, wait for the Amplify release to succeed, hard-refresh public
`/live`, and require four-feed Computer Use evidence. Only then terminate the
exact captured old perception PID. Verify that exactly one cloudflared process
targets `localhost:8090`. If any gate fails, stop the new supervised service and
keep the old process/public configuration intact.

The first staged perception start keeps uploads disabled through the required
tracked `/etc/v2x-perception.env` gate. The unit deliberately does not set the
same variable, so this file is the single effective source:

```text
V2X_PERCEPTION_UPLOAD=false
```

Only after local and public ch1-ch4 freshness plus changing-frame proof should
the controlled deployment set that value to `true` and restart perception.
Then require a newly ingested DynamoDB record before accepting upload parity.

Enable timers only after local and public acceptance passes. The Drive timer
also requires `/drive-config` route/object/rollback acceptance before its repair
gate is installed:

```bash
sudo install -m 0600 scripts/systemd/v2x-drive-link-health.env.example \
  /etc/v2x-drive-link-health.env
sudo systemctl enable --now \
  v2x-drive-link-health.timer \
  v2x-perception-link-health.timer \
  v2x-hourly-drive-restart.timer
```

## Tunnel modes and endpoint publication

Both tunnel units default to Quick Tunnel mode to match the recovered runtime.
Their logs are truncated at each supervised start so a publisher cannot select
an endpoint from a dead process. Quick hostnames are process-scoped; a named
tunnel is the durable target.

Drive overrides live in `/etc/v2x-drive-tunnel.env`; perception overrides live
in `/etc/v2x-perception-tunnel.env`. Supported values for
`DRIVE_TUNNEL_MODE` are `quick`, `named-config`, and `named-token`. Token mode
requires a `TUNNEL_TOKEN_FILE`; tokens are never passed in process arguments.
Systemd is the sole parser of `/etc/v2x-*-tunnel.env`; the launcher never
sources those root-owned files as shell code. The token file itself must remain
non-world-readable while being readable by `User=path`, for example
`root:path` mode `0640` or `path:path` mode `0600`. Example perception
named-tunnel plan:

`LOG_FILE` in either tunnel environment file is the authoritative log path for
systemd cleanup and the launcher. Perception health/publication derives
`PERCEPTION_LOG_FILE` from that same value unless an explicit
`PERCEPTION_LOG_FILE` override is supplied.

```bash
PLAN_ONLY=true \
TUNNEL_NAME=v2x-perception \
DRIVE_HOSTNAME=perception.path2v2x.net \
ORIGIN_SERVICE=http://localhost:8090 \
CONFIG_OUTPUT=/etc/cloudflared/v2x-perception.yml \
ENV_OUTPUT=/etc/v2x-perception-tunnel.env \
scripts/provision-cloudflare-drive-tunnel.sh
```

The provisioner validates generated ingress, refuses to overwrite an existing
different CNAME unless `OVERWRITE_DNS=true` is explicitly approved, and has a
read-only `PLAN_ONLY=true` mode.
For named perception mode, the link-health unit also loads
`/etc/v2x-perception-tunnel.env` and derives `https://$PUBLIC_HOSTNAME`. An
explicit `PERCEPTION_PUBLIC_URL=https://perception.path2v2x.net` in
`/etc/v2x-perception-link-health.env` is also supported.

The Drive endpoint is a short-lived, versioned S3 overlay served by
`GET /drive-config`. Plan and publish with an optimistic version gate:

```bash
ACTION=plan scripts/publish-drive-tunnel-config.sh
ACTION=publish EXPECTED_CURRENT_VERSION=<observed-version> \
  scripts/publish-drive-tunnel-config.sh
```

Every replacement first makes a conditional backup, then uses an S3
`If-Match`/`If-None-Match` write. Rollback republishes a selected backup as a
new monotonically increasing version:

```bash
ACTION=rollback EXPECTED_CURRENT_VERSION=<observed-version> \
ROLLBACK_BACKUP_KEY=<api/drive-config-backups/...json> \
  scripts/publish-drive-tunnel-config.sh
```

When the first publication observed that `api/drive-config.json` was absent,
the publisher writes a version-gated `drive-config-prior-absence` evidence
marker under the backup prefix. Rolling back to that marker does not physically
delete a now-audited object: it publishes a higher-version, explicitly expired
`tombstone=true` overlay. Browsers reject that overlay and use the static
configuration, while the audit/version chain remains intact.

Drive and perception endpoints embedded by Amplify are reconciled together by
`scripts/publish-amplify-runtime-config.sh`. It preserves the complete branch
environment, validates candidate endpoints, and saves a mode-0600 rollback
snapshot. Its default is a read-only plan. A branch-variable update does not
change the public site until a release succeeds:

```bash
# Read-only; requires both candidate endpoints to be healthy by default.
ACTION=plan scripts/publish-amplify-runtime-config.sh

# Preserve variables and stage endpoint values, but do not release stale repo source.
ACTION=publish START_RELEASE=false EXPECTED_CURRENT_HASH=<plan-hash> \
  scripts/publish-amplify-runtime-config.sh

# Only after canonical repository and Amplify IAM repair are proven.
ACTION=publish START_RELEASE=true EXPECTED_CURRENT_HASH=<plan-hash> \
  scripts/publish-amplify-runtime-config.sh

# Restore non-endpoint values while preserving current endpoint variables.
ACTION=rollback ROLLBACK_ENDPOINT_MODE=preserve-current \
ROLLBACK_ENV_FILE=<mode-0600-backup.json> START_RELEASE=false \
  scripts/publish-amplify-runtime-config.sh
```

A Quick Tunnel hostname cannot be rolled back after its process exits. Never
restore a saved `*.trycloudflare.com` value from an environment snapshot.
Restore non-endpoint variables with `preserve-current`, then run a normal
`ACTION=publish` against the currently supervised, healthy URL. Exact endpoint
rollback is accepted only with `ROLLBACK_ENDPOINT_MODE=exact-named`, and the
publisher rejects Quick Tunnel values in that mode.

`v2x-perception-link-health.timer` closes the Quick Tunnel durability gap. It
compares the current supervised tunnel URL with public `config.json`, requires
fresh/streaming ch1-ch4 plus successful stream HEAD requests, and does nothing
mutating by default. After the canonical Amplify repository can clone/build and
release successfully, enable automatic repair in
`/etc/v2x-perception-link-health.env`:

```text
PERCEPTION_LINK_HEALTH_REPAIR=true
AMPLIFY_RELEASE_ENABLED=true
MIN_RELEASE_INTERVAL_SECONDS=1800
```

The two independent booleans prevent pre-repair releases. A state-directory
lock prevents concurrent repairs, every attempt enters a 30-minute cooldown
(including failures), the shared publisher saves rollback environment JSON,
and success is recorded only after public `config.json` converges. Prefer a
named perception tunnel so routine restarts need no Amplify release.

After `GET /drive-config`, first publication, public overlay selection, and a
real WebSocket handshake all pass, enable Drive overlay repair and only then
start its timer:

```bash
sudo install -m 0600 scripts/systemd/v2x-drive-link-health.env.example \
  /etc/v2x-drive-link-health.env
sudo systemctl enable --now v2x-drive-link-health.timer
```

Quick mode derives the live hostname from the tunnel log. Named mode loads
`/etc/v2x-drive-tunnel.env` and derives `wss://$PUBLIC_HOSTNAME`; an explicit
`DRIVE_WS_URL` in the link-health file takes precedence. Keep repair disabled
until the route/object rollback gate has passed.

### Rolling detection corpus export

`v2x-detection-corpus-export.timer` is an optional read-only hourly snapshot of
the public 24-hour detection window. It atomically writes sanitized range pages,
timeline reconciliation, canonical NDJSON, and SHA-256 manifests under
`/home/path/V2XCarla/v2x-evidence/detection-corpus`. It never retains URL query
strings and its output is explicitly ineligible as calibration truth because
GPS/CARLA positions are derived from the active camera model.

Test the tracked exporter manually before installing either unit:

```bash
/home/path/V2XCarla/perception-venv/bin/python \
  apps/perception/tools/export_detection_corpus.py \
  https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com \
  /home/path/V2XCarla/v2x-evidence/detection-corpus
```

Only after the snapshot manifest reconciles with `/detections/timeline`:

```bash
sudo install -m 0644 scripts/systemd/v2x-detection-corpus-export.service \
  /etc/systemd/system/
sudo install -m 0644 scripts/systemd/v2x-detection-corpus-export.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now v2x-detection-corpus-export.timer
```

This timer does not authorize frame extraction, retention-policy changes, or
deployment of a fitted calibration. The tracked unit applies a fixed 72-snapshot
(three-day) retention bound, `UMask=0077`, a 15-minute runtime ceiling, and a
write allowlist limited to the corpus root. Copy any snapshot selected for
review or holdout to separately retained immutable evidence before it ages out.
Changing that policy remains a separate gate.

## Acceptance evidence

Require all of the following before restoring timers:

```bash
systemctl is-active \
  v2x-carla-rr.service v2x-drive.service v2x-web.service v2x-perception.service \
  v2x-cloudflared-drive.service v2x-cloudflared-perception.service
scripts/run-carla-rr.sh validate
/home/path/V2XCarla/carla-venv-310/bin/python - <<'PY'
import carla
c = carla.Client("127.0.0.1", 2000)
c.set_timeout(20)
print(c.get_client_version(), c.get_server_version(), c.get_world().get_map().name)
PY
curl -fsS http://127.0.0.1:8090/health | jq .
/home/path/V2XCarla/perception-venv/bin/python \
  apps/perception/tools/verify_live_feeds.py http://127.0.0.1:8090
curl -fsS http://127.0.0.1:5173/config.json | jq .
systemctl show v2x-web.service --property=Environment,UnsetEnvironment
DRIVE_LINK_HEALTH_REPAIR=false scripts/check-drive-frontend-link.sh
```

Perception passes only when `/health` reports `status=ok`, `ready=true`, and
fresh/streaming ch1-ch4 across repeated samples; HTTP 200 or MJPEG bytes alone
are insufficient. Acceptance records use schema-v2 trusted HLS
`EXT-X-PROGRAM-DATE-TIME`: `timestamp_utc` must equal
`media_timestamp_utc`, `media_time_trusted=true`, and the persisted media-clock
anchor/position must reconstruct that timestamp. Wall-clock fallback is
untrusted and cannot support archive replay or calibration. Run
`verify_live_feeds.py` against both the
local origin and browser-selected public HTTPS origin; it samples timestamps
twice and compares two complete JPEG hashes for ch1-ch4. The link-health timer
is not a substitute. After uploads are explicitly enabled, require a new
DynamoDB event with close decode-receipt and ingestion timestamps.

## Rollback

On any failed layer gate, keep all mutation-capable timers stopped, restore that layer's unit,
environment, and source from the captured bundle, run `systemctl daemon-reload`,
and restart only the affected service. Restore the previous S3 overlay or
Amplify environment using the versioned publisher commands above. Do not
replace the RR container unless its pre-deployment inspect JSON, image ID, and
command are available. Re-enable timers only after the full acceptance sequence
passes on the restored version.
