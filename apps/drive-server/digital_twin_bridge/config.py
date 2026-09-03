"""
Environment-based configuration for the Digital Twin Camera Bridge.

All settings are read from environment variables with sensible defaults.
"""

import os
import logging
from dataclasses import dataclass


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

    # Logging
    LOG_LEVEL: str = "INFO"

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

    def setup_logging(self) -> None:
        """Configure the root logger based on ``LOG_LEVEL``."""
        numeric_level = getattr(logging, self.LOG_LEVEL.upper(), logging.INFO)
        logging.basicConfig(
            level=numeric_level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
