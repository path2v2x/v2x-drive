"""Regression tests for bounded, event-loop-safe scene reconstruction."""

import asyncio
import threading

import pytest

from tests.conftest import SAMPLE_DETECTIONS


@pytest.mark.unit
class TestAsyncSceneStart:
    @pytest.mark.asyncio
    async def test_total_timeout_includes_waiting_for_worker_slot(self, mock_world):
        from digital_twin_bridge.drive_server import DriveSession

        started = []
        release = threading.Event()

        class BlockingReconstructor:
            def __init__(self, name):
                self.name = name

            def fetch(self, *_args, should_stop=None, **_kwargs):
                started.append(self.name)
                assert release.wait(1.0)
                return self.name

        def session(name, timeout):
            value = DriveSession(
                world=mock_world,
                carla_map=mock_world.get_map(),
                api_fetcher=lambda *_args, **_kwargs: {"items": []},
                scene_fetch_timeout_seconds=timeout,
            )
            value._reconstructor = BlockingReconstructor(name)
            return value

        first = session("first", 1.0)
        second = session("second", 1.0)
        queued = session("queued", 0.03)
        first_task = asyncio.create_task(first._fetch_scene_result("start", "end"))
        second_task = asyncio.create_task(second._fetch_scene_result("start", "end"))
        try:
            for _ in range(100):
                if set(started) == {"first", "second"}:
                    break
                await asyncio.sleep(0.005)
            assert set(started) == {"first", "second"}

            with pytest.raises(RuntimeError, match="Historical scene fetch timed out"):
                await queued._fetch_scene_result("start", "end")
            assert "queued" not in started
        finally:
            release.set()
            assert await first_task == "first"
            assert await second_task == "second"

    @pytest.mark.asyncio
    async def test_slow_http_fetch_keeps_event_loop_responsive_and_spawns_on_loop(
        self, mock_world, monkeypatch
    ):
        from digital_twin_bridge import drive_server
        from digital_twin_bridge.drive_server import DriveSession
        from tests.conftest import MockActor

        fetch_started = threading.Event()
        release_fetch = threading.Event()
        fetch_threads = []
        spawn_threads = []
        event_loop_thread = threading.get_ident()

        def slow_fetch(_start, _end, _limit=500):
            fetch_threads.append(threading.get_ident())
            fetch_started.set()
            assert release_fetch.wait(1.0)
            return {"items": [SAMPLE_DETECTIONS[0]], "count": 1}

        original_spawn = mock_world.try_spawn_actor

        def recording_spawn(*args, **kwargs):
            spawn_threads.append(threading.get_ident())
            return original_spawn(*args, **kwargs)

        monkeypatch.setattr(mock_world, "try_spawn_actor", recording_spawn)
        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=slow_fetch,
            scene_fetch_timeout_seconds=1.0,
        )
        control_session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=lambda *_args, **_kwargs: {"items": []},
        )
        control_session.vehicle = MockActor(9_001)
        mock_world._actors[control_session.vehicle.id] = control_session.vehicle
        control_session._active = True
        drive_server._active_sessions.append(session)

        start_task = asyncio.create_task(
            session.start("2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z")
        )
        try:
            for _ in range(50):
                if fetch_started.is_set():
                    break
                await asyncio.sleep(0.005)
            assert fetch_started.is_set()

            # Reaching this point while the fetch is blocked is the heartbeat:
            # the bridge loop can still service control/status work.
            await asyncio.sleep(0.02)
            heartbeat = control_session.apply_control(0.1, 0.2, 0.0)
            assert heartbeat["type"] == "telemetry"
            assert drive_server.active_session_count() == 1
            assert not start_task.done()

            release_fetch.set()
            result = await start_task
            assert result["type"] == "session_ready"
            assert fetch_threads and fetch_threads[0] != event_loop_thread
            assert spawn_threads and set(spawn_threads) == {event_loop_thread}
        finally:
            release_fetch.set()
            if not start_task.done():
                await start_task
            session.end()
            control_session.end()
            if session in drive_server._active_sessions:
                drive_server._active_sessions.remove(session)

    @pytest.mark.asyncio
    async def test_timed_out_fetch_can_finish_reads_but_never_spawn_actors(
        self, mock_world
    ):
        from digital_twin_bridge.drive_server import DriveSession

        fetch_started = threading.Event()
        release_fetch = threading.Event()

        def slow_fetch(_start, _end, _limit=500):
            fetch_started.set()
            assert release_fetch.wait(1.0)
            return {"items": [SAMPLE_DETECTIONS[0]], "count": 1}

        session = DriveSession(
            world=mock_world,
            carla_map=mock_world.get_map(),
            api_fetcher=slow_fetch,
            scene_fetch_timeout_seconds=0.03,
        )
        try:
            with pytest.raises(RuntimeError, match="Historical scene fetch timed out"):
                await session.start(
                    "2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z"
                )
            assert fetch_started.is_set()
            assert mock_world.spawned_actors == []

            release_fetch.set()
            # Let the abandoned read observe cancellation and release its slot.
            for _ in range(50):
                await asyncio.sleep(0.005)
                if not mock_world.spawned_actors:
                    continue
            assert mock_world.spawned_actors == []
        finally:
            release_fetch.set()
