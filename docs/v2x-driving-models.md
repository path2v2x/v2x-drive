# V2X-reactive driving models

Server-owned vehicles that react to emergency vehicles the way drivers are
expected to: civilians pull over for an emergency vehicle (ERV) coming up
behind them with lights and siren and slow down past a stopped one; an ERV
model (ambulance first) drives with lights and siren, stops at controls,
waits for traffic to yield and passes on the left.

The models live in `apps/drive-server/digital_twin_bridge/v2x_behaviors/`.
The decision logic is pure Python (unit tested with plain numbers); the
CARLA binding uses the vendored CARLA 0.10 `agents` package (see
`apps/drive-server/agents/VENDOR.md`) for lane following and routing.

## What the vehicles know about emergency vehicles

The fleet keeps an **ERV tracker** fed by three sources:

* the V2X feed of the message viewer: the latest BSM per sender is decoded
  and its Part II `vehicleAlerts` (siren / light bar in use), `role` and
  `responseType` drive the track; position comes from the mapped CARLA actor
  (ghosts, scenario cars) or WGS-84 coordinates;
* CARLA light state: an emergency-blueprint vehicle (ambulance, fire truck,
  police car) with `Special1` on counts as lights and siren in use. This
  covers a human driving PATH-M-1 in the DT manual control (Ctrl+L) and
  works without a DT session;
* the fleet's own ERVs.

A track is *active* when siren and lights are on, *stationary* on
`responseType stationary` or after a minute below 5 km/h, and expires 15 s
after its last update.

## Civilian behaviour (CARMA / state statutes / FHWA, adapted to RFS)

```
NORMAL -> MONITOR (active ERV behind: 300 m or 60 s to pass)
       -> YIELD_PREP (150 m or 25 s: right blinker, slow a little)
       -> PULL_OVER (after 3 s of signalling, never within 2 s of a junction:
                     right edge of the lane plus up to 1 m of shoulder, or
                     stop in lane when the lane is narrower than 2.6 m)
       -> STOPPED_HOLD (hazards; up to 90 s)
       -> WAIT_CLEAR (ERV 30 m past; a second ERV keeps the car parked)
       -> MERGE_BACK (3 s later, left blinker, stays 91 m behind the ERV)
       -> NORMAL
Overrides: ERV in my lane with < 8 s to pass -> SLOW_IN_LANE (hazards,
limit - 16 km/h); two same-direction lanes and a 72 km/h limit -> MOVE_OVER
(lane change away from the ERV, limit - 16 km/h, no stop).
Stopped ERV ahead: PASS_STOPPED_ERV (max(8 km/h, limit - 32 km/h), shifted
0.5 m away, 150 m before to 50 m past), a lane change when there is a second
lane; a blocked single lane -> WAIT_BEHIND, then OPPOSING_PASS when the
oncoming lane is clear.
```

Car-following against whatever is ahead in the lane and the agent's own
obstacle detection run underneath every state. Civilians keep roaming: when
they reach their destination they pick a new random one, so they never park
on the road (an ERV that has arrived does park, which is the stopped-ERV
case for everyone else).

## ERV behaviour (ambulance profile)

```
EN_ROUTE (limit + 16 km/h, 0.3 m toward the centerline, siren + lights)
  -> APPROACH_JUNCTION (< 40 m: back to the limit)
  -> STOP_AT_CONTROL (stop sign, or a signal treated as red: full stop 1 s,
                      then only when conflicting traffic has stopped)
  -> CROSSING (10 km/h) -> EN_ROUTE
  -> FOLLOW_SLOW (slow lead within 30 m) -> WAIT_FOR_YIELD (1.6 s)
  -> PASS_LEFT_IN_LANE (lead moved right, 32 km/h) or
     OPPOSING_LANE_PASS (two-way road, oncoming lane clear, 45 m sight)
  -> NO_PROGRESS (stuck 10 s: siren off until the gap opens)
  -> ARRIVED (within 8 m of the destination: siren off, lights on, rear
              doors open, responseType stationary)
MANUAL: operator hold with siren / lights under manual control.
```

