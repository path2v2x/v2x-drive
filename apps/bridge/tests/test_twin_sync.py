"""Tests for TwinSync: detections -> CARLA actor lifecycle."""

import hashlib
import threading
import time

import pytest

from digital_twin_bridge import twin_sync as twin_sync_module
from digital_twin_bridge.twin_sync import TwinSync

from tests.conftest import MockBlueprint, MockLocation, MockTransform


def make_detection(object_id="global_car_1", object_type="car", lat=37.9155, lon=-122.3348):
    return {
        "event_id": "event-1",
        "object_id": object_id,
        "object_type": object_type,
        "confidence_score": 0.9,
        "timestamp_utc": "2026-07-10T03:57:26.073Z",
        "media_timestamp_utc": "2026-07-10T03:57:18.942Z",
        "timestamp_schema_version": 2,
        "media_time_trusted": True,
        "media_clock": {
            "schema_version": 1,
            "source": "hls_ext_x_program_date_time",
            "anchor_program_date_time_utc": "2026-07-10T03:57:10.000Z",
            "position_milliseconds": 8942,
        },
        "device_id": "ch1",
        "track_id": 13,
        "bbox": {"x1": 488.0, "y1": 122.0, "x2": 836.0, "y2": 336.0},
        "gps_location": {"latitude": lat, "longitude": lon},
    }


@pytest.fixture
def sync(mock_world, monkeypatch):
    # GPS -> CARLA: deterministic linear mapping so movement is observable.
    monkeypatch.setattr(
        twin_sync_module,
        "gps_to_carla",
        lambda m, lat, lon: MockLocation((lat - 37.9) * 1000.0, (lon + 122.3) * 1000.0, 0.0),
    )
    # The default MockBlueprintLibrary.filter does substring matching that
    # misses "vehicle.*"; substitute wildcard-aware pools.
    pools = {
        "vehicle.*": [MockBlueprint("vehicle.tesla.model3"), MockBlueprint("vehicle.ford.truck")],
        "walker.pedestrian.*": [MockBlueprint("walker.pedestrian.0001")],
    }
    mock_world._blueprint_library.filter = lambda pattern: list(pools.get(pattern, []))
    return TwinSync(mock_world, mock_world.get_map(), poll_interval=1.0, despawn_after=12.0)


