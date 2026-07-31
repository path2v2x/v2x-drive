"""
Shared pytest fixtures for the Digital Twin Drive Server tests.

Provides mock CARLA objects, mock WebSocket, and fake V2X API
so unit tests run fast without real CARLA or network access.
"""

import json
import asyncio
import sys
import time
import types
from dataclasses import dataclass, field
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest


# ──────────────────────────────────────────────────────────────
# Mock CARLA types
# ──────────────────────────────────────────────────────────────

@dataclass
class MockLocation:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def distance(self, other: "MockLocation") -> float:
        return (
            (self.x - other.x) ** 2 + (self.y - other.y) ** 2 + (self.z - other.z) ** 2
        ) ** 0.5


@dataclass
class MockRotation:
    pitch: float = 0.0
    yaw: float = 0.0
    roll: float = 0.0


@dataclass
class MockTransform:
    location: MockLocation = field(default_factory=MockLocation)
    rotation: MockRotation = field(default_factory=MockRotation)

    def get_forward_vector(self):
        import math
        yaw_rad = math.radians(self.rotation.yaw)
        return MockLocation(x=math.cos(yaw_rad), y=math.sin(yaw_rad), z=0.0)


@dataclass
class MockVehicleControl:
    throttle: float = 0.0
    steer: float = 0.0
    brake: float = 0.0
    hand_brake: bool = False
    reverse: bool = False
    manual_gear_shift: bool = False
    gear: int = 0


class MockVehiclePhysicsControl:
    max_rpm = 6000.0


@dataclass
class MockColor:
    r: int = 0
    g: int = 0
    b: int = 0
    a: int = 255


@dataclass
class MockWeatherParameters:
    cloudiness: float = 0.0
    precipitation: float = 0.0
    precipitation_deposits: float = 0.0
    wind_intensity: float = 0.0
    sun_azimuth_angle: float = 0.0
    sun_altitude_angle: float = 0.0
    fog_density: float = 0.0
    fog_distance: float = 0.0
    fog_falloff: float = 0.0
    wetness: float = 0.0
    scattering_intensity: float = 1.0
    mie_scattering_scale: float = 0.03
    rayleigh_scattering_scale: float = 0.0331
    dust_storm: float = 0.0


class MockDebugHelper:
    def __init__(self):
        self.lines: list[dict[str, Any]] = []

    def draw_line(self, start, end, thickness=0.1, color=None, life_time=0.0) -> None:
        self.lines.append({
            "start": start,
            "end": end,
            "thickness": thickness,
            "color": color,
            "life_time": life_time,
        })


class MockActor:
    """Mock CARLA actor (vehicle or prop)."""

    def __init__(self, actor_id: int, type_id: str = "vehicle.tesla.model3"):
        self.id = actor_id
        self.type_id = type_id
        self._transform = MockTransform()
        self._control = MockVehicleControl()
        self._velocity = MockLocation(x=0, y=0, z=0)
        self._angular_velocity = MockLocation(x=0, y=0, z=0)
        self._destroyed = False
        self.autopilot_enabled = False
        self.traffic_manager_port: Optional[int] = None
        self.control_history: list[MockVehicleControl] = []
        self.attributes: dict[str, str] = {}
        self.physics_enabled = True
        self._listener = None
        self.bounding_box = types.SimpleNamespace(
            extent=MockLocation(x=2.35, y=0.95, z=0.8)
        )

    def listen(self, callback) -> None:
        self._listener = callback

    def stop(self) -> None:
        self._listener = None

    def set_simulate_physics(self, enabled: bool) -> None:
        self.physics_enabled = enabled

    def get_transform(self) -> MockTransform:
        return self._transform

    def get_velocity(self) -> MockLocation:
        return self._velocity

    def get_control(self) -> MockVehicleControl:
        return self._control

    def get_physics_control(self) -> MockVehiclePhysicsControl:
        return MockVehiclePhysicsControl()

    def apply_control(self, control: MockVehicleControl) -> None:
        self._control = control
        self.control_history.append(MockVehicleControl(
            throttle=control.throttle,
            steer=control.steer,
            brake=control.brake,
            hand_brake=control.hand_brake,
            reverse=control.reverse,
            gear=control.gear,
        ))

    def set_transform(self, transform: MockTransform) -> None:
        self._transform = transform

    def set_target_velocity(self, velocity: MockLocation) -> None:
        self._velocity = velocity

    def set_target_angular_velocity(self, velocity: MockLocation) -> None:
        self._angular_velocity = velocity

    def set_autopilot(self, enabled: bool, tm_port: Optional[int] = None) -> None:
        self.autopilot_enabled = enabled
        self.traffic_manager_port = tm_port

    def destroy(self) -> bool:
        self._destroyed = True
        return True

    @property
    def is_destroyed(self) -> bool:
        return self._destroyed


