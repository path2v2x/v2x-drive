"""
Environment-based configuration for the Digital Twin Camera Bridge.

All settings are read from environment variables with sensible defaults.
"""

import os
import logging
from dataclasses import dataclass


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    """Bridge configuration loaded from environment variables."""

    # CARLA connection
    CARLA_HOST: str = "localhost"
    CARLA_PORT: int = 2000
    CARLA_MAP: str = "San_Ramon"

    # Detection history served by the twin server on the same host
    DETECTIONS_HISTORY_URL: str = "http://127.0.0.1:8190/detections/history"
    V2X_POLL_INTERVAL: float = 5.0
    V2X_LIMIT: int = 500
    V2X_STALE_SECONDS: float = 300.0
    STATE_OBJECT_MAX_AGE_SECONDS: float = 30.0
    STATE_SNAPSHOT_MAX_AGE_SECONDS: float = 90.0
    # Historical Drive reconstruction is HTTP-only in a worker, with CARLA
    # mutation performed after it returns.  Keep both individual requests and
    # the complete pagination job bounded so abandoned sessions cannot occupy
    # the worker pool indefinitely.
    SCENE_FETCH_REQUEST_TIMEOUT_SECONDS: float = 5.0
    SCENE_FETCH_TOTAL_TIMEOUT_SECONDS: float = 20.0
    SCENE_FETCH_MAX_PAGES: int = 20
    SCENE_FETCH_MAX_ITEMS: int = 10_000

    # Camera settings
    NUM_CAMERAS: int = 4
    CAM_IMAGE_WIDTH: int = 1920
    CAM_IMAGE_HEIGHT: int = 1080
    JPEG_QUALITY: int = 92
    CAM_OFFSET_DISTANCE: float = 8.0
    CAM_OFFSET_HEIGHT: float = 4.0
    SETTLE_TICKS: int = 2
    CAPTURE_INTERVAL: float = 30.0  # seconds between capture cycles

    # Dashboard data publishing (served by nginx at PUBLISH_BASE_URL)
    PUBLISH_DIR: str = "/var/www/v2x-drive-data"
    PUBLISH_BASE_URL: str = "/data"

    # Local scratch for map exports
    LOCAL_SNAPSHOT_DIR: str = "snapshots/"

    # Drive server settings
    WS_PORT: int = 8765
    WS_MAX_MESSAGE_BYTES: int = 16 * 1024 * 1024
    VEHICLE_BLUEPRINT: str = "vehicle.tesla.model3"
    WEBRTC_PORT: int = 8766
    SESSION_DIR: str = "sessions/"

    # Legacy HIL/test WebSocket. Disabled by default and requires a bearer
    # token when explicitly enabled; it shares no implicit trust with /drive.
    TEST_WS_ENABLED: str = "off"
    TEST_WS_TOKEN: str = ""
    TEST_WS_MAX_UPLOAD_BYTES: int = 8 * 1024 * 1024
    TEST_WS_QUEUE_SIZE: int = 64

    # OpenSCENARIO / ScenarioRunner — absolute path to the cloned
    # https://github.com/carla-simulator/scenario_runner repo on the dev PC.
    # Empty string disables the feature.
    SCENARIO_RUNNER_PATH: str = ""
    # Python interpreter used to launch scenario_runner.py. Empty → use
    # the bridge's own interpreter (sys.executable).
    SCENARIO_RUNNER_PYTHON: str = ""
    # Colon-separated paths prepended to PYTHONPATH for the subprocess.
    # Typically points to the CARLA PythonAPI's `carla/` directory so
    # ScenarioRunner can import the `agents` package.
    SCENARIO_RUNNER_PYTHONPATH: str = ""

    # Distance (meters) within which an approaching emergency vehicle
    # (vehicle.carlamotors.firetruck) triggers a "pull over" v2x_alert toast
    # on the ego's browser. Only fires when the EVA is closing on the ego.
    EVA_WARNING_DISTANCE_M: float = 20.0

    # Driving HUD: an IMU and a collision sensor on the ego feed the accelerometer,
    # gyroscope and collision lines. "0" keeps the camera as the only sensor; the
    # HUD then derives acceleration from the velocity and shows no collisions.
    HUD_SENSORS: str = "1"

    # Editor mode: camera-only sessions with a fly camera. Each one is a full
    # UE5 scene render (~27 ms, ~0.9 GB VRAM), so keep them few.
    EDITOR_MAX_SESSIONS: int = 2

    # Logging
    LOG_LEVEL: str = "INFO"

    # coexist branch: sharing the CARLA world with other clients (DT adapter, HIL_Tool)
    PROTECT_FOREIGN_ACTORS: str = "1"  # "1": never destroy actors this server did not create
    TM_PORT: int = 8000                 # Traffic Manager port for traffic/dynamic actors; 8100 avoids DT's manual car, pending the traffic hang investigation
    TM_OSM_MODE: str = "1"             # "0" keeps traffic cars at dead ends instead of deleting them; left on until the traffic hang is understood
    VOICES_EGO_ROLE: str = ""          # e.g. "PATH-M-1": first session's ego takes this role_name
    KEEP_VOICES_EGO: str = "1"         # "1": a VOICES-role ego survives session end; next session adopts it
    VOICES_SLOT_FILE: str = ""         # parked slot cars (adoptable after a restart); empty: ~/.cache/v2x-drive/voices_slots.json
    VOICES_SLOT_DEFAULT: str = "PATH-BG-5"  # slot the start screen preselects for "publish to VOICES"

    # V2X message viewer: per-vehicle J2735 feeds from the DT (VOICES) stack.
    V2X_FEED_ENABLED: str = "1"
    V2X_JSON_STREAM_HOST: str = "127.0.0.1"   # the DT JSON streamer connects to us here
    V2X_JSON_STREAM_PORT: int = 8005          # 0 disables the TCP listener
    V2X_UDP_HOST: str = "127.0.0.1"           # raw UPER datagrams from the TENA V2X adapter
    V2X_UDP_PORT: int = 5398                  # 0 disables the UDP tap
    V2X_SCENARIO_XML: str = ""                # empty: resolve from ~/.dt_scenario_config
    V2X_LOCAL_ORG: str = "PATH"
    V2X_REGISTRY_EVERY_N_TICKS: int = 5       # vehicle table refresh cadence (20 Hz tick -> 4 Hz)
    V2X_PUSH_INTERVAL_S: float = 0.1          # batching interval for v2x_messages pushes
    V2X_QUEUE_MAX: int = 2000                 # per-connection buffered messages before dropping oldest
    V2X_FEED_STALE_S: float = 3.0             # sender considered silent after this many seconds
    V2X_DECODE_CACHE: int = 4096
    V2X_STREAMER_AUTOLAUNCH: str = "1"        # relaunch DT's JSON streamer in dt-core when it is missing
    V2X_STREAMER_CONTAINER: str = "dt-core"

    # V2X-reactive driving models: the server-owned fleet of civilians and ERVs.
    FLEET_ENABLED: str = "1"
    FLEET_MAX_VEHICLES: int = 12
    FLEET_DECISION_EVERY_N_TICKS: int = 2     # behaviour decisions per vehicle at 20 Hz / N
    FLEET_TICK_BUDGET_MS: float = 20.0        # p95 fleet tick time before decisions slow down
    FLEET_LIGHT_SCAN_EVERY_N_TICKS: int = 10  # emergency light-state scan cadence
    # Where the models learn about emergency vehicles: "bsm" (the TENA/UDP V2X feed),
    # "lights" (CARLA Special1 light state of emergency blueprints), "fleet" (our own ERVs).
    # "bsm" alone makes every reaction depend on real V2X messages.
    FLEET_ERV_SOURCES: str = "bsm,lights,fleet"

    @classmethod
    def from_env(cls) -> "Config":
        """Create a Config instance from environment variables.

        Environment variable names match the field names, prefixed with
        ``DTB_`` (e.g. ``DTB_CARLA_HOST``).  If the variable is not set
        the dataclass default is used.
        """
        kwargs: dict = {}
        for fld in cls.__dataclass_fields__.values():
            env_key = f"DTB_{fld.name}"
            env_val = os.environ.get(env_key)
            if env_val is not None:
                # Cast to the declared type
                target_type = fld.type
                if target_type == "int" or target_type is int:
                    kwargs[fld.name] = int(env_val)
                elif target_type == "float" or target_type is float:
                    kwargs[fld.name] = float(env_val)
                else:
                    kwargs[fld.name] = env_val
        return cls(**kwargs)

    # coexist branch helpers
    @property
    def protect_foreign_actors(self) -> bool:
        return _truthy(self.PROTECT_FOREIGN_ACTORS)

    @property
    def keep_voices_ego(self) -> bool:
        return _truthy(self.KEEP_VOICES_EGO)

    @property
    def tm_osm_mode(self) -> bool:
        return _truthy(self.TM_OSM_MODE)

    @property
    def voices_roles(self) -> tuple:
        return tuple(r.strip() for r in str(self.VOICES_EGO_ROLE or "").split(",") if r.strip())

    @property
    def v2x_feed_enabled(self) -> bool:
        return _truthy(self.V2X_FEED_ENABLED)

    @property
    def v2x_streamer_autolaunch(self) -> bool:
        return _truthy(self.V2X_STREAMER_AUTOLAUNCH)

    @property
    def fleet_enabled(self) -> bool:
        return _truthy(self.FLEET_ENABLED)

    @property
    def hud_sensors(self) -> bool:
        return _truthy(self.HUD_SENSORS)

    @property
    def fleet_erv_sources(self) -> frozenset:
        return frozenset(s.strip().lower() for s in str(self.FLEET_ERV_SOURCES or "").split(",") if s.strip())

    def setup_logging(self) -> None:
        """Configure the root logger based on ``LOG_LEVEL``."""
        numeric_level = getattr(logging, self.LOG_LEVEL.upper(), logging.INFO)
        logging.basicConfig(
            level=numeric_level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
