"""
Unified entry point for the Digital Twin Bridge.

Combines the drive server (WebSocket vehicle control + MJPEG streaming)
with V2X observation (state publishing, map export).

Run with:  python -m digital_twin_bridge.drive_main

Architecture:

  ┌─────────────────────────────────────────────┐
  │  Unified Server (asyncio)                   │
  │                                             │
  │  CARLA tick loop ─── 20 Hz physics          │
  │  WebSocket server ── per-client drive sess  │
  │  Detection poll ──── twin-server history    │
  │  State publisher ─── state.json → data dir  │
  │  Map exporter ────── road network → data dir│
  │  Actor audit ─────── orphan cleanup / 60s   │
  └─────────────────────────────────────────────┘
"""

import asyncio
import hmac
import json
import logging
import os
import sys
import time
from typing import Mapping

import websockets

from digital_twin_bridge.config import Config
from digital_twin_bridge.carla_connection import CarlaConnection, drive_map_status
from digital_twin_bridge.detections_api import make_history_fetcher
from digital_twin_bridge.drive_server import serve_drive, active_session_count
from digital_twin_bridge.object_registry import ObjectRegistry
from digital_twin_bridge.publisher import StatePublisher
from digital_twin_bridge.trajectory_player import TrajectoryPlayer
from digital_twin_bridge.openscenario_runner import OpenScenarioRunner
from digital_twin_bridge.v2x_poller import V2XPoller

logger = logging.getLogger(__name__)


# ── State publisher ─────────────────────────────────────────────────