class MockBlueprintAttribute:
    def __init__(self, value: str, recommended_values: Optional[list[str]] = None):
        self.value = value
        self.recommended_values = recommended_values or [value]

    def as_int(self) -> int:
        return int(self.value)

    def __int__(self) -> int:
        return int(self.value)

    def __str__(self) -> str:
        return self.value


class MockBlueprint:
    def __init__(self, bp_id: str, attributes: Optional[dict[str, MockBlueprintAttribute]] = None):
        self.id = bp_id
        self._attributes = attributes or {}
        self.set_attribute_calls: list[tuple[str, str]] = []
        if bp_id.startswith("vehicle."):
            self._attributes.setdefault("number_of_wheels", MockBlueprintAttribute("4"))
            self._attributes.setdefault("color", MockBlueprintAttribute("255,255,255", ["255,255,255", "180,0,0"]))
        if bp_id == "sensor.camera.rgb":
            for key, value in {
                "lens_k": "-1.0",
                "lens_kcube": "0.0",
                "lens_circle_falloff": "5.0",
                "lens_circle_multiplier": "0.0",
                "lens_x_size": "0.08",
                "lens_y_size": "0.08",
            }.items():
                self._attributes.setdefault(key, MockBlueprintAttribute(value))

    def has_attribute(self, key: str) -> bool:
        return key in self._attributes

    def get_attribute(self, key: str) -> MockBlueprintAttribute:
        return self._attributes[key]

    def set_attribute(self, key: str, value: str) -> None:
        self.set_attribute_calls.append((key, value))
        self._attributes[key] = MockBlueprintAttribute(value)


class MockBlueprintLibrary:
    def __init__(self):
        self._blueprints = {
            "vehicle.tesla.model3": MockBlueprint("vehicle.tesla.model3"),
            "vehicle.carlamotors.firetruck": MockBlueprint("vehicle.carlamotors.firetruck"),
            "static.prop.trafficcone01": MockBlueprint("static.prop.trafficcone01"),
            "static.prop.trafficwarning": MockBlueprint("static.prop.trafficwarning"),
            "sensor.camera.rgb": MockBlueprint("sensor.camera.rgb"),
        }

    def filter(self, pattern: str) -> list[MockBlueprint]:
        return [bp for key, bp in self._blueprints.items() if pattern in key]

    def find(self, bp_id: str) -> Optional[MockBlueprint]:
        return self._blueprints.get(bp_id)


class MockGeoLocation:
    def __init__(self, latitude=0.0, longitude=0.0, altitude=0.0):
        self.latitude = latitude
        self.longitude = longitude
        self.altitude = altitude


class MockWaypoint:
    def __init__(self, transform=None):
        self.transform = transform or MockTransform()


class MockMap:
    def __init__(self, name: str = "TestMap"):
        self.name = name
        self._origin_lat = 37.915
        self._origin_lon = -122.335

    def get_spawn_points(self) -> list[MockTransform]:
        return [
            MockTransform(MockLocation(100, 200, 0), MockRotation(0, 0, 0)),
            MockTransform(MockLocation(150, 250, 0), MockRotation(0, 90, 0)),
            MockTransform(MockLocation(200, 300, 0), MockRotation(0, 180, 0)),
            MockTransform(MockLocation(250, 350, 0), MockRotation(0, 270, 0)),
        ]

    def transform_to_geolocation(self, location: MockLocation) -> MockGeoLocation:
        return MockGeoLocation(latitude=self._origin_lat, longitude=self._origin_lon)

    def geolocation_to_transform(self, geo: MockGeoLocation) -> MockTransform:
        # Simplified: just return a transform near origin
        return MockTransform(MockLocation(x=100.0, y=200.0, z=0.0))

    def get_waypoint(self, location, project_to_road=True):
        return MockWaypoint(MockTransform(
            MockLocation(location.x, location.y, 0.1)
        ))


@dataclass
class MockWorldSettings:
    synchronous_mode: bool = False
    fixed_delta_seconds: float = 0.05


