"""
Drive Server — WebSocket server for real-time vehicle control.

Manages driving sessions: scene reconstruction, vehicle spawning,
steering input, camera switching, telemetry + MJPEG frame streaming.
"""

import asyncio
import io
import json
import logging
import math
import os
import re
import time
import threading
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import websockets
from PIL import Image

from digital_twin_bridge.scene_reconstructor import SceneReconstructor
from digital_twin_bridge.openscenario_runner import list_xosc
from digital_twin_bridge.perception import PerceptionService
from digital_twin_bridge.trajectory_player import (
    TrajectoryPlayer,
    list_trajectory_files,
    save_trajectory_file,
)

logger = logging.getLogger(__name__)

VALID_CAMERA_VIEWS = {"chase", "hood", "bird", "free"}

# Teleport is a privileged mutation of this session's ego actor.  Keep the
# accepted values finite and close to the active map/road so malformed or
# hostile WS messages cannot fling actors into extreme Unreal coordinates.
TELEPORT_COORD_ABS_LIMIT_M = 100_000.0
TELEPORT_MAP_MARGIN_M = 500.0
TELEPORT_MAX_ROAD_DISTANCE_M = 100.0
TELEPORT_MIN_Z_M = -20.0
TELEPORT_MAX_Z_M = 500.0
TELEPORT_MAX_ROAD_Z_OFFSET_M = 50.0
TELEPORT_MAX_ABS_YAW_DEG = 360.0
TELEPORT_REQUEST_ID_MAX_LENGTH = 128
TELEPORT_CONFIRM_TIMEOUT_SECONDS = 2.0
TELEPORT_CONFIRM_TOLERANCE_M = 2.0
TELEPORT_CONFIRM_POLL_INTERVAL_SECONDS = 0.05
TELEPORT_CONFIRM_YAW_TOLERANCE_DEG = 3.0

# Historical range reads are isolated from the CARLA event loop.  Limit
# concurrent workers globally per event loop; a timed-out worker keeps its slot
# until its current bounded request observes cancellation and exits.
SCENE_FETCH_MAX_CONCURRENT = 2
DEFAULT_SCENE_FETCH_TIMEOUT_SECONDS = 20.0
DEFAULT_SCENE_FETCH_MAX_PAGES = 20
DEFAULT_SCENE_FETCH_MAX_ITEMS = 10_000
DEFAULT_PERCEPTION_SCAN_INTERVAL_SECONDS = 0.1
_scene_fetch_limiters = weakref.WeakKeyDictionary()


def _scene_fetch_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limiter = _scene_fetch_limiters.get(loop)
    if limiter is None:
        limiter = asyncio.Semaphore(SCENE_FETCH_MAX_CONCURRENT)
        _scene_fetch_limiters[loop] = limiter
    return limiter

# Default vehicle if none specified
DEFAULT_VEHICLE = "vehicle.tesla.model3"
FALLBACK_VEHICLES = [
    DEFAULT_VEHICLE,
    "vehicle.lincoln.mkz",
    "vehicle.dodge.charger",
    "vehicle.nissan.patrol",
    "vehicle.mini.cooper",
]
DEFAULT_DRIVE_WEATHER = {
    "cloudiness": 0.0,
    "precipitation": 0.0,
    "precipitation_deposits": 0.0,
    "wind_intensity": 30.0,
    "sun_azimuth_angle": 180.0,
    "sun_altitude_angle": 75.0,
    "fog_density": 0.0,
    "fog_distance": 100000.0,
    "fog_falloff": 0.1,
    "wetness": 0.0,
    "scattering_intensity": 1.0,
    "mie_scattering_scale": 0.03,
    "rayleigh_scattering_scale": 0.0331,
    "dust_storm": 0.0,
}
SAFE_WEATHER_LIMITS = {
    "cloudiness": (0.0, 85.0),
    "precipitation": (0.0, 70.0),
    "precipitation_deposits": (0.0, 70.0),
    "wind_intensity": (0.0, 80.0),
    "sun_azimuth_angle": (-1.0, 360.0),
    "sun_altitude_angle": (10.0, 90.0),
    "fog_density": (0.0, 25.0),
    "fog_distance": (25.0, 100000.0),
    "fog_falloff": (0.05, 5.0),
    "wetness": (0.0, 80.0),
    "scattering_intensity": (0.5, 2.0),
    "mie_scattering_scale": (0.0, 0.2),
    "rayleigh_scattering_scale": (0.0, 0.08),
    "dust_storm": (0.0, 30.0),
}
SAFE_CAMERA_ATTR_LIMITS = {
    "bloom_intensity": (0.1, 0.8),
    "lens_flare_intensity": (0.0, 0.2),
    "motion_blur_intensity": (0.0, 0.45),
    "motion_blur_max_distortion": (0.0, 0.35),
    "exposure_compensation": (0.0, 1.0),
    "exposure_min_bright": (8.0, 12.0),
    "exposure_max_bright": (10.0, 16.0),
    "exposure_speed_up": (1.0, 4.0),
    "exposure_speed_down": (0.8, 4.0),
    "gamma": (2.0, 2.4),
    "temp": (5500.0, 7500.0),
    "tint": (-0.2, 0.2),
    "slope": (0.7, 1.0),
    "toe": (0.4, 0.7),
    "shoulder": (0.2, 0.4),
    "black_clip": (0.0, 0.02),
    "white_clip": (0.02, 0.06),
    "chromatic_aberration_intensity": (0.0, 0.2),
    "lens_circle_multiplier": (0.0, 0.2),
    "lens_circle_falloff": (4.0, 8.0),
}

# Bird's-eye zoom: the top-down camera dollies up/down by scaling its
# parent-relative altitude.  On a Rigid-attached sensor set_location is
# parent-relative and preserves the attachment (verified against CARLA
# 0.10.0; set_transform is world-space and detaches), so zooming needs no
# sensor respawn and never drops a frame.  FOV zoom is not an option —
# blueprint attributes are immutable after spawn.
BIRD_ZOOM_DEFAULT_ALTITUDE_M = 25.0
BIRD_ZOOM_MIN_ALTITUDE_M = 8.0
BIRD_ZOOM_MAX_ALTITUDE_M = 100.0
# Per-message factor bounds: a wheel gesture coalesces to small steps, so a
# huge single factor is either a bug or a hostile message.
BIRD_ZOOM_MIN_STEP_FACTOR = 0.2
BIRD_ZOOM_MAX_STEP_FACTOR = 5.0

# Traffic presets
TRAFFIC_PRESETS = {
    "none":   {"vehicles": 0,   "speed_diff": 0,   "distance": 2.0, "ignore_lights": 0,  "ignore_signs": 0},
    "light":  {"vehicles": 20,  "speed_diff": 30,  "distance": 3.0, "ignore_lights": 0,  "ignore_signs": 0},
    "medium": {"vehicles": 60,  "speed_diff": 10,  "distance": 2.0, "ignore_lights": 5,  "ignore_signs": 2},
    "heavy":  {"vehicles": 120, "speed_diff": 0,   "distance": 1.5, "ignore_lights": 15, "ignore_signs": 10},
    "chaos":  {"vehicles": 180, "speed_diff": -20, "distance": 1.0, "ignore_lights": 35, "ignore_signs": 30},
}

# Module-level traffic tracking so periodic_actor_audit can exclude them
_traffic_actor_ids: set[int] = set()

# ── coexist branch helpers ──────────────────────────────────────────────────
from digital_twin_bridge.actor_ownership import is_drive_owned  # noqa: E402


def _drive_config():
    from digital_twin_bridge.config import Config
    return Config.from_env()


def _drive_tm_port() -> int:
    return _drive_config().TM_PORT


def _protect_foreign() -> bool:
    return _drive_config().protect_foreign_actors


def _voices_roles() -> tuple:
    return _drive_config().voices_roles


def _keep_voices_ego() -> bool:
    return _drive_config().keep_voices_ego


def _claim_voices_role(session) -> str:
    """First configured VOICES role (e.g. PATH-M-1) not held by another session, else ''."""
    taken = {getattr(s, "_voices_role", "") for s in _active_sessions if s is not session}
    for role in _voices_roles():
        if role not in taken:
            return role
    return ""


def _adopt_kept_ego(world, role: str, spawn_transform):
    """Reuse the vehicle a previous session left in the world under ``role``."""
    import carla
    for actor in world.get_actors().filter("vehicle.*"):
        attrs = actor.attributes
        if not attrs or attrs.get("role_name") != role:
            continue
        try:
            actor.set_transform(spawn_transform)
            actor.set_target_velocity(carla.Vector3D(0, 0, 0))
            actor.apply_control(carla.VehicleControl())
        except Exception:
            logger.warning("Could not reset kept ego %s", role, exc_info=True)
        logger.info("Adopted kept VOICES ego %s (actor id %d)", role, actor.id)
        return actor
    return None

# Dynamic actors are individually spawned from the Add Actor panel and carry
# session-scoped moving geofences.
_dynamic_actor_ids: set[int] = set()
DYNAMIC_GEOFENCE_DRAW_SEGMENTS = 32
DYNAMIC_GEOFENCE_DRAW_LIFETIME = 0.25
DYNAMIC_GEOFENCE_DRAW_INTERVAL = 0.10


@dataclass
class DynamicActorMeta:
    actor_id: int
    blueprint: str
    name: str
    geofence_radius: float
    message: str


def blueprint_wheel_count(blueprint, default: int = 4) -> int:
    """Return CARLA blueprint wheel count across real and mocked attributes."""
    if not blueprint.has_attribute("number_of_wheels"):
        return default

    attr = blueprint.get_attribute("number_of_wheels")
    try:
        if hasattr(attr, "as_int"):
            return int(attr.as_int())
        values = getattr(attr, "recommended_values", None)
        if values:
            return int(values[0])
        value = getattr(attr, "value", None)
        if value is not None:
            return int(value)
        return int(attr)
    except Exception:
        text = str(attr)
        import re as _re
        match = _re.search(r"value=([-+]?\d+)", text)
        if match:
            return int(match.group(1))
        raise


def get_available_vehicles(world) -> list[dict]:
    """Query CARLA for all spawnable vehicle blueprints."""
    bp_lib = world.get_blueprint_library()
    vehicles = []
    for bp in bp_lib.filter("vehicle.*"):
        bp_id = bp.id
        # Extract make and model from blueprint id (e.g. "vehicle.tesla.model3")
        parts = bp_id.split(".")
        if len(parts) >= 3:
            make = parts[1].title()
            model = parts[2].replace("_", " ").title()
            display_name = f"{make} {model}"
        else:
            display_name = bp_id

        # Get number of wheels to filter out bikes if desired
        num_wheels = 4
        try:
            num_wheels = blueprint_wheel_count(bp)
        except Exception:
            pass

        vehicles.append({
            "id": bp_id,
            "name": display_name,
            "wheels": num_wheels,
        })

    # Sort: 4-wheeled first, then alphabetically
    vehicles.sort(key=lambda v: (0 if v["wheels"] >= 4 else 1, v["name"]))
    return vehicles


def resolve_vehicle_blueprint(bp_lib, requested_blueprint: str):
    """Resolve a requested vehicle, falling back across CARLA catalogs."""
    vehicle_bps = bp_lib.filter(requested_blueprint)
    if vehicle_bps:
        return vehicle_bps[0]

    for fallback in FALLBACK_VEHICLES:
        vehicle_bps = bp_lib.filter(fallback)
        if vehicle_bps:
            logger.warning(
                "Vehicle '%s' not found, falling back to '%s'",
                requested_blueprint,
                vehicle_bps[0].id,
            )
            return vehicle_bps[0]

    for bp in bp_lib.filter("vehicle.*"):
        try:
            if blueprint_wheel_count(bp) >= 4:
                logger.warning(
                    "Vehicle '%s' not found, falling back to first available four-wheel vehicle '%s'",
                    requested_blueprint,
                    bp.id,
                )
                return bp
        except Exception:
            continue

    return None