def _producer_epoch(value) -> float | None:
    """Parse a producer timestamp without substituting receipt time."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        epoch = float(value)
        if epoch > 1_000_000_000_000:
            epoch /= 1000.0
        return epoch if epoch >= 0 and epoch < float("inf") else None

    from datetime import datetime, timezone

    text = str(value).strip()
    if not text:
        return None
    try:
        if text.replace(".", "", 1).isdigit():
            return _producer_epoch(float(text))
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (OverflowError, ValueError):
        return None


def _producer_is_fresh(
    value,
    *,
    now: float,
    max_age_seconds: float,
    future_tolerance_seconds: float = 5.0,
) -> bool:
    epoch = _producer_epoch(value)
    if epoch is None:
        return False
    age = now - epoch
    return -future_tolerance_seconds <= age <= max(0.0, max_age_seconds)


def build_state_snapshot(
    registry,
    health,
    *,
    now: float | None = None,
    max_object_age_seconds: float | None = None,
    max_snapshot_age_seconds: float | None = None,
):
    """Build the state payload from the actor-free detection registry.

    Registry timestamps describe when each detection was actually refreshed;
    they must not be replaced with the publisher's current time, which made
    stale detections appear live.  This registry is metadata-only in drive
    mode and does not authorize PropSpawner or road-cone creation.
    """
    published_at = time.time() if now is None else now
    state_objects = []
    for obj in registry.get_all():
        producer_epoch = _producer_epoch(obj.timestamp_utc)
        if max_object_age_seconds is not None and not _producer_is_fresh(
            obj.timestamp_utc,
            now=published_at,
            max_age_seconds=max_object_age_seconds,
        ):
            continue

        snapshot_url = getattr(obj, "snapshot_url", None)
        snapshot_timestamp = getattr(obj, "snapshot_timestamp", None)
        if (
            snapshot_url
            and max_snapshot_age_seconds is not None
            and not _producer_is_fresh(
                snapshot_timestamp,
                now=published_at,
                max_age_seconds=max_snapshot_age_seconds,
            )
        ):
            # Preserve the source timestamp for diagnostics, but never expose
            # an old image URL as the current view of a fresh object.
            snapshot_url = None

        state_objects.append({
            "object_id": obj.object_id,
            "object_type": obj.object_type,
            "lat": obj.lat,
            "lon": obj.lon,
            "confidence": obj.confidence,
            "street_name": obj.street_name,
            "timestamp_utc": obj.timestamp_utc,
            "snapshot_url": snapshot_url,
            "snapshot_timestamp": snapshot_timestamp,
            "last_updated": (
                int(producer_epoch * 1000) if producer_epoch is not None else 0
            ),
        })

    status = health.get_status()
    bridge_status = {
        "status": "connected",
        "carla_fps": status.get("effective_fps", 0),
        "objects_tracked": len(state_objects),
        "cameras_active": 0,
        "state_source": "v2x_api_registry",
        "road_props_spawned": 0,
        "last_heartbeat": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(published_at)
        ),
    }
    return state_objects, bridge_status


async def state_publisher(config, registry, health, publisher, interval=5.0):
    """Periodically publish state.json so the dashboard stays live."""
    loop = asyncio.get_running_loop()

    while True:
        try:
            state_objects, bridge_status = build_state_snapshot(
                registry,
                health,
                max_object_age_seconds=config.STATE_OBJECT_MAX_AGE_SECONDS,
                max_snapshot_age_seconds=config.STATE_SNAPSHOT_MAX_AGE_SECONDS,
            )
            await loop.run_in_executor(
                None, publisher.publish_state, state_objects, bridge_status
            )
        except Exception as e:
            logger.debug("State publish failed: %s", e)

        await asyncio.sleep(interval)



def test_ws_access(config: Config, headers: Mapping[str, str] | None) -> tuple[bool, str]:
    """Authorize the legacy HIL/test socket on a separate opt-in boundary."""
    if str(config.TEST_WS_ENABLED).strip().lower() not in {"1", "true", "yes", "on"}:
        return False, "test WebSocket is disabled"
    expected = str(config.TEST_WS_TOKEN or "")
    if not expected:
        return False, "test WebSocket token is not configured"
    authorization = "" if headers is None else str(headers.get("Authorization", ""))
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return False, "bearer token required"
    if not hmac.compare_digest(authorization[len(prefix):], expected):
        return False, "invalid bearer token"
    return True, "authorized"


def enqueue_bounded(queue: asyncio.Queue, event: dict) -> None:
    """Keep subscriber queues bounded by dropping the oldest log event."""
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        # A racing consumer/producer should never make logging block control.
        pass


def cleanup_drive_world(world) -> None:
    """Remove drive-owned actors from the current world."""
    for actor in world.get_actors().filter("vehicle.*"):
        logger.info("Cleaning up leftover vehicle: %s (id=%d)", actor.type_id, actor.id)
        actor.destroy()
    for actor in world.get_actors().filter("walker.*"):
        logger.info("Cleaning up leftover walker: %s (id=%d)", actor.type_id, actor.id)
        try:
            actor.destroy()
        except Exception as e:
            logger.debug("Walker destroy failed (id=%d): %s", actor.id, e)
    for actor in world.get_actors().filter("sensor.*"):
        logger.info("Cleaning up leftover sensor: %s (id=%d)", actor.type_id, actor.id)
        actor.destroy()
    leftover_props = world.get_actors().filter("static.prop.*")
    if leftover_props:
        logger.info("Cleaning up %d leftover static prop(s)", len(leftover_props))
        for actor in leftover_props:
            try:
                actor.destroy()
            except Exception as e:
                logger.debug("Prop destroy failed (id=%d): %s", actor.id, e)
def create_openscenario_runner(config: Config, world):
    return OpenScenarioRunner(
        scenario_runner_path=config.SCENARIO_RUNNER_PATH,
        carla_host=config.CARLA_HOST,
        carla_port=config.CARLA_PORT,
        python_executable=config.SCENARIO_RUNNER_PYTHON or None,
        pythonpath_prefix=config.SCENARIO_RUNNER_PYTHONPATH,
        world=world,
    )


def export_current_map_data(conn: CarlaConnection, config: Config, publisher) -> None:
    """Export the active CARLA map and publish it for the web map overlay."""
    if publisher is None:
        return
    from digital_twin_bridge.map_data import MapDataExporter

    map_exporter = MapDataExporter(conn)
    snapshot_dir = config.LOCAL_SNAPSHOT_DIR
    os.makedirs(snapshot_dir, exist_ok=True)
    map_data = map_exporter.export_to_json(
        os.path.join(snapshot_dir, "map_data.json")
    )
    publisher.publish_map_data(map_data)
    logger.info("Map data exported and published")


class DriveMapController:
    """Coordinates safe runtime switching between the two public drive maps."""

    def __init__(
        self,
        conn: CarlaConnection,
        config: Config,
        runtime: dict,
        publisher,
    ) -> None:
        self._conn = conn
        self._config = config
        self._runtime = runtime
        self._publisher = publisher
        self._lock = asyncio.Lock()

    @property
    def world(self):
        return self._runtime["world"]

    @property
    def carla_map(self):
        return self._runtime["carla_map"]

    @property
    def trajectory_player(self):
        return self._runtime["trajectory_player"]

    @property
    def openscenario_runner(self):
        return self._runtime["openscenario_runner"]

    def status_payload(self) -> dict:
        return drive_map_status(self._conn.carla_map.name)

    async def switch_map(self, map_id: str) -> dict:
        async with self._lock:
            if active_session_count() > 0:
                raise RuntimeError("End the active drive session before switching maps")
            if self.trajectory_player.is_active():
                raise RuntimeError("Stop trajectory playback before switching maps")
            if self.openscenario_runner.is_running:
                raise RuntimeError("Stop OpenSCENARIO before switching maps")

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, self._conn.switch_drive_map, map_id)

            try:
                self.trajectory_player.stop()
            except Exception as e:
                logger.debug("Trajectory stop before map switch failed: %s", e)
            try:
                self.openscenario_runner.stop()
            except Exception as e:
                logger.debug("OpenSCENARIO stop before map switch failed: %s", e)

            cleanup_drive_world(self._conn.world)
            self._runtime["world"] = self._conn.world
            self._runtime["carla_map"] = self._conn.carla_map
            self._runtime["trajectory_player"] = TrajectoryPlayer(
                self._conn.world,
                self._conn.carla_map,
            )
            self._runtime["openscenario_runner"] = create_openscenario_runner(
                self._config,
                self._conn.world,
            )

            try:
                await loop.run_in_executor(None, export_current_map_data, self._conn, self._config, self._publisher)
            except Exception:
                logger.warning("Map data export after map switch failed", exc_info=True)

            return {"type": "map_set", **result}


# ── Main ────────────────────────────────────────────────────────────


async def main():
    config = Config.from_env()
    config.setup_logging()

    if "--dry-run" in sys.argv:
        logger.info("Unified server dry-run OK")
        return

    # ── Connect to CARLA (CarlaConnection handles sync mode + restore) ──
    conn = CarlaConnection(config)
    conn.connect()

    world = conn.world
    carla_map = conn.carla_map

    cleanup_drive_world(world)

    # ── V2X state registry: metadata polling only, no boot-time props ──
    # PropSpawner remains intentionally absent.  The registry keeps state.json
    # truthful and current, while the poller receives no CARLA map and therefore
    # cannot resolve/spawn the road cones that disrupted drive scenarios.
    registry = ObjectRegistry()
    state_poller = V2XPoller(config, registry, carla_map=None)

    # ── Map data: export road network for the web map overlay ──
    publisher = None
    try:
        publisher = StatePublisher(config)
        export_current_map_data(conn, config, publisher)
    except Exception:
        logger.warning("Map data export failed (non-fatal)", exc_info=True)

    # ── Health monitor ──
    from digital_twin_bridge.health import HealthMonitor

    health = HealthMonitor()

    # ── Trajectory player (singleton: one playback at a time, shared world) ──
    trajectory_player = TrajectoryPlayer(world, carla_map)

    # ── OpenSCENARIO runner (singleton: one .xosc at a time across all sessions) ──
    openscenario_runner = create_openscenario_runner(config, world)
    runtime = {
        "world": world,
        "carla_map": carla_map,
        "trajectory_player": trajectory_player,
        "openscenario_runner": openscenario_runner,
    }
    map_controller = DriveMapController(conn, config, runtime, publisher)

    # ── Drive server setup ──
    api_fetcher = make_history_fetcher(config)

    _test_log_subscribers: list[asyncio.Queue] = []

    async def _serve_test(websocket):
        import json
        import os
        import re
        from datetime import datetime
        TEST_LOG = "/tmp/bridge-test.log"
        UPLOAD_DIR = "/tmp/bridge-uploads"
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        def _broadcast(event):
            for q in list(_test_log_subscribers):
                enqueue_bounded(q, event)
        def _tlog(line):
            full = f"{datetime.now().isoformat(timespec='seconds')} {line}"
            try:
                with open(TEST_LOG, "a") as f:
                    f.write(full + "\n")
            except Exception:
                pass
            _broadcast({"type": "log_line", "line": full})
        def _file_entry(name):
            try:
                st = os.stat(os.path.join(UPLOAD_DIR, name))
                return {
                    "name": name,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                }
            except OSError:
                return None
        def _safe_name(name):
            base = os.path.basename((name or "upload.bin")).strip()
            base = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "upload.bin"
            base = base.lstrip(".") or "upload.bin"
            return base[:120]
        addr = websocket.remote_address
        logger.info("Test connection from %s", addr)
        await websocket.send(json.dumps({
            "type": "test_hello",
            "remote": str(addr),
        }))

        try:
            with open(TEST_LOG) as f:
                history = f.read().splitlines()[-500:]
        except FileNotFoundError:
            history = []
        await websocket.send(json.dumps({"type": "log_history", "lines": history}))

        try:
            files = []
            for name in sorted(os.listdir(UPLOAD_DIR)):
                entry = _file_entry(name)
                if entry is not None:
                    files.append(entry)
        except OSError:
            files = []
        await websocket.send(json.dumps({"type": "uploads_listing", "files": files}))

        log_q: asyncio.Queue = asyncio.Queue(
            maxsize=max(1, config.TEST_WS_QUEUE_SIZE)
        )
        _test_log_subscribers.append(log_q)
        async def _log_pump():
            try:
                while True:
                    event = await log_q.get()
                    await websocket.send(json.dumps(event))
            except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                pass
            except Exception:
                pass
        pump_task = asyncio.create_task(_log_pump())

        _tlog(f"OPEN  {addr}")
        pending_upload = None
        try:
            async for msg in websocket:
                if isinstance(msg, bytes):
                    if pending_upload is not None:
                        expected_size = pending_upload["size"]
                        if (
                            len(msg) > config.TEST_WS_MAX_UPLOAD_BYTES
                            or len(msg) != expected_size
                        ):
                            await websocket.send(json.dumps({
                                "type": "upload_error",
                                "message": (
                                    "upload payload size does not match the bounded "
                                    "upload_start declaration"
                                ),
                            }))
                            pending_upload = None
                            continue
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                        saved_name = f"{ts}_{_safe_name(pending_upload.get('filename'))}"
                        saved_path = os.path.join(UPLOAD_DIR, saved_name)
                        try:
                            with open(saved_path, "wb") as f:
                                f.write(msg)
                            _tlog(
                                f"UPLOAD {addr} saved={saved_path} bytes={len(msg)} "
                                f"original={pending_upload.get('filename')!r} "
                                f"mime={pending_upload.get('mime')!r}"
                            )
                            entry = _file_entry(saved_name)
                            if entry is not None:
                                _broadcast({"type": "uploads_added", "file": entry})
                            await websocket.send(json.dumps({
                                "type": "upload_done",
                                "saved_as": saved_path,
                                "size": len(msg),
                                "original_filename": pending_upload.get("filename"),
                            }))
                        except Exception as e:
                            _tlog(f"UPLOAD_ERROR {addr} {e!r}")
                            await websocket.send(json.dumps({
                                "type": "upload_error",
                                "message": str(e),
                            }))
                        finally:
                            pending_upload = None
                    else:
                        hex_preview = msg[:32].hex(" ")
                        _tlog(f"IN    {addr} (binary, {len(msg)}B) {hex_preview}")
                        await websocket.send(msg)
                else:
                    _tlog(f"IN    {addr} (text) {msg[:4096]!r}")
                    try:
                        parsed = json.loads(msg)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, dict) and parsed.get("type") == "upload_start":
                        try:
                            declared_size = int(parsed.get("size"))
                        except (TypeError, ValueError):
                            declared_size = -1
                        if not 0 <= declared_size <= config.TEST_WS_MAX_UPLOAD_BYTES:
                            pending_upload = None
                            await websocket.send(json.dumps({
                                "type": "upload_error",
                                "message": (
                                    "upload size must be between 0 and "
                                    f"{config.TEST_WS_MAX_UPLOAD_BYTES} bytes"
                                ),
                            }))
                            continue
                        pending_upload = {
                            "filename": _safe_name(parsed.get("filename")),
                            "mime": str(parsed.get("mime") or "application/octet-stream")[:120],
                            "size": declared_size,
                        }
                        _tlog(
                            f"UPLOAD_START {addr} filename={parsed.get('filename')!r} "
                            f"size={parsed.get('size')} mime={parsed.get('mime')!r}"
                        )
                        await websocket.send(json.dumps({
                            "type": "upload_ready",
                            "filename": parsed.get("filename"),
                        }))
                    else:
                        await websocket.send(json.dumps({"type": "echo", "data": msg}))
        except websockets.exceptions.ConnectionClosed:
            logger.info("Test connection closed")
            _tlog(f"CLOSE {addr}")
        finally:
            try:
                _test_log_subscribers.remove(log_q)
            except ValueError:
                pass
            pump_task.cancel()

    async def handler(websocket):
        from urllib.parse import urlparse

        request_path = websocket.request.path
        route = urlparse(request_path).path
        if route == "/test":
            allowed, reason = test_ws_access(config, websocket.request.headers)
            if not allowed:
                await websocket.send(json.dumps({
                    "type": "test_error",
                    "message": reason,
                }))
                await websocket.close(code=1008, reason=reason)
                return
            await _serve_test(websocket)
            return
        await serve_drive(
            websocket,
            runtime["world"],
            runtime["carla_map"],
            api_fetcher,
            trajectory_player=runtime["trajectory_player"],
            openscenario_runner=runtime["openscenario_runner"],
            eva_warning_distance_m=config.EVA_WARNING_DISTANCE_M,
            map_controller=map_controller,
            scene_fetch_timeout_seconds=config.SCENE_FETCH_TOTAL_TIMEOUT_SECONDS,
            scene_fetch_max_pages=config.SCENE_FETCH_MAX_PAGES,
            scene_fetch_max_items=config.SCENE_FETCH_MAX_ITEMS,
        )

    async def tick_loop():
        """Advance CARLA physics at 20 Hz.

        In sync mode the world does not tick on its own; we drive it from
        here. While ScenarioRunner is running it owns the tick (launched
        with --sync, locally patched to pace at 20 Hz wall-time so user
        controls feel normal). The bridge yields its tick during that
        window. SR turns sync mode off on exit, so we re-arm it before
        resuming. After each bridge tick we step the trajectory player so
        its controller stays in lockstep with sim time.
        """
        loop = asyncio.get_running_loop()
        target_dt = 0.05
        was_running = False
        while True:
            world = runtime["world"]
            trajectory_player = runtime["trajectory_player"]
            openscenario_runner = runtime["openscenario_runner"]
            if openscenario_runner.is_running:
                was_running = True
                await asyncio.sleep(target_dt)
                continue
            if was_running:
                try:
                    settings = world.get_settings()
                    if not settings.synchronous_mode:
                        settings.synchronous_mode = True
                        settings.fixed_delta_seconds = target_dt
                        world.apply_settings(settings)
                        logger.info("Re-applied sync mode after scenario finished")
                except Exception as e:
                    logger.warning("Failed to restore sync mode after scenario: %s", e)
                was_running = False

            start = loop.time()
            try:
                await loop.run_in_executor(None, world.tick)
            except Exception as e:
                logger.warning("world.tick() failed: %s", e)
                await asyncio.sleep(target_dt)
                continue
            try:
                trajectory_player.tick()
            except Exception as e:
                logger.warning("trajectory_player.tick() failed: %s", e)
            elapsed = loop.time() - start
            await asyncio.sleep(max(0.0, target_dt - elapsed))

    async def periodic_actor_audit():
        """Every 60s, check for orphaned actors when no sessions are active."""
        while True:
            await asyncio.sleep(60)
            if active_session_count() > 0:
                continue
            try:
                from digital_twin_bridge.drive_server import _traffic_actor_ids
                vehicles = [v for v in world.get_actors().filter("vehicle.*")
                            if v.id not in _traffic_actor_ids
                            and v.attributes.get("role_name") != "trajectory"]
                sensors = list(world.get_actors().filter("sensor.*"))
                walkers = list(world.get_actors().filter("walker.*"))
                # Historical props are session-owned and must be gone when no
                # sessions are active.  Any remaining static prop is orphaned.
                props = list(world.get_actors().filter("static.prop.*"))
                props_to_clean = props
                orphaned = (
                    len(vehicles) + len(walkers) + len(sensors) + len(props_to_clean)
                )
                if orphaned > 0:
                    logger.warning(
                        "Actor audit: %d orphaned actors (vehicles=%d, walkers=%d, sensors=%d, props=%d). Cleaning up.",
                        orphaned, len(vehicles), len(walkers), len(sensors), len(props_to_clean),
                    )
                    for a in sensors:
                        try:
                            a.stop()
                        except Exception:
                            pass
                        try:
                            a.destroy()
                        except Exception:
                            pass
                    for a in vehicles:
                        try:
                            a.destroy()
                        except Exception:
                            pass
                    for a in walkers:
                        try:
                            a.destroy()
                        except Exception:
                            pass
                    for a in props_to_clean:
                        try:
                            a.destroy()
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("Actor audit error: %s", e)

    port = config.WS_PORT

    logger.info("=" * 60)
    logger.info("  Digital Twin -- Unified Server")
    logger.info("=" * 60)
    logger.info("  CARLA       : %s:%d (sync mode, 20 Hz)", config.CARLA_HOST, config.CARLA_PORT)
    logger.info("  Drive WS    : ws://0.0.0.0:%d", port)
    logger.info("  V2X props   : session-owned historical reconstruction")
    logger.info("  State source: metadata-only V2X registry (no prop spawning)")
    logger.info("  State pub   : %s", publisher.describe() if publisher else "disabled")
    logger.info("=" * 60)

    tick_task = None
    audit_task = None
    publish_task = None

    try:
        tick_task = asyncio.create_task(tick_loop())
        audit_task = asyncio.create_task(periodic_actor_audit())

        # Publish state.json locally so the web dashboard stays live
        if publisher is not None:
            state_poller.start()
            publish_task = asyncio.create_task(
                state_publisher(config, registry, health, publisher)
            )

        async with websockets.serve(
            handler,
            "0.0.0.0",
            port,
            ping_interval=5,
            ping_timeout=15,
            close_timeout=5,
            max_size=max(1024, config.WS_MAX_MESSAGE_BYTES),
        ):
            logger.info("Unified server ready. Waiting for connections...")
            await asyncio.Future()
    finally:
        # Metadata polling owns no CARLA actors and can stop independently.
        try:
            state_poller.stop()
        except Exception as e:
            logger.debug("State poller stop failed: %s", e)

        for task in [tick_task, audit_task, publish_task]:
            if task is not None:
                try:
                    task.cancel()
                except Exception:
                    pass

        # Stop any active trajectory playback (server-owned, not session-owned).
        try:
            runtime["trajectory_player"].stop()
        except Exception as e:
            logger.debug("Trajectory stop on shutdown failed: %s", e)

        # Stop any running OpenSCENARIO subprocess.
        try:
            runtime["openscenario_runner"].stop()
        except Exception as e:
            logger.debug("OpenSCENARIO stop on shutdown failed: %s", e)

        conn.disconnect()
        logger.info("Unified server stopped")


if __name__ == "__main__":
    asyncio.run(main())
