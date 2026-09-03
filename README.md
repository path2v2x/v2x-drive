# V2X Drive

V2X Drive is the CARLA 0.10 driving and V2X dashboard used at Richmond Field Station.
The canonical repository is [`path2v2x/v2x-drive`](https://github.com/path2v2x/v2x-drive).

## Components

| Path | Purpose |
| --- | --- |
| `apps/drive-server` | Python `digital_twin_bridge` WebSocket drive server and CARLA integration |
| `apps/drive-web` | SvelteKit site served at [path2v2x.net](https://path2v2x.net) |
| `apps/dev-console` | Local developer console for the drive WebSocket API |
| `scripts/deploy.sh` | The one deploy command (see below) |
| `scripts/ops/nginx` | Tracked nginx vhosts for path-rfs |
| `scripts/ops/camera-relay` | MediaMTX camera relay, 72 h recording archive, archive guard |
| `scripts/systemd` | Units for CARLA, the drive server, watchdog, nightly restart |

## One host

Everything runs on path-rfs (128.32.129.4) and is served by nginx from one
origin, `https://path2v2x.net` (`www` redirects to the apex;
`drive.path2v2x.net` is an alias for the same vhost):

| Path | Backend |
| --- | --- |
| `/` | static build of `apps/drive-web` in `/var/www/v2x-drive` |
| `/ws` | drive server (`apps/drive-server`, `:8765`) |
| `/camera/` | MediaMTX low-latency HLS, live cameras |
| `/archive/` | MediaMTX playback server, 72 h (3 day) recording archive |
| `/detections/` | twin server detection history (72 h SQLite, `path2v2x/v2x-digital-twin`, `:8190`) |
| `/data/` | files the drive server publishes: `api/state.json`, `api/map-data.json`, snapshots, demo videos |

The site's `static/config.json` is root-relative for that reason and ships with
the build; there is no per-environment configuration step. Local development
proxies those paths to the production host (`vite.config.ts`,
`DRIVE_PROXY_TARGET` overrides).

AWS holds only the Route 53 zone for `path2v2x.net`.

## Deploying

```bash
git push origin main
scripts/deploy.sh            # site + nginx
scripts/deploy.sh --server   # also restart the drive server (refuses while someone is driving)
scripts/deploy.sh --dry-run  # print the plan
```

The script fast-forwards `/home/path/v2x-drive` on path-rfs, builds the site,
publishes it, installs the tracked nginx vhosts and checks health. Details and
rollback: [`docs/deploy-path-rfs.md`](docs/deploy-path-rfs.md).

## Runtime on path-rfs

CARLA runs in the `carla-rr-maps` Docker container. The drive server connects
to CARLA on ports 2000-2002, listens for WebSocket clients on `:8765`, polls
the twin server's detection history for the dashboard's object registry, and
publishes `state.json`/`map-data.json` under `/var/www/v2x-drive-data`.

systemd supervises CARLA, the drive server and the drive-link watchdog. The
tracked restart timer runs the CARLA/drive restart at 04:00 local time.

## Video and detections

Perception is owned by the sibling
[`path2v2x/co-perception`](https://github.com/path2v2x/co-perception) repository
and runs on path-rfs from the local camera sockets (service and config in
`v2x-digital-twin`). Its detections are recorded by the twin server, which is
what the Timeline page reads.

Live and archived raw video are served locally from path-rfs by the copy-only
MediaMTX relay in `scripts/ops/camera-relay`. Public paths `ch1` through `ch4`
are available as low-latency HLS under `/camera/`, while the Timeline reads the
72-hour recording archive under `/archive/`. The archive lives on the second
NVMe at `/mnt/archive/v2x-camera/recordings`; a 10-minute guard deletes oldest
segments if free space drops below 60 GB until 80 GB is free. Camera video never
leaves path-rfs.

## Local development

```bash
make drive-web-install
make drive-web-dev
```

Run the drive server without CARLA:

```bash
make drive-server-install
make drive-server-dry-run
```

Run the developer console:

```bash
cd apps/dev-console
npm ci
npm run dev
```

To run against CARLA, activate a compatible CARLA Python environment and use:

```bash
./scripts/launch-drive.sh
```

## Related repositories

- [`path2v2x/v2x-digital-twin`](https://github.com/path2v2x/v2x-digital-twin) — standalone digital twin, detection history, perception service
- [`path2v2x/co-perception`](https://github.com/path2v2x/co-perception) — production multi-camera perception