class MockWorld:
    """Mock CARLA world that tracks spawned actors and applied controls."""

    def __init__(self):
        self._actors: dict[int, MockActor] = {}
        self._next_id = 1
        self._settings = MockWorldSettings()
        self._map = MockMap()
        self.weather: Optional[MockWeatherParameters] = None
        self._tick_count = 0
        self._blueprint_library = MockBlueprintLibrary()
        self.debug = MockDebugHelper()

    def get_map(self) -> MockMap:
        return self._map

    def get_settings(self) -> MockWorldSettings:
        return MockWorldSettings(
            synchronous_mode=self._settings.synchronous_mode,
            fixed_delta_seconds=self._settings.fixed_delta_seconds,
        )

    def apply_settings(self, settings: MockWorldSettings) -> None:
        self._settings.synchronous_mode = settings.synchronous_mode
        self._settings.fixed_delta_seconds = settings.fixed_delta_seconds

    def set_weather(self, weather: MockWeatherParameters) -> None:
        self.weather = weather

    def get_blueprint_library(self) -> MockBlueprintLibrary:
        return self._blueprint_library

    def get_spectator(self) -> MockActor:
        if 0 not in self._actors:
            self._actors[0] = MockActor(0, "spectator")
        return self._actors[0]

    def try_spawn_actor(self, blueprint, transform, attach_to=None, **kwargs) -> Optional[MockActor]:
        actor = MockActor(self._next_id, getattr(blueprint, "id", "unknown"))
        actor.attributes = {
            key: str(value)
            for key, value in getattr(blueprint, "_attributes", {}).items()
        }
        self._actors[actor.id] = actor
        actor._transform = transform
        self._next_id += 1
        return actor

    def spawn_actor(self, blueprint, transform, attach_to=None, **kwargs) -> MockActor:
        actor = self.try_spawn_actor(blueprint, transform, attach_to, **kwargs)
        if actor is None:
            raise RuntimeError("Failed to spawn actor")
        return actor

    def get_actor(self, actor_id: int) -> Optional[MockActor]:
        return self._actors.get(actor_id)

    def get_actors(self):
        """Return mock actor list with filter support."""
        actors = list(self._actors.values())
        mock_list = MagicMock()
        mock_list.filter = lambda pattern: [
            a for a in actors if pattern.replace("*", "") in a.type_id
        ]
        mock_list.__iter__ = lambda self_: iter(actors)
        mock_list.__len__ = lambda self_: len(actors)
        return mock_list

    def tick(self) -> int:
        self._tick_count += 1
        return self._tick_count

    def ground_projection(self, location, search_distance=10.0):
        """Mock ground projection — just return the location with z=0."""
        result = MagicMock()
        result.location = MockLocation(location.x, location.y, 0.1)
        return result

    @property
    def spawned_actors(self) -> list[MockActor]:
        return [a for a in self._actors.values() if a.id != 0]


class MockTrafficManager:
    def __init__(self, port: int = 8000):
        self._port = port
        self.synchronous_mode = False
        self.speed_difference = 0.0
        self.distance_to_leading_vehicle = 2.0
        self.ignore_lights: dict[int, float] = {}
        self.ignore_signs: dict[int, float] = {}

    def get_port(self) -> int:
        return self._port

    def set_synchronous_mode(self, enabled: bool) -> None:
        self.synchronous_mode = enabled

    def global_percentage_speed_difference(self, value: float) -> None:
        self.speed_difference = value

    def set_global_distance_to_leading_vehicle(self, value: float) -> None:
        self.distance_to_leading_vehicle = value

    def ignore_lights_percentage(self, actor: MockActor, value: float) -> None:
        self.ignore_lights[actor.id] = value

    def ignore_signs_percentage(self, actor: MockActor, value: float) -> None:
        self.ignore_signs[actor.id] = value


class MockClient:
    """Mock CARLA client."""

    def __init__(self, world: Optional[MockWorld] = None):
        self._world = world or MockWorld()
        self._timeout = 10.0
        self._traffic_manager = MockTrafficManager()
        self.available_maps = ["TestMap", "San_Ramon"]
        self.loaded_maps: list[str] = []

    def get_world(self) -> MockWorld:
        return self._world

    def set_timeout(self, seconds: float) -> None:
        self._timeout = seconds

    def get_available_maps(self) -> list[str]:
        return self.available_maps

    def load_world(self, map_name: str) -> MockWorld:
        self.loaded_maps.append(map_name)
        self._world = MockWorld()
        self._world._map = MockMap(map_name)
        return self._world

    def get_trafficmanager(self) -> MockTrafficManager:
        return self._traffic_manager


class MockCarlaClient(MockClient):
    """Drop-in carla.Client test double accepting CARLA's host/port signature."""

    def __init__(self, *args, **kwargs):
        world = args[0] if args and isinstance(args[0], MockWorld) else None
        super().__init__(world)