def _safe_float(params: dict, key: str, default: float, limits: tuple[float, float]) -> float:
    value = params.get(key, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    lo, hi = limits
    return max(lo, min(hi, parsed))


def safe_drive_weather(params: dict | None = None) -> dict[str, float]:
    """Return weather values constrained to keep the drive camera usable."""
    params = params or {}
    return {
        key: _safe_float(params, key, DEFAULT_DRIVE_WEATHER[key], limits)
        for key, limits in SAFE_WEATHER_LIMITS.items()
    }


def apply_default_drive_weather(world) -> None:
    """Reset CARLA to a bright weather state for the shared drive world."""
    import carla

    world.set_weather(carla.WeatherParameters(**safe_drive_weather(DEFAULT_DRIVE_WEATHER)))


def get_spawnable_objects(world) -> list[dict]:
    """Query CARLA for all spawnable objects (vehicles + static props)."""
    bp_lib = world.get_blueprint_library()
    objects = []

    # Vehicles (can be placed as parked cars, police cars, etc.)
    for bp in bp_lib.filter("vehicle.*"):
        parts = bp.id.split(".")
        if len(parts) >= 3:
            make = parts[1].title()
            model = parts[2].replace("_", " ").title()
            name = f"{make} {model}"
        else:
            name = bp.id
        objects.append({"id": bp.id, "name": name, "category": "vehicle"})

    # Static props (cones, barriers, signs, etc.)
    for bp in bp_lib.filter("static.prop.*"):
        parts = bp.id.split(".")
        name = parts[-1].replace("_", " ").title() if parts else bp.id
        objects.append({"id": bp.id, "name": name, "category": "prop"})

    # Sort by category then name
    objects.sort(key=lambda o: (0 if o["category"] == "vehicle" else 1, o["name"]))
    return objects


def display_name_from_blueprint(blueprint_id: str) -> str:
    parts = blueprint_id.split(".")
    if len(parts) >= 3:
        make = parts[1].title()
        model = parts[2].replace("_", " ").title()
        return f"{make} {model}"
    return blueprint_id


# ── Scenario file I/O ──

BRIDGE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APPS_ROOT = os.path.abspath(os.path.join(BRIDGE_ROOT, ".."))
SCENARIOS_DIR = os.path.join(BRIDGE_ROOT, "scenes")
LEGACY_SCENARIOS_DIR = os.path.join(APPS_ROOT, "v2x-digital-twin-bridge", "scenes")


def _ensure_scenes_dir():
    os.makedirs(SCENARIOS_DIR, exist_ok=True)


def _scenario_dirs() -> list[str]:
    """Search current storage first, then the pre-reorg legacy location."""
    dirs = [SCENARIOS_DIR]
    if LEGACY_SCENARIOS_DIR != SCENARIOS_DIR:
        dirs.append(LEGACY_SCENARIOS_DIR)
    return dirs


def _resolve_scenario_path(filename: str) -> str:
    """Find a scenario file in the current or legacy storage location."""
    for base_dir in _scenario_dirs():
        fpath = os.path.join(base_dir, filename)
        if os.path.isfile(fpath):
            return fpath
    raise FileNotFoundError(f"Scenario file not found: {filename}")


def _sanitize_name(name: str) -> str:
    """Convert a scenario name to a safe filename slug."""
    slug = re.sub(r"[^a-zA-Z0-9_\- ]", "", name).strip().replace(" ", "_").lower()
    if not slug:
        slug = "untitled"
    return slug


def list_scenarios() -> list[dict]:
    """List all saved scenario files."""
    _ensure_scenes_dir()
    scenarios_by_file: dict[str, dict] = {}
    for base_dir in _scenario_dirs():
        if not os.path.isdir(base_dir):
            continue
        for fname in sorted(os.listdir(base_dir)):
            if not fname.endswith(".json") or fname in scenarios_by_file:
                continue
            fpath = os.path.join(base_dir, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
                scenarios_by_file[fname] = {
                    "name": data.get("name", fname.replace(".json", "")),
                    "file": fname,
                    "object_count": len(data.get("objects", [])),
                    "zone_count": len(data.get("zones", [])),
                }
            except Exception:
                continue
    scenarios = list(scenarios_by_file.values())
    scenarios.sort(key=lambda scenario: scenario["name"].lower())
    return scenarios


def save_scenario(name: str, objects: list[dict], zones: list[dict] | None = None) -> dict:
    """Save a scenario to disk. Includes both placed CARLA objects and V2X zones."""
    _ensure_scenes_dir()
    zones = zones or []
    slug = _sanitize_name(name)
    fpath = os.path.join(SCENARIOS_DIR, f"{slug}.json")
    data = {"name": name, "objects": objects, "zones": zones}
    with open(fpath, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Scenario saved: %s (%d objects, %d zones) → %s", name, len(objects), len(zones), fpath)
    return {
        "type": "scenario_saved",
        "name": name,
        "file": f"{slug}.json",
        "object_count": len(objects),
        "zone_count": len(zones),
    }


def load_scenario(filename: str) -> dict:
    """Load a scenario from disk."""
    fpath = _resolve_scenario_path(filename)
    with open(fpath) as f:
        return json.load(f)


def delete_scenario(filename: str) -> dict:
    """Delete a scenario file."""
    fpath = _resolve_scenario_path(filename)
    os.remove(fpath)
    logger.info("Scenario deleted: %s", filename)
    return {"type": "scenario_deleted", "file": filename}


class DriveSession:
    """
    Manages a single driving session.
    Lifecycle: start() -> apply_control() (repeated) -> end()
    """

    def __init__(
        self,
        world,
        carla_map,
        api_fetcher: Callable,
        trajectory_player: Optional[TrajectoryPlayer] = None,
        openscenario_runner=None,
        eva_warning_distance_m: float = 20.0,
        scene_fetch_timeout_seconds: float = DEFAULT_SCENE_FETCH_TIMEOUT_SECONDS,
        scene_fetch_max_pages: int = DEFAULT_SCENE_FETCH_MAX_PAGES,
        scene_fetch_max_items: int = DEFAULT_SCENE_FETCH_MAX_ITEMS,
        perception_scan_interval_seconds: float = DEFAULT_PERCEPTION_SCAN_INTERVAL_SECONDS,
    ):
        self._world = world
        self._map = carla_map
        self._api_fetcher = api_fetcher
        # Emergency-vehicle pull-over warning: every tick, broadcast a
        # v2x_alert for each firetruck within this radius. Browser dedups by
        # actor id (single toast per truck, distance updates in place) and
        # auto-dismisses when alerts stop arriving.
        self._eva_warning_distance_m = eva_warning_distance_m
        # Per-firetruck timestamps of when the ego entered the truck's forward
        # path. Used to debounce the "please yield" alert: it only fires after
        # the ego has been blocking the truck for >10s. Cleared as soon as the
        # ego leaves the truck's forward cone.
        self._in_front_since: dict[int, float] = {}
        # Per-session unique ego role_name. ScenarioRunner attaches to the ego
        # via its role_name; with multiple browsers sharing a CARLA world, a
        # global "ego_vehicle" tag would let SR pick whichever ego it found
        # first instead of the one belonging to the session that clicked Start.
        # Each session stamps its own ego with a unique role and the runner
        # rewrites the .xosc on launch to reference that exact role.
        self._ego_role = f"ego_vehicle_{id(self):x}"
        self._voices_role = ""  # coexist: scenario role taken by this session, if any
        self._release_voices_ego = False
        self._scene_fetch_timeout_seconds = max(
            0.1, float(scene_fetch_timeout_seconds)
        )
        self._scene_fetch_max_pages = max(1, int(scene_fetch_max_pages))
        self._scene_fetch_max_items = max(1, int(scene_fetch_max_items))
        # Server-owned trajectory player; one playback shared across all sessions
        # in the world. None → trajectory feature disabled.
        self._trajectory_player = trajectory_player
        # Server-owned OpenSCENARIO runner; one scenario runs at a time across
        # all sessions. None → feature disabled.
        self._openscenario_runner = openscenario_runner
        self._reconstructor: Optional[SceneReconstructor] = None
        self.vehicle = None
        self.active_camera: str = "chase"
        self._active = False
        self._starting = False
        self._camera_sensor = None
        self._latest_frame: Optional[bytes] = None
        self._frame_lock = threading.Lock()
        # Session-owned perception sensors; never shared across browser egos.
        self._perception = PerceptionService()
        self._perception_scan_interval_seconds = max(
            0.0, float(perception_scan_interval_seconds)
        )
        self._last_perception_scan_monotonic: Optional[float] = None
        self._cached_perception_detections: list[dict] = []
        self._perception_scan_executor: Optional[ThreadPoolExecutor] = None
        self._perception_scan_future: Optional[Future] = None
        self._perception_scan_future_generation: Optional[int] = None
        self._retired_perception_scan_future: Optional[Future] = None
        self._perception_scan_generation = 0
        self._accepting_frames = False  # Guard against callbacks after stop
        self._placed_objects: list = []  # User-placed objects (actor, blueprint_id, pos)
        self._dynamic_actors: dict[int, DynamicActorMeta] = {}
        self._last_dynamic_geofence_draw = 0.0
        # Camera stream config — survives set_camera_settings respawns.
        # Default to 1:1 square to match the drive UI's split layout.
        self._camera_width = 720
        self._camera_height = 720
        self._camera_fov = 90.0
        # Custom post-processing attrs persisted across set_camera_settings respawns.
        self._camera_extra_attrs: dict[str, str] = {}
        # Bird's-eye dolly altitude (m above the vehicle), adjusted via
        # camera_zoom messages; persists across view switches and respawns.
        self._bird_altitude = BIRD_ZOOM_DEFAULT_ALTITUDE_M
        # Vehicle bounding-box half-extents, populated after spawn. Camera
        # transforms scale by these so they fit any vehicle (matches the
        # bound_x/y/z idiom in CARLA's manual_control.py).
        self._bound_x = 2.5
        self._bound_y = 1.0
        self._bound_z = 0.8

    def update_runtime(
        self,
        world,
        carla_map,
        trajectory_player: Optional[TrajectoryPlayer] = None,
        openscenario_runner=None,
    ) -> None:
        """Refresh server-owned CARLA references after an idle map switch."""
        if self._active or self._starting:
            raise RuntimeError("Cannot update session runtime while active")
        self._world = world
        self._map = carla_map
        self._trajectory_player = trajectory_player
        self._openscenario_runner = openscenario_runner

    async def _fetch_scene_result(self, start: str, end: str):
        """Fetch historical pages off-loop without permitting CARLA mutation."""
        if self._reconstructor is None:
            raise RuntimeError("scene reconstructor is not initialized")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._scene_fetch_timeout_seconds
        limiter = _scene_fetch_limiter()
        try:
            await asyncio.wait_for(
                limiter.acquire(), timeout=self._scene_fetch_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                "Historical scene fetch timed out after "
                f"{self._scene_fetch_timeout_seconds:g} seconds"
            ) from exc

        remaining = deadline - loop.time()
        if remaining <= 0:
            limiter.release()
            raise RuntimeError(
                "Historical scene fetch timed out after "
                f"{self._scene_fetch_timeout_seconds:g} seconds"
            )
        cancel_fetch = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._reconstructor.fetch,
                start,
                end,
                should_stop=cancel_fetch.is_set,
            )
        )
        release_when_done = False

        def finish_abandoned_fetch(task: asyncio.Task) -> None:
            # Retrieve the worker result/exception to avoid an unhandled-task
            # warning, then return its capacity slot.  No CARLA calls occur in
            # this task even when it completes after the client timed out.
            try:
                task.result()
            except BaseException:
                pass
            limiter.release()

        try:
            return await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:
            cancel_fetch.set()
            worker.add_done_callback(finish_abandoned_fetch)
            release_when_done = True
            raise RuntimeError(
                "Historical scene fetch timed out after "
                f"{self._scene_fetch_timeout_seconds:g} seconds"
            ) from exc
        except asyncio.CancelledError:
            cancel_fetch.set()
            worker.add_done_callback(finish_abandoned_fetch)
            release_when_done = True
            raise
        finally:
            if not release_when_done:
                limiter.release()

    async def start(self, start: str, end: str, vehicle_blueprint: str = DEFAULT_VEHICLE) -> dict:
        """Start a driving session: reconstruct scene, spawn vehicle, attach camera.

        If any step fails, _force_cleanup() ensures no actors are leaked.
        """
        if self._active or self._starting:
            raise RuntimeError("Session already active")

        self._starting = True
        try:
            if not any(s.is_active for s in _active_sessions):
                apply_default_drive_weather(self._world)
            self._reconstructor = SceneReconstructor(
                world=self._world,
                carla_map=self._map,
                api_fetcher=self._api_fetcher,
                max_pages=self._scene_fetch_max_pages,
                max_items=self._scene_fetch_max_items,
            )
            fetched_scene = await self._fetch_scene_result(start, end)
            # CARLA actor creation remains on this event-loop thread.  A timed
            # out worker can finish only HTTP/dedup work and can never reach it.
            recon_result = self._reconstructor.spawn(fetched_scene)

            bp_lib = self._world.get_blueprint_library()
            ego_bp = resolve_vehicle_blueprint(bp_lib, vehicle_blueprint)
            if ego_bp is None:
                raise RuntimeError("Vehicle blueprint not found")

            # Tag the ego so ScenarioRunner attaches to it by role_name
            # instead of trying to spawn a duplicate from the .xosc. The role
            # is per-session (see self._ego_role) so SR picks this session's
            # ego specifically when other drivers are sharing the world.
            # coexist: optionally take a VOICES/DT scenario role (e.g. PATH-M-1) so the
            # TENA adapter publishes this browser-driven car.
            self._voices_role = _claim_voices_role(self)
            if self._voices_role:
                self._ego_role = self._voices_role
            ego_bp.set_attribute("role_name", self._ego_role)

            import random
            spawn_points = self._map.get_spawn_points()
            if not spawn_points:
                raise RuntimeError("No spawn points available")

            random.shuffle(spawn_points)
            self.vehicle = None
            if self._voices_role:
                # coexist: reuse the car a previous session left under this role
                self.vehicle = _adopt_kept_ego(self._world, self._voices_role, spawn_points[0])
            if self.vehicle is None:
                for sp in spawn_points:
                    self.vehicle = self._world.try_spawn_actor(ego_bp, sp)
                    if self.vehicle is not None:
                        break
            if self.vehicle is None:
                raise RuntimeError("Failed to spawn vehicle")

            # Physics power cap removed — vehicle runs at stock max_rpm / torque curve.

            # Stable wheel-ground contact at speed. CARLA's default raycast
            # wheels can momentarily lose contact during fast cornering,
            # which feels like the car "gliding" or losing grip. Sweep
            # collision (used in CARLA's own manual_control.py example)
            # tracks the wheel volume across each frame so it can't skip
            # over the road. Pair with a modest tire-friction bump above
            # the Tesla Model 3's stock 3.5 — the stock Tesla is on the
            # slipperier end of CARLA's catalog and the ±0.7 steering cap
            # alone wasn't quite enough to keep it planted at speed.
            try:
                physics = self.vehicle.get_physics_control()
                physics.use_sweep_wheel_collision = True
                wheels = physics.wheels
                for wh in wheels:
                    wh.tire_friction = 4.5
                physics.wheels = wheels
                self.vehicle.apply_physics_control(physics)
            except Exception as e:
                logger.warning("Failed to apply ego physics tweaks: %s", e)

            # Cache vehicle half-extents so camera transforms scale to the
            # actual model (matches manual_control.py's bound_x/y/z idiom).
            try:
                bb = self.vehicle.bounding_box.extent
                self._bound_x = 0.5 + bb.x
                self._bound_y = 0.5 + bb.y
                self._bound_z = 0.5 + bb.z
            except Exception:
                self._bound_x, self._bound_y, self._bound_z = 2.5, 1.0, 0.8

            # Attach RGB camera sensor to the vehicle
            self._attach_camera(bp_lib)

            # Attach this session's semantic/depth camera pairs to this ego.
            # Perception is non-critical: control remains usable if a sensor
            # blueprint is unavailable, and cleanup still detaches partial work.
            try:
                self._perception.attach(self._world, self.vehicle)
            except Exception as e:
                logger.warning("Perception attach failed: %s", e, exc_info=True)

            self._accepting_frames = True
            self._active = True
            self._starting = False
            self.active_camera = "chase"
            self._last_perception_scan_monotonic = None
            self._cached_perception_detections = []

            logger.info(
                "Drive session started: vehicle=%d, objects=%d",
                self.vehicle.id, len(recon_result.spawned_actors),
            )

            sensor_actor_ids = self.sensor_actor_ids()
            scene_actor_ids = self._reconstructor.actor_ids()
            return {
                "type": "session_ready",
                "vehicle_id": self.vehicle.id,
                "objects_count": len(recon_result.spawned_actors),
                "sensor_actor_ids": sensor_actor_ids,
                "scene_actor_ids": scene_actor_ids,
                "ego_role": self._ego_role,
                "owned_actor_ids": self.owned_actor_ids(),
            }
        except Exception:
            # If anything fails during startup, clean up whatever was partially created
            self._force_cleanup()
            raise

    @staticmethod
    def _attachment_for_view(view: str):
        """SpringArmGhost auto-orients the camera toward the parent and
        smoothly lags during rotation — great for external chase-style
        views, terrible for cockpit/hood (would face backward at the
        parent) or bird (spring can't reasonably extend 25 m straight up).
        Match manual_control.py: Rigid for cockpit, SpringArmGhost for
        external follow cameras.
        """
        import carla
        if view in ("hood", "bird"):
            return carla.AttachmentType.Rigid
        return carla.AttachmentType.SpringArmGhost

    def _transform_for_view(self, view: str):
        """Camera transforms scaled by the vehicle's bounding box, copied
        from manual_control.py's `_camera_transforms` list (lines 1080-85).
        """
        import carla
        bx, _, bz = self._bound_x, self._bound_y, self._bound_z
        if view == "hood":
            # manual_control index 1: dashboard / front-bumper view
            return carla.Transform(carla.Location(x=+0.8 * bx, y=0.0, z=1.3 * bz))
        if view == "free":
            # manual_control index 3: high-back chase, slightly tilted
            return carla.Transform(
                carla.Location(x=-2.8 * bx, y=0.0, z=4.6 * bz),
                carla.Rotation(pitch=6.0),
            )
        if view == "bird":
            # No equivalent in manual_control — true top-down for the map
            # view.  Altitude is the zoom level (see zoom_camera).
            return carla.Transform(
                carla.Location(x=0.0, z=self._bird_altitude),
                carla.Rotation(pitch=-90.0),
            )
        # chase (default): manual_control index 0, with z slightly raised
        # because the SpringArmGhost settled position lags below the
        # configured offset, so the configured z has to be a touch above
        # the desired *settled* height.
        return carla.Transform(
            carla.Location(x=-2.0 * bx, y=0.0, z=2.4 * bz),
            carla.Rotation(pitch=8.0),
        )

    def _attach_camera(self, bp_lib):
        """Attach an RGB camera sensor to the vehicle for streaming frames."""
        try:
            camera_bp = bp_lib.find("sensor.camera.rgb")
            if camera_bp is None:
                logger.warning("sensor.camera.rgb blueprint not found")
                return

            # Set camera resolution — lower for streaming performance
            camera_bp.set_attribute("image_size_x", str(self._camera_width))
            camera_bp.set_attribute("image_size_y", str(self._camera_height))
            camera_bp.set_attribute("fov", str(self._camera_fov))
            camera_bp.set_attribute("sensor_tick", "0.05")  # 20 FPS

            # Initial transform: chase camera, scaled to vehicle bounds
            # exactly the way manual_control.py does it (index 0 of its
            # _camera_transforms list).
            cam_transform = self._transform_for_view(self.active_camera)

            self._camera_sensor = self._world.spawn_actor(
                camera_bp, cam_transform, attach_to=self.vehicle,
                attachment_type=self._attachment_for_view(self.active_camera),
            )
            self._camera_sensor.listen(self._on_camera_frame)
            logger.info("Camera sensor attached (%dx%d @ 20fps)", self._camera_width, self._camera_height)
        except ImportError:
            logger.info("CARLA not available — camera sensor skipped (mock mode)")
        except Exception as e:
            logger.warning("Failed to attach camera sensor: %s", e)

    def _on_camera_frame(self, image):
        """Callback from CARLA camera sensor — encode frame to JPEG."""
        if not self._accepting_frames:
            return
        try:
            # Convert CARLA image to numpy array
            array = np.frombuffer(image.raw_data, dtype=np.uint8)
            array = array.reshape((image.height, image.width, 4))  # BGRA
            rgb = array[:, :, :3][:, :, ::-1]  # BGRA → RGB

            # Encode to JPEG
            pil_image = Image.fromarray(rgb)
            buffer = io.BytesIO()
            pil_image.save(buffer, format="JPEG", quality=70)
            jpeg_bytes = buffer.getvalue()

            with self._frame_lock:
                self._latest_frame = jpeg_bytes
        except Exception as e:
            logger.debug("Frame encode error: %s", e)

    def get_latest_frame(self) -> Optional[bytes]:
        """Get the most recent JPEG frame (thread-safe)."""
        with self._frame_lock:
            return self._latest_frame

    def sensor_actor_ids(self) -> list[int]:
        """Return RGB and perception sensor IDs owned by this session."""
        actor_ids = []
        if self._camera_sensor is not None:
            actor_id = getattr(self._camera_sensor, "id", None)
            if isinstance(actor_id, int):
                actor_ids.append(actor_id)
        actor_ids.extend(self._perception.actor_ids())
        return sorted(set(actor_ids))

    def owned_actor_ids(self) -> list[int]:
        """Return every currently known CARLA actor owned by this session."""
        actor_ids = set(self.sensor_actor_ids())
        if self.vehicle is not None and isinstance(getattr(self.vehicle, "id", None), int):
            actor_ids.add(self.vehicle.id)
        if self._reconstructor is not None:
            actor_ids.update(self._reconstructor.actor_ids())
        actor_ids.update(self._dynamic_actors)
        for entry in self._placed_objects:
            actor_id = getattr(entry.get("actor"), "id", None)
            if isinstance(actor_id, int):
                actor_ids.add(actor_id)
        return sorted(actor_ids)

    def _scan_perception_at_sensor_cadence(self) -> list[dict]:
        """Schedule at most one CPU scan and return the latest completed cache.

        Capturing frames and assigning stable IDs stay on the CARLA/event-loop
        thread.  Only immutable numpy-frame analysis runs in the bounded worker,
        so dense semantic masks cannot stall control or ``world.tick``.
        """
        now = time.monotonic()
        retired = self._retired_perception_scan_future
        if retired is not None:
            if not retired.done():
                return list(self._cached_perception_detections)
            try:
                retired.result()
            except Exception:
                pass
            self._retired_perception_scan_future = None

        future = self._perception_scan_future
        if future is not None and future.done():
            generation = self._perception_scan_future_generation
            self._perception_scan_future = None
            self._perception_scan_future_generation = None
            try:
                analyzed = future.result()
                if generation == self._perception_scan_generation and self._active:
                    tracked = self._perception.finalize_scan(analyzed)
                    self._cached_perception_detections = [
                        detection.to_dict() for detection in tracked
                    ]
            except Exception as e:
                if generation == self._perception_scan_generation:
                    logger.warning("Perception scan failed: %s", e, exc_info=True)
                    self._cached_perception_detections = []

        due = (
            self._last_perception_scan_monotonic is None
            or now - self._last_perception_scan_monotonic
            >= self._perception_scan_interval_seconds
        )
        if self._perception_scan_future is None and due:
            snapshot = self._perception.capture_scan_snapshot()
            self._last_perception_scan_monotonic = now
            if snapshot:
                if self._perception_scan_executor is None:
                    self._perception_scan_executor = ThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix=f"v2x-perception-{id(self):x}",
                    )
                future = self._perception_scan_executor.submit(
                    self._perception.analyze_scan_snapshot, snapshot
                )
                self._perception_scan_future = future
                self._perception_scan_future_generation = (
                    self._perception_scan_generation
                )
            else:
                self._cached_perception_detections = []

        return list(self._cached_perception_detections)

    def _shutdown_perception_scan_worker(self) -> None:
        """Invalidate queued/results while allowing pure CPU work to wind down."""
        self._perception_scan_generation += 1
        future = self._perception_scan_future
        self._perception_scan_future = None
        self._perception_scan_future_generation = None
        if future is not None:
            cancelled = future.cancel()
            if not cancelled and not future.done():
                # Do not start a new generation's worker until this pure CPU
                # task winds down; this preserves a strict one-worker bound.
                self._retired_perception_scan_future = future
        executor = self._perception_scan_executor
        self._perception_scan_executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def apply_control(self, steer: float, throttle: float, brake: float, reverse: bool = False) -> dict:
        """Apply vehicle control and return telemetry."""
        if not self._active or self.vehicle is None:
            raise RuntimeError("No active session")

        # Throttle pass-through — top speed is governed by CARLA vehicle physics,
        # same as PythonAPI/examples/manual_control.py.
        capped_throttle = max(0.0, min(1.0, throttle))

        import carla
        control = carla.VehicleControl(
            steer=max(-1.0, min(1.0, steer)),
            throttle=capped_throttle,
            brake=max(0.0, min(1.0, brake)),
            reverse=reverse,
        )

        self.vehicle.apply_control(control)

        transform = self.vehicle.get_transform()
        velocity = self.vehicle.get_velocity()
        speed_ms = math.sqrt(velocity.x**2 + velocity.y**2 + velocity.z**2)
        speed_kmh = speed_ms * 3.6

        detections = self._scan_perception_at_sensor_cadence()

        telemetry = {
            "type": "telemetry",
            "speed": round(speed_kmh, 1),
            "gear": getattr(self.vehicle.get_control(), "gear", 0),
            "pos": [
                round(transform.location.x, 2),
                round(transform.location.y, 2),
                round(transform.location.z, 2),
            ],
            "rot": [
                round(transform.rotation.pitch, 2),
                round(transform.rotation.yaw, 2),
                round(transform.rotation.roll, 2),
            ],
            "steer": round(steer, 3),
            "throttle": round(throttle, 3),
            "brake": round(brake, 3),
            "nearby_actors": self.get_nearby_actors(),
            "dynamic_actors": self.get_dynamic_actors_snapshot(),
            # Always include the list so the dashboard can distinguish a
            # healthy empty scan from the historical missing-payload regression.
            "detections": detections,
        }
        self._draw_dynamic_actor_geofences()
        eva_alerts = self._check_emergency_vehicle_proximity()
        yield_alerts = self._check_yield_to_firetruck()
        all_alerts = eva_alerts + yield_alerts
        if all_alerts:
            telemetry["v2x_alerts"] = all_alerts
        return telemetry

    def _update_camera_transform(self):
        """Switch to the active view by respawning the camera sensor.

        We don't use `set_transform` here because the camera is attached
        with `SpringArmGhost`, which has an internal arm-length that
        evolves over time from the parent toward the desired offset.
        Calling `set_transform` snaps the spring to the full configured
        offset (the rigid desired position), bypassing the natural
        settling animation. Respawning gives every view-switch the same
        fresh spring extension behavior the initial spawn has.
        """
        if self._camera_sensor is None or self.vehicle is None:
            return
        try:
            self._accepting_frames = False
            try:
                self._camera_sensor.stop()
            except Exception:
                pass
            try:
                self._camera_sensor.destroy()
            except Exception:
                pass

            bp_lib = self._world.get_blueprint_library()
            camera_bp = bp_lib.find("sensor.camera.rgb")
            camera_bp.set_attribute("image_size_x", str(self._camera_width))
            camera_bp.set_attribute("image_size_y", str(self._camera_height))
            camera_bp.set_attribute("fov", str(self._camera_fov))
            camera_bp.set_attribute("sensor_tick", "0.05")
            for key, value in self._camera_extra_attrs.items():
                try:
                    camera_bp.set_attribute(key, str(value))
                except Exception:
                    pass

            new_transform = self._transform_for_view(self.active_camera)
            self._camera_sensor = self._world.spawn_actor(
                camera_bp, new_transform, attach_to=self.vehicle,
                attachment_type=self._attachment_for_view(self.active_camera),
            )
            self._camera_sensor.listen(self._on_camera_frame)
            self._accepting_frames = True
        except Exception as e:
            logger.warning("Camera respawn for view switch failed: %s", e)

    def respawn(self) -> dict:
        """Teleport the vehicle to a random spawn point on the road."""
        if not self._active or self.vehicle is None:
            raise RuntimeError("No active session")

        import random
        spawn_points = self._map.get_spawn_points()
        if not spawn_points:
            raise RuntimeError("No spawn points available")

        new_spawn = random.choice(spawn_points)
        self.vehicle.set_transform(new_spawn)

        # Zero out velocity so the car doesn't keep flying
        try:
            import carla
            self.vehicle.set_target_velocity(carla.Vector3D(0, 0, 0))
        except Exception:
            pass

        transform = self.vehicle.get_transform()
        logger.info("Vehicle respawned at (%.1f, %.1f, %.1f)",
                     transform.location.x, transform.location.y, transform.location.z)

        return {
            "type": "respawned",
            "pos": [
                round(transform.location.x, 2),
                round(transform.location.y, 2),
                round(transform.location.z, 2),
            ],
        }

    @staticmethod
    def _teleport_number(value, name: str) -> float:
        """Coerce a protocol number while rejecting booleans/NaN/infinity."""
        if value is None or isinstance(value, bool):
            raise ValueError(f"teleport requires finite numeric '{name}'")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"teleport requires finite numeric '{name}'") from exc
        if not math.isfinite(number):
            raise ValueError(f"teleport requires finite numeric '{name}'")
        return number

    @staticmethod
    def _teleport_request_id(value) -> str:
        """Validate the correlation id required by the teleport protocol."""
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > TELEPORT_REQUEST_ID_MAX_LENGTH
        ):
            raise ValueError(
                "teleport requires a non-empty string 'request_id' of at most "
                f"{TELEPORT_REQUEST_ID_MAX_LENGTH} characters"
            )
        return value

    def _validate_teleport_map_bounds(self, x: float, y: float) -> None:
        if abs(x) > TELEPORT_COORD_ABS_LIMIT_M or abs(y) > TELEPORT_COORD_ABS_LIMIT_M:
            raise ValueError("teleport coordinates exceed the world safety limit")

        # Spawn points provide a cheap map-specific envelope.  A generous
        # margin permits roads without spawn points while rejecting obviously
        # unrelated coordinates before CARLA attempts the transform.
        spawn_points = self._map.get_spawn_points()
        if not spawn_points:
            raise RuntimeError("Active map has no spawn points")
        xs = [float(point.location.x) for point in spawn_points]
        ys = [float(point.location.y) for point in spawn_points]
        if not (
            min(xs) - TELEPORT_MAP_MARGIN_M
            <= x
            <= max(xs) + TELEPORT_MAP_MARGIN_M
            and min(ys) - TELEPORT_MAP_MARGIN_M
            <= y
            <= max(ys) + TELEPORT_MAP_MARGIN_M
        ):
            raise ValueError("teleport coordinates are outside the active map envelope")

    async def teleport(self, x, y, z=None, yaw=None) -> dict:
        """Move only this session's ego to a validated active-map coordinate."""
        if not self._active or self.vehicle is None:
            raise RuntimeError("No active session")

        import carla

        x_value = self._teleport_number(x, "x")
        y_value = self._teleport_number(y, "y")
        self._validate_teleport_map_bounds(x_value, y_value)

        current = self.vehicle.get_transform()
        probe = carla.Location(
            x=x_value,
            y=y_value,
            z=float(current.location.z),
        )
        waypoint = self._map.get_waypoint(probe, project_to_road=True)
        if waypoint is None:
            raise ValueError("teleport target has no nearby road waypoint")
        road_location = waypoint.transform.location
        road_distance = math.hypot(
            x_value - float(road_location.x),
            y_value - float(road_location.y),
        )
        if road_distance > TELEPORT_MAX_ROAD_DISTANCE_M:
            raise ValueError("teleport target is too far from a road")

        snapped_to_road = z is None
        if z is None:
            z_value = float(road_location.z) + 0.5
        else:
            z_value = self._teleport_number(z, "z")
            if not TELEPORT_MIN_Z_M <= z_value <= TELEPORT_MAX_Z_M:
                raise ValueError(
                    f"teleport z must be between {TELEPORT_MIN_Z_M:g} and "
                    f"{TELEPORT_MAX_Z_M:g} metres"
                )
            if abs(z_value - float(road_location.z)) > TELEPORT_MAX_ROAD_Z_OFFSET_M:
                raise ValueError("teleport z is too far from the road surface")

        if yaw is None:
            yaw_value = float(current.rotation.yaw)
        else:
            yaw_value = self._teleport_number(yaw, "yaw")
            if abs(yaw_value) > TELEPORT_MAX_ABS_YAW_DEG:
                raise ValueError(
                    f"teleport yaw must be within ±{TELEPORT_MAX_ABS_YAW_DEG:g} degrees"
                )
            yaw_value = ((yaw_value + 180.0) % 360.0) - 180.0

        target = carla.Transform(
            carla.Location(x=x_value, y=y_value, z=z_value),
            carla.Rotation(pitch=0.0, yaw=yaw_value, roll=0.0),
        )
        self.vehicle.set_transform(target)

        # Reset both linear and angular momentum.  CARLA 0.9/0.10 expose
        # these on actors; keep angular reset optional for older test doubles.
        zero = carla.Vector3D(0.0, 0.0, 0.0)
        self.vehicle.set_target_velocity(zero)
        set_angular = getattr(self.vehicle, "set_target_angular_velocity", None)
        if set_angular is not None:
            set_angular(carla.Vector3D(0.0, 0.0, 0.0))

        # RR/CARLA 0.10 can return the actor's pre-mutation transform until the
        # bridge's synchronous 20 Hz tick loop advances. Yield to that loop and
        # confirm the mutation without calling world.tick()/wait_for_tick from
        # this handler, which would compete with the single CARLA tick owner.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TELEPORT_CONFIRM_TIMEOUT_SECONDS
        await asyncio.sleep(
            min(
                TELEPORT_CONFIRM_POLL_INTERVAL_SECONDS,
                TELEPORT_CONFIRM_TIMEOUT_SECONDS,
            )
        )
        actual = self.vehicle.get_transform()
        confirmation_error = math.inf
        confirmation_yaw_error = math.inf
        while True:
            confirmation_error = math.sqrt(
                (float(actual.location.x) - x_value) ** 2
                + (float(actual.location.y) - y_value) ** 2
                + (float(actual.location.z) - z_value) ** 2
            )
            confirmation_yaw_error = abs(
                ((float(actual.rotation.yaw) - yaw_value + 180.0) % 360.0)
                - 180.0
            )
            if (
                confirmation_error <= TELEPORT_CONFIRM_TOLERANCE_M
                and confirmation_yaw_error
                <= TELEPORT_CONFIRM_YAW_TOLERANCE_DEG
            ):
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(
                min(TELEPORT_CONFIRM_POLL_INTERVAL_SECONDS, remaining)
            )
            actual = self.vehicle.get_transform()
        if (
            confirmation_error > TELEPORT_CONFIRM_TOLERANCE_M
            or confirmation_yaw_error > TELEPORT_CONFIRM_YAW_TOLERANCE_DEG
        ):
            raise RuntimeError(
                "CARLA did not confirm the requested Teleport pose"
            )
        logger.info(
            "Session %s teleported ego %s to (%.1f, %.1f, %.1f)",
            self._ego_role,
            getattr(self.vehicle, "id", "unknown"),
            actual.location.x,
            actual.location.y,
            actual.location.z,
        )
        return {
            "type": "teleported",
            "success": True,
            "pos": [
                round(actual.location.x, 2),
                round(actual.location.y, 2),
                round(actual.location.z, 2),
            ],
            "yaw": round(actual.rotation.yaw, 2),
            "snapped_to_road": snapped_to_road,
        }

    def spawn_object(self, blueprint_id: str, forward_offset: float = 8.0) -> dict:
        """Spawn an object near the vehicle's current position.

        The object is placed forward_offset meters ahead of the vehicle,
        matching the vehicle's yaw so parked cars face the same direction.
        """
        if not self._active or self.vehicle is None:
            raise RuntimeError("No active session")

        import carla

        bp_lib = self._world.get_blueprint_library()
        bp = bp_lib.find(blueprint_id)
        if bp is None:
            raise ValueError(f"Blueprint not found: {blueprint_id}")

        # Calculate spawn position: forward_offset meters ahead of the vehicle
        vehicle_transform = self.vehicle.get_transform()
        yaw_rad = math.radians(vehicle_transform.rotation.yaw)
        spawn_loc = carla.Location(
            x=vehicle_transform.location.x + forward_offset * math.cos(yaw_rad),
            y=vehicle_transform.location.y + forward_offset * math.sin(yaw_rad),
            z=vehicle_transform.location.z + 0.5,  # slightly above ground to avoid clipping
        )
        spawn_transform = carla.Transform(
            spawn_loc,
            carla.Rotation(yaw=vehicle_transform.rotation.yaw),
        )

        actor = self._world.try_spawn_actor(bp, spawn_transform)
        if actor is None:
            raise RuntimeError(f"Failed to spawn {blueprint_id} — location may be blocked")

        pos = [round(spawn_loc.x, 2), round(spawn_loc.y, 2), round(spawn_loc.z, 2)]
        yaw = round(vehicle_transform.rotation.yaw, 2)
        self._placed_objects.append({
            "actor": actor,
            "blueprint": blueprint_id,
            "pos": pos,
            "yaw": yaw,
        })

        logger.info("Placed object %s (id=%d) at (%.1f, %.1f, %.1f)",
                     blueprint_id, actor.id, spawn_loc.x, spawn_loc.y, spawn_loc.z)

        return {
            "type": "object_spawned",
            "actor_id": actor.id,
            "blueprint": blueprint_id,
            "pos": pos,
            "placed_count": len(self._placed_objects),
        }

    def undo_place(self) -> dict:
        """Remove the most recently placed object."""
        if not self._active:
            raise RuntimeError("No active session")
        if not self._placed_objects:
            return {"type": "undo_empty", "message": "No objects to undo"}

        entry = self._placed_objects.pop()
        actor = entry["actor"]
        try:
            actor.destroy()
            logger.info("Undid placement of %s (id=%d)", entry["blueprint"], actor.id)
        except Exception as e:
            logger.warning("Failed to destroy placed object: %s", e)

        return {
            "type": "object_removed",
            "blueprint": entry["blueprint"],
            "pos": entry["pos"],
            "placed_count": len(self._placed_objects),
        }

    def get_placed_snapshot(self) -> list[dict]:
        """Return a serializable snapshot of all placed objects (no actor refs)."""
        return [
            {"blueprint": o["blueprint"], "pos": o["pos"], "yaw": o.get("yaw", 0)}
            for o in self._placed_objects
        ]

    def load_scenario_objects(self, objects: list[dict]) -> dict:
        """Spawn a list of objects from a scenario definition."""
        if not self._active:
            raise RuntimeError("No active session")

        import carla

        bp_lib = self._world.get_blueprint_library()
        spawned = 0
        failed = 0

        for obj in objects:
            bp = bp_lib.find(obj["blueprint"])
            if bp is None:
                logger.warning("Scenario: blueprint not found: %s", obj["blueprint"])
                failed += 1
                continue

            pos = obj["pos"]
            yaw = obj.get("yaw", 0)
            transform = carla.Transform(
                carla.Location(x=pos[0], y=pos[1], z=pos[2]),
                carla.Rotation(yaw=yaw),
            )

            actor = self._world.try_spawn_actor(bp, transform)
            if actor is None:
                logger.warning("Scenario: failed to spawn %s at %s", obj["blueprint"], pos)
                failed += 1
                continue

            self._placed_objects.append({
                "actor": actor,
                "blueprint": obj["blueprint"],
                "pos": pos,
                "yaw": yaw,
            })
            spawned += 1

        logger.info("Scenario loaded: %d spawned, %d failed", spawned, failed)
        return {
            "type": "scenario_loaded",
            "spawned": spawned,
            "failed": failed,
            "placed_count": len(self._placed_objects),
        }

    def set_camera_settings(self, params: dict) -> dict:
        """Update camera sensor post-processing attributes at runtime.

        Destroys the current camera sensor and respawns it with the new
        attributes, since CARLA does not support changing blueprint
        attributes after spawn.
        """
        if not self._active or self._camera_sensor is None:
            raise RuntimeError("No active session or camera")

        # Stop accepting frames during swap
        self._accepting_frames = False

        # Attached actors require a vehicle-relative transform.  Feeding the
        # sensor's world transform back here causes drift after every respawn.
        local_transform = self._transform_for_view(self.active_camera)

        # Stop and destroy old sensor
        try:
            self._camera_sensor.stop()
        except Exception:
            pass
        try:
            self._camera_sensor.destroy()
        except Exception:
            pass

        # Pull resolution / fov into persistent instance attrs so that later
        # post-processing edits don't revert the user's aspect ratio.
        if "image_size_x" in params:
            try:
                self._camera_width = max(480, min(1280, int(float(params.pop("image_size_x")))))
            except (TypeError, ValueError):
                params.pop("image_size_x", None)
        if "image_size_y" in params:
            try:
                self._camera_height = max(480, min(1280, int(float(params.pop("image_size_y")))))
            except (TypeError, ValueError):
                params.pop("image_size_y", None)
        if "fov" in params:
            try:
                self._camera_fov = max(50.0, min(110.0, float(params.pop("fov"))))
            except (TypeError, ValueError):
                params.pop("fov", None)

        # Respawn with new attributes
        bp_lib = self._world.get_blueprint_library()
        camera_bp = bp_lib.find("sensor.camera.rgb")

        # Base attributes (use instance state so aspect ratio persists)
        camera_bp.set_attribute("image_size_x", str(self._camera_width))
        camera_bp.set_attribute("image_size_y", str(self._camera_height))
        camera_bp.set_attribute("fov", str(self._camera_fov))
        camera_bp.set_attribute("sensor_tick", "0.05")

        # Apply remaining post-processing settings, persisting them so
        # later view-switch respawns don't reset the user's tweaks.
        safe_params = {}
        for key, value in params.items():
            if key == "exposure_mode":
                value_str = str(value)
                safe_params[key] = value_str if value_str in {"manual", "histogram"} else "histogram"
                continue
            if key == "enable_postprocess_effects":
                safe_params[key] = "true"
                continue
            limits = SAFE_CAMERA_ATTR_LIMITS.get(key)
            if limits is None:
                logger.debug("Ignoring unsupported camera attribute '%s'", key)
                continue
            safe_params[key] = _safe_float(params, key, limits[0], limits)

        for key, value in safe_params.items():
            try:
                camera_bp.set_attribute(key, str(value))
                self._camera_extra_attrs[key] = str(value)
            except Exception as e:
                logger.debug("Camera attribute '%s' failed: %s", key, e)

        self._camera_sensor = self._world.spawn_actor(
            camera_bp, local_transform, attach_to=self.vehicle,
            attachment_type=self._attachment_for_view(self.active_camera),
        )
        self._camera_sensor.listen(self._on_camera_frame)
        self._accepting_frames = True

        logger.info(
            "Camera settings updated: %dx%d fov=%.1f, %d extra attrs",
            self._camera_width, self._camera_height, self._camera_fov, len(safe_params),
        )
        return {
            "type": "camera_settings_set",
            "width": self._camera_width,
            "height": self._camera_height,
            "fov": self._camera_fov,
        }

    def _get_traffic_manager(self):
        """Return a CARLA Traffic Manager and its port."""
        import carla
        from digital_twin_bridge.config import Config

        config = Config.from_env()
        client = carla.Client(config.CARLA_HOST, config.CARLA_PORT)

        client.set_timeout(10.0)
        tm = client.get_trafficmanager(config.TM_PORT)  # coexist: DT's manual car owns 8000
        tm.set_synchronous_mode(True)
        if not config.tm_osm_mode:
            try:
                tm.set_osm_mode(False)  # CARLA 0.10.0 default deletes cars at dead-end roads
            except Exception:
                pass
        return tm, tm.get_port()

    def _build_transform(self, location, rotation):
        import carla
        return carla.Transform(location, rotation)

    def spawn_dynamic_actor(
        self,
        blueprint_id: str,
        geofence_radius: float = 35.0,
        message: str = "",
    ) -> dict:
        """Spawn one selected vehicle as an autopilot actor with a moving geofence."""
        if not self._active or self.vehicle is None:
            raise RuntimeError("No active session")
        if not blueprint_id.startswith("vehicle."):
            raise ValueError("Dynamic actors must use vehicle blueprints")

        import random

        bp_lib = self._world.get_blueprint_library()
        bp = bp_lib.find(blueprint_id)
        if bp is None:
            raise ValueError(f"Blueprint not found: {blueprint_id}")

        if blueprint_wheel_count(bp) != 4:
            raise ValueError("Dynamic actors must be four-wheeled vehicles")

        radius = max(5.0, min(250.0, float(geofence_radius)))
        actor_name = display_name_from_blueprint(blueprint_id)
        actor_message = str(message).strip() or f"{actor_name} geofence active"

        tm, tm_port = self._get_traffic_manager()

        if bp.has_attribute("color"):
            colors = bp.get_attribute("color").recommended_values
            if colors:
                bp.set_attribute("color", random.choice(colors))
        bp.set_attribute("role_name", "dynamic_geofence")

        spawn_points = self._filter_spawn_points_near_placed(self._map.get_spawn_points(), radius=12.0)
        random.shuffle(spawn_points)
        if not spawn_points:
            raise RuntimeError("No safe spawn points available for dynamic actor")

        actor = None
        for spawn_point in spawn_points:
            actor = self._world.try_spawn_actor(bp, spawn_point)
            if actor is not None:
                break
        if actor is None:
            raise RuntimeError(f"Failed to spawn {blueprint_id} for autopilot")

        try:
            actor.set_autopilot(True, tm_port)
        except Exception:
            try:
                actor.destroy()
            except Exception as e:
                logger.warning("Failed to destroy dynamic actor after autopilot setup failed: %s", e)
            raise

        try:
            tm.ignore_lights_percentage(actor, 0.0)
            tm.ignore_signs_percentage(actor, 0.0)
        except Exception:
            pass

        meta = DynamicActorMeta(
            actor_id=actor.id,
            blueprint=blueprint_id,
            name=actor_name,
            geofence_radius=radius,
            message=actor_message,
        )
        self._dynamic_actors[actor.id] = meta
        _dynamic_actor_ids.add(actor.id)

        logger.info(
            "Spawned dynamic actor %s (id=%d) geofence=%.1fm",
            blueprint_id,
            actor.id,
            radius,
        )

        return {
            "type": "dynamic_actor_spawned",
            "actor": self._serialize_dynamic_actor(actor, meta),
            "count": len(self._dynamic_actors),
        }

    def _serialize_dynamic_actor(self, actor, meta: DynamicActorMeta) -> dict:
        transform = actor.get_transform()
        return {
            "actor_id": meta.actor_id,
            "blueprint": meta.blueprint,
            "name": meta.name,
            "pos": [
                round(transform.location.x, 2),
                round(transform.location.y, 2),
                round(transform.location.z, 2),
            ],
            "yaw": round(transform.rotation.yaw, 1),
            "geofence_radius": meta.geofence_radius,
            "message": meta.message,
            "autopilot": True,
        }

    def get_dynamic_actors_snapshot(self) -> list[dict]:
        """Return live dynamic actor positions and prune actors no longer in the world."""
        snapshot: list[dict] = []
        stale_ids: list[int] = []

        for actor_id, meta in self._dynamic_actors.items():
            actor = self._world.get_actor(actor_id)
            if actor is None or getattr(actor, "is_destroyed", False):
                stale_ids.append(actor_id)
                continue
            snapshot.append(self._serialize_dynamic_actor(actor, meta))

        for actor_id in stale_ids:
            self._dynamic_actors.pop(actor_id, None)
            _dynamic_actor_ids.discard(actor_id)

        return snapshot

    def _draw_dynamic_actor_geofences(self) -> None:
        """Draw moving dynamic actor geofence outlines in CARLA debug view."""
        if not self._dynamic_actors:
            return

        now = time.monotonic()
        if now - self._last_dynamic_geofence_draw < DYNAMIC_GEOFENCE_DRAW_INTERVAL:
            return
        self._last_dynamic_geofence_draw = now

        try:
            import carla
            color = carla.Color(255, 60, 60, 220)
        except Exception:
            return

        for actor_id, meta in list(self._dynamic_actors.items()):
            actor = self._world.get_actor(actor_id)
            if actor is None or getattr(actor, "is_destroyed", False):
                continue

            radius = float(meta.geofence_radius)
            if not math.isfinite(radius) or radius <= 0:
                continue

            try:
                center = actor.get_transform().location
                z = center.z + 0.20
                points = [
                    carla.Location(
                        x=center.x + math.cos((2 * math.pi * i) / DYNAMIC_GEOFENCE_DRAW_SEGMENTS) * radius,
                        y=center.y + math.sin((2 * math.pi * i) / DYNAMIC_GEOFENCE_DRAW_SEGMENTS) * radius,
                        z=z,
                    )
                    for i in range(DYNAMIC_GEOFENCE_DRAW_SEGMENTS)
                ]
                for i, start in enumerate(points):
                    self._world.debug.draw_line(
                        start,
                        points[(i + 1) % len(points)],
                        thickness=0.12,
                        color=color,
                        life_time=DYNAMIC_GEOFENCE_DRAW_LIFETIME,
                    )
            except Exception as e:
                logger.debug("Failed to draw dynamic geofence for actor %d: %s", actor_id, e)

    def _destroy_dynamic_actor(self, actor_id: int) -> bool:
        actor = self._world.get_actor(actor_id)
        destroyed = False
        if actor is not None:
            try:
                actor.set_autopilot(False, _drive_tm_port())
            except Exception:
                pass
            try:
                actor.destroy()
                destroyed = True
            except Exception as e:
                logger.debug("Failed to destroy dynamic actor %d: %s", actor_id, e)

        self._dynamic_actors.pop(actor_id, None)
        _dynamic_actor_ids.discard(actor_id)
        return destroyed

    def despawn_dynamic_actor(self, actor_id: int) -> dict:
        """Remove one Add Actor autopilot vehicle."""
        if not self._active:
            raise RuntimeError("No active session")

        actor_id = int(actor_id)
        if actor_id not in self._dynamic_actors:
            return {"type": "dynamic_actor_missing", "actor_id": actor_id, "count": len(self._dynamic_actors)}

        self._destroy_dynamic_actor(actor_id)
        return {"type": "dynamic_actor_despawned", "actor_id": actor_id, "count": len(self._dynamic_actors)}

    def despawn_dynamic_actors(self) -> dict:
        """Remove all Add Actor autopilot vehicles."""
        if not self._active:
            raise RuntimeError("No active session")

        count = 0
        for actor_id in list(self._dynamic_actors):
            if self._destroy_dynamic_actor(actor_id):
                count += 1
        return {"type": "dynamic_actors_despawned", "count": count}

    def spawn_traffic(self, preset: str = "medium") -> dict:
        """Spawn autonomous NPC vehicles using CARLA's Traffic Manager.

        Replaces any existing traffic. Uses preset config for count + behavior.
        """
        if not self._active:
            raise RuntimeError("No active session")

        import random

        # Clean up existing traffic first
        self.despawn_traffic()

        config = TRAFFIC_PRESETS.get(preset, TRAFFIC_PRESETS["medium"])
        target_count = config["vehicles"]

        if target_count == 0:
            return {"type": "traffic_spawned", "preset": preset, "count": 0}

        tm, tm_port = self._get_traffic_manager()

        tm.global_percentage_speed_difference(config["speed_diff"])
        tm.set_global_distance_to_leading_vehicle(config["distance"])

        bp_lib = self._world.get_blueprint_library()
        vehicle_bps = [bp for bp in bp_lib.filter("vehicle.*")
                       if blueprint_wheel_count(bp) == 4]

        spawn_points = self._map.get_spawn_points()
        # Drop spawn points sitting on top of the player, the trajectory
        # car, or any user/scenario-placed actor. Without this an autopilot
        # NPC spawns at the same point and physics shoves the placement
        # off-road — which looks like the placed actor "disappeared".
        spawn_points = self._filter_spawn_points_near_placed(spawn_points, radius=8.0)
        random.shuffle(spawn_points)

        available_spawns = spawn_points[: min(len(spawn_points), target_count)]

        spawned = 0
        for sp in available_spawns:
            bp = random.choice(vehicle_bps)
            if bp.has_attribute("color"):
                colors = bp.get_attribute("color").recommended_values
                if colors:
                    bp.set_attribute("color", random.choice(colors))
            bp.set_attribute("role_name", "autopilot")

            actor = self._world.try_spawn_actor(bp, sp)
            if actor is None:
                continue

            actor.set_autopilot(True, tm_port)

            # Per-vehicle aggression
            if config["ignore_lights"] > 0:
                tm.ignore_lights_percentage(actor, float(config["ignore_lights"]))
            if config["ignore_signs"] > 0:
                tm.ignore_signs_percentage(actor, float(config["ignore_signs"]))

            _traffic_actor_ids.add(actor.id)
            spawned += 1

        logger.info("Spawned %d traffic vehicles (preset=%s)", spawned, preset)
        return {"type": "traffic_spawned", "preset": preset, "count": spawned}

    def _filter_spawn_points_near_placed(self, spawn_points, radius: float = 8.0):
        """Return spawn points not within ``radius`` of any protected actor.

        Protected actors: the player vehicle, every entry in
        ``_placed_objects`` (user spawns + scenario loads), dynamic
        Add Actor vehicles, and the trajectory player's car if it's active.
        Used by ``spawn_traffic`` and dynamic actor spawning to keep
        autopilot vehicles from spawning on top of protected actors.
        """
        blocked: list[tuple[float, float]] = []

        if self.vehicle is not None:
            try:
                loc = self.vehicle.get_transform().location
                blocked.append((loc.x, loc.y))
            except Exception:
                pass

        for entry in self._placed_objects:
            actor = entry.get("actor")
            if actor is not None:
                try:
                    loc = actor.get_transform().location
                    blocked.append((loc.x, loc.y))
                    continue
                except Exception:
                    pass
            # Fall back to the recorded spawn pos if the actor is gone.
            pos = entry.get("pos")
            if pos and len(pos) >= 2:
                blocked.append((float(pos[0]), float(pos[1])))

        for actor_id in self._dynamic_actors:
            actor = self._world.get_actor(actor_id)
            if actor is None or getattr(actor, "is_destroyed", False):
                continue
            try:
                loc = actor.get_transform().location
                blocked.append((loc.x, loc.y))
            except Exception:
                pass

        tp = self._trajectory_player
        if tp is not None and tp.is_active() and tp.vehicle is not None:
            try:
                loc = tp.vehicle.get_transform().location
                blocked.append((loc.x, loc.y))
            except Exception:
                pass

        if not blocked:
            return list(spawn_points)

        r2 = radius * radius
        safe = []
        for sp in spawn_points:
            sx, sy = sp.location.x, sp.location.y
            if any((sx - bx) * (sx - bx) + (sy - by) * (sy - by) < r2 for bx, by in blocked):
                continue
            safe.append(sp)
        return safe

    def clear_non_ego_vehicles(self) -> dict:
        """Destroy every vehicle in the world that isn't tagged as ego.

        Preserves any actor whose role_name starts with ``"ego_vehicle"`` so
        every drive session keeps its car (each session stamps its ego with a
        per-session unique suffix; see ``self._ego_role``). Wipes traffic
        NPCs, OpenSCENARIO actors, the trajectory playback car, and any
        user-placed vehicles.
        """
        if not self._active:
            raise RuntimeError("No active session")

        destroyed_ids: set[int] = set()
        preserved = 0
        for actor in self._world.get_actors().filter("vehicle.*"):
            role = actor.attributes.get("role_name", "") if actor.attributes else ""
            if role.startswith("ego_vehicle"):
                preserved += 1
                continue
            if _protect_foreign() and not is_drive_owned(actor, _voices_roles(), _keep_voices_ego()):
                preserved += 1  # coexist: other clients' cars (DT, HIL_Tool) are not ours to clear
                continue
            try:
                actor.set_autopilot(False, _drive_tm_port())
            except Exception:
                pass
            try:
                actor.destroy()
                destroyed_ids.add(actor.id)
            except Exception as e:
                logger.debug("Failed to destroy actor %d: %s", actor.id, e)

        _traffic_actor_ids.difference_update(destroyed_ids)
        self._placed_objects = [
            o for o in self._placed_objects
            if o.get("actor") is not None and o["actor"].id not in destroyed_ids
        ]

        logger.info(
            "Cleared %d non-ego vehicles (preserved %d ego)",
            len(destroyed_ids), preserved,
        )
        return {
            "type": "non_ego_vehicles_cleared",
            "destroyed": len(destroyed_ids),
            "preserved": preserved,
            "placed_count": len(self._placed_objects),
        }

    def despawn_traffic(self) -> dict:
        """Remove all traffic vehicles spawned by spawn_traffic."""
        if not self._active:
            raise RuntimeError("No active session")

        destroyed = 0
        for actor_id in list(_traffic_actor_ids):
            actor = self._world.get_actor(actor_id)
            if actor is not None:
                try:
                    actor.set_autopilot(False, _drive_tm_port())
                except Exception:
                    pass
                try:
                    actor.destroy()
                    destroyed += 1
                except Exception as e:
                    logger.debug("Failed to destroy traffic %d: %s", actor_id, e)
            _traffic_actor_ids.discard(actor_id)

        logger.info("Despawned %d traffic vehicles", destroyed)
        return {"type": "traffic_despawned", "count": destroyed}

    def _check_emergency_vehicle_proximity(self) -> list[dict]:
        """Return a v2x_alert for every firetruck approaching from behind the ego, every tick.

        Only firetrucks behind the ego (negative projection on the ego's
        forward axis) qualify — there's no point telling the driver to pull
        over for a truck they've already passed.

        The browser dedups by ``id``: the first message creates a toast, every
        subsequent message updates the same toast's distance in place. The
        toast auto-dismisses when no message arrives for the actor (i.e. it
        left range or was destroyed). No backend-side debouncing — keeping
        emission stateless avoids the prior "velocity dot oscillates around
        zero → repeated re-alerts" bug.
        """
        if self.vehicle is None or self._world is None:
            return []

        player_transform = self.vehicle.get_transform()
        player_loc = player_transform.location
        forward = player_transform.get_forward_vector()
        threshold_sq = self._eva_warning_distance_m * self._eva_warning_distance_m
        alerts: list[dict] = []

        for actor in self._world.get_actors().filter("vehicle.carlamotors.firetruck"):
            if actor.id == self.vehicle.id:
                continue
            loc = actor.get_transform().location
            dx = loc.x - player_loc.x
            dy = loc.y - player_loc.y
            dist_sq = dx * dx + dy * dy
            if dist_sq > threshold_sq:
                continue
            # Project ego→truck displacement onto the ego's forward axis.
            # Negative means the truck is behind the ego.
            if forward.x * dx + forward.y * dy >= 0:
                continue
            alerts.append({
                "id": actor.id,
                "message": "Firetruck approaching from behind",
                "signal_type": "warning",
                "distance": round(math.sqrt(dist_sq), 1),
            })

        return alerts

    def _check_yield_to_firetruck(self) -> list[dict]:
        """Return a v2x_alert when the ego has been blocking a firetruck for >10s.

        "Blocking" is from the truck's perspective: the ego sits ahead along
        the truck's forward axis, within ``eva_warning_distance_m`` meters,
        and within ~4 m of its centerline (about a lane width). The 10-second
        debounce avoids triggering on transient passes (oncoming lanes,
        crossing intersections at speed) — only sustained obstruction trips
        the alert.

        Independent of ``_check_emergency_vehicle_proximity``: that one keys
        off the ego's heading (truck is behind ego) while this one keys off
        the truck's heading. Both can fire at once when the ego is stopped
        in the truck's path. Alert ``id`` is offset by 1_000_000 so the
        browser keeps the two toasts as separate entries.
        """
        if self.vehicle is None or self._world is None:
            return []

        now = time.monotonic()
        ego_loc = self.vehicle.get_transform().location
        threshold = self._eva_warning_distance_m
        threshold_sq = threshold * threshold
        alerts: list[dict] = []
        seen_truck_ids: set[int] = set()

        for actor in self._world.get_actors().filter("vehicle.carlamotors.firetruck"):
            if actor.id == self.vehicle.id:
                continue
            t = actor.get_transform()
            truck_loc = t.location
            dx = ego_loc.x - truck_loc.x
            dy = ego_loc.y - truck_loc.y
            dist_sq = dx * dx + dy * dy
            if dist_sq > threshold_sq:
                continue
            forward = t.get_forward_vector()
            right = t.get_right_vector()
            forward_dist = forward.x * dx + forward.y * dy
            lateral = abs(right.x * dx + right.y * dy)
            if forward_dist <= 0 or lateral > 4.0:
                continue

            seen_truck_ids.add(actor.id)
            since = self._in_front_since.get(actor.id)
            if since is None:
                self._in_front_since[actor.id] = now
                continue
            if now - since < 10.0:
                continue

            alerts.append({
                "id": actor.id + 1_000_000,
                "message": "Yield to clear firetruck path",
                "signal_type": "warning",
                "distance": round(math.sqrt(dist_sq), 1),
            })

        # Reset the timer for trucks no longer in the cone (drove past, swerved
        # away, destroyed). Without this, a brief gap and re-entry would skip
        # the 10s wait.
        for tid in list(self._in_front_since):
            if tid not in seen_truck_ids:
                del self._in_front_since[tid]

        return alerts

    def get_nearby_actors(self, radius: float = 250.0) -> list[dict]:
        """Return all vehicles within radius meters of the player vehicle.

        Used to enrich telemetry with actors for the mini-map display.
        """
        if self.vehicle is None:
            return []

        player_loc = self.vehicle.get_transform().location
        actors = []

        for a in self._world.get_actors().filter("vehicle.*"):
            if a.id == self.vehicle.id:
                continue
            t = a.get_transform()
            dx = t.location.x - player_loc.x
            dy = t.location.y - player_loc.y
            if dx * dx + dy * dy > radius * radius:
                continue
            actors.append({
                "id": a.id,
                "pos": [round(t.location.x, 2), round(t.location.y, 2)],
                "yaw": round(t.rotation.yaw, 1),
                "type": (
                    "dynamic" if a.id in _dynamic_actor_ids
                    else "traffic" if a.id in _traffic_actor_ids
                    else "other"
                ),
            })

        return actors

    def set_weather(self, params: dict) -> dict:
        """Apply weather parameters to the CARLA world."""
        if not self._active:
            raise RuntimeError("No active session")

        import carla

        safe_params = safe_drive_weather(params)
        weather = carla.WeatherParameters(**safe_params)
        self._world.set_weather(weather)
        logger.info(
            "Weather updated: sun_alt=%.0f, cloud=%.0f, rain=%.0f, fog=%.0f",
            weather.sun_altitude_angle,
            weather.cloudiness,
            weather.precipitation,
            weather.fog_density,
        )
        return {"type": "weather_set", "params": safe_params}

    def sync_v2x_zones(self, zones: list[dict]) -> dict:
        """Draw V2X zone outlines + hatching on the CARLA ground.

        Each zone is a dict with 'polygon' (list of [lon, lat] pairs),
        'signal_type', and 'color'. Lines are drawn at ground level
        with a 6s lifetime (redrawn periodically by the frontend).
        """
        if not self._active:
            raise RuntimeError("No active session")

        import carla
        from digital_twin_bridge.geo_utils import gps_to_carla

        COLORS = {
            "warning": carla.Color(255, 60, 60, 255),
            "alert": carla.Color(255, 150, 50, 255),
            "info": carla.Color(60, 130, 255, 255),
        }
        # Dimmer version for hatching
        HATCH_COLORS = {
            "warning": carla.Color(255, 60, 60, 80),
            "alert": carla.Color(255, 150, 50, 80),
            "info": carla.Color(60, 130, 255, 80),
        }

        drawn = 0
        for zone in zones:
            polygon = zone.get("polygon", [])
            if len(polygon) < 3:
                continue

            sig_type = zone.get("signal_type", "warning")

            # Info zones: skip 3D visualization entirely. Proximity alerts still fire.
            if sig_type == "info":
                continue

            color = COLORS.get(sig_type, COLORS["warning"])
            hatch_color = HATCH_COLORS.get(sig_type, HATCH_COLORS["warning"])

            # Convert GPS polygon vertices to CARLA locations at ground level
            carla_points = []
            for lon, lat in polygon:
                try:
                    loc = gps_to_carla(self._map, lat, lon)
                    loc.z += 0.15
                    carla_points.append(loc)
                except Exception:
                    continue

            if len(carla_points) < 3:
                continue

            # Draw outline (warning/alert only)
            for i in range(len(carla_points)):
                start = carla_points[i]
                end = carla_points[(i + 1) % len(carla_points)]
                self._world.debug.draw_line(
                    start, end,
                    thickness=0.15,
                    color=color,
                    life_time=6.0,
                )

            # Draw diagonal hatching inside the polygon (warning/alert only)
            hatches = self._compute_hatching(carla_points, spacing=2.0)
            for h_start, h_end in hatches:
                self._world.debug.draw_line(
                    h_start, h_end,
                    thickness=0.08,
                    color=hatch_color,
                    life_time=6.0,
                )

            drawn += 1

        return {"type": "v2x_zones_synced", "drawn": drawn}

    @staticmethod
    def _compute_hatching(carla_points, spacing=2.0):
        """Generate diagonal hatching line segments inside a polygon.

        Uses a scanline approach: sweeps 45-degree lines across the
        polygon bounding box and clips them to the polygon boundary.
        """
        import carla

        if len(carla_points) < 3:
            return []

        xs = [p.x for p in carla_points]
        ys = [p.y for p in carla_points]
        avg_z = sum(p.z for p in carla_points) / len(carla_points)

        # Diagonal scanline: y = x + c
        # Range of c: (min_y - max_x) to (max_y - min_x)
        c_min = min(ys) - max(xs)
        c_max = max(ys) - min(xs)

        # Build edge list as (x1,y1,x2,y2) for intersection tests
        n = len(carla_points)
        edges = []
        for i in range(n):
            p1 = carla_points[i]
            p2 = carla_points[(i + 1) % n]
            edges.append((p1.x, p1.y, p2.x, p2.y))

        segments = []
        step = spacing * 1.414  # diagonal spacing
        c = c_min + step
        while c < c_max:
            # Find intersections of y = x + c with each edge
            intersections = []
            for x1, y1, x2, y2 in edges:
                dx = x2 - x1
                dy = y2 - y1
                # Parametric: P = (x1,y1) + t*(dx,dy)
                # Scanline: y = x + c => y1 + t*dy = x1 + t*dx + c
                denom = dy - dx
                if abs(denom) < 1e-10:
                    continue
                t = (x1 - y1 + c) / denom
                if t < 0.0 or t > 1.0:
                    continue
                ix = x1 + t * dx
                intersections.append(ix)

            # Sort and pair up (entry/exit)
            intersections.sort()
            for i in range(0, len(intersections) - 1, 2):
                sx = intersections[i]
                ex = intersections[i + 1]
                segments.append((
                    carla.Location(x=sx, y=sx + c, z=avg_z),
                    carla.Location(x=ex, y=ex + c, z=avg_z),
                ))
            c += step

        return segments

    def switch_camera(self, view: str) -> None:
        """Switch the active camera view."""
        if view not in VALID_CAMERA_VIEWS:
            raise ValueError(f"Invalid camera view: {view}. Must be one of {VALID_CAMERA_VIEWS}")
        self.active_camera = view
        self._update_camera_transform()

    def zoom_camera(self, factor: float = 1.0, reset: bool = False) -> dict:
        """Zoom the bird's-eye view by scaling the camera's altitude.

        factor > 1 zooms out (camera climbs), factor < 1 zooms in, clamped
        to [BIRD_ZOOM_MIN_ALTITUDE_M, BIRD_ZOOM_MAX_ALTITUDE_M].  Applied
        in place with set_location — parent-relative on attached sensors —
        so the camera stays glued to the vehicle with no respawn.
        """
        if reset:
            new_altitude = BIRD_ZOOM_DEFAULT_ALTITUDE_M
        else:
            factor = float(factor)
            if not math.isfinite(factor) or not (
                BIRD_ZOOM_MIN_STEP_FACTOR <= factor <= BIRD_ZOOM_MAX_STEP_FACTOR
            ):
                raise ValueError(
                    f"Invalid zoom factor: {factor}. Must be within "
                    f"[{BIRD_ZOOM_MIN_STEP_FACTOR}, {BIRD_ZOOM_MAX_STEP_FACTOR}]"
                )
            new_altitude = max(
                BIRD_ZOOM_MIN_ALTITUDE_M,
                min(BIRD_ZOOM_MAX_ALTITUDE_M, self._bird_altitude * factor),
            )
        self._bird_altitude = new_altitude
        if self.active_camera == "bird" and self._camera_sensor is not None:
            try:
                import carla
                self._camera_sensor.set_location(carla.Location(z=new_altitude))
            except Exception as e:
                logger.warning("Failed to apply bird zoom altitude: %s", e)
        return {
            "type": "camera_zoomed",
            "altitude": round(new_altitude, 2),
            "min": BIRD_ZOOM_MIN_ALTITUDE_M,
            "max": BIRD_ZOOM_MAX_ALTITUDE_M,
        }

    def end(self, release: bool = False) -> dict:
        """End the session: destroy camera, vehicle, cleanup scene.

        coexist: ``release`` also destroys a kept VOICES ego.
        """
        self._release_voices_ego = bool(release)
        self._force_cleanup()
        if not any(s is not self and s.is_active for s in _active_sessions):
            try:
                apply_default_drive_weather(self._world)
            except Exception:
                logger.warning("Failed to reset drive weather on session end", exc_info=True)
        logger.info("Drive session ended")
        return {"type": "session_ended"}

    def _force_cleanup(self):
        """
        Unconditionally destroy all owned CARLA actors.
        Safe to call multiple times. Each resource has its own try/except
        so one failure doesn't prevent cleanup of the rest.
        """
        # Stop accepting frames first to prevent callback race
        self._accepting_frames = False
        self._active = False
        self._starting = False

        # Invalidate data-only scan work before detaching CARLA sensors.  A
        # running worker owns only immutable numpy references and cannot call
        # back into this session after its generation is discarded.
        self._shutdown_perception_scan_worker()

        # Perception sensors are children of the ego; destroy them before the
        # parent actor.  ``detach`` is idempotent for partial start failures.
        try:
            self._perception.detach()
        except Exception as e:
            logger.warning("Perception detach failed: %s", e, exc_info=True)

        # Camera sensor: stop and destroy in separate try blocks
        if self._camera_sensor is not None:
            try:
                self._camera_sensor.stop()
            except Exception as e:
                logger.debug("Camera stop failed (may already be stopped): %s", e)
            try:
                self._camera_sensor.destroy()
            except Exception as e:
                logger.warning("Camera destroy failed: %s", e)
            self._camera_sensor = None

        # Vehicle
        if self.vehicle is not None:
            if self._voices_role and _keep_voices_ego() and not self._release_voices_ego:
                # coexist: keep the VOICES ego alive so the DT adapter's binding survives;
                # the next session with this role adopts it (see _adopt_kept_ego).
                try:
                    import carla
                    self.vehicle.apply_control(carla.VehicleControl(brake=1.0, hand_brake=True))
                    self.vehicle.set_target_velocity(carla.Vector3D(0, 0, 0))
                except Exception as e:
                    logger.debug("Kept ego park failed: %s", e)
                logger.info("Kept VOICES ego %s (actor id %s) in the world",
                            self._voices_role, getattr(self.vehicle, "id", "?"))
            else:
                try:
                    self.vehicle.destroy()
                except Exception as e:
                    logger.warning("Vehicle destroy failed: %s", e)
            self.vehicle = None
        self._voices_role = ""
        self._release_voices_ego = False

        # Dynamic Add Actor autopilot vehicles
        for actor_id in list(self._dynamic_actors):
            self._destroy_dynamic_actor(actor_id)
        self._dynamic_actors.clear()

        # User-placed objects
        for entry in self._placed_objects:
            try:
                entry["actor"].destroy()
            except Exception as e:
                logger.debug("Placed object destroy failed: %s", e)
        self._placed_objects.clear()

        # Scene objects
        if self._reconstructor is not None:
            try:
                self._reconstructor.cleanup()
            except Exception as e:
                logger.warning("Scene cleanup failed: %s", e)
            self._reconstructor = None

        self._latest_frame = None
        self._last_perception_scan_monotonic = None
        self._cached_perception_detections = []

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def is_starting(self) -> bool:
        return self._starting


