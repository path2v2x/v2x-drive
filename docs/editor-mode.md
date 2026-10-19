# Editor mode

`/editor` opens the shared CARLA world the way the Unreal editor shows a level:
a free camera you fly, an outliner of every actor in the world, and details of
the selected one. Nothing in it drives, spawns or deletes; the only thing an
editor session owns is its camera. The start screen of `/drive` has a Mode
choice (Driving / Editor), the driving control bar has "Switch to editor"
(the camera starts just behind the car) and the editor's Session section has
"Switch to driving here" (the next drive starts at the lane nearest the camera).

## Flying

Hold the right mouse button to look around (pointer lock, with a plain drag as
the fallback), `W A S D` fly along the view and sideways, `E` / `Q` go up and
down the world axis, `Shift` is four times faster, the wheel steps the speed
between 1 and 200 m/s, `F` jumps to a viewpoint behind the selected actor and
`Escape` releases the mouse or the selection. The camera is kept inside an
envelope 300 m around the map's spawn points. The spectator CARLA itself
provides is never touched: the DT scripts own it.

Follow puts the camera on a spring arm behind an actor; any fly input takes it
back as a free camera where the actor was. A followed actor that disappears
releases the camera on the next input.

## Outliner

Every actor, grouped: browser cars, VOICES vehicles (ours, with the owner:
session, fleet, a parked car of this server, or the DT stack), VOICES ghosts,
V2X fleet, traffic, dynamic actors, trajectory playback, other clients' cars
(HIL_Tool `bkg` traffic), walkers, props, sensors, signals, other. Rows show
the name, the role name and the distance from the camera. One click selects
(details load on the left), a double click focuses. The list is polled once a
second, capped at 400 rows.

## Protocol (drive WebSocket)

| Client → server | Server → client |
| --- | --- |
| `start_session {mode: "editor", pose?: {x, y, z, yaw, pitch}}` | `session_ready {mode: "editor", vehicle_id: null, camera}` |
| `camera_move {f, r, u, dyaw, dpitch, dt, speed?, boost?}` (axes in -1..1, degrees, ≤ 20 Hz, only when something changed) | `camera_pose {x, y, z, yaw, pitch, speed, following}` |
| `camera_focus {actor_id}` | `camera_pose` |
| `camera_follow {actor_id \| null}` | `camera_pose` |
| `list_world_actors {max}` | `world_actors {actors[], total, truncated, groups, following}` |
| `inspect_actor {actor_id}` | `actor_details {…row, attributes, bbox, velocity, rot, control?, lights?, speed_limit?, parent_type}` |
| `start_session {…, spawn: {x, y, yaw}}` (driving) | the car spawns on the lane nearest to that point |

Car-only actions (`control`, `teleport`, `respawn`, `camera_switch`,
`camera_zoom`) answer `error {code: "needs_vehicle"}` in an editor session.
`server_status` carries `mode`, `editor_supported`, `editor_sessions` and
`editor_max_sessions`.

Server side: `editor_camera.py` (the pure fly maths and the focus pose),
`world_outliner.py` (grouping and details), the editor methods of
`DriveSession` (`start_editor`, `camera_move`, `camera_focus`,
`camera_follow`, `list_world_actors`, `inspect_actor`). The camera is a
`sensor.camera.rgb` at 960×540, FOV 90, 20 Hz, with role `drive_editor_<id>`
so the ownership rules and the audit treat it as this server's. Editor sessions
count as active sessions (they stream frames and hold the audit off) and are
capped by `DTB_EDITOR_MAX_SESSIONS` (2): each one is a full UE5 scene render.

## Verification

* `cd apps/drive-server && python -m pytest` (`test_editor_camera`,
  `test_world_outliner`, `test_editor_session`).
* `cd apps/drive-web && npm run check && npm test` (`fly-input`, `editor-protocol`).
