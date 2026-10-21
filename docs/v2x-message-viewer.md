# V2X message viewer

Live J2735 (SAE V2X) messages per vehicle, straight from the VOICES
Distributed Testing (DT) stack that shares the CARLA world with the drive
server. The web app lists every vehicle that has a real V2X feed and opens one
draggable, resizable window per vehicle that streams its messages (Basic
Safety Messages first; any other J2735 type is shown by name and size).

Only vehicles that really publish are listed: the scenario vehicles our DT
adapter owns (PATH-M-1, PATH-BG-1..5), the other organizations' vehicles that
the adapter clones into our world as ghosts (VW-M-1, AMG-BG-n, UTC-BG-n, ...),
and the browser ego when it drives under one of the PATH scenario slots.
Web-app traffic, placed cars and HIL_Tool `bkg` traffic have no V2X messages
and are not listed. Nothing is synthesized.

The start screen's **VOICES** block (`PUBLISH` / `LOCAL` plus a slot picker,
default `PATH-BG-5`) gives the ego a scenario name for the session so the DT
adapter binds it and generates its BSM, which then flows back through the
TENA feed like any other vehicle. The picker shows who holds each slot
(`free`, your parked car, another browser, the fleet, the DT manual car or
background traffic). Ending the session parks the car instead of destroying
it, because the adapter binds a name once per DT session; the next session
under that slot adopts the same car. See "VOICES scenario slots" in
`v2x-driving-models.md` for the server side.

## Where the messages come from

```
dt-core container                          drive server (apps/drive-server)
┌──────────────────────────┐   TCP client   ┌───────────────────────────────┐
│ JSONstreamer (TENA sub.) ├───────────────►│ JsonStreamSource :8005        │
│  V2X, LandVehicle,       │  RS-delimited  │   ├─ V2xHub (fan-out, rates)  │
│  Scenario objects        │  JSON records  │   ├─ ScenarioRoster (names)   │
└──────────────────────────┘                │   └─ VehicleRegistry (CARLA)  │
┌──────────────────────────┐   UDP          │ UdpUperSource :5398 (fallback)│
│ TENA V2X adapter         ├───────────────►│ Codec (j2735_202409, pycrate) │
│  raw UPER MessageFrames  │                │ V2xViewerConn per WebSocket   │
└──────────────────────────┘                └──────────────┬────────────────┘
                                                           │ v2x_* JSON
                                                           ▼
                                          apps/drive-web  V2X windows
```

* **JSON streamer (primary).** DT's `JSONstreamer-VUG` subscribes to every
  TENA object/message and writes 0x1E-prefixed JSON records to a TCP listener.
  It is a *client*: the drive server listens on `127.0.0.1:8005` and the
  streamer connects to it. Records carry `metadata.type_name` and
  `attributes`; we keep `VUG::TV2XMsg::V2X` (raw UPER bytes in
  `binaryContent`, `senderIdentifier` = scenario vehicle name = CARLA
  `role_name`, `uuid`), `VUG::Entities::LandVehicle` (position,
  `specialLightsOn`) and `VUG::Configuration::Scenario` (the live roster).
* **UDP tap (fallback).** The TENA V2X adapter also emits every frame as a raw
  UPER datagram on `127.0.0.1:5398` with no sender. Frames are matched to a
  vehicle name through the BSM `coreData.id` ↔ scenario `bsmid` table and used
  only while the JSON stream is silent for that sender (3 s).
* **Decoder.** `j2735_202409` (USDOT FHWA STOL, Apache-2.0, the same package
  DT uses) on `pycrate`. pycrate objects are process-wide singletons, so every
  decode runs on one dedicated thread under a lock, with an LRU cache per
  frame. If the package is missing the server still routes raw frames and the
  UI shows hex plus a "decoder missing" badge.
* **Streamer keeper.** The streamer exits when it cannot reach the listener,
  so after a drive-server restart a running DT session has no streamer. Every
  20 s the server checks `docker exec dt-core pgrep -f jsonStreamer-VUG` and
  relaunches DT's own `start-json-streamer.sh` when needed (at most once a
  minute). Disable with `V2X_STREAMER_AUTOLAUNCH=0`.

### DT-side configuration (coordinate with Jack; the repo has local edits)

* `VUG_DOCKER_START_JSON_STREAMER=true` in `config/site_config/path_rfs_site*.config`
  so a DT session starts the streamer.
