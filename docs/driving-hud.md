# Driving HUD and control bar

The driving view is laid out like CARLA's `manual_control.py` pygame window: a
translucent telemetry column on the left of the camera and a matching control
bar on the right instead of the old button stacks. The previous overlay (speed + bars at the
bottom, buttons bottom-left, camera tabs top-right) is still there behind
`/drive?hud=classic`.

## The column (left, 300 px)

Lines in `manual_control.py`'s order: Server FPS (the drive server's real
`world.tick()` rate), Client FPS (camera frames received per second), Vehicle,
Map, Simulation time, Speed (with the road's limit), Compass with the heading
letters, Accelerometer and Gyroscope, Location, GNSS, Height, the throttle /
steer / brake bars, the Reverse / Hand brake / Manual boxes, Gear, the
collision graph (the last 200 frames, scaled to the window's maximum, plus the
last thing hit), Number of vehicles and the nearby vehicles within 200 m,
nearest first. The footer shows the input mode.

Keys while driving: `Tab` cycles chase → hood → bird → free, `H` hides and
shows the column. Keys typed into any text field, select or editable element
never reach the car or the shortcuts (`$lib/inputGate.ts`, shared by the
driving controls and the page).

## The control bar (right, 172 px)

The same translucent column as the HUD, with every button named and grouped:
**Panels** (Weather, Traffic, Camera settings, Trajectory, Teleport, Packets,
V2X messages, Fleet, Scenarios, Map floating / full panel), **Camera** (Chase,
Hood, Bird, Free, with the bird altitude underneath) and **Session**
(Calibrate wheel for wheel input, Clear NPCs, Respawn, End). The open panel
and the active camera are highlighted. Overlays and panels live between the
two columns: the floating mini-map sits bottom-right of that area, toasts
top-right, and panels that open from the bar keep their usual corners.

## Server side

`telemetry.hud` is added to every telemetry frame (`drive_server._hud_snapshot`):

| field | source |
| --- | --- |
| `server_fps` | `tick_stats.TICK_STATS`, marked by `tick_loop` after each `world.tick()`; 0 when the loop stalls |
| `frame`, `sim_time` | `world.get_snapshot()` |
| `speed_limit` | `vehicle.get_speed_limit()` |
| `compass` | CARLA yaw + 90, clockwise from north (the same conversion the maps use) |
| `accel`, `gyro`, `imu` | `sensor.other.imu` on the ego (`DTB_HUD_SENSORS=1`, default); without it, acceleration from the velocity change between frames and the gyro from `get_angular_velocity()` |
| `gnss` | `geo_utils.carla_to_gps` |
| `height`, `hand_brake`, `manual` | the vehicle transform and control |
| `collision` | `sensor.other.collision` on the ego: the hardest hit since the previous frame (`intensity` = impulse length, `with` = display name, `count`), else `null` |
| `vehicles`, `vehicle`, `map` | vehicle count of the world, the ego's display name, the map name |

`nearby_actors[]` also carries `type_id` now. The two sensors are owned by the
session like the camera (`sensor_actor_ids`) and destroyed with it. With
`DTB_HUD_SENSORS=0` only the camera is attached.

## Verification

* `cd apps/drive-server && python -m pytest` (`test_tick_stats`, `test_hud_telemetry`).
* `cd apps/drive-web && npm run check && npm test` (`hud-format`, `DrivingHud`).