async def handle_message(session: DriveSession, msg: dict, map_controller=None) -> dict:
    """Route an incoming WebSocket message to the appropriate session method."""
    msg_type = msg.get("type", "")

    try:
        if msg_type == "server_status":
            response = {
                "type": "server_status",
                "active_sessions": active_session_count(),
                "this_session_active": session.is_active,
            }
            if map_controller is not None:
                response["map"] = map_controller.status_payload()
            return response
        elif msg_type == "list_maps":
            if map_controller is None:
                return {"type": "map_status", "current_map": None, "maps": []}
            return {"type": "map_status", **map_controller.status_payload()}
        elif msg_type == "set_map":
            if map_controller is None:
                return {"type": "error", "message": "Map switching is unavailable"}
            result = await map_controller.switch_map(str(msg.get("map", "")))
            session.update_runtime(
                map_controller.world,
                map_controller.carla_map,
                map_controller.trajectory_player,
                map_controller.openscenario_runner,
            )
            return result
        elif msg_type == "list_vehicles":
            vehicles = get_available_vehicles(session._world)
            return {"type": "vehicle_list", "vehicles": vehicles}
        elif msg_type == "list_objects":
            objects = get_spawnable_objects(session._world)
            return {"type": "object_list", "objects": objects}
        elif msg_type == "spawn_object":
            return session.spawn_object(
                blueprint_id=msg["blueprint"],
                forward_offset=float(msg.get("offset", 8.0)),
            )
        elif msg_type == "spawn_dynamic_actor":
            return session.spawn_dynamic_actor(
                blueprint_id=msg["blueprint"],
                geofence_radius=float(msg.get("geofence_radius", 35.0)),
                message=str(msg.get("message", "")),
            )
        elif msg_type == "despawn_dynamic_actor":
            return session.despawn_dynamic_actor(int(msg["actor_id"]))
        elif msg_type == "despawn_dynamic_actors":
            return session.despawn_dynamic_actors()
        elif msg_type == "undo_place":
            return session.undo_place()
        elif msg_type == "list_scenarios":
            return {"type": "scenario_list", "scenarios": list_scenarios()}
        elif msg_type == "save_scenario":
            snapshot = session.get_placed_snapshot()
            zones = msg.get("zones", []) or []
            if not snapshot and not zones:
                return {"type": "error", "message": "Nothing to save — place objects or draw zones first"}
            return save_scenario(name=msg["name"], objects=snapshot, zones=zones)
        elif msg_type == "load_scenario":
            data = load_scenario(msg["file"])
            objects = data.get("objects", [])
            zones = data.get("zones", [])
            result = {
                "type": "scenario_loaded",
                "name": data.get("name", ""),
                "file": msg["file"],
                "zones": zones,
                "spawned": 0,
                "failed": 0,
                "placed_count": len(session._placed_objects) if session.is_active else 0,
            }
            # Only spawn CARLA objects if session is active; zones load either way
            if session.is_active and objects:
                spawn_result = session.load_scenario_objects(objects)
                result["spawned"] = spawn_result["spawned"]
                result["failed"] = spawn_result["failed"]
                result["placed_count"] = spawn_result["placed_count"]
            return result
        elif msg_type == "delete_scenario":
            return delete_scenario(msg["file"])
        elif msg_type == "list_xosc_scenarios":
            runner = session._openscenario_runner
            status = runner.status() if runner is not None else {
                "running": False, "scenario_runner_configured": False,
            }
            return {"type": "xosc_list", "scenarios": list_xosc(), "status": status}
        elif msg_type == "start_xosc_scenario":
            if session._openscenario_runner is None:
                return {"type": "error", "message": "OpenSCENARIO runner unavailable"}
            if not msg.get("file"):
                return {"type": "error", "message": "start_xosc_scenario requires 'file'"}
            return session._openscenario_runner.start(msg["file"], ego_role=session._ego_role)
        elif msg_type == "stop_xosc_scenario":
            if session._openscenario_runner is None:
                return {"type": "error", "message": "OpenSCENARIO runner unavailable"}
            return session._openscenario_runner.stop()
        elif msg_type == "start_session":
            vehicle_bp = msg.get("vehicle", DEFAULT_VEHICLE)
            return await session.start(
                start=msg["start"],
                end=msg["end"],
                vehicle_blueprint=vehicle_bp,
            )
        elif msg_type == "control":
            return session.apply_control(
                steer=float(msg.get("s", 0)),
                throttle=float(msg.get("t", 0)),
                brake=float(msg.get("b", 0)),
                reverse=bool(msg.get("rev", False)),
            )
        elif msg_type == "camera_switch":
            session.switch_camera(msg["view"])
            return {"type": "camera_switched", "view": msg["view"]}
        elif msg_type == "camera_zoom":
            return session.zoom_camera(
                factor=msg.get("factor", 1.0),
                reset=bool(msg.get("reset", False)),
            )
        elif msg_type == "set_weather":
            return session.set_weather(msg.get("params", {}))
        elif msg_type == "set_camera_settings":
            return session.set_camera_settings(msg.get("params", {}))
        elif msg_type == "spawn_traffic":
            return session.spawn_traffic(msg.get("preset", "medium"))
        elif msg_type == "despawn_traffic":
            return session.despawn_traffic()
        elif msg_type == "clear_non_ego_vehicles":
            return session.clear_non_ego_vehicles()
        elif msg_type == "sync_v2x_zones":
            return session.sync_v2x_zones(msg.get("zones", []))
        elif msg_type == "respawn":
            return session.respawn()
        elif msg_type == "teleport":
            raw_request_id = msg.get("request_id")
            try:
                request_id = session._teleport_request_id(raw_request_id)
            except ValueError as exc:
                # Keep a bounded string when possible so a malformed client can
                # still correlate its rejection without reflecting large input.
                rejected_id = (
                    raw_request_id
                    if isinstance(raw_request_id, str)
                    and len(raw_request_id) <= TELEPORT_REQUEST_ID_MAX_LENGTH
                    else ""
                )
                return {
                    "type": "teleport_error",
                    "success": False,
                    "request_id": rejected_id,
                    "message": str(exc),
                }
            try:
                response = await session.teleport(
                    x=msg.get("x"),
                    y=msg.get("y"),
                    z=msg.get("z"),
                    yaw=msg.get("yaw"),
                )
                response["request_id"] = request_id
                return response
            except (RuntimeError, TypeError, ValueError) as exc:
                return {
                    "type": "teleport_error",
                    "success": False,
                    "request_id": request_id,
                    "message": str(exc),
                }
            except Exception:
                # Preserve the teleport protocol contract for unexpected
                # CARLA transport/runtime failures without leaking internals
                # into the browser.  The detailed traceback stays server-side.
                logger.error("CARLA teleport failed", exc_info=True)
                return {
                    "type": "teleport_error",
                    "success": False,
                    "request_id": request_id,
                    "message": "Teleport failed in CARLA",
                }
        elif msg_type == "list_trajectories":
            if session._trajectory_player is None:
                return {"type": "trajectory_list", "trajectories": []}
            files = list_trajectory_files()
            status = session._trajectory_player.status()
            return {"type": "trajectory_list", "trajectories": files, "status": status}
        elif msg_type == "upload_trajectory":
            if session._trajectory_player is None:
                return {"type": "error", "message": "Trajectory player unavailable"}
            name = msg.get("name") or "uploaded"
            data = msg.get("data")
            if not isinstance(data, list):
                return {"type": "error", "message": "trajectory 'data' must be a JSON array"}
            fname = name if name.endswith(".json") else f"{name}.json"
            save_trajectory_file(fname, data)
            return {"type": "trajectory_uploaded", "file": fname}
        elif msg_type == "start_trajectory":
            if session._trajectory_player is None:
                return {"type": "error", "message": "Trajectory player unavailable"}
            file = msg.get("file")
            if not file:
                return {"type": "error", "message": "start_trajectory requires 'file'"}
            vehicle_bp = msg.get("vehicle", DEFAULT_VEHICLE)
            session._trajectory_player.load_from_file(file)
            result = session._trajectory_player.start(vehicle_blueprint=vehicle_bp)
            return {"type": "trajectory_started", **result}
        elif msg_type == "stop_trajectory":
            if session._trajectory_player is None:
                return {"type": "error", "message": "Trajectory player unavailable"}
            return {"type": "trajectory_stopped", **session._trajectory_player.stop()}
        elif msg_type == "trajectory_status":
            if session._trajectory_player is None:
                return {"type": "trajectory_status", "active": False}
            return {"type": "trajectory_status", **session._trajectory_player.status()}
        elif msg_type == "end_session":
            return session.end(release=bool(msg.get("release", False)))
        else:
            return {"type": "error", "message": f"Unknown message type: {msg_type}"}
    except Exception as e:
        logger.error("Error handling message type=%s: %s", msg_type, e, exc_info=True)
        return {"type": "error", "message": str(e)}