* `scripts/run_scripts/start-json-streamer.sh` runs the streamer with
  `-nogui -subscribe` (it needs no display; backup `.orig-pre-nogui`).
* In formal-event mode DT rewrites the streamer target address to the VPN IP;
  keep `VUG_STREAMER_BIND_IP=127.0.0.1` for this host.

### BSM quirks of the PATH adapter (2.4.1)

PATH vehicles encode `lat`/`long` relative to the scenario origin (values near
0,0; the UI labels them `origin-relative`), other organizations send WGS-84.
`msgCnt` is always 0 and `secMark` is whole seconds. Part II carries
`SpecialVehicleExtensions.vehicleAlerts` (`sirenUse`/`lightsUse`, driven by the
CARLA `Special1` light state, Ctrl+L in the DT manual control) and
`SupplementalVehicleExtensions.classDetails.role` (`ambulance`).

## Protocol (drive WebSocket)

| Client → server | Server → client |
| --- | --- |
| `v2x_list_vehicles` | `v2x_vehicle_list {version, ts, feed, vehicles[]}`; also pushed whenever the table changes while the client watches (list requested in the last 60 s or any subscription) |
| `v2x_subscribe {keys[], detail: compact\|full}` | `v2x_subscribed {keys[], detail, unknown[]}` |
| `v2x_unsubscribe {keys[]}` (no keys = all) | `v2x_subscribed {keys[]}` |
| `v2x_perspective_subscribe {radius_m?, interval_ms?, detail?: simple\|full}` | `v2x_perspective {active, radius_m, interval_ms, count, transmitting, vehicles[{key, name, kind, subkind, org, emergency, pos, yaw, speed_mps, tx{msg_type, t_rx, age_s, rate_hz, count, source, size, summary}, rx[{key, name, kind, org, emergency, distance_m, bearing_deg, rel_bearing_deg, msg_type, age_s, rate_hz, source, summary}], rx_count}]}` (the reply, then pushed every `interval_ms`) |
| `v2x_perspective_set {radius_m?, detail?}` | `v2x_perspective` (`detail: full` adds `decoded`, the whole J2735 body, to `tx` and each `rx`) |
| `v2x_perspective_unsubscribe` | `v2x_perspective {active: false}` |
| `v2x_decode {raw_hex}` | `v2x_decoded {raw_hex, msg_type, decoded, summary, error}` |
| | `v2x_messages {dropped, batch[]}` pushed at most 10 times a second, ≤ 400 rows |

A vehicle entry: `key` (role name, `bsm:<id>` for an unmatched UDP sender),
`actor_id`, `role`, `type_id`, `kind` (`voices` ours, `ghost` other org,
`drive` this server, `other`), `subkind` (`ego`, `scenario`, `traffic`,
`feed-only`, ...), `org`, `bsm_id`, `pos`, `yaw`, `speed_mps`, `emergency`,
`special_lights`, `feed {source: tena|udp|stale|none, rate_hz, count, age_s}`,
`in_world`.

A message row: `key, seq, t_rx, msg_type, source, sender, uuid, raw_hex, size,
summary, error, tx` and, with `detail: full`, `decoded` (the whole J2735
MessageFrame as JSON). The summary flattens the BSM core data to SI units
(`speed_mps`, `heading_deg`, `elev_m`, `lat`/`lon` in degrees, `brakes`,
`lights {siren, lights, multi, role}`, `coords`, `emergency`).

Server configuration (`config.py`, all overridable from the environment):
`V2X_FEED_ENABLED`, `V2X_JSON_STREAM_HOST/PORT` (8005, 0 disables),
`V2X_UDP_HOST/PORT` (5398, 0 disables), `V2X_SCENARIO_XML` (default: resolved
from `~/.dt_scenario_config` → `scenario_files/<VUG_SCENARIO_FILE>`),
`V2X_LOCAL_ORG` (PATH), `V2X_REGISTRY_EVERY_N_TICKS` (5), `V2X_PUSH_INTERVAL_S`
(0.1), `V2X_QUEUE_MAX` (2000 per connection, oldest dropped and reported),
`V2X_FEED_STALE_S` (3), `V2X_DECODE_CACHE` (4096), `V2X_STREAMER_AUTOLAUNCH`,
`V2X_STREAMER_CONTAINER` (dt-core).