def _install_fake_carla_module() -> None:
    if "carla" in sys.modules:
        return

    fake_carla = types.ModuleType("carla")
    fake_carla.Location = MockLocation
    fake_carla.Rotation = MockRotation
    fake_carla.Transform = MockTransform
    fake_carla.GeoLocation = MockGeoLocation
    fake_carla.LaneType = types.SimpleNamespace(Driving=1, Sidewalk=2)
    fake_carla.VehicleControl = MockVehicleControl
    fake_carla.Vector3D = MockLocation
    fake_carla.Color = MockColor
    fake_carla.WeatherParameters = MockWeatherParameters
    fake_carla.Client = MockCarlaClient
    fake_carla.Map = MockMap
    fake_carla.World = MockWorld
    fake_carla.Vehicle = MockActor
    fake_carla.AttachmentType = types.SimpleNamespace(
        Rigid="Rigid",
        SpringArmGhost="SpringArmGhost",
    )
    sys.modules["carla"] = fake_carla


_install_fake_carla_module()


# ──────────────────────────────────────────────────────────────
# Mock WebSocket
# ──────────────────────────────────────────────────────────────

class MockWebSocket:
    """Mock WebSocket that records sent messages and allows injecting received messages."""

    def __init__(self):
        self.sent: list[str] = []
        self._recv_queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionError("WebSocket is closed")
        self.sent.append(message)

    async def recv(self) -> str:
        if self.closed:
            raise ConnectionError("WebSocket is closed")
        return await self._recv_queue.get()

    async def close(self) -> None:
        self.closed = True

    async def inject(self, message: str) -> None:
        """Inject a message as if the client sent it."""
        await self._recv_queue.put(message)

    def get_sent_json(self) -> list[dict]:
        """Return all sent messages parsed as JSON."""
        return [json.loads(m) for m in self.sent]

    def last_sent_json(self) -> Optional[dict]:
        if not self.sent:
            return None
        return json.loads(self.sent[-1])


# ──────────────────────────────────────────────────────────────
# Fake V2X API
# ──────────────────────────────────────────────────────────────

SAMPLE_DETECTIONS = [
    {
        "object_id": "traffic_cone_001",
        "object_type": "traffic_cone",
        "gps_location": {"latitude": 37.91542, "longitude": -122.33492},
        "confidence_score": 0.95,
        "timestamp_utc": "2026-03-22T17:03:12Z",
        "street_name_normalized": "University Ave",
        "ts_event": "2026-03-22T17:03:12Z#traffic_cone_001_1",
        "event_id": "evt_001",
    },
    {
        "object_id": "traffic_cone_002",
        "object_type": "traffic_cone",
        "gps_location": {"latitude": 37.91550, "longitude": -122.33480},
        "confidence_score": 0.88,
        "timestamp_utc": "2026-03-22T17:05:30Z",
        "street_name_normalized": "University Ave",
        "ts_event": "2026-03-22T17:05:30Z#traffic_cone_002_1",
        "event_id": "evt_002",
    },
    {
        "object_id": "traffic_cone_001",
        "object_type": "traffic_cone",
        "gps_location": {"latitude": 37.91543, "longitude": -122.33491},
        "confidence_score": 0.97,
        "timestamp_utc": "2026-03-22T17:08:45Z",
        "street_name_normalized": "University Ave",
        "ts_event": "2026-03-22T17:08:45Z#traffic_cone_001_2",
        "event_id": "evt_003",
    },
]


class FakeV2XApi:
    """Fake V2X API server for testing scene reconstruction."""

    def __init__(self, detections: Optional[list[dict]] = None):
        self.detections = detections if detections is not None else SAMPLE_DETECTIONS
        self.call_count = 0
        self.last_params: dict = {}

    def get_detections_range(self, start: str, end: str, limit: int = 500) -> dict:
        self.call_count += 1
        self.last_params = {"start": start, "end": end, "limit": limit}
        # Filter by time range
        filtered = [
            d for d in self.detections
            if start <= d["timestamp_utc"] <= end
        ]
        return {"items": filtered[:limit], "count": len(filtered[:limit])}


# ──────────────────────────────────────────────────────────────
# Pytest fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def mock_world():
    """Fresh MockWorld for each test."""
    return MockWorld()


@pytest.fixture
def mock_client(mock_world):
    """MockClient wrapping a MockWorld."""
    return MockClient(mock_world)


@pytest.fixture
def mock_websocket():
    """Fresh MockWebSocket for each test."""
    return MockWebSocket()


@pytest.fixture
def fake_v2x_api():
    """FakeV2XApi with sample detections."""
    return FakeV2XApi()


@pytest.fixture
def empty_v2x_api():
    """FakeV2XApi with no detections."""
    return FakeV2XApi(detections=[])


@pytest.fixture
def sample_detections():
    """Raw sample detection data."""
    return SAMPLE_DETECTIONS.copy()
