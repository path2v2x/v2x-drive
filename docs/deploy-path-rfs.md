# Deploying V2X Drive on path-rfs

## The one command

```bash
git push origin main
scripts/deploy.sh [--server] [--dry-run]
```

`scripts/deploy.sh` connects to `root@100.126.56.83` (Tailscale SSH) and, in
order:

1. fast-forwards `/home/path/v2x-drive` to `origin/main` as user `path`;
2. runs `npm ci` in `apps/drive-web` only when `package-lock.json` changed,
   then `npm run build`;
3. rsyncs `apps/drive-web/build/` to `/var/www/v2x-drive/` (the root-relative
   `static/config.json` ships with the build; nothing is patched on the host);
4. makes sure `/var/www/v2x-drive-data/{api,snapshots,demo-videos}` exist for
   the drive server's publications;
5. installs `scripts/ops/nginx/{v2x-drive-public,acme-http}.conf`, runs
   `nginx -t` and reloads nginx;
6. with `--server`, installs `scripts/systemd/v2x-drive.service` and restarts
   `v2x-drive` unless a drive session is connected on `:8765` (then it exits 2
   and asks you to retry later);
7. checks `https://path2v2x.net/` returns 200, `/config.json` carries
   `detectionsBaseUrl`, and `/detections/coverage` answers.

Rollback: `git revert` (or check out the previous commit on `main`), push, and
run the script again. The host never carries state that the script does not
rebuild.

Other repos have the same shape: `v2x-digital-twin/scripts/deploy.sh` deploys
the twin server, the perception service and the Studio UI.

## Production layout

Everything is served by nginx on path-rfs from the single origin
`https://path2v2x.net` (`www` redirects to the apex; `drive.path2v2x.net`
resolves to the same vhost). `twin.path2v2x.net` is the Digital Twin
(`path2v2x/v2x-digital-twin`). One Let's Encrypt certificate covers all four
names:

```bash
certbot certonly --nginx --cert-name drive.path2v2x.net --expand \
  -d path2v2x.net -d www.path2v2x.net -d drive.path2v2x.net -d twin.path2v2x.net
```

| Path on `path2v2x.net` | Backend |
| --- | --- |
| `/` | `/var/www/v2x-drive` (built site) |
| `/ws` | drive server `127.0.0.1:8765` |
| `/camera/` | MediaMTX LL-HLS `127.0.0.1:8888` |
| `/archive/` | MediaMTX playback `127.0.0.1:9996` |
| `/detections/` | twin server `127.0.0.1:8190/detections/` |
| `/data/` | `/var/www/v2x-drive-data` (drive server publications; `demo-videos/` is a JSON autoindex) |
| `/perception/…` | co-perception live viewer (jpark's `ws_broadcast_server`, `127.0.0.1:8766`) |

The production checkout is `/home/path/v2x-drive`. The CARLA 0.10 container is
`carla-rr-maps`; its Python environment is `/home/path/V2XCarla/carla-venv-310`.

| Unit | Repository path used |
| --- | --- |
| `v2x-drive.service` | repository root, `scripts/wait-for-carla.sh`, `scripts/launch-drive.sh` |
| `v2x-drive-watchdog.service` / `.timer` | `scripts/ops/v2x-drive-watchdog.sh` |
| `v2x-nightly-restart.service` / `.timer` | `scripts/ops/v2x-nightly-restart.sh` |
| `v2x-firewall.service` | `scripts/ops/v2x-firewall.sh` |
| `v2x-carla-event-logger.service` | `scripts/ops/v2x-carla-event-logger.sh` |
| `mediamtx.service` / `v2x-camera-relay@.service` / `v2x-archive-guard.timer` | `scripts/ops/camera-relay/` |

`v2x-drive-watchdog.timer` runs every two minutes. The nightly timer runs at
04:00 local time and skips its restart while a client is connected to `:8765`.

## First-time install

Run as root after cloning the checkout as `path`:

```bash
cd /home/path/v2x-drive
install -m 0644 scripts/systemd/* /etc/systemd/system/
chmod +x scripts/ops/*.sh
systemctl daemon-reload
systemctl enable --now \
  v2x-drive.service \
  v2x-drive-watchdog.timer \
  v2x-nightly-restart.timer \
  v2x-firewall.service \
  v2x-carla-event-logger.service
ln -s /etc/nginx/sites-available/v2x-drive-public /etc/nginx/sites-enabled/
ln -s /etc/nginx/sites-available/acme-http /etc/nginx/sites-enabled/
scripts/ops/camera-relay/install.sh
```

then run `scripts/deploy.sh --server` from a workstation.

The drive server publishes `api/state.json` every 5 s, `api/map-data.json`
after each map export, and object snapshots under `snapshots/` to
`/var/www/v2x-drive-data` (config `DTB_PUBLISH_DIR`, URL base
`DTB_PUBLISH_BASE_URL=/data`). It reads detections from the twin server at
`DTB_DETECTIONS_HISTORY_URL=http://127.0.0.1:8190/detections/history`.

## Camera relay

The raw Richmond Field Station feeds are copied from the demux Unix sockets
into MediaMTX; the relay never opens a second camera RTSP session. Install or
update the relay as root:

```bash
cd /home/path/v2x-drive
scripts/ops/camera-relay/install.sh
```

MediaMTX binds RTSP to `127.0.0.1:8554`, low-latency HLS to
`127.0.0.1:8888`, and recording playback to `127.0.0.1:9996`. The checked-in
firewall drops new external connections to all loopback service ports.

MediaMTX records 15-minute fMP4 segments beneath
`/mnt/archive/v2x-camera/recordings` on the second NVMe and removes them after
72 hours (3 days). Mount the existing ext4 filesystem before running the camera
installer:

```text
UUID=aa97b6ff-ab06-42e5-a597-7613dae0376f /mnt/archive ext4 defaults,nofail,noatime 0 2
```

Create `/mnt/archive`, add that line to `/etc/fstab`, run `mount -a`, then create
the recordings directory owned by `v2x-camera:v2x-camera` with mode `0750`.
The measured four-camera rate is about 260 GB/day, so 72 hours requires about
780 GB. The `v2x-archive-guard.timer` runs every 10 minutes; if free space on
`/mnt/archive` falls below 60 GB, it deletes the oldest `.mp4` segments across
all channels until 80 GB is free. The guard is restricted to the recordings
directory.
