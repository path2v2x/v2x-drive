"""Acceptance tests for the Phase 1 bridge recovery work."""

import asyncio
import json
import math
import threading
from types import SimpleNamespace

import pytest

from tests.conftest import MockActor, MockGeoLocation, MockLocation, MockMap


@pytest.mark.unit
class TestTeleportProtocol:
    @staticmethod
    def _active_session(mock_world, actor_id: int):
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=lambda *_args, **_kwargs: {"items": []},
        )
        vehicle = MockActor(actor_id)
        mock_world._actors[actor_id] = vehicle
        session.vehicle = vehicle
        session._active = True
        return session

    @pytest.mark.asyncio
    async def test_teleport_is_session_owned_and_resets_both_velocities(
        self, mock_world
    ):
        first = self._active_session(mock_world, 1001)
        second = self._active_session(mock_world, 1002)
        second.vehicle.set_transform(
            type(first.vehicle.get_transform())(
                MockLocation(210.0, 310.0, 0.6)
            )
        )
        second_before = second.vehicle.get_transform()
        first.vehicle._velocity = MockLocation(22.0, -3.0, 1.0)
        first.vehicle._angular_velocity = MockLocation(0.0, 0.0, 8.0)

        response = await first.teleport(175.0, 275.0, yaw=270.0)

        assert response == {
            "type": "teleported",
            "success": True,
            "pos": [175.0, 275.0, 0.6],
            "yaw": -90.0,
            "snapped_to_road": True,
        }
        assert first.vehicle._velocity == MockLocation()
        assert first.vehicle._angular_velocity == MockLocation()
        assert second.vehicle.get_transform() is second_before
        assert second.vehicle.get_transform().location == MockLocation(210.0, 310.0, 0.6)
        assert first._perception is not second._perception

    @pytest.mark.asyncio
    async def test_teleport_ack_waits_for_confirmed_carla_snapshot(self, mock_world):
        session = self._active_session(mock_world, 1001)
        original_set_transform = session.vehicle.set_transform
        original_get_transform = session.vehicle.get_transform
        pending = []
        stale_reads = []

        def defer_transform(transform):
            pending.append(transform)

        def confirm_after_stale_read():
            if pending:
                if not stale_reads:
                    stale_reads.append(True)
                else:
                    original_set_transform(pending.pop())
            return original_get_transform()

        session.vehicle.set_transform = defer_transform
        session.vehicle.get_transform = confirm_after_stale_read

        response = await session.teleport(175.0, 275.0, yaw=90.0)

        assert stale_reads == [True]
        assert pending == []
        assert response["pos"] == [175.0, 275.0, 0.6]
        assert response["yaw"] == 90.0

    @pytest.mark.asyncio
    async def test_unconfirmed_teleport_gets_typed_runtime_error(
        self, mock_world, monkeypatch
    ):
        from digital_twin_bridge import drive_server

        session = self._active_session(mock_world, 1001)
        session.vehicle.set_transform = lambda _transform: None
        monkeypatch.setattr(drive_server, "TELEPORT_CONFIRM_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(
            drive_server, "TELEPORT_CONFIRM_POLL_INTERVAL_SECONDS", 0.001
        )
        response = await drive_server.handle_message(
            session,
            {
                "type": "teleport",
                "request_id": "unconfirmed-runtime-test",
                "x": 175.0,
                "y": 275.0,
            },
        )

        assert response == {
            "type": "teleport_error",
            "success": False,
            "request_id": "unconfirmed-runtime-test",
            "message": "CARLA did not confirm the requested Teleport pose",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "pose_override",
        [
            {"yaw": 90.0},
            {"z": 10.0},
        ],
    )
    async def test_same_xy_unconfirmed_yaw_or_z_cannot_acknowledge(
        self, mock_world, monkeypatch, pose_override
    ):
        from digital_twin_bridge import drive_server

        session = self._active_session(mock_world, 1001)
        current = session.vehicle.get_transform()
        session.vehicle.set_transform = lambda _transform: None
        monkeypatch.setattr(drive_server, "TELEPORT_CONFIRM_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(
            drive_server, "TELEPORT_CONFIRM_POLL_INTERVAL_SECONDS", 0.001
        )
        message = {
            "type": "teleport",
            "request_id": "same-xy-unconfirmed",
            "x": current.location.x,
            "y": current.location.y,
            **pose_override,
        }

        response = await drive_server.handle_message(session, message)

        assert response["type"] == "teleport_error"
        assert response["request_id"] == "same-xy-unconfirmed"
        assert response["message"] == "CARLA did not confirm the requested Teleport pose"

    @pytest.mark.asyncio
    async def test_invalid_values_get_a_typed_teleport_error(self, mock_world):
        from digital_twin_bridge.drive_server import handle_message

        session = self._active_session(mock_world, 1001)
        for value in (None, True, math.nan, math.inf, "not-a-number"):
            response = await handle_message(
                session,
                {
                    "type": "teleport",
                    "request_id": "validation-test",
                    "x": value,
                    "y": 275.0,
                },
            )
            assert response["type"] == "teleport_error"
            assert response["success"] is False
            assert response["request_id"] == "validation-test"
            assert isinstance(response["message"], str) and response["message"]

    @pytest.mark.asyncio
    async def test_carla_runtime_failure_still_gets_typed_error(
        self, mock_world, monkeypatch
    ):
        from digital_twin_bridge.drive_server import handle_message

        session = self._active_session(mock_world, 1001)

        def fail_transform(_transform):
            raise OSError("CARLA transport closed")

        monkeypatch.setattr(session.vehicle, "set_transform", fail_transform)
        response = await handle_message(
            session,
            {
                "type": "teleport",
                "request_id": "carla-runtime-test",
                "x": 175.0,
                "y": 275.0,
            },
        )

        assert response["type"] == "teleport_error"
        assert response["success"] is False
        assert response["request_id"] == "carla-runtime-test"
        assert response["message"] == "Teleport failed in CARLA"

    @pytest.mark.asyncio
    async def test_success_echoes_request_id_for_ack_correlation(self, mock_world):
        from digital_twin_bridge.drive_server import handle_message

        response = await handle_message(
            self._active_session(mock_world, 1001),
            {
                "type": "teleport",
                "request_id": "teleport-018f",
                "x": 175.0,
                "y": 275.0,
            },
        )

        assert response["type"] == "teleported"
        assert response["success"] is True
        assert response["request_id"] == "teleport-018f"

    @pytest.mark.parametrize(
        ("request_id", "echoed"),
        [
            (None, ""),
            ("", ""),
            ("   ", "   "),
            (7, ""),
            ("x" * 129, ""),
        ],
    )
    @pytest.mark.asyncio
    async def test_missing_or_malformed_request_id_is_rejected_before_mutation(
        self, mock_world, request_id, echoed
    ):
        from digital_twin_bridge.drive_server import handle_message

        session = self._active_session(mock_world, 1001)
        original = session.vehicle.get_transform()
        message = {
            "type": "teleport",
            "x": 175.0,
            "y": 275.0,
        }
        if request_id is not None:
            message["request_id"] = request_id

        response = await handle_message(session, message)

        assert response["type"] == "teleport_error"
        assert response["success"] is False
        assert response["request_id"] == echoed
        assert "request_id" in response["message"]
        assert session.vehicle.get_transform() is original

    @pytest.mark.parametrize(
        "message",
        [
            {
                "type": "teleport",
                "request_id": "bounds-x",
                "x": 1_000_000,
                "y": 275,
            },
            {
                "type": "teleport",
                "request_id": "bounds-z",
                "x": 175,
                "y": 275,
                "z": 501,
            },
            {
                "type": "teleport",
                "request_id": "bounds-yaw",
                "x": 175,
                "y": 275,
                "yaw": 361,
            },
        ],
    )
    @pytest.mark.asyncio
    async def test_safety_bounds_are_rejected(self, mock_world, message):
        from digital_twin_bridge.drive_server import handle_message

        response = await handle_message(
            self._active_session(mock_world, 1001), message
        )
        assert response["type"] == "teleport_error"
        assert response["success"] is False
        assert response["request_id"] == message["request_id"]