class TestSpawn:
    def test_blueprint_selection_is_stable_across_instances(self, sync, mock_world):
        sync._load_blueprints()
        first = sync._blueprint_for(twin_sync_module.TwinTrack("global_car_42", "car")).id
        other = TwinSync(mock_world, mock_world.get_map())
        other._vehicle_blueprints = list(sync._vehicle_blueprints)
        other._truck_blueprints = list(sync._truck_blueprints)
        other._walker_blueprints = list(sync._walker_blueprints)
        other._blueprints_loaded = True
        second = other._blueprint_for(twin_sync_module.TwinTrack("global_car_42", "car")).id
        assert second == first

    def test_car_detection_spawns_vehicle(self, sync, mock_world):
        sync._apply([make_detection()])
        assert len(sync.actor_ids()) == 1
        actor = mock_world.get_actor(next(iter(sync.actor_ids())))
        assert actor.type_id.startswith("vehicle.")
        assert actor.physics_enabled is False

    def test_person_detection_spawns_walker(self, sync, mock_world):
        sync._apply([make_detection(object_id="global_person_1", object_type="person")])
        actor = mock_world.get_actor(next(iter(sync.actor_ids())))
        assert actor.type_id.startswith("walker.")

    def test_track_keeps_stable_blueprint_and_actor(self, sync):
        sync._apply([make_detection()])
        first_ids = sync.actor_ids()
        sync._apply([make_detection(lat=37.9156)])
        assert sync.actor_ids() == first_ids

    def test_status_exposes_detection_to_actor_evidence(self, sync, mock_world):
        sync._apply([make_detection()])

        status = sync.status()

        assert status["actors"] == 1
        assert len(status["objects"]) == 1
        evidence = status["objects"][0]
        actor = mock_world.get_actor(evidence["actor_id"])
        assert evidence == {
            "object_id": "global_car_1",
            "object_type": "car",
            "event_id": "event-1",
            "detection_timestamp_utc": "2026-07-10T03:57:26.073Z",
            "media_timestamp_utc": "2026-07-10T03:57:18.942Z",
            "timestamp_schema_version": 2,
            "media_time_trusted": True,
            "media_clock": {
                "schema_version": 1,
                "source": "hls_ext_x_program_date_time",
                "anchor_program_date_time_utc": "2026-07-10T03:57:10.000Z",
                "position_milliseconds": 8942,
            },
            "device_id": "ch1",
            "track_id": 13,
            "bbox": {"x1": 488.0, "y1": 122.0, "x2": 836.0, "y2": 336.0},
            "gps_location": {"latitude": 37.9155, "longitude": -122.3348},
            "raw_carla_location": {
                "x": pytest.approx(15.5),
                "y": pytest.approx(-34.8),
                "z": pytest.approx(0.0),
            },
            "target_carla_location": {
                "x": pytest.approx(15.5),
                "y": pytest.approx(-34.8),
                "z": pytest.approx(0.4),
            },
            "lane_snap_distance_m": pytest.approx(0.0),
            "raw_to_target_planar_m": pytest.approx(0.0),
            "raw_to_actor_planar_m": pytest.approx(0.0),
            "reference_to_actor_planar_m": None,
            "placement_planar_error_m": None,
            "placement_metric_status": "independent_reference_missing",
            "tracked_actor_id": actor.id,
            "actor_id": actor.id,
            "actor_present": True,
            "actor_type": actor.type_id,
            "carla_transform": {
                "location": {
                    "x": actor.get_transform().location.x,
                    "y": actor.get_transform().location.y,
                    "z": actor.get_transform().location.z,
                },
                "rotation": {
                    "pitch": actor.get_transform().rotation.pitch,
                    "yaw": actor.get_transform().rotation.yaw,
                    "roll": actor.get_transform().rotation.roll,
                },
            },
            "cleanup_failure": None,
            "actor_quarantined": False,
            "quarantined_reason": None,
        }

    def test_status_fails_closed_when_tracked_actor_vanished(self, sync, mock_world):
        sync._apply([make_detection()])
        tracked_actor_id = next(iter(sync.actor_ids()))
        mock_world._actors.clear()

        status = sync.status()

        assert status["actors"] == 0
        evidence = status["objects"][0]
        assert evidence["tracked_actor_id"] == tracked_actor_id
        assert evidence["actor_id"] is None
        assert evidence["actor_present"] is False
        assert evidence["actor_type"] is None
        assert evidence["carla_transform"] is None

    def test_unknown_types_and_missing_gps_ignored(self, sync):
        sync._apply([
            make_detection(object_type="traffic light"),
            {"object_id": "x", "object_type": "car"},  # no gps_location
            {"object_type": "car", "gps_location": {"latitude": 1, "longitude": 2}},  # no id
        ])
        assert sync.actor_ids() == set()

    def test_vehicle_far_from_driving_lane_fails_closed(self, sync, mock_world):
        waypoint = mock_world.get_map().get_waypoint(MockLocation())
        waypoint.transform.location = MockLocation(100.0, 100.0, 0.0)
        mock_world.get_map().get_waypoint = lambda *_args, **_kwargs: waypoint
        sync._apply([make_detection()])
        assert sync.actor_ids() == set()
        status = sync.status()["objects"][0]
        assert status["actor_present"] is False
        assert status["lane_snap_distance_m"] > 4.0

    def test_nearby_lane_supplies_only_height_and_yaw(self, sync, mock_world):
        waypoint = mock_world.get_map().get_waypoint(MockLocation())
        waypoint.transform.location = MockLocation(18.5, -34.8, 1.25)
        waypoint.transform.rotation.yaw = 73.0
        mock_world.get_map().get_waypoint = lambda *_args, **_kwargs: waypoint
        sync._apply([make_detection()])
        actor = mock_world.get_actor(next(iter(sync.actor_ids())))
        transform = actor.get_transform()
        assert transform.location.x == pytest.approx(15.5)
        assert transform.location.y == pytest.approx(-34.8)
        assert transform.location.z == pytest.approx(1.55)
        assert transform.rotation.yaw == pytest.approx(73.0)
        status = sync.status()["objects"][0]
        assert status["lane_snap_distance_m"] == pytest.approx(3.0)
        assert status["raw_to_target_planar_m"] == pytest.approx(0.0)
        assert status["placement_planar_error_m"] is None
        assert status["placement_metric_status"] == "independent_reference_missing"

    def test_firetruck_never_selected(self, sync, mock_world):
        pools = {
            "vehicle.*": [
                MockBlueprint("vehicle.carlamotors.firetruck"),
                MockBlueprint("vehicle.tesla.model3"),
            ],
            "walker.pedestrian.*": [],
        }
        mock_world._blueprint_library.filter = lambda pattern: list(pools.get(pattern, []))
        for i in range(8):
            sync._apply([make_detection(object_id=f"global_car_{i}")])
        for actor_id in sync.actor_ids():
            assert "firetruck" not in mock_world.get_actor(actor_id).type_id

    def test_first_spawn_candidate_and_final_transform_are_exact(
        self, sync, mock_world, monkeypatch
    ):
        attempts = []
        original_spawn = mock_world.try_spawn_actor

        def recording_spawn(blueprint, transform, *args, **kwargs):
            attempts.append(transform)
            return original_spawn(blueprint, transform, *args, **kwargs)

        monkeypatch.setattr(mock_world, "try_spawn_actor", recording_spawn)

        sync._apply([make_detection()])

        assert len(attempts) == 1
        track = sync._tracks["global_car_1"]
        actor = mock_world.get_actor(track.actor_id)
        assert attempts[0] == MockTransform(track.current, attempts[0].rotation)
        assert actor.physics_enabled is False
        assert actor.get_transform() == MockTransform(track.current, attempts[0].rotation)

    def test_blocked_exact_candidate_uses_nearby_bootstrap_then_exact_transform(
        self, sync, mock_world, monkeypatch
    ):
        attempts = []
        original_spawn = mock_world.try_spawn_actor

        def block_exact_once(blueprint, transform, *args, **kwargs):
            attempts.append(transform)
            if len(attempts) == 1:
                return None
            return original_spawn(blueprint, transform, *args, **kwargs)

        monkeypatch.setattr(mock_world, "try_spawn_actor", block_exact_once)

        sync._apply([make_detection()])

        assert len(attempts) == 2
        track = sync._tracks["global_car_1"]
        actor = mock_world.get_actor(track.actor_id)
        intended = MockTransform(track.current, attempts[0].rotation)
        assert attempts[0] == intended
        assert attempts[1].location != intended.location
        assert attempts[1].location.distance(intended.location) <= (
            twin_sync_module.SPAWN_BOOTSTRAP_MAX_OFFSET_M
        )
        assert actor.physics_enabled is False
        assert actor.get_transform() == intended
        assert track.current == intended.location
        assert track.target == intended.location

    def test_all_bootstrap_candidates_blocked_is_bounded_and_has_no_actor(
        self, sync, mock_world, monkeypatch
    ):
        attempts = []

        def always_block(_blueprint, transform, *_args, **_kwargs):
            attempts.append(transform)
            return None

        monkeypatch.setattr(mock_world, "try_spawn_actor", always_block)

        sync._apply([make_detection()])

        assert len(attempts) == len(twin_sync_module.SPAWN_BOOTSTRAP_OFFSETS)
        assert all(
            attempt.location.distance(attempts[0].location)
            <= twin_sync_module.SPAWN_BOOTSTRAP_MAX_OFFSET_M
            for attempt in attempts
        )
        assert sync.actor_ids() == set()
        assert mock_world.spawned_actors == []
        assert sync._tracks["global_car_1"].actor_id is None

    def test_setup_failure_destroys_provisional_actors_and_tracks_none(
        self, sync, mock_world, monkeypatch
    ):
        provisional_actors = []
        original_spawn = mock_world.try_spawn_actor

        def spawn_with_failed_exact_transform(blueprint, transform, *args, **kwargs):
            actor = original_spawn(blueprint, transform, *args, **kwargs)
            provisional_actors.append(actor)

            def fail_exact_transform(_transform):
                raise RuntimeError("set_transform failed")

            actor.set_transform = fail_exact_transform
            return actor

        monkeypatch.setattr(
            mock_world,
            "try_spawn_actor",
            spawn_with_failed_exact_transform,
        )

        sync._apply([make_detection()])

        assert len(provisional_actors) == len(
            twin_sync_module.SPAWN_BOOTSTRAP_OFFSETS
        )
        assert all(actor.physics_enabled is False for actor in provisional_actors)
        assert all(actor.is_destroyed for actor in provisional_actors)
        assert sync.actor_ids() == set()
        assert sync._tracks["global_car_1"].actor_id is None

    def test_successful_retry_clears_provisional_quarantine(
        self, sync, mock_world, monkeypatch
    ):
        original_spawn = mock_world.try_spawn_actor
        provisional = None

        def fail_first_setup(blueprint, transform, *args, **kwargs):
            nonlocal provisional
            actor = original_spawn(blueprint, transform, *args, **kwargs)
            if provisional is None:
                provisional = actor

                def fail_exact_transform(_transform):
                    raise RuntimeError("set_transform failed")

                actor.set_transform = fail_exact_transform
            return actor

        monkeypatch.setattr(mock_world, "try_spawn_actor", fail_first_setup)

        sync._apply([make_detection()])

        track = sync._tracks["global_car_1"]
        actor = mock_world.get_actor(track.actor_id)
        assert provisional.is_destroyed
        assert actor is not provisional
        assert track.cleanup_failure is None
        assert track.quarantined_reason is None
        assert sync.status()["objects"][0]["actor_present"] is True

        sync._apply([make_detection(lat=37.9156)])
        track.lerp_start = time.time() - 5.0
        sync.tick()
        assert actor.get_transform().location.x == pytest.approx(track.target.x)

    def test_retry_reuses_candidate_order_and_never_duplicates_actor(
        self, sync, mock_world, monkeypatch
    ):
        attempts = []
        first_poll_attempts = len(twin_sync_module.SPAWN_BOOTSTRAP_OFFSETS)
        original_spawn = mock_world.try_spawn_actor

        def block_first_poll(blueprint, transform, *args, **kwargs):
            attempts.append((blueprint.id, transform))
            if len(attempts) <= first_poll_attempts:
                return None
            if len(attempts) == first_poll_attempts + 1:
                return None
            return original_spawn(blueprint, transform, *args, **kwargs)

        monkeypatch.setattr(mock_world, "try_spawn_actor", block_first_poll)

        sync._apply([make_detection()])
        assert sync.actor_ids() == set()
        sync._apply([make_detection()])
        actor_ids = sync.actor_ids()
        assert len(actor_ids) == 1
        attempts_after_spawn = len(attempts)
        sync._apply([make_detection(lat=37.9156)])

        assert sync.actor_ids() == actor_ids
        assert len(mock_world.spawned_actors) == 1
        assert len(attempts) == attempts_after_spawn
        first_order = [
            attempt.location for _blueprint, attempt in attempts[:first_poll_attempts]
        ]
        retry_order = [
            attempt.location
            for _blueprint, attempt in attempts[
                first_poll_attempts:first_poll_attempts + 2
            ]
        ]
        assert retry_order == first_order[:2]
        assert len({blueprint for blueprint, _attempt in attempts}) == 1

    def test_blueprint_selection_uses_stable_object_digest(self, sync):
        sync._load_blueprints()
        track = twin_sync_module.TwinTrack("global_car_digest", "car")
        digest = hashlib.sha256(track.object_id.encode("utf-8")).digest()
        expected_index = int.from_bytes(digest[:8], "big") % len(
            sync._vehicle_blueprints
        )

        assert sync._blueprint_for(track).id == (
            sync._vehicle_blueprints[expected_index].id
        )