## The vehicle perspective (what the page shows)

The right column of `/drive` and the lower part of the right column of `/editor`
(under the outliner) carry a permanent "V2X" block: one row per connected vehicle, that is every
vehicle with a real feed on TENA (our scenario cars, the browser ego when it
publishes, the other sites' ghosts). A row expands into two blocks that update
in place, never as a log:

* **sends**: the vehicle's own latest BSM as any receiver would decode it (rate,
  speed, heading, id, msgCnt, position, siren/lights, age, source, size).
* **hears**: the latest BSM of every other transmitting vehicle inside the
  reception radius, nearest first, with the distance between the two vehicles,
  where the sender sits relative to the vehicle's heading (ahead, behind, left,
  right) and the sender's speed. A vehicle never hears itself.

The block has two views: *simple* (speed, heading, distances) and *full*, which
adds every decoded BSM field and the raw J2735 body for what the vehicle sends and,
per heard sender on click, for what it hears. The choice is remembered per browser
and changes what the server includes (`detail` in the protocol).

The range (default 300 m, chosen from a list, remembered per browser) stands in for
radio range: TENA delivers every message everywhere, so the cut is applied on the
receiving side, like a real onboard unit that only hears what is in range. The
distance is measured between the CARLA actors (for ghosts that is the position the
adapter set from the sender's BSM), so it is the ground truth at the moment of the
snapshot. Snapshots are pushed twice a second.

## Using it (scripts)

* Lobby: **V2X MESSAGE VIEWER**. Driving: **V2X** in the bottom-left bar.
  Both toggle the floating vehicle list; it works with or without a drive
  session.
* The list header shows the feed health: `stream` (JSON streamer connected,
  records/s), `udp` (datagrams/s), `decoder ok|missing`, the scenario name.
  Rows show the feed dot (green TENA, amber UDP, grey stale), rate, org badge
  and an `ERV`/`LIGHTS` badge for emergency vehicles. A sender that goes
  silent stays listed as `stale` for a minute and then drops out unless its
  CARLA actor is still a scenario vehicle in this world. Tick a vehicle to open
  its window; **open all** opens a whole group.
* A stream window shows the latest BSM summary (id, speed, heading, lights,
  position), then the rows (time received, type, one-line summary). Click a
  row to expand the decoded body (compact mode asks the server to decode that
  frame once). **Pause/Resume**, **Clear**, **Raw** (hex under each row),
  **Full** (every row carries the decoded body), a type filter and **.jsonl**
  (download the 200 buffered rows).
* Drag by the title bar, resize at the bottom-right corner; positions persist
  per window in `localStorage` (`v2x-window:<id>`). **Esc** closes the
  topmost window before it ends a drive session. Windows close when the
  socket drops.

## Verification

* Server: `cd apps/drive-server && python -m pytest` (`test_v2x_codec`
  decodes golden frames recorded from the VOICES network, `test_v2x_feed`,
  `test_scenario_roster`, `test_v2x_registry`, `test_v2x_viewer`). Fixtures in
  `tests/fixtures/v2x/` (`*.hex` BSM frames, `streamer_sample.raw` 11 JSON
  streamer records).
* Web: `cd apps/drive-web && npm run check && npm test`
  (`v2x-viewer-protocol`, `window-manager`, `v2x-format`, `FloatingWindow`,
  `V2xVehicleListPanel`, `V2xStreamWindow`).
* On path-rfs: start a DT session, `bash ~/sumo-bg-demo.sh 300` for background
  traffic, open the viewer: PATH-M-1 (ambulance, `ERV`) and PATH-BG-1..5 at
  ~10 Hz over TENA; Ctrl+L in the DT manual control flips the siren/lights
  flags in PATH-M-1's rows; stopping the demo turns the rows stale and the
  list shrinks. Restart `v2x-drive` and watch the keeper bring the streamer
  back (`stream` turns green within ~20 s).

## Installing the decoder

`apps/drive-server/requirements.txt` pins `pycrate` and the `j2735_202409`
wheel (by commit). `scripts/deploy.sh` installs the requirements into the
service venv whenever `requirements.txt` changed; by hand:

```bash
/home/path/V2XCarla/carla-venv-310/bin/python -m pip install -r apps/drive-server/requirements.txt
```