# Track all active sessions for the periodic actor audit
_active_sessions: list[DriveSession] = []


def active_session_count() -> int:
    return sum(
        1 for session in _active_sessions if session.is_active or session.is_starting
    )


async def serve_drive(
    websocket,
    world,
    carla_map,
    api_fetcher,
    trajectory_player: Optional[TrajectoryPlayer] = None,
    openscenario_runner=None,
    eva_warning_distance_m: float = 20.0,
    map_controller=None,
    scene_fetch_timeout_seconds: float = DEFAULT_SCENE_FETCH_TIMEOUT_SECONDS,
    scene_fetch_max_pages: int = DEFAULT_SCENE_FETCH_MAX_PAGES,
    scene_fetch_max_items: int = DEFAULT_SCENE_FETCH_MAX_ITEMS,
):
    """
    Handle a single WebSocket connection for driving.

    Multiplayer: each connection gets its own vehicle, camera, and frame stream
    in the same CARLA world. All players see each other's cars.

    Historical V2X props are session-owned.  Concurrent ranges never reuse an
    actor solely because their source records share an ``object_id``.

    ``trajectory_player`` is the server-owned playback singleton; sessions
    issue start/stop/list commands but never own the player.

    ``openscenario_runner`` is the server-owned ScenarioRunner wrapper; the
    serve_drive task subscribes to its event stream and forwards events to
    this connection's browser.
    """
    if map_controller is not None:
        world = map_controller.world
        carla_map = map_controller.carla_map
        trajectory_player = map_controller.trajectory_player
        openscenario_runner = map_controller.openscenario_runner

    session = DriveSession(
        world=world,
        carla_map=carla_map,
        api_fetcher=api_fetcher,
        trajectory_player=trajectory_player,
        openscenario_runner=openscenario_runner,
        eva_warning_distance_m=eva_warning_distance_m,
        scene_fetch_timeout_seconds=scene_fetch_timeout_seconds,
        scene_fetch_max_pages=scene_fetch_max_pages,
        scene_fetch_max_items=scene_fetch_max_items,
    )
    frame_task = None
    frame_stop = asyncio.Event()
    xosc_task = None
    xosc_queue = None

    async def stream_frames():
        """Send MJPEG frames at ~20fps as binary WebSocket messages."""
        last_frame_id = None
        while not frame_stop.is_set():
            if not session.is_active:
                await asyncio.sleep(0.1)
                continue
            frame = session.get_latest_frame()
            if frame is not None and frame is not last_frame_id:
                try:
                    await websocket.send(frame)  # binary message
                    last_frame_id = frame
                except Exception:
                    break
            await asyncio.sleep(0.05)  # 20fps cap

    async def pump_xosc_events():
        """Forward OpenSCENARIO events from the runner queue to this socket."""
        if xosc_queue is None:
            return
        while True:
            try:
                event = await xosc_queue.get()
            except asyncio.CancelledError:
                raise
            try:
                await websocket.send(json.dumps(event))
            except Exception:
                break

    if openscenario_runner is not None:
        try:
            xosc_queue = openscenario_runner.subscribe()
            xosc_task = asyncio.create_task(pump_xosc_events())
        except Exception as e:
            logger.debug("OpenSCENARIO subscribe failed: %s", e)

    _active_sessions.append(session)
    try:
        async for raw_message in websocket:
            if isinstance(raw_message, bytes):
                continue

            msg = json.loads(raw_message)
            response = await handle_message(session, msg, map_controller=map_controller)
            await websocket.send(json.dumps(response))

            # Start frame streaming once the session becomes active.
            if session.is_active and frame_task is None:
                frame_task = asyncio.create_task(stream_frames())

    except websockets.exceptions.ConnectionClosed:
        logger.info("WebSocket connection closed by client")
    except Exception as e:
        logger.error("WebSocket connection error: %s", e)
    finally:
        frame_stop.set()
        if frame_task is not None:
            frame_task.cancel()
            try:
                await frame_task
            except (asyncio.CancelledError, Exception):
                pass

        if xosc_task is not None:
            xosc_task.cancel()
            try:
                await xosc_task
            except (asyncio.CancelledError, Exception):
                pass
        if openscenario_runner is not None and xosc_queue is not None:
            openscenario_runner.unsubscribe(xosc_queue)

        session._force_cleanup()
        if session in _active_sessions:
            _active_sessions.remove(session)
        logger.info("Session cleaned up after disconnect")