@pytest.mark.unit
class TestCarlaGeolocationCompatibility:
    def test_carla_09_transform_result_is_normalized_to_location(self):
        from digital_twin_bridge.geo_utils import gps_to_carla

        location = gps_to_carla(MockMap(), 37.9, -122.3)

        assert isinstance(location, MockLocation)
        assert location == MockLocation(100.0, 200.0, 0.1)

    def test_carla_010_inverse_projection_without_removed_api(self):
        from digital_twin_bridge.geo_utils import gps_to_carla
        from v2x_common.geodesy import TransverseMercator

        origin_lat = 37.9
        origin_lon = -122.3
        georeference = (
            f"+proj=tmerc +lat_0={origin_lat} +lon_0={origin_lon} +k=1 "
            "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
        )
        projection = TransverseMercator.from_proj_string(georeference)
        metres_per_lon = 111_320.0 * math.cos(math.radians(origin_lat))

        class Carla010Map:
            def transform_to_geolocation(self, location):
                return MockGeoLocation(
                    latitude=origin_lat - location.y / 111_320.0,
                    longitude=origin_lon + location.x / metres_per_lon,
                )

            def get_waypoint(self, _location, project_to_road=True):
                assert project_to_road is True
                return None

            def to_opendrive(self):
                return (
                    "<OpenDRIVE><header><geoReference><![CDATA["
                    + georeference
                    + "]]></geoReference></header></OpenDRIVE>"
                )

        latitude, longitude = projection.inverse(35.0, 20.0)

        location = gps_to_carla(
            Carla010Map(),
            latitude,
            longitude,
        )

        assert location.x == pytest.approx(35.0)
        assert location.y == pytest.approx(-20.0)
        assert location.z == 0.0

    @staticmethod
    def _road_export_map(carla_09: bool):
        class Vector:
            def __init__(self, x=0.0, y=0.0, z=0.0):
                self.x, self.y, self.z = x, y, z

            def __add__(self, other):
                return Vector(self.x + other.x, self.y + other.y, self.z + other.z)

            def __mul__(self, scale):
                return Vector(self.x * scale, self.y * scale, self.z * scale)

            __rmul__ = __mul__

        class Transform:
            def __init__(self, x):
                self.location = Vector(x=x)
                self.rotation = SimpleNamespace(yaw=0.0)

            def get_forward_vector(self):
                radians = math.radians(self.rotation.yaw)
                return Vector(math.cos(radians), math.sin(radians), 0.0)

        class Waypoint:
            road_id = 1
            lane_width = 2.0

            def __init__(self, x, successor=None):
                self._x = x
                self._successor = successor

            @property
            def transform(self):
                return Transform(self._x)

            def next(self, _precision):
                return [] if self._successor is None else [self._successor]

        end = Waypoint(1.0)
        start = Waypoint(0.0, end)

        class RoadMap:
            def get_topology(self):
                return [(start, end)]

            def transform_to_geolocation(self, location):
                return MockGeoLocation(
                    latitude=10.0 - location.y / 100.0,
                    longitude=20.0 + location.x / 100.0,
                )

        road_map = RoadMap()
        if carla_09:
            road_map.geolocation_to_transform = lambda _geo: None
        return road_map

    def test_road_export_preserves_carla_010_wgs84_latitude(self):
        from digital_twin_bridge.geo_utils import extract_road_network_gps

        road_lines = extract_road_network_gps(self._road_export_map(carla_09=False))

        assert road_lines[0][0][1] == pytest.approx(10.01)

    def test_road_export_retains_carla_09_latitude_mirror(self):
        from digital_twin_bridge.geo_utils import extract_road_network_gps

        road_lines = extract_road_network_gps(self._road_export_map(carla_09=True))

        assert road_lines[0][0][1] == pytest.approx(9.99)