class TestLifecycle:
    def test_stale_track_despawns(self, sync, mock_world):
        sync._apply([make_detection()])
        actor_id = next(iter(sync.actor_ids()))
        track = sync._tracks["global_car_1"]
        track.last_seen = time.time() - 20.0
        sync._despawn_stale(time.time())
        assert sync.actor_ids() == set()
        assert mock_world.get_actor(actor_id).is_destroyed

    def test_fresh_track_survives_despawn_pass(self, sync):
        sync._apply([make_detection()])
        sync._despawn_stale(time.time())
        assert len(sync.actor_ids()) == 1

    def test_stop_destroys_everything(self, sync, mock_world):
        sync._apply([make_detection(), make_detection(object_id="global_car_2")])
        actor_ids = set(sync.actor_ids())
        sync.stop()
        assert sync.actor_ids() == set()
        for actor_id in actor_ids:
            assert mock_world.get_actor(actor_id).is_destroyed


class TestTick:
    def test_tick_lerps_towards_new_fix(self, sync, mock_world):
        sync._apply([make_detection(lat=37.9155)])
        actor_id = next(iter(sync.actor_ids()))
        track = sync._tracks["global_car_1"]

        # New fix ~11m north; pretend the poll happened 0.5s ago (mid-lerp).
        sync._apply([make_detection(lat=37.9156)])
        track.lerp_start = time.time() - 0.5
        sync.tick()

        actor = mock_world.get_actor(actor_id)
        x = actor.get_transform().location.x
        assert track.current.x < x <= track.target.x

        # Once the lerp window has fully elapsed, we land on the target.
        track.lerp_start = time.time() - 5.0
        sync.tick()
        assert actor.get_transform().location.x == pytest.approx(track.target.x)

    def test_tick_retains_ownership_when_actor_lookup_returns_none(
        self, sync, mock_world
    ):
        sync._apply([make_detection()])
        track = sync._tracks["global_car_1"]
        actor_id = track.actor_id
        actor = mock_world.get_actor(actor_id)
        original_get_actor = mock_world.get_actor
        calls = 0

        def miss_first_lookup(requested_actor_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return original_get_actor(requested_actor_id)

        mock_world.get_actor = miss_first_lookup
        sync.tick()
        assert track.actor_id == actor_id
        assert sync.actor_ids() == {actor_id}
        assert track.quarantined_reason == "actor_lookup_missing"
        assert track.cleanup_failure == "actor_lookup_missing"

        status = sync.status()
        evidence = status["objects"][0]
        assert evidence["tracked_actor_id"] == actor_id
        assert evidence["actor_id"] is None
        assert evidence["actor_present"] is False
        assert evidence["actor_quarantined"] is True
        assert evidence["quarantined_reason"] == "actor_lookup_missing"
        assert status["cleanup_failures"] == {
            "global_car_1": "actor_lookup_missing"
        }

        sync._apply([make_detection(lat=37.9156)])
        assert actor.is_destroyed
        assert sync.actor_ids() == set()
        assert sync.status()["objects"] == []
        assert len(mock_world.spawned_actors) == 1

        sync._apply([make_detection(lat=37.9156)])
        assert len(mock_world.spawned_actors) == 2
        assert sync.actor_ids() != {actor_id}


class TestReplay:
    def make_replay_sync(self, mock_world, monkeypatch, items):
        monkeypatch.setattr(
            twin_sync_module,
            "gps_to_carla",
            lambda m, lat, lon: MockLocation((lat - 37.9) * 1000.0, (lon + 122.3) * 1000.0, 0.0),
        )
        pools = {
            "vehicle.*": [MockBlueprint("vehicle.tesla.model3")],
            "walker.pedestrian.*": [MockBlueprint("walker.pedestrian.0001")],
        }
        mock_world._blueprint_library.filter = lambda pattern: list(pools.get(pattern, []))
        calls = []

        def fetcher(start, end, limit=200):
            calls.append((start, end))
            return {"items": [i for i in items if start <= i["timestamp_utc"] <= end]}

        sync = TwinSync(
            mock_world, mock_world.get_map(),
            poll_interval=1.0, despawn_after=12.0, range_fetcher=fetcher,
        )
        return sync, calls

    def test_replay_requires_fetcher(self, sync):
        import pytest as _pytest
        with _pytest.raises(RuntimeError):
            sync.start_replay(time.time() - 3600)
        assert sync.status()["replay_supported"] is False

    def test_replay_feeds_recorded_detections(self, mock_world, monkeypatch):
        start = time.time() - 3600.0
        iso = twin_sync_module._epoch_to_iso
        items = [{
            "object_id": "global_car_9", "object_type": "car",
            "timestamp_utc": iso(start + 1.0),
            "gps_location": {"latitude": 37.9155, "longitude": -122.3348},
        }]
        sync, calls = self.make_replay_sync(mock_world, monkeypatch, items)
        sync.start_replay(start)
        assert sync.mode == "replay"
        # Pretend 5s of wall time elapsed since replay start.
        sync._replay["wall0"] = time.time() - 5.0
        sync._fetch_replay_chunk()
        sync._apply_pending_replay()
        assert len(sync.actor_ids()) == 1
        assert calls and calls[0][0].startswith(iso(start)[:19])
        clock = sync.replay_clock()
        assert abs(clock - (start + 5.0)) < 1.0

    def test_replay_despawns_by_virtual_clock(self, mock_world, monkeypatch):
        start = time.time() - 3600.0
        iso = twin_sync_module._epoch_to_iso
        items = [{
            "object_id": "global_car_9", "object_type": "car",
            "timestamp_utc": iso(start + 1.0),
            "gps_location": {"latitude": 37.9155, "longitude": -122.3348},
        }]
        sync, _ = self.make_replay_sync(mock_world, monkeypatch, items)
        sync.start_replay(start)
        sync._replay["wall0"] = time.time() - 5.0
        sync._fetch_replay_chunk()
        sync._apply_pending_replay()
        assert len(sync.actor_ids()) == 1
        # Jump the virtual clock far past the detection: despawn on next step.
        sync._replay["wall0"] = time.time() - 60.0
        sync._replay["cursor"] = start + 30.0
        sync._fetch_replay_chunk()
        sync._apply_pending_replay()
        assert sync.actor_ids() == set()

    def test_go_live_clears_replay_actors(self, mock_world, monkeypatch):
        start = time.time() - 3600.0
        iso = twin_sync_module._epoch_to_iso
        items = [{
            "object_id": "global_car_9", "object_type": "car",
            "timestamp_utc": iso(start + 1.0),
            "gps_location": {"latitude": 37.9155, "longitude": -122.3348},
        }]
        sync, _ = self.make_replay_sync(mock_world, monkeypatch, items)
        sync.start_replay(start)
        sync._replay["wall0"] = time.time() - 5.0
        sync._fetch_replay_chunk()
        sync._apply_pending_replay()
        actor_ids = set(sync.actor_ids())
        assert actor_ids
        sync.go_live()
        assert sync.mode == "live"
        assert sync.replay_clock() is None
        assert sync.actor_ids() == set()
        for actor_id in actor_ids:
            assert mock_world.get_actor(actor_id).is_destroyed

    def test_superseded_replay_fetch_cannot_apply_to_new_generation(self, sync):
        fetch_started = threading.Event()
        release_fetch = threading.Event()

        def fetcher(_start, _end, _limit=200):
            fetch_started.set()
            assert release_fetch.wait(1.0)
            return {"items": [{"object_id": "old-replay-result"}]}

        sync._range_fetcher = fetcher
        first_start = time.time() - 120.0
        sync.start_replay(first_start)
        sync._replay["wall0"] = time.time() - 1.0
        worker = threading.Thread(target=sync._fetch_replay_chunk)
        worker.start()
        try:
            assert fetch_started.wait(1.0)
            sync.start_replay(first_start + 60.0)
            release_fetch.set()
            worker.join(timeout=1.0)
            assert not worker.is_alive()

            applied = []
            sync._apply = lambda items, **_kwargs: applied.extend(items)
            sync._apply_pending_replay()

            assert applied == []
            assert sync._pending_replay is None
            assert sync._replay["start"] == pytest.approx(first_start + 60.0)
        finally:
            release_fetch.set()
            worker.join(timeout=1.0)


class TestFetchParsing:
    def test_flattens_cameras_and_skips_stale(self, sync, monkeypatch):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        stale = (now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        payload = {
            "cameras": {
                "ch1": {"updated_at": fresh, "detections": [make_detection()]},
                "ch2": {"updated_at": stale, "detections": [make_detection(object_id="old")]},
            }
        }

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        monkeypatch.setattr(
            twin_sync_module.requests, "get", lambda url, timeout=5: FakeResponse()
        )
        detections = sync._fetch_detections()
        assert [d["object_id"] for d in detections] == ["global_car_1"]

    def test_missing_malformed_future_and_stale_camera_times_fail_closed(
        self, sync, monkeypatch
    ):
        now = 2_000_000_000.0
        monkeypatch.setattr(twin_sync_module.time, "time", lambda: now)
        iso = twin_sync_module._epoch_to_iso
        payload = {
            "cameras": {
                "fresh": {
                    "updated_at": iso(now - 1.0),
                    "detections": [make_detection(object_id="fresh")],
                },
                "future-within-tolerance": {
                    "updated_at": iso(now + 4.0),
                    "detections": [make_detection(object_id="tolerated")],
                },
                "missing": {
                    "detections": [make_detection(object_id="missing")],
                },
                "malformed": {
                    "updated_at": "not-a-time",
                    "detections": [make_detection(object_id="malformed")],
                },
                "future": {
                    "updated_at": iso(now + 6.0),
                    "detections": [make_detection(object_id="future")],
                },
                "stale": {
                    "updated_at": iso(now - 9.0),
                    "detections": [make_detection(object_id="stale")],
                },
            }
        }

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        monkeypatch.setattr(
            twin_sync_module.requests, "get", lambda *_args, **_kwargs: FakeResponse()
        )

        detections = sync._fetch_detections()

        assert [item["object_id"] for item in detections] == [
            "fresh",
            "tolerated",
        ]