Fire profile: no opposing-lane pass, 2 s stops. Police: limit + 24 km/h,
`pursuit` response type.

## Protocol (drive WebSocket)

| Client → server | Server → client |
| --- | --- |
| `spawn_v2x_vehicle {kind: civilian\|erv, profile?, blueprint?, spawn, destination?, params?}` | `v2x_vehicle_spawned {vehicle, count}` |
| `despawn_v2x_vehicle {actor_id}` | `v2x_vehicle_despawned {actor_id, count}` |
| `despawn_v2x_vehicles` | `v2x_vehicles_despawned {count}` |
| `list_v2x_vehicles` | `v2x_vehicles {vehicles[], tracks[], stats}` |
| `set_erv_state {actor_id, siren?, lights?, arrived?, manual?, response_type?, destination?}` | `v2x_vehicle_state {vehicle}` |
| | `v2x_vehicle_event {actor_id, name, kind, from, to, reason, t}` pushed to every client |

`spawn` is `"random"`, `{ahead_of: "ego"|actor_id, distance_m}`,
`{behind: ...}`, `{spawn_index}`, `{x, y, yaw?}` or `{lat, lon}`; spots
within 6 m of another vehicle are skipped along the lane. `destination` is
`"random"` (default, a spawn point at least 150 m away with a route),
`"none"` (roam) or `{x, y}` / `{lat, lon}`. `params` overrides profile
numbers (`{"over_limit_kmh": 20}`).

Vehicles carry `drive_civ_<n>` / `drive_erv_<n>` role names: the drive
server owns them (clear NPCs removes them, the audit leaves them alone),
they survive browser reconnects, and `set_map` is refused while the fleet
is populated. Telemetry gains `v2x_fleet[]` (compact positions and states)
and `nearby_actors[].type` can be `civilian` or `erv`. The human driver gets
advisory toasts through `v2x_alerts` for an active ERV approaching from
behind and for a stopped ERV ahead.

Config (`DTB_FLEET_*`): `FLEET_ENABLED`, `FLEET_MAX_VEHICLES` (12),
`FLEET_DECISION_EVERY_N_TICKS` (2, decisions at 10 Hz), `FLEET_TICK_BUDGET_MS`
(20, decisions halve when the p95 tick time exceeds it),
`FLEET_LIGHT_SCAN_EVERY_N_TICKS` (10).

## Web

The **Fleet** button in the driving bar opens the panel: spawn an ERV
(ambulance / fire / police, behind the ego or at a random spot), spawn N
civilians ahead of the ego, a list with state badges and the ERV controls
(siren, lights, hold, arrived, new call), the emergency vehicles the tracker
sees, and the last events. Fleet vehicles are rose (ERV) and sky blue
(civilian) on the mini-map.

## Verification

* `cd apps/drive-server && python -m pytest` (`test_v2x_behaviors_core`,
  `test_civilian_fsm`, `test_erv_fsm`, `test_v2x_fleet`).
* `cd apps/drive-web && npm run check && npm test` (`v2x-fleet-protocol`,
  `V2xVehiclePanel`).
* On path-rfs with CARLA up:
  `python scripts/v2x_fleet_smoke.py --url ws://127.0.0.1:8765 --duration 150`
  starts a short ego session, spawns two civilians ahead and an ambulance
  behind, ends the session, and watches the fleet: it prints the state
  timeline per vehicle, the closest any two fleet vehicles got, and whether
  the civilians yielded.

## Known limits

* A car that pushes the throttle for 6 s without moving is wedged against
  map geometry (the RFS mesh has gates and kerbs the OpenDRIVE lanes run
  through): it is re-routed once, then moved to a free spawn point, and
  removed after three recoveries (each step is an event).
* Sight distance is approximated by the distance to the next junction.
* Oncoming traffic is only considered on the same road; a car entering the
  road from a side street during a pass is handled by the agent's obstacle
  stop, not by the pass planner.
* The PATH adapter encodes PATH BSM positions relative to the scenario
  origin, so external PATH vehicles are tracked through their CARLA actor
  (always present on this host) rather than their BSM position.