@pytest.mark.unit
class TestPerceptionTelemetry:
    def test_control_telemetry_always_contains_session_scan(self, mock_world):
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=lambda *_args, **_kwargs: {"items": []},
        )
        vehicle = MockActor(1001)
        mock_world._actors[vehicle.id] = vehicle
        session.vehicle = vehicle
        session._active = True
        expected = {
            "id": "vehicle-3",
            "class": "vehicle",
            "pos": [12.0, 0.5],
            "distance": 12.01,
            "bbox_dim": [4.5, 1.8],
            "in_path": True,
            "alert": "warn",
        }
        session._perception.capture_scan_snapshot = lambda: {"frame": object()}
        session._perception.analyze_scan_snapshot = lambda _snapshot: [
            SimpleNamespace(to_dict=lambda: expected)
        ]
        session._perception.finalize_scan = lambda detections: detections

        scheduled = session.apply_control(0.0, 0.0, 0.0)
        session._perception_scan_future.result(timeout=1.0)
        telemetry = session.apply_control(0.0, 0.0, 0.0)

        assert scheduled["detections"] == []
        assert telemetry["type"] == "telemetry"
        assert telemetry["detections"] == [expected]
        session.end()

    def test_control_frames_reuse_scan_until_sensor_cadence(
        self, mock_world, monkeypatch
    ):
        from digital_twin_bridge import drive_server
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=lambda *_args, **_kwargs: {"items": []},
            perception_scan_interval_seconds=0.1,
        )
        vehicle = MockActor(1001)
        mock_world._actors[vehicle.id] = vehicle
        session.vehicle = vehicle
        session._active = True
        clock = {"now": 100.0}
        monkeypatch.setattr(drive_server.time, "monotonic", lambda: clock["now"])

        scan_calls = []

        def analyze(_snapshot):
            scan_calls.append(clock["now"])
            return [
                SimpleNamespace(
                    to_dict=lambda: {
                        "id": f"vehicle-{len(scan_calls)}",
                        "class": "vehicle",
                    }
                )
            ]

        session._perception.capture_scan_snapshot = lambda: {"frame": object()}
        session._perception.analyze_scan_snapshot = analyze
        session._perception.finalize_scan = lambda detections: detections

        first_scheduled = session.apply_control(0.0, 0.0, 0.0)
        session._perception_scan_future.result(timeout=1.0)
        clock["now"] = 100.01
        first = session.apply_control(0.0, 0.0, 0.0)
        clock["now"] = 100.05
        cached = session.apply_control(0.0, 0.0, 0.0)
        clock["now"] = 100.11
        refresh_scheduled = session.apply_control(0.0, 0.0, 0.0)
        session._perception_scan_future.result(timeout=1.0)
        clock["now"] = 100.12
        refreshed = session.apply_control(0.0, 0.0, 0.0)

        assert len(scan_calls) == 2
        assert first_scheduled["detections"] == []
        assert cached["detections"] == first["detections"]
        assert refresh_scheduled["detections"] == first["detections"]
        assert refreshed["detections"] != first["detections"]
        session.end()

    @pytest.mark.asyncio
    async def test_dense_scan_is_single_worker_responsive_and_cleanup_safe(
        self, mock_world
    ):
        from digital_twin_bridge.drive_server import DriveSession

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=lambda *_args, **_kwargs: {"items": []},
            perception_scan_interval_seconds=0.0,
        )
        vehicle = MockActor(1001)
        mock_world._actors[vehicle.id] = vehicle
        session.vehicle = vehicle
        session._active = True

        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_threads = []
        event_loop_thread = threading.get_ident()

        session._perception.capture_scan_snapshot = lambda: {"dense": object()}

        def analyze(_snapshot):
            worker_threads.append(threading.get_ident())
            worker_started.set()
            assert release_worker.wait(1.0)
            return [
                SimpleNamespace(
                    to_dict=lambda: {"id": "vehicle-worker", "class": "vehicle"}
                )
            ]

        session._perception.analyze_scan_snapshot = analyze
        session._perception.finalize_scan = lambda detections: detections

        try:
            first = session.apply_control(0.0, 0.0, 0.0)
            running_future = session._perception_scan_future
            assert worker_started.wait(1.0)

            # Multiple controls stay responsive and cannot queue a second scan.
            await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
            second = session.apply_control(0.0, 0.0, 0.0)
            third = session.apply_control(0.0, 0.0, 0.0)
            assert first["detections"] == second["detections"] == third["detections"] == []
            assert len(worker_threads) == 1
            assert worker_threads[0] != event_loop_thread

            # Cleanup invalidates the generation without waiting for CPU work.
            session.end()
            assert session._perception_scan_future is None
            assert session._perception_scan_executor is None
            release_worker.set()
            running_future.result(timeout=1.0)
            assert session._cached_perception_detections == []
        finally:
            release_worker.set()
            session.end()


@pytest.mark.unit
class TestTestWebSocketBoundary:
    def test_test_socket_is_opt_in_and_bearer_authenticated(self):
        from digital_twin_bridge import drive_main
        from digital_twin_bridge.config import Config

        config = Config()
        assert drive_main.test_ws_access(config, None) == (
            False,
            "test WebSocket is disabled",
        )

        config.TEST_WS_ENABLED = "on"
        assert drive_main.test_ws_access(config, {}) == (
            False,
            "test WebSocket token is not configured",
        )

        config.TEST_WS_TOKEN = "test-secret"
        assert drive_main.test_ws_access(config, {}) == (
            False,
            "bearer token required",
        )
        assert drive_main.test_ws_access(
            config, {"Authorization": "Bearer wrong"}
        ) == (False, "invalid bearer token")
        assert drive_main.test_ws_access(
            config, {"Authorization": "Bearer test-secret"}
        ) == (True, "authorized")

    def test_log_subscriber_queue_stays_bounded(self):
        from digital_twin_bridge import drive_main

        queue = asyncio.Queue(maxsize=2)
        drive_main.enqueue_bounded(queue, {"id": 1})
        drive_main.enqueue_bounded(queue, {"id": 2})
        drive_main.enqueue_bounded(queue, {"id": 3})

        assert queue.qsize() == 2
        assert queue.get_nowait() == {"id": 2}
        assert queue.get_nowait() == {"id": 3}


@pytest.mark.unit
class TestPublisherPath:
    def test_uplink_writes_canonical_state_object_key(self, tmp_path):
        from digital_twin_bridge.config import Config
        from digital_twin_bridge.uplink import Uplink

        captured = {}

        class S3Client:
            def put_object(self, **kwargs):
                captured.update(kwargs)

        config = Config(LOCAL_SNAPSHOT_DIR=str(tmp_path))
        uplink = Uplink(config)
        uplink._s3_client = S3Client()

        uplink.publish_state(
            [{"object_id": "global-car-7"}],
            {"status": "connected", "objects_tracked": 1},
        )

        body = json.loads(captured["Body"])
        assert captured["Bucket"] == config.S3_BUCKET
        assert captured["Key"] == "api/state.json"
        assert captured["ContentType"] == "application/json"
        assert captured["CacheControl"] == "max-age=2"
        assert body["objects"] == [{"object_id": "global-car-7"}]
        assert body["bridge_status"]["objects_tracked"] == 1
        assert isinstance(body["updated_at"], str) and body["updated_at"]

    def test_state_snapshot_uses_producer_timestamp_for_last_updated(self):
        from digital_twin_bridge import drive_main

        tracked = SimpleNamespace(
            object_id="global-car-7",
            object_type="car",
            lat=37.9,
            lon=-122.3,
            confidence=0.91,
            street_name="Macdonald Avenue",
            timestamp_utc="2026-07-09T22:11:12.345Z",
            snapshot_url="https://example.invalid/latest.jpg",
            snapshot_timestamp="2026-07-09T22:11:11.000Z",
            last_seen=1_700_000_000.125,
        )
        registry = SimpleNamespace(get_all=lambda: [tracked])
        health = SimpleNamespace(get_status=lambda: {"effective_fps": 19.8})

        objects, status = drive_main.build_state_snapshot(
            registry, health, now=1_800_000_000.0
        )

        assert objects[0]["timestamp_utc"] == tracked.timestamp_utc
        assert objects[0]["snapshot_timestamp"] == tracked.snapshot_timestamp
        assert objects[0]["last_updated"] == int(
            drive_main._producer_epoch(tracked.timestamp_utc) * 1000
        )
        assert objects[0]["last_updated"] != int(tracked.last_seen * 1000)
        assert objects[0]["last_updated"] != 1_800_000_000_000
        assert status["state_source"] == "v2x_api_registry"
        assert status["road_props_spawned"] == 0

    def test_range_fetcher_forwards_opaque_continuation(self, monkeypatch):
        from digital_twin_bridge import drive_main
        from digital_twin_bridge.config import Config

        captured = {}

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"items": [], "next": None}

        def fake_get(url, *, params, timeout):
            captured.update(url=url, params=params, timeout=timeout)
            return Response()

        monkeypatch.setattr(drive_main.requests, "get", fake_get)
        fetch = drive_main.make_api_fetcher(Config())
        fetch("start", "end", 200, next_token="opaque-token")

        assert captured["url"].endswith("/detections/range")
        assert captured["params"] == {
            "start": "start",
            "end": "end",
            "limit": 200,
            "next": "opaque-token",
        }
        assert captured["timeout"] == Config().SCENE_FETCH_REQUEST_TIMEOUT_SECONDS

    def test_state_snapshot_rejects_stale_objects_and_snapshot_urls(self):
        from digital_twin_bridge import drive_main

        def tracked(object_id, event_age, snapshot_age):
            now = 2_000_000_000.0
            return SimpleNamespace(
                object_id=object_id,
                object_type="car",
                lat=37.9,
                lon=-122.3,
                confidence=0.9,
                street_name="Macdonald Avenue",
                timestamp_utc=now - event_age,
                snapshot_url=f"https://example.invalid/{object_id}.jpg",
                snapshot_timestamp=now - snapshot_age,
                last_seen=now,
            )

        fresh = tracked("fresh", event_age=2, snapshot_age=3)
        stale_snapshot = tracked("stale-snapshot", event_age=2, snapshot_age=91)
        stale_object = tracked("stale-object", event_age=31, snapshot_age=2)
        invalid_object = tracked("invalid-object", event_age=2, snapshot_age=2)
        invalid_object.timestamp_utc = "not-a-timestamp"

        objects, status = drive_main.build_state_snapshot(
            SimpleNamespace(
                get_all=lambda: [fresh, stale_snapshot, stale_object, invalid_object]
            ),
            SimpleNamespace(get_status=lambda: {"effective_fps": 20}),
            now=2_000_000_000.0,
            max_object_age_seconds=30,
            max_snapshot_age_seconds=90,
        )

        assert [obj["object_id"] for obj in objects] == ["fresh", "stale-snapshot"]
        assert objects[0]["snapshot_url"].endswith("/fresh.jpg")
        assert objects[1]["snapshot_url"] is None
        assert objects[1]["snapshot_timestamp"] == 1_999_999_909.0
        assert status["objects_tracked"] == 2

    def test_empty_api_page_prunes_stale_registry_entries(self, monkeypatch):
        from digital_twin_bridge.config import Config
        from digital_twin_bridge.v2x_poller import V2XPoller

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"items": []}

        monkeypatch.setattr(
            "digital_twin_bridge.v2x_poller.requests.get",
            lambda *_args, **_kwargs: Response(),
        )
        registry = SimpleNamespace(
            remove_stale=lambda **kwargs: setattr(
                registry, "removed_with", kwargs["max_age_seconds"]
            ) or 2,
            update_from_v2x=lambda _items: None,
            count=0,
        )
        config = Config()
        config.V2X_STALE_SECONDS = 123.0

        assert V2XPoller(config, registry, carla_map=None).poll_once() == 0
        assert registry.removed_with == 123.0
