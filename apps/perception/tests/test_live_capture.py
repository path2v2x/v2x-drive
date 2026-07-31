import sys
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import select
import subprocess
import threading
import time
import unittest


PERCEPTION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PERCEPTION_DIR))

from live_capture import (  # noqa: E402
    LiveStreamReader,
    _AsyncCapturePreparation,
    _AsyncMediaClockResolution,
    _begin_terminal_recovery,
    _cancel_proactive_preparations,
    _DIAGNOSTIC_CLEANUP_LIMIT,
    _DIAGNOSTIC_PREPARATION_LIMIT,
    _promote_reader_owned_cleanup,
    _PROACTIVE_PREPARATIONS,
    _PROACTIVE_PREPARATIONS_LOCK,
    _start_reader_owned_cleanup,
    _start_terminal_cleanup,
    _TERMINAL_CLEANUPS,
    _TERMINAL_CLEANUPS_LOCK,
    capture_preparation_topology,
)
from decoder_admission import AUXILIARY_DECODER_ADMISSION  # noqa: E402
from kinesis_utils import (  # noqa: E402
    _NVDEC_URGENT_FRAGMENT_MATCH_EXECUTOR,
    _run_nvdec_fragment_match,
)
from runtime_health import StreamRecovery  # noqa: E402


class ScriptedCapture:
    def __init__(self, frames, block_after_frames=None):
        self.frames = list(frames)
        self.block_after_frames = block_after_frames
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        if self.frames:
            return True, self.frames.pop(0)
        if self.block_after_frames is not None:
            self.block_after_frames.wait(1.0)
        return False, None

    def release(self):
        self.released = True


class FakeMediaClock:
    def __init__(self, timestamp="2026-07-10T03:57:23.388Z"):
        self.positions = []
        self.timestamp = timestamp

    def metadata_at(self, position_milliseconds):
        self.positions.append(position_milliseconds)
        return {
            "media_timestamp_utc": self.timestamp,
            "media_clock": {
                "source": "hls_ext_x_program_date_time",
                "position_milliseconds": position_milliseconds,
            },
        }


class FakeTransportMediaClock(FakeMediaClock):
    evidence_method = "exact_same_session_pts"


class TransportClockCapture:
    """Capture fixture exposing same-session transport evidence per read."""

    def __init__(self, frames, positions, next_frame_gate=None):
        self.frames = list(frames)
        self.positions = list(positions)
        self.current_position = None
        self.next_frame_gate = next_frame_gate
        self.released = False
        self.clock = FakeTransportMediaClock()

    def isOpened(self):
        return not self.released

    def read(self):
        if self.released or not self.frames:
            return False, None
        if self.current_position is not None and self.next_frame_gate is not None:
            self.next_frame_gate.wait(1.0)
        self.current_position = self.positions.pop(0)
        return True, self.frames.pop(0)

    def get(self, _property):
        return self.current_position

    def transport_media_clock(self):
        return None if self.current_position is None else self.clock

    def transport_clock_diagnostic(self):
        return "starting" if self.current_position is None else "matched"

    def release(self):
        self.released = True


class SequencedMediaClock:
    def __init__(self, timestamps):
        self.timestamps = list(timestamps)

    def metadata_at(self, position_milliseconds):
        timestamp = self.timestamps.pop(0) if len(self.timestamps) > 1 else self.timestamps[0]
        return {
            "media_timestamp_utc": timestamp,
            "media_clock": {
                "source": "hls_ext_x_program_date_time",
                "position_milliseconds": position_milliseconds,
            },
        }


class GatedPositionCapture:
    def __init__(self, frames, positions, next_frame_gate):
        self.frames = list(frames)
        self.positions = list(positions)
        self.next_frame_gate = next_frame_gate
        self.index = 0
        self.current_position = None
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        if self.index >= len(self.frames):
            self.next_frame_gate.wait(1.0)
            return False, None
        if self.index > 0:
            self.next_frame_gate.wait(1.0)
        frame = self.frames[self.index]
        self.current_position = self.positions[self.index]
        self.index += 1
        return True, frame

    def get(self, _property):
        return self.current_position

    def release(self):
        self.released = True


class PositionedScriptedCapture:
    def __init__(self, frames, positions, block_after_frames=None):
        self.frames = list(frames)
        self.positions = list(positions)
        self.block_after_frames = block_after_frames
        self.current_position = None
        self.released = False

    def isOpened(self):
        return not self.released

    def read(self):
        if self.released:
            return False, None
        if self.frames:
            self.current_position = self.positions.pop(0)
            return True, self.frames.pop(0)
        if self.block_after_frames is not None:
            self.block_after_frames.wait(1.0)
        return False, None

    def get(self, _property):
        return self.current_position

    def release(self):
        self.released = True


class ContinuousCapture:
    def __init__(self, prefix):
        self.prefix = prefix
        self.count = 0
        self.released = False

    def isOpened(self):
        return not self.released

    def read(self):
        if self.released:
            return False, None
        time.sleep(0.002)
        self.count += 1
        return True, f"{self.prefix}-frame-{self.count}"

    def get(self, _property):
        return self.count * 50.0

    def release(self):
        self.released = True


class LiveStreamReaderTests(unittest.TestCase):
    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def test_topology_sanitizes_unknown_state_and_never_blocks_on_locks(self):
        source_entered = threading.Event()
        release_source = threading.Event()
        secret = "https://camera.invalid/live?token=top-secret"

        def blocked_source():
            source_entered.set()
            release_source.wait(1.0)
            raise RuntimeError(secret)

        preparation = _AsyncCapturePreparation(
            source_factory=blocked_source,
            clock_source_factory=None,
            capture_factory=lambda _source: None,
            media_clock_factory=None,
            media_clock_validator=None,
            capture_position_milliseconds=lambda _cap: 0.0,
            media_frame_identity=lambda frame: frame,
            stop_event=threading.Event(),
            wall_time=time.time,
            monotonic=time.monotonic,
            reserve_decoder_slot=True,
        )
        try:
            self.assertTrue(source_entered.wait(1.0))
            preparation._set_stage(secret)
            topology = capture_preparation_topology()
            self.assertEqual(topology["proactive_preparation_snapshot"], "ok")
            self.assertEqual(
                topology["proactive_preparation_states"]["stage_counts"],
                {"other": 1},
            )
            encoded = json.dumps(topology, sort_keys=True)
            self.assertNotIn("top-secret", encoded)
            self.assertNotIn(str(id(preparation)), encoded)

            with preparation._stage_lock:
                started = time.monotonic()
                busy = capture_preparation_topology()
                self.assertLess(time.monotonic() - started, 0.1)
            self.assertEqual(
                busy["proactive_preparation_states"]["stage_counts"],
                {"lock_busy": 1},
            )

            with preparation._result_lock:
                busy = capture_preparation_topology()
            self.assertEqual(
                busy["proactive_preparation_states"][
                    "claimed_lock_busy_count"
                ],
                1,
            )

            with _PROACTIVE_PREPARATIONS_LOCK:
                started = time.monotonic()
                busy = capture_preparation_topology()
                self.assertLess(time.monotonic() - started, 0.1)
            self.assertEqual(busy["proactive_preparation_snapshot"], "lock_busy")
            self.assertEqual(busy["proactive_preparations"], -1)

            with _TERMINAL_CLEANUPS_LOCK:
                started = time.monotonic()
                busy = capture_preparation_topology()
                self.assertLess(time.monotonic() - started, 0.1)
            self.assertEqual(busy["terminal_cleanup_snapshot"], "lock_busy")
            self.assertEqual(busy["terminal_cleanups"], -1)
            self.assertEqual(busy["terminal_cleanup_sampled_count"], 0)
        finally:
            preparation.discard()
            release_source.set()
            preparation.join(1.0)
        self.assertTrue(preparation.wait_quiesced(1.0))

    def test_topology_state_sampling_has_fixed_cardinality(self):
        class FakePreparation:
            def diagnostic_state(self):
                return {
                    "stage": "source",
                    "claimed": False,
                    "done": False,
                    "discarded": False,
                    "quiesced": False,
                }

        preparations = {
            FakePreparation()
            for _ in range(_DIAGNOSTIC_PREPARATION_LIMIT + 5)
        }
        with _PROACTIVE_PREPARATIONS_LOCK:
            _PROACTIVE_PREPARATIONS.update(preparations)
        try:
            topology = capture_preparation_topology()
            states = topology["proactive_preparation_states"]
            self.assertEqual(
                topology["proactive_preparation_snapshot"], "truncated"
            )
            self.assertEqual(
                states["sampled_count"], _DIAGNOSTIC_PREPARATION_LIMIT
            )
            self.assertEqual(
                sum(states["stage_counts"].values()),
                _DIAGNOSTIC_PREPARATION_LIMIT,
            )
        finally:
            with _PROACTIVE_PREPARATIONS_LOCK:
                _PROACTIVE_PREPARATIONS.difference_update(preparations)

        class FakeCleanup:
            def diagnostic_kind(self):
                return "candidate"

            def age_seconds(self, _now):
                return 1.0

        cleanup_entries = {
            ("test-cardinality", index): FakeCleanup()
            for index in range(_DIAGNOSTIC_CLEANUP_LIMIT + 5)
        }
        with _TERMINAL_CLEANUPS_LOCK:
            _TERMINAL_CLEANUPS.update(cleanup_entries)
        try:
            topology = capture_preparation_topology()
            self.assertEqual(
                topology["terminal_cleanup_snapshot"], "truncated"
            )
            self.assertEqual(
                topology["terminal_cleanup_sampled_count"],
                _DIAGNOSTIC_CLEANUP_LIMIT,
            )
            self.assertEqual(
                sum(
                    state["count"]
                    for state in topology["terminal_cleanup_states"].values()
                ),
                _DIAGNOSTIC_CLEANUP_LIMIT,
            )
        finally:
            with _TERMINAL_CLEANUPS_LOCK:
                for key in cleanup_entries:
                    _TERMINAL_CLEANUPS.pop(key, None)

    def test_topology_counts_ready_and_claimed_preparation_without_ids(self):
        source = "https://camera.invalid/live?token=claim-secret"
        capture = PositionedScriptedCapture(["frame"], [0.0])
        preparation = _AsyncCapturePreparation(
            source_factory=lambda: source,
            clock_source_factory=None,
            capture_factory=lambda _source: capture,
            media_clock_factory=None,
            media_clock_validator=None,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_frame_identity=lambda frame: frame,
            stop_event=threading.Event(),
            wall_time=time.time,
            monotonic=time.monotonic,
            reserve_decoder_slot=True,
        )
        result = None
        try:
            self.assertTrue(self.wait_until(lambda: preparation.poll()[0]))
            ready = capture_preparation_topology()
            ready_states = ready["proactive_preparation_states"]
            self.assertEqual(ready_states["stage_counts"], {"first_frame": 1})
            self.assertEqual(ready_states["done_count"], 1)
            self.assertEqual(ready_states["claimed_count"], 0)
            self.assertEqual(ready_states["claimed_lock_busy_count"], 0)

            result = preparation.take()
            claimed = capture_preparation_topology()
            self.assertEqual(
                claimed["proactive_preparation_states"]["claimed_count"], 1
            )
            encoded = json.dumps(claimed, sort_keys=True)
            self.assertNotIn("claim-secret", encoded)
            self.assertNotIn(str(id(preparation)), encoded)
        finally:
            preparation.adopt()
            if result is not None:
                result[0].release()

    def test_terminal_cleanup_kind_age_and_identifier_are_bounded(self):
        entered = threading.Event()
        release = threading.Event()
        secret_key = ("https://cleanup.invalid?token=secret", 987654321)

        def blocked_cleanup():
            entered.set()
            release.wait(1.0)

        cleanup = _start_terminal_cleanup(secret_key, blocked_cleanup)
        try:
            self.assertTrue(entered.wait(1.0))
            cleanup._started_monotonic -= 2.0
            topology = capture_preparation_topology()
            state = topology["terminal_cleanup_states"]["other"]
            self.assertEqual(state["count"], 1)
            self.assertGreaterEqual(state["oldest_age_seconds"], 2.0)
            encoded = json.dumps(topology, sort_keys=True)
            self.assertNotIn("cleanup.invalid", encoded)
            self.assertNotIn("987654321", encoded)
        finally:
            release.set()
            self.assertTrue(cleanup.wait(1.0))

    def test_promoted_reader_cleanup_preserves_original_age(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_cleanup():
            entered.set()
            release.wait(1.0)

        owned = _start_reader_owned_cleanup(
            ("owned", 123456789), blocked_cleanup
        )
        promoted = None
        try:
            self.assertTrue(entered.wait(1.0))
            owned._started_monotonic -= 2.0
            promoted = _promote_reader_owned_cleanup(owned)
            state = capture_preparation_topology()[
                "terminal_cleanup_states"
            ]["reader-owned"]
            self.assertEqual(state["count"], 1)
            self.assertGreaterEqual(state["oldest_age_seconds"], 2.0)
        finally:
            release.set()
            self.assertTrue(owned.wait(1.0))
            if promoted is not None:
                self.assertTrue(promoted.wait(1.0))

    def test_preparation_prefers_same_session_pts_without_clock_session(self):
        capture = TransportClockCapture(["static-frame"], [2204.0])
        clock_source_calls = []
        fallback_calls = []
        preparation = _AsyncCapturePreparation(
            source_factory=lambda: "capture-source",
            clock_source_factory=lambda: clock_source_calls.append(True),
            capture_factory=lambda _source: capture,
            media_clock_factory=lambda *_args, **_kwargs: (
                fallback_calls.append(True)
            ),
            media_clock_validator=lambda *_args: True,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_frame_identity=lambda frame: frame,
            stop_event=threading.Event(),
            wall_time=time.time,
            monotonic=time.monotonic,
            serialize_preparation=False,
        )
        try:
            self.assertTrue(self.wait_until(lambda: preparation.poll()[0]))
            result = preparation.take()
            self.assertIsNotNone(result)
            self.assertIs(result[5], capture.clock)
            self.assertEqual(result[7], "capture-source")
            self.assertEqual(preparation.evidence(), "exact_same_session_pts")
            self.assertEqual(clock_source_calls, [])
            self.assertEqual(fallback_calls, [])
            result[0].release()
        finally:
            preparation.discard()

    def test_active_reader_publishes_same_session_pts_without_pixel_match(self):
        next_frame_gate = threading.Event()
        capture = TransportClockCapture(
            ["identical", "identical"], [2204.0, 2237.333], next_frame_gate
        )
        clock_source_calls = []
        fallback_calls = []
        states = []
        reader = LiveStreamReader(
            source_factory=lambda: "capture-source",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            media_clock_factory=lambda *_args, **_kwargs: (
                fallback_calls.append(True)
            ),
            media_clock_source_factory=lambda: clock_source_calls.append(True),
            media_clock_validator=lambda *_args: True,
            capture_position_milliseconds=lambda cap: cap.get(0),
            duplicate_frame_limit=10,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            self.assertIsNotNone(first["media_clock"])
            next_frame_gate.set()
            second = reader.wait_for_frame(first["sequence"], timeout=1.0)
            self.assertIsNotNone(second)
            self.assertEqual(second["frame"], "identical")
            self.assertGreater(second["sequence"], first["sequence"])
            self.assertEqual(clock_source_calls, [])
            self.assertEqual(fallback_calls, [])
            self.assertTrue(any(
                event["state"] == "transport_diagnostic"
                and event["stage"] == "matched"
                for event in states
            ))
        finally:
            reader.stop(timeout=2.0)

    def test_missing_current_transport_pts_immediately_starts_fallback(self):
        class LosingTransportCapture(TransportClockCapture):
            def transport_media_clock(self):
                if self.current_position == 2204.0:
                    return self.clock
                return None

        next_frame_gate = threading.Event()
        capture = LosingTransportCapture(
            ["identical", "identical"],
            [2204.0, 2237.333],
            next_frame_gate,
        )
        fallback_started = threading.Event()
        clock_sources = []

        def fallback(*_args, **_kwargs):
            fallback_started.set()
            return FakeMediaClock()

        reader = LiveStreamReader(
            source_factory=lambda: "capture-source",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=fallback,
            media_clock_source_factory=lambda: (
                clock_sources.append("clock-fallback") or "clock-fallback"
            ),
            media_clock_validator=lambda *_args: True,
            capture_position_milliseconds=lambda cap: cap.get(0),
            duplicate_frame_limit=10,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            next_frame_gate.set()
            self.assertTrue(fallback_started.wait(1.0))
            second = reader.wait_for_frame(first["sequence"], timeout=1.0)
            self.assertIsNotNone(second)
            self.assertEqual(clock_sources, ["clock-fallback"])
        finally:
            reader.stop(timeout=2.0)

    def test_static_transport_loss_hands_over_to_fresh_trusted_session(self):
        class PersistentLosingCapture:
            def __init__(self):
                self.count = 0
                self.position = None
                self.released = False
                self.clock = FakeTransportMediaClock()

            def isOpened(self):
                return not self.released

            def read(self):
                if self.released:
                    return False, None
                time.sleep(0.002)
                self.count += 1
                self.position = 2204.0 + (self.count - 1) * 33.333
                return True, "identical-static-frame"

            def get(self, _property):
                return self.position

            def transport_media_clock(self):
                return self.clock if self.count == 1 else None

            def release(self):
                self.released = True

        class ContinuousTransportCapture:
            def __init__(self):
                self.count = 0
                self.position = None
                self.released = False
                self.clock = FakeTransportMediaClock()

            def isOpened(self):
                return not self.released

            def read(self):
                if self.released:
                    return False, None
                time.sleep(0.002)
                self.count += 1
                self.position = 5000.0 + self.count * 33.333
                return True, f"replacement-{self.count}"

            def get(self, _property):
                return self.position

            def transport_media_clock(self):
                return self.clock

            def release(self):
                self.released = True

        primary = PersistentLosingCapture()
        replacement = ContinuousTransportCapture()
        source_calls = []
        fallback_calls = []

        def source_factory():
            source = f"capture-session-{len(source_calls) + 1}"
            source_calls.append(source)
            return source

        def capture_factory(source):
            return primary if source.endswith("-1") else replacement

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=lambda *_args, **_kwargs: (
                fallback_calls.append(True) and None
            ),
            media_clock_source_factory=lambda: "fallback-clock-session",
            media_clock_validator=lambda *_args: True,
            capture_position_milliseconds=lambda cap: cap.get(0),
            duplicate_frame_limit=10,
            connection_max_age_seconds=300.0,
            connection_renewal_lead_seconds=15.0,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            replacement_frame = reader.wait_for_frame(
                first["sequence"], timeout=2.0
            )
            self.assertIsNotNone(replacement_frame)
            self.assertTrue(
                str(replacement_frame["frame"]).startswith("replacement-")
            )
            self.assertGreaterEqual(len(source_calls), 2)
            self.assertTrue(fallback_calls)
            self.assertTrue(primary.released)
            self.assertIsNotNone(replacement_frame["media_clock"])
        finally:
            reader.stop(timeout=2.0)
        self.assertTrue(self.wait_until(
            lambda: (
                capture_preparation_topology()["proactive_preparations"] == 0
                and capture_preparation_topology()["terminal_cleanups"] == 0
            )
        ))

    def test_discarded_media_clock_resolution_cancels_and_quiesces(self):
        entered = threading.Event()
        exited = threading.Event()

        def resolve(_source, cancel_event=None):
            entered.set()
            cancel_event.wait(1.0)
            exited.set()
            return None

        resolution = _AsyncMediaClockResolution(resolve, ("signed-source",))
        self.assertTrue(entered.wait(1.0))
        resolution.discard()
        resolution.join(1.0)
        self.assertTrue(exited.is_set())
        self.assertFalse(resolution.is_alive())

    def test_proactive_decoder_lease_is_held_until_atomic_adoption(self):
        capture = PositionedScriptedCapture(["frame"], [0.0])
        preparation = _AsyncCapturePreparation(
            source_factory=lambda: "capture-source",
            clock_source_factory=None,
            capture_factory=lambda _source: capture,
            media_clock_factory=None,
            media_clock_validator=None,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_frame_identity=lambda frame: frame,
            stop_event=threading.Event(),
            wall_time=time.time,
            monotonic=time.monotonic,
            reserve_decoder_slot=True,
        )
        self.assertTrue(self.wait_until(lambda: preparation.poll()[0]))
        self.assertEqual(
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"], 1
        )
        result = preparation.take()
        self.assertIsNotNone(result)
        self.assertEqual(
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"], 1
        )
        lock = threading.Lock()
        active_matches = 0
        maximum_matches = 0

        def matcher(value):
            nonlocal active_matches, maximum_matches
            with lock:
                active_matches += 1
                maximum_matches = max(maximum_matches, active_matches)
            try:
                time.sleep(0.03)
                return value
            finally:
                with lock:
                    active_matches -= 1

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(
                _run_nvdec_fragment_match,
                matcher,
                (value,),
                {},
            ) for value in range(3)]
            self.assertEqual(
                [future.result(timeout=1.0) for future in futures],
                [0, 1, 2],
            )
        self.assertEqual(maximum_matches, 1)
        preparation.adopt()
        self.assertEqual(
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"], 0
        )
        result[0].release()

    def test_done_publication_cannot_drop_claimed_handover_lease(self):
        read_entered = threading.Event()
        allow_read = threading.Event()
        done_published = threading.Event()
        allow_done_return = threading.Event()
        coordination_failed = threading.Event()

        class BlockingCapture(PositionedScriptedCapture):
            def read(self):
                read_entered.set()
                if not allow_read.wait(2.0):
                    coordination_failed.set()
                return super().read()

        class GateEvent:
            def __init__(self):
                self._event = threading.Event()

            def set(self):
                self._event.set()
                done_published.set()
                if not allow_done_return.wait(2.0):
                    coordination_failed.set()

            def is_set(self):
                return self._event.is_set()

            def wait(self, timeout=None):
                return self._event.wait(timeout)

        capture = BlockingCapture(["frame"], [0.0])
        preparation = _AsyncCapturePreparation(
            source_factory=lambda: "capture-source",
            clock_source_factory=None,
            capture_factory=lambda _source: capture,
            media_clock_factory=None,
            media_clock_validator=None,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_frame_identity=lambda frame: frame,
            stop_event=threading.Event(),
            wall_time=time.time,
            monotonic=time.monotonic,
            reserve_decoder_slot=True,
        )
        self.assertTrue(read_entered.wait(1.0))
        preparation._done = GateEvent()
        allow_read.set()
        self.assertTrue(done_published.wait(1.0))
        consumed = threading.Event()
        outcome = {}

        def consume():
            outcome["poll"] = preparation.poll()
            outcome["result"] = preparation.take()
            consumed.set()

        consumer = threading.Thread(target=consume)
        consumer.start()
        self.assertFalse(consumed.wait(0.02))
        allow_done_return.set()
        consumer.join(1.0)
        preparation.join(1.0)

        self.assertTrue(consumed.is_set())
        self.assertFalse(preparation.is_alive())
        self.assertFalse(coordination_failed.is_set())
        done, polled_result, failed = outcome["poll"]
        self.assertTrue(done)
        self.assertFalse(failed)
        self.assertIsNotNone(polled_result)
        result = outcome["result"]
        self.assertIsNotNone(result)
        self.assertEqual(
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"], 1
        )
        self.assertEqual(
            capture_preparation_topology()["proactive_preparations"], 1
        )
        preparation.adopt()
        self.assertEqual(
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"], 0
        )
        result[0].release()

    def test_discard_during_done_publication_cannot_leak_lease(self):
        read_entered = threading.Event()
        allow_read = threading.Event()
        done_published = threading.Event()
        allow_done_return = threading.Event()
        coordination_failed = threading.Event()

        class BlockingCapture(PositionedScriptedCapture):
            def read(self):
                read_entered.set()
                if not allow_read.wait(2.0):
                    coordination_failed.set()
                return super().read()

        class GateEvent:
            def __init__(self):
                self._event = threading.Event()

            def set(self):
                self._event.set()
                done_published.set()
                if not allow_done_return.wait(2.0):
                    coordination_failed.set()

            def is_set(self):
                return self._event.is_set()

            def wait(self, timeout=None):
                return self._event.wait(timeout)

        capture = BlockingCapture(["frame"], [0.0])
        preparation = _AsyncCapturePreparation(
            source_factory=lambda: "capture-source",
            clock_source_factory=None,
            capture_factory=lambda _source: capture,
            media_clock_factory=None,
            media_clock_validator=None,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_frame_identity=lambda frame: frame,
            stop_event=threading.Event(),
            wall_time=time.time,
            monotonic=time.monotonic,
            reserve_decoder_slot=True,
        )
        self.assertTrue(read_entered.wait(1.0))
        preparation._done = GateEvent()
        allow_read.set()
        self.assertTrue(done_published.wait(1.0))
        discard = threading.Thread(target=preparation.discard)
        discard.start()
        self.assertTrue(discard.is_alive())
        allow_done_return.set()
        discard.join(1.0)
        preparation.join(1.0)

        self.assertFalse(discard.is_alive())
        self.assertFalse(preparation.is_alive())
        self.assertFalse(coordination_failed.is_set())
        self.assertTrue(capture.released)
        self.assertTrue(self.wait_until(lambda: (
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"] == 0
            and capture_preparation_topology()["proactive_preparations"] == 0
        )))

    def test_global_cancel_cannot_release_a_claimed_handover_lease(self):
        capture = PositionedScriptedCapture(["frame"], [0.0])
        preparation = _AsyncCapturePreparation(
            source_factory=lambda: "capture-source",
            clock_source_factory=None,
            capture_factory=lambda _source: capture,
            media_clock_factory=None,
            media_clock_validator=None,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_frame_identity=lambda frame: frame,
            stop_event=threading.Event(),
            wall_time=time.time,
            monotonic=time.monotonic,
            reserve_decoder_slot=True,
        )
        self.assertTrue(self.wait_until(lambda: preparation.poll()[0]))
        result = preparation.take()
        self.assertIsNotNone(result)

        self.assertFalse(_cancel_proactive_preparations(timeout=0.02))
        self.assertFalse(capture.released)
        self.assertEqual(
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"], 1
        )

        preparation.adopt()
        self.assertEqual(
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"], 0
        )
        result[0].release()

    def test_claimed_handover_quiesces_after_old_writer_termination(self):
        old_release_entered = threading.Event()
        old_writer_dead = threading.Event()
        capture = PositionedScriptedCapture(["frame"], [0.0])
        preparation = _AsyncCapturePreparation(
            source_factory=lambda: "capture-source",
            clock_source_factory=None,
            capture_factory=lambda _source: capture,
            media_clock_factory=None,
            media_clock_validator=None,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_frame_identity=lambda frame: frame,
            stop_event=threading.Event(),
            wall_time=time.time,
            monotonic=time.monotonic,
            reserve_decoder_slot=True,
        )
        self.assertTrue(self.wait_until(lambda: preparation.poll()[0]))
        result = preparation.take()
        self.assertIsNotNone(result)

        class OldCapture:
            def release(self):
                old_release_entered.set()
                self_test.assertTrue(old_writer_dead.wait(1.0))

        self_test = self

        def handover():
            OldCapture().release()
            preparation.adopt()

        owner = threading.Thread(target=handover)
        owner.start()
        self.assertTrue(old_release_entered.wait(1.0))
        self.assertFalse(_cancel_proactive_preparations(timeout=0.02))
        self.assertEqual(
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"], 1
        )
        self.assertEqual(
            capture_preparation_topology()["proactive_preparations"], 1
        )

        old_writer_dead.set()
        owner.join(1.0)
        self.assertFalse(owner.is_alive())
        self.assertTrue(self.wait_until(lambda: (
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"] == 0
            and capture_preparation_topology()["proactive_preparations"] == 0
            and capture_preparation_topology()["terminal_cleanups"] == 0
        )))
        result[0].release()

    def test_terminal_window_rejects_new_proactive_registration(self):
        capture_factory_called = threading.Event()
        terminal_window = _begin_terminal_recovery()
        try:
            preparation = _AsyncCapturePreparation(
                source_factory=lambda: "capture-source",
                clock_source_factory=None,
                capture_factory=lambda _source: (
                    capture_factory_called.set()
                    or PositionedScriptedCapture(["frame"], [0.0])
                ),
                media_clock_factory=None,
                media_clock_validator=None,
                capture_position_milliseconds=lambda cap: cap.get(0),
                media_frame_identity=lambda frame: frame,
                stop_event=threading.Event(),
                wall_time=time.time,
                monotonic=time.monotonic,
                reserve_decoder_slot=True,
            )
            preparation.join(1.0)
            self.assertFalse(preparation.is_alive())
            self.assertTrue(preparation.poll()[2])
            self.assertFalse(capture_factory_called.is_set())
            self.assertTrue(preparation.is_quiesced())
            self.assertEqual(
                AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"], 0
            )
        finally:
            terminal_window.release()

    def test_discarded_ready_preparation_closes_capture_and_releases_lease(self):
        capture = PositionedScriptedCapture(["frame"], [0.0])
        preparation = _AsyncCapturePreparation(
            source_factory=lambda: "capture-source",
            clock_source_factory=None,
            capture_factory=lambda _source: capture,
            media_clock_factory=None,
            media_clock_validator=None,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_frame_identity=lambda frame: frame,
            stop_event=threading.Event(),
            wall_time=time.time,
            monotonic=time.monotonic,
            reserve_decoder_slot=True,
        )
        self.assertTrue(self.wait_until(lambda: preparation.poll()[0]))
        preparation.discard()
        self.assertTrue(capture.released)
        self.assertEqual(
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"], 0
        )

    def test_global_terminal_cancel_quiesces_proactive_clock_resolver(self):
        resolver_entered = threading.Event()
        resolver_exited = threading.Event()

        def resolve_clock(_source, *_args, cancel_event=None):
            resolver_entered.set()
            cancel_event.wait(1.0)
            resolver_exited.set()
            return None

        preparation = _AsyncCapturePreparation(
            source_factory=lambda: "capture-source",
            clock_source_factory=lambda: "clock-source",
            capture_factory=lambda _source: ContinuousCapture("prepared"),
            media_clock_factory=resolve_clock,
            media_clock_validator=lambda *_args: True,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_frame_identity=lambda frame: frame,
            stop_event=threading.Event(),
            wall_time=time.time,
            monotonic=time.monotonic,
            reserve_decoder_slot=True,
        )
        self.assertTrue(resolver_entered.wait(1.0))
        self.assertTrue(_cancel_proactive_preparations(timeout=1.0))
        self.assertTrue(resolver_exited.is_set())
        self.assertFalse(preparation.is_alive())
        self.assertEqual(
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"], 0
        )

    def test_proactive_clock_retry_uses_latest_three_frame_sequence(self):
        calls = []
        clock_sources = []
        monotonic_value = [0.0]

        def monotonic():
            monotonic_value[0] += 0.5
            return monotonic_value[0]

        def clock_source_factory():
            source = f"clock-source-{len(clock_sources) + 1}"
            clock_sources.append(source)
            return source

        def resolve_clock(source, _frame, _position, _identity, **kwargs):
            calls.append((source, kwargs.get("reference_sequence")))
            return None if len(calls) == 1 else FakeMediaClock()

        preparation = _AsyncCapturePreparation(
            source_factory=lambda: "capture-source",
            clock_source_factory=clock_source_factory,
            capture_factory=lambda _source: ContinuousCapture("prepared"),
            media_clock_factory=resolve_clock,
            media_clock_validator=lambda *_args: True,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_frame_identity=lambda frame: frame,
            stop_event=threading.Event(),
            wall_time=time.time,
            monotonic=monotonic,
            serialize_preparation=False,
        )
        try:
            self.assertTrue(self.wait_until(lambda: preparation.poll()[0]))
            result = preparation.take()
            self.assertIsNotNone(result)
            self.assertEqual(calls[0], ("clock-source-1", None))
            self.assertEqual(calls[1][0], "clock-source-2")
            self.assertEqual(len(calls[1][1]), 3)
            positions = tuple(
                position for _frame, position in calls[1][1]
            )
            self.assertEqual(positions, tuple(sorted(positions)))
            self.assertGreater(positions[-1], positions[0])
            self.assertEqual(result[7], "clock-source-2")
            result[0].release()
        finally:
            preparation.discard()

    def test_proactive_handover_quarantines_active_clock_resolver(self):
        active_entered = threading.Event()
        release_active = threading.Event()
        states = []
        source_calls = []
        clock_source_calls = []

        def source_factory():
            source = f"capture-source-{len(source_calls) + 1}"
            source_calls.append(source)
            return source

        def clock_source_factory():
            source = f"clock-source-{len(clock_source_calls) + 1}"
            clock_source_calls.append(source)
            return source

        def resolve_clock(source, *_args, cancel_event=None, **_kwargs):
            if source == "clock-source-1":
                active_entered.set()
                release_active.wait(2.0)
                return None
            return FakeMediaClock()

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=lambda source: ContinuousCapture(source),
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            media_clock_factory=resolve_clock,
            media_clock_source_factory=clock_source_factory,
            media_clock_validator=lambda *_args: True,
            capture_position_milliseconds=lambda cap: cap.get(0),
            connection_max_age_seconds=0.15,
            connection_renewal_lead_seconds=0.05,
        )
        reader.start()
        try:
            self.assertTrue(active_entered.wait(1.0))
            self.assertTrue(self.wait_until(lambda: any(
                event["state"] == "renewed" for event in states
            )))
            self.assertGreaterEqual(
                capture_preparation_topology()["terminal_cleanups"], 1
            )
            reader.request_stop()
            reader.join(0.05)
            self.assertTrue(reader.is_alive())
        finally:
            release_active.set()
            reader.join(2.0)
        self.assertFalse(reader.is_alive())
        self.assertTrue(self.wait_until(lambda: (
            capture_preparation_topology()["terminal_cleanups"] == 0
        )))

    def test_stop_during_failed_read_skips_terminal_recovery(self):
        read_entered = threading.Event()

        class BlockingFailedCapture:
            def __init__(self):
                self.released = False
                self.cancel_event = None

            def isOpened(self):
                return not self.released

            def read(self):
                read_entered.set()
                self_test.assertIsNotNone(self.cancel_event)
                self_test.assertTrue(self.cancel_event.wait(1.0))
                return False, None

            def release(self):
                self.released = True

        capture = BlockingFailedCapture()
        self_test = self
        cancel_events = []

        def capture_factory(_source, cancel_event=None):
            cancel_events.append(cancel_event)
            capture.cancel_event = cancel_event
            return capture

        reader = LiveStreamReader(
            source_factory=lambda: "capture-source",
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            terminal_read_failover_seconds=0.5,
        )
        terminal_calls = []
        reader._recover_terminal_read = lambda *_args, **_kwargs: (
            terminal_calls.append(True)
        )
        reader.start()
        self.assertTrue(read_entered.wait(1.0))
        deadline = time.monotonic() + 0.5
        reader.request_stop(deadline=deadline)
        reader.join(1.0)

        self.assertFalse(reader.is_alive())
        self.assertEqual(terminal_calls, [])
        self.assertEqual(cancel_events, [reader._stop_event])
        self.assertTrue(capture.released)
        self.assertEqual(
            capture_preparation_topology()["terminal_cleanups"], 0
        )

    def test_process_sigterm_reaps_blocked_fifo_writer_without_sigkill(self):
        script = r'''
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, sys.argv[1])
from ffmpeg_capture import FfmpegNvdecCapture
from live_capture import LiveStreamReader
from runtime_health import StreamRecovery

created = threading.Event()
shutdown = threading.Event()

class BlockingFifoCapture:
    def __init__(self, process):
        self.process = process
        self.released = False

    def isOpened(self):
        return not self.released

    def read(self):
        while self.process.poll() is None:
            time.sleep(0.01)
        return False, None

    def release(self):
        if self.process.poll() is None:
            raise RuntimeError("writer still alive at OpenCV release")
        self.released = True

def capture_factory(_source, cancel_event=None):
    process = subprocess.Popen(
        ["/bin/sleep", "60"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    capture = object.__new__(FfmpegNvdecCapture)
    capture._capture = BlockingFifoCapture(process)
    capture._process = process
    capture._test_pid = process.pid
    capture._memfd = None
    capture._mediator = None
    capture._pts_sidecar = None
    capture._pts_write_fd = None
    capture._temporary_directory = None
    capture._opened = True
    capture._cancel_event = cancel_event
    capture._cancel_watcher_stop = threading.Event()
    capture._process_lock = threading.RLock()
    capture._release_lock = threading.Lock()
    capture._last_source_position_ms = None
    capture._last_transport_exact = False
    capture._cancel_watcher = threading.Thread(
        target=capture._watch_for_cancel,
        daemon=True,
    )
    capture._cancel_watcher.start()
    print(f"READY {process.pid}", flush=True)
    created.set()
    return capture

reader = LiveStreamReader(
    source_factory=lambda: "local-fifo",
    capture_factory=capture_factory,
    recovery=StreamRecovery(0.1, 0.1),
    terminal_read_failover_seconds=0.5,
)

def stop(_signum, _frame):
    shutdown.set()
    reader.request_stop(deadline=time.monotonic() + 2.0)

signal.signal(signal.SIGTERM, stop)
reader.start()
if not created.wait(2.0):
    raise SystemExit(3)
while not shutdown.wait(0.05):
    pass
reader.join(2.5)
if reader.is_alive():
    raise SystemExit(4)
print("DONE", flush=True)
'''
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(PERCEPTION_DIR)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], 3.0)
            self.assertTrue(ready, "signal harness did not become ready")
            line = process.stdout.readline().strip()
            self.assertRegex(line, r"^READY [0-9]+$")
            child_pid = int(line.split()[1])
            process.terminate()
            stdout, stderr = process.communicate(timeout=5.0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=2.0)

        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("DONE", stdout)
        self.assertFalse(os.path.exists(f"/proc/{child_pid}"))

    def test_process_sigterm_during_claimed_handover_reaches_zero_topology(self):
        script = r'''
import atexit
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, sys.argv[1])
from decoder_admission import AUXILIARY_DECODER_ADMISSION
from ffmpeg_capture import FfmpegNvdecCapture
from live_capture import (
    LiveStreamReader,
    _cancel_proactive_preparations,
    capture_preparation_topology,
    wait_for_terminal_cleanups,
)
from runtime_health import StreamRecovery

handover_blocked = threading.Event()
release_old_capture = threading.Event()
old_release_finished = threading.Event()
replacement_release_entered = threading.Event()
release_replacement_capture = threading.Event()
replacement_release_finished = threading.Event()
shutdown = threading.Event()
captures = []

def cleanup_owned_children():
    release_old_capture.set()
    release_replacement_capture.set()
    for capture in tuple(captures):
        try:
            capture.release()
        except Exception:
            process = getattr(capture, "_test_process", None)
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass

atexit.register(cleanup_owned_children)

class SyntheticOpenCvCapture:
    def __init__(self, process, block_release):
        self.process = process
        self.block_release = block_release
        self.released = False
        self.position = 0.0

    def isOpened(self):
        return not self.released

    def read(self):
        if self.process.poll() is not None:
            return False, None
        self.position += 50.0
        time.sleep(0.002)
        return True, f"frame-{self.process.pid}-{self.position}"

    def get(self, _property):
        return self.position

    def release(self):
        if self.process.poll() is None:
            raise RuntimeError("writer still alive at OpenCV release")
        if self.block_release:
            handover_blocked.set()
            if not release_old_capture.wait(8.0):
                raise RuntimeError("handover release gate timed out")
            old_release_finished.set()
        else:
            replacement_release_entered.set()
            if not release_replacement_capture.wait(8.0):
                raise RuntimeError("replacement release gate timed out")
            replacement_release_finished.set()
        self.released = True

def capture_factory(_source, cancel_event=None):
    process = subprocess.Popen(
        ["/bin/sleep", "60"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    capture = object.__new__(FfmpegNvdecCapture)
    capture._capture = SyntheticOpenCvCapture(
        process, block_release=(len(captures) == 0)
    )
    capture._process = process
    capture._test_pid = process.pid
    capture._test_process = process
    capture._memfd = None
    capture._mediator = None
    capture._pts_sidecar = None
    capture._pts_write_fd = None
    capture._temporary_directory = None
    capture._opened = True
    capture._cancel_event = cancel_event
    capture._cancel_watcher_stop = threading.Event()
    capture._process_lock = threading.RLock()
    capture._release_lock = threading.Lock()
    capture._source_pts_timeout_seconds = 0.01
    capture._last_source_position_ms = None
    capture._last_transport_exact = False
    capture._transport_diagnostic = "starting"
    capture._last_emitted_source_pts = None
    capture._consecutive_overlap_frames = 0
    capture._overlap_frames_dropped = 0
    capture._cancel_watcher = threading.Thread(
        target=capture._watch_for_cancel,
        daemon=True,
    )
    capture._cancel_watcher.start()
    captures.append(capture)
    return capture

reader = LiveStreamReader(
    source_factory=lambda: "local-fifo",
    capture_factory=capture_factory,
    recovery=StreamRecovery(0.1, 0.1),
    frame_identity=lambda frame: frame,
    connection_max_age_seconds=0.08,
    connection_renewal_lead_seconds=0.02,
    terminal_read_failover_seconds=0.5,
    reserve_proactive_decoder_slot=True,
)

def stop(_signum, _frame):
    shutdown.set()
    reader.request_stop(deadline=time.monotonic() + 2.5)

signal.signal(signal.SIGTERM, stop)
reader.start()
if not handover_blocked.wait(8.0):
    raise SystemExit(3)
if len(captures) != 2:
    raise SystemExit(4)
children = [capture._test_pid for capture in captures]
print("READY " + " ".join(str(pid) for pid in children), flush=True)
while not shutdown.wait(0.05):
    pass

cleanup_result = []
cleanup = threading.Thread(
    target=lambda: cleanup_result.append(
        _cancel_proactive_preparations(timeout=2.0)
    )
)
cleanup.start()
deadline = time.monotonic() + 1.0
while (
    capture_preparation_topology()["terminal_cleanups"] < 1
    and time.monotonic() < deadline
):
    time.sleep(0.01)
pre = capture_preparation_topology()
admission_pre = AUXILIARY_DECODER_ADMISSION.snapshot()
if pre["proactive_preparations"] != 1 or admission_pre["in_use"] != 1:
    raise SystemExit(5)
if not replacement_release_entered.wait(1.0):
    raise SystemExit(9)
if old_release_finished.is_set():
    raise SystemExit(10)
if AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"] != 1:
    raise SystemExit(11)
release_replacement_capture.set()
if not replacement_release_finished.wait(1.0):
    raise SystemExit(12)
deadline = time.monotonic() + 1.0
while (
    AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"] != 0
    and time.monotonic() < deadline
):
    time.sleep(0.01)
if AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"] != 0:
    raise SystemExit(13)
if old_release_finished.is_set():
    raise SystemExit(14)
reader.join(3.0)
if reader.is_alive():
    raise SystemExit(15)
if wait_for_terminal_cleanups(0.0):
    raise SystemExit(16)
release_old_capture.set()
cleanup.join(3.0)
deadline = time.monotonic() + 1.0
while time.monotonic() < deadline:
    topology = capture_preparation_topology()
    admission = AUXILIARY_DECODER_ADMISSION.snapshot()
    if (
        topology["proactive_preparations"] == 0
        and topology["terminal_recoveries"] == 0
        and topology["terminal_cleanups"] == 0
        and topology["terminal_cleanup_failures"] == 0
        and admission["in_use"] == 0
    ):
        break
    time.sleep(0.01)
if cleanup.is_alive() or cleanup_result != [True]:
    raise SystemExit(6)
if any(os.path.exists(f"/proc/{pid}") for pid in children):
    raise SystemExit(7)
topology = capture_preparation_topology()
admission = AUXILIARY_DECODER_ADMISSION.snapshot()
if any((
    topology["proactive_preparations"],
    topology["terminal_recoveries"],
    topology["terminal_cleanups"],
    topology["terminal_cleanup_failures"],
    admission["in_use"],
)):
    raise SystemExit(8)
print("DONE topology=zero children=reaped", flush=True)
'''
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(PERCEPTION_DIR)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10.0)
            if not ready:
                process.kill()
                stdout, stderr = process.communicate(timeout=2.0)
                self.fail(
                    "claimed-handover harness was not ready: "
                    f"returncode={process.returncode} stdout={stdout!r} "
                    f"stderr={stderr!r}"
                )
            line = process.stdout.readline().strip()
            if not line:
                stdout, stderr = process.communicate(timeout=2.0)
                self.fail(
                    "claimed-handover harness exited before READY: "
                    f"returncode={process.returncode} stdout={stdout!r} "
                    f"stderr={stderr!r}"
                )
            self.assertRegex(line, r"^READY [0-9]+ [0-9]+$")
            child_pids = [int(value) for value in line.split()[1:]]
            process.terminate()
            stdout, stderr = process.communicate(timeout=10.0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=2.0)

        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("DONE topology=zero children=reaped", stdout)
        self.assertTrue(all(
            not os.path.exists(f"/proc/{pid}") for pid in child_pids
        ))

    def test_promoted_reader_cleanup_counts_one_underlying_failure(self):
        script = r'''
import sys

sys.path.insert(0, sys.argv[1])
from live_capture import (
    _promote_reader_owned_cleanup,
    _start_reader_owned_cleanup,
    capture_preparation_topology,
)

def fail_once():
    raise RuntimeError("synthetic reader-owned cleanup failure")

owned = _start_reader_owned_cleanup(("owned", 1), fail_once)
promoted = _promote_reader_owned_cleanup(owned)
if not promoted.wait(1.0):
    raise SystemExit(2)
topology = capture_preparation_topology()
if owned.succeeded() or not promoted.succeeded():
    raise SystemExit(3)
if topology["terminal_cleanups"] != 0:
    raise SystemExit(4)
if topology["terminal_cleanup_failures"] != 1:
    raise SystemExit(5)
print("DONE one-failure", flush=True)
'''
        process = subprocess.run(
            [sys.executable, "-c", script, str(PERCEPTION_DIR)],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("DONE one-failure", process.stdout)

    def test_terminal_recovery_waits_for_active_clock_cleanup(self):
        resolver_entered = threading.Event()
        release_resolver = threading.Event()
        states = []
        captures = []

        def capture_factory(_source):
            capture = PositionedScriptedCapture(["primary"], [0.0])
            captures.append(capture)
            return capture

        def resolve_clock(_source, *_args, cancel_event=None, **_kwargs):
            resolver_entered.set()
            # Deliberately model a transport that cannot observe cancellation
            # until its bounded request returns.
            release_resolver.wait(2.0)
            return None

        reader = LiveStreamReader(
            source_factory=lambda: "capture-signed-url",
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            media_clock_factory=resolve_clock,
            media_clock_source_factory=lambda: "clock-signed-url",
            capture_position_milliseconds=lambda cap: cap.get(0),
            terminal_read_failover_seconds=0.1,
        )
        reader.start()
        try:
            self.assertTrue(resolver_entered.wait(1.0))
            self.assertTrue(self.wait_until(lambda: any(
                event["state"] == "terminal_failover_failed"
                and event["stage"] == "active_clock_cleanup"
                for event in states
            )))
            self.assertEqual(len(captures), 1)
            self.assertGreaterEqual(
                capture_preparation_topology()["terminal_cleanups"], 1
            )
            reader.request_stop()
            reader.join(0.05)
            self.assertTrue(reader.is_alive())
        finally:
            release_resolver.set()
            reader.join(2.0)
        self.assertFalse(reader.is_alive())
        self.assertTrue(self.wait_until(lambda: (
            capture_preparation_topology()["terminal_cleanups"] == 0
        )))

    def test_failed_reader_release_is_inside_terminal_deadline(self):
        captures = []
        states = []
        terminal_capture_counts = []

        class SlowReleaseCapture(ScriptedCapture):
            def release(self):
                time.sleep(0.25)
                super().release()

        def capture_factory(_source):
            capture = (
                SlowReleaseCapture(["primary"])
                if not captures
                else ContinuousCapture("replacement")
            )
            capture.get = lambda _property: 0.0
            captures.append(capture)
            return capture

        def state_callback(**event):
            states.append(event)
            if event["state"] == "terminal_failover_failed":
                terminal_capture_counts.append(len(captures))

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=state_callback,
            media_clock_factory=lambda *_args: FakeMediaClock(),
            media_clock_validator=lambda *_args: True,
            capture_position_milliseconds=lambda cap: cap.get(0),
            terminal_read_failover_seconds=0.05,
        )
        reader.start()
        try:
            self.assertIsNotNone(reader.wait_for_frame(0, timeout=1.0))
            self.assertTrue(self.wait_until(lambda: any(
                event["state"] == "terminal_failover_failed"
                for event in states
            )), states)
            terminal = next(
                event for event in states
                if event["state"] == "terminal_failover_failed"
            )
            self.assertEqual(terminal["stage"], "old_capture_release")
            self.assertGreaterEqual(terminal["delay_seconds"], 0.05)
            self.assertLess(terminal["delay_seconds"], 0.15)
            self.assertEqual(terminal_capture_counts, [1])
        finally:
            reader.stop(timeout=2.0)

    def test_reader_does_not_reopen_until_quarantined_capture_is_dead(self):
        captures = []
        release_finished = threading.Event()
        replacement_opened = threading.Event()
        opened_before_release = []

        class SlowPrimary(ScriptedCapture):
            def release(self):
                time.sleep(0.15)
                super().release()
                release_finished.set()

        def capture_factory(_source):
            if not captures:
                capture = SlowPrimary(["primary"])
            else:
                opened_before_release.append(not release_finished.is_set())
                replacement_opened.set()
                capture = ContinuousCapture("replacement")
            capture.get = lambda _property: 0.0
            captures.append(capture)
            return capture

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.01, 0.01),
            media_clock_factory=lambda *_args: FakeMediaClock(),
            media_clock_validator=lambda *_args: True,
            capture_position_milliseconds=lambda cap: cap.get(0),
            terminal_read_failover_seconds=0.02,
        )
        reader.start()
        try:
            self.assertIsNotNone(reader.wait_for_frame(0, timeout=1.0))
            self.assertTrue(replacement_opened.wait(1.0))
            self.assertEqual(opened_before_release, [False])
        finally:
            reader.stop(timeout=2.0)

    def test_terminal_recovery_rejects_success_completed_after_deadline(self):
        events = []

        class LateCandidate:
            def __init__(self):
                self.discarded = False
                self.joined = False

            def poll(self):
                time.sleep(0.06)
                return True, ("late-result",), False

            def take(self):
                return ("late-result",)

            def stage(self):
                return "ready"

            def evidence(self):
                return "late"

            def discard(self):
                self.discarded = True

            def join(self, timeout=None):
                self.joined = True

        candidate = LateCandidate()
        reader = LiveStreamReader(
            source_factory=lambda: "unused",
            capture_factory=lambda _source: None,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: events.append(event),
            terminal_read_failover_seconds=0.05,
        )
        result = reader._recover_terminal_read(
            candidate, None, None, None, None,
            started=time.monotonic(),
        )

        self.assertIsNone(result)
        self.assertTrue(candidate.discarded)
        self.assertTrue(candidate.joined)
        terminal = next(
            event for event in events
            if event["state"] == "terminal_failover_failed"
        )
        self.assertTrue(terminal["stage"].startswith("deadline_exceeded:"))
        self.assertGreaterEqual(terminal["delay_seconds"], 0.05)

    def test_terminal_recovery_rejects_handover_that_crosses_deadline(self):
        capture = PositionedScriptedCapture(["frame"], [0.0])
        events = []

        class SlowTakeCandidate:
            def poll(self):
                return True, (capture,), False

            def take(self):
                time.sleep(0.06)
                return (capture,)

            def stage(self):
                return "ready"

            def evidence(self):
                return "slow-handover"

            def discard(self):
                capture.release()

            def join(self, timeout=None):
                return None

        reader = LiveStreamReader(
            source_factory=lambda: "unused",
            capture_factory=lambda _source: None,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: events.append(event),
            terminal_read_failover_seconds=0.05,
        )
        result = reader._recover_terminal_read(
            SlowTakeCandidate(), None, None, None, None,
            started=time.monotonic(),
        )

        self.assertIsNone(result)
        self.assertTrue(capture.released)
        terminal = next(
            event for event in events
            if event["state"] == "terminal_failover_failed"
        )
        self.assertEqual(
            terminal["stage"], "deadline_exceeded:handover"
        )

    def test_terminal_timeout_quiesces_candidate_before_failure_callback(self):
        callback_saw_joined = []

        class TimedOutCandidate:
            def __init__(self):
                self.discarded = False
                self.joined = False

            def poll(self):
                return False, None, False

            def stage(self):
                return "capture_open"

            def discard(self):
                self.discarded = True

            def join(self, timeout=None):
                self.joined = True

        candidate = TimedOutCandidate()

        def state_callback(**event):
            if event["state"] == "terminal_failover_failed":
                callback_saw_joined.append(candidate.joined)

        reader = LiveStreamReader(
            source_factory=lambda: "unused",
            capture_factory=lambda _source: None,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=state_callback,
            terminal_read_failover_seconds=0.02,
        )
        result = reader._recover_terminal_read(
            candidate, None, None, None, None,
            started=time.monotonic(),
        )

        self.assertIsNone(result)
        self.assertTrue(candidate.discarded)
        self.assertTrue(candidate.joined)
        self.assertEqual(callback_saw_joined, [True])

    def test_slow_terminal_cleanup_is_quarantined_inside_wall_bound(self):
        cleanup_started = threading.Event()
        cleanup_finished = threading.Event()

        class SlowCleanupCandidate:
            def poll(self):
                return False, None, False

            def stage(self):
                return "capture_open"

            def discard(self):
                cleanup_started.set()

            def join(self, timeout=None):
                time.sleep(0.25)
                cleanup_finished.set()

        reader = LiveStreamReader(
            source_factory=lambda: "unused",
            capture_factory=lambda _source: None,
            recovery=StreamRecovery(0.1, 0.1),
            terminal_read_failover_seconds=0.02,
        )
        started = time.monotonic()
        result = reader._recover_terminal_read(
            SlowCleanupCandidate(), None, None, None, None,
            started=started,
        )
        elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertTrue(cleanup_started.is_set())
        self.assertFalse(cleanup_finished.is_set())
        self.assertLess(elapsed, 0.10)
        self.assertEqual(
            capture_preparation_topology()["terminal_cleanups"], 1
        )
        self.assertEqual(len(reader._pending_terminal_cleanups), 1)
        self.assertTrue(reader._wait_for_terminal_cleanups())
        self.assertTrue(cleanup_finished.is_set())
        self.assertEqual(
            capture_preparation_topology()["terminal_cleanups"], 0
        )

    def test_slow_nested_resolver_is_tracked_until_it_quiesces(self):
        resolver_entered = threading.Event()
        resolver_exited = threading.Event()

        def resolve_clock(_source, *_args, cancel_event=None):
            resolver_entered.set()
            time.sleep(0.25)
            resolver_exited.set()
            return None

        preparation = _AsyncCapturePreparation(
            source_factory=lambda: "capture-source",
            clock_source_factory=lambda: "clock-source",
            capture_factory=lambda _source: ContinuousCapture("prepared"),
            media_clock_factory=resolve_clock,
            media_clock_validator=lambda *_args: True,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_frame_identity=lambda frame: frame,
            stop_event=threading.Event(),
            wall_time=time.time,
            monotonic=time.monotonic,
            reserve_decoder_slot=True,
        )
        self.assertTrue(resolver_entered.wait(1.0))
        reader = LiveStreamReader(
            source_factory=lambda: "unused",
            capture_factory=lambda _source: None,
            recovery=StreamRecovery(0.1, 0.1),
            terminal_read_failover_seconds=0.02,
        )
        started = time.monotonic()
        result = reader._recover_terminal_read(
            preparation, None, None, None, None,
            started=started,
        )
        elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertLess(elapsed, 0.10)
        self.assertFalse(resolver_exited.is_set())
        self.assertEqual(
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"], 1
        )
        topology = capture_preparation_topology()
        self.assertEqual(topology["proactive_preparations"], 1)
        self.assertEqual(topology["terminal_cleanups"], 1)

        self.assertTrue(resolver_exited.wait(1.0))
        self.assertTrue(self.wait_until(lambda: (
            AUXILIARY_DECODER_ADMISSION.snapshot()["in_use"] == 0
            and capture_preparation_topology()["proactive_preparations"] == 0
            and capture_preparation_topology()["terminal_cleanups"] == 0
        )))

    def test_simultaneous_terminal_restarts_stay_inside_decoder_budget(self):
        lock = threading.Lock()
        capture_active = 0
        fragment_active = 0
        maximum_total = 0
        primary_frames = 0
        replacement_captures = 0
        all_primaries_ready = threading.Event()
        all_replacements_ready = threading.Event()
        readers = []
        states = [[], [], [], []]

        def record_total():
            nonlocal maximum_total
            maximum_total = max(
                maximum_total,
                capture_active + fragment_active,
            )

        class CountedCapture:
            def __init__(self, label, primary):
                nonlocal capture_active, replacement_captures
                self.label = label
                self.primary = primary
                self.count = 0
                self.released = False
                with lock:
                    capture_active += 1
                    if not primary:
                        replacement_captures += 1
                        if replacement_captures == 4:
                            all_replacements_ready.set()
                    record_total()

            def isOpened(self):
                return not self.released

            def read(self):
                nonlocal primary_frames
                if self.released:
                    return False, None
                if self.primary and self.count >= 1:
                    all_primaries_ready.wait(1.0)
                    return False, None
                self.count += 1
                if self.primary:
                    with lock:
                        primary_frames += 1
                        if primary_frames == 4:
                            all_primaries_ready.set()
                return True, f"{self.label}-{self.count}"

            def get(self, _property):
                return float(self.count * 50)

            def release(self):
                nonlocal capture_active
                with lock:
                    if self.released:
                        return
                    self.released = True
                    capture_active -= 1

        def fragment_match(label):
            nonlocal fragment_active
            with lock:
                fragment_active += 1
                record_total()
            try:
                time.sleep(0.05)
                return label
            finally:
                with lock:
                    fragment_active -= 1

        def make_reader(index):
            captures = []

            def capture_factory(_source):
                capture = CountedCapture(
                    f"reader-{index}",
                    primary=not captures,
                )
                captures.append(capture)
                return capture

            def media_clock_factory(*_args, urgent=False, **_kwargs):
                if urgent:
                    self.assertTrue(all_replacements_ready.wait(1.0))
                    futures = [
                        _NVDEC_URGENT_FRAGMENT_MATCH_EXECUTOR.submit(
                            _run_nvdec_fragment_match,
                            fragment_match,
                            (f"{index}-{part}",),
                            {},
                            None,
                            True,
                        )
                        for part in range(2)
                    ]
                    for future in futures:
                        future.result(timeout=1.0)
                return FakeMediaClock()

            return LiveStreamReader(
                source_factory=lambda: f"signed-{index}",
                capture_factory=capture_factory,
                recovery=StreamRecovery(0.1, 0.1),
                state_callback=lambda **event: states[index].append(event),
                media_clock_factory=media_clock_factory,
                media_clock_validator=lambda *_args: True,
                capture_position_milliseconds=lambda cap: cap.get(0),
                terminal_read_failover_seconds=1.0,
            )

        readers = [make_reader(index) for index in range(4)]
        for reader in readers:
            reader.start()
        try:
            self.assertTrue(self.wait_until(lambda: all(any(
                event["state"] == "terminal_failover_succeeded"
                for event in reader_states
            ) for reader_states in states)))
            with lock:
                self.assertEqual(maximum_total, 6)
        finally:
            for reader in readers:
                reader.stop(timeout=2.0)
        with lock:
            self.assertEqual(capture_active, 0)
            self.assertEqual(fragment_active, 0)

    def test_timed_out_terminal_capture_open_receives_cancellation(self):
        captures = []
        cancelled = threading.Event()
        terminal_failed = threading.Event()

        def capture_factory(_source, cancel_event=None):
            if not captures:
                capture = ScriptedCapture(["primary"])
                capture.get = lambda _property: 0.0
            elif cancel_event is not None:
                cancel_event.wait(1.0)
                if cancel_event.is_set():
                    cancelled.set()
                capture = ScriptedCapture([])
            else:
                capture = ScriptedCapture([])
            captures.append(capture)
            return capture

        def state_callback(**event):
            if event["state"] == "terminal_failover_failed":
                terminal_failed.set()

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=capture_factory,
            recovery=StreamRecovery(1.0, 1.0),
            state_callback=state_callback,
            capture_position_milliseconds=lambda cap: cap.get(0),
            terminal_read_failover_seconds=0.1,
        )
        reader.start()
        try:
            self.assertIsNotNone(reader.wait_for_frame(0, timeout=1.0))
            self.assertTrue(terminal_failed.wait(1.0))
            self.assertTrue(cancelled.wait(1.0))
        finally:
            reader.stop(timeout=2.0)

    def test_reconnect_renews_source_and_delivers_a_new_sequence(self):
        source_calls = []
        capture_sources = []
        states = []

        def source_factory():
            source = f"signed-session-{len(source_calls) + 1}"
            source_calls.append(source)
            return source

        def capture_factory(source):
            capture_sources.append(source)
            return ScriptedCapture([f"frame-{len(capture_sources)}"])

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            terminal_read_failover_seconds=0.0,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            second = reader.wait_for_frame(first["sequence"], timeout=2.0)
            self.assertIsNotNone(second)
            self.assertGreater(second["sequence"], first["sequence"])
            self.assertGreaterEqual(len(source_calls), 2)
            self.assertNotEqual(capture_sources[0], capture_sources[1])
            self.assertTrue(any(event["state"] == "reconnecting" for event in states))
        finally:
            reader.stop(timeout=2.0)

    def test_terminal_read_hot_failover_keeps_streaming_state_and_clock(self):
        captures = []
        clock_sources = []
        clock_sequences = []
        states = []

        def source_factory():
            return f"signed-session-{len(captures) + 1}"

        def capture_factory(source):
            if not captures:
                capture = ScriptedCapture([f"{source}-primary"])
                capture.get = lambda _property: 0.0
            else:
                capture = ContinuousCapture(source)
            captures.append(capture)
            return capture

        def media_clock_factory(source, *_args, **kwargs):
            clock_sources.append(source)
            clock_sequences.append(kwargs.get("reference_sequence"))
            clock = FakeMediaClock()
            if kwargs.get("reference_sequence") is not None:
                clock.anchor_match_frame_count = 3
            return clock

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            media_clock_factory=media_clock_factory,
            media_clock_source_factory=lambda: "clock-session-1",
            media_clock_validator=lambda *_args: True,
            capture_position_milliseconds=lambda cap: cap.get(0),
            terminal_read_failover_seconds=0.5,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            replacement = reader.wait_for_frame(first["sequence"], timeout=2.0)
            self.assertIsNotNone(replacement)
            self.assertIn("signed-session-1", replacement["frame"])
            self.assertIsNotNone(replacement["media_clock"])
            self.assertTrue(captures[0].released)
            self.assertTrue(any(
                event["state"] == "renewed" for event in states
            ))
            terminal = [
                event for event in states
                if event["state"] == "terminal_failover_succeeded"
            ]
            self.assertEqual(len(terminal), 1)
            self.assertGreaterEqual(terminal[0]["delay_seconds"], 0.0)
            self.assertEqual(
                terminal[0]["method"], "same_session_restart"
            )
            self.assertEqual(terminal[0]["stage"], "ready")
            self.assertEqual(
                terminal[0]["evidence"], "exact_fragment_sequence"
            )
            self.assertEqual(len(clock_sources), 2)
            self.assertEqual(clock_sources, [
                "clock-session-1", "signed-session-1"
            ])
            self.assertIsNone(clock_sequences[0])
            self.assertEqual(len(clock_sequences[1]), 3)
            self.assertEqual(
                tuple(position for _frame, position in clock_sequences[1]),
                (50.0, 100.0, 150.0),
            )
            self.assertFalse(any(
                event["state"] == "reconnecting" for event in states
            ))
        finally:
            reader.stop(timeout=2.0)

    def test_terminal_restart_does_not_trust_a_prior_cursor_without_exact_evidence(self):
        captures = []
        clock_calls = []
        states = []

        def capture_factory(source):
            if not captures:
                capture = ScriptedCapture([f"{source}-primary"])
                capture.get = lambda _property: 0.0
            else:
                capture = ContinuousCapture(source)
            captures.append(capture)
            return capture

        exact_clock = FakeMediaClock()

        def media_clock_factory(*_args):
            clock_calls.append(_args)
            return exact_clock

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session-1",
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            media_clock_factory=media_clock_factory,
            media_clock_source_factory=lambda: "clock-session-1",
            media_clock_validator=lambda *_args: True,
            capture_position_milliseconds=lambda cap: cap.get(0),
            terminal_read_failover_seconds=0.5,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            replacement = reader.wait_for_frame(first["sequence"], timeout=2.0)
            self.assertIsNotNone(replacement)
            self.assertEqual(
                replacement["media_clock"]["media_timestamp_utc"],
                "2026-07-10T03:57:23.388Z",
            )
            first_session = first["media_clock"]["media_clock"]["session_id"]
            replacement_session = (
                replacement["media_clock"]["media_clock"]["session_id"]
            )
            self.assertRegex(first_session, r"^capture-v1-[0-9a-f-]+$")
            self.assertNotEqual(first_session, replacement_session)
            # The prior cursor is never trusted merely because its mapped time
            # remains receipt-plausible. A second exact fragment match is required.
            self.assertEqual(len(clock_calls), 2)
            terminal = next(
                event for event in states
                if event["state"] == "terminal_failover_succeeded"
            )
            self.assertEqual(terminal["stage"], "ready")
            self.assertEqual(
                terminal["evidence"], "exact_fragment_match"
            )
        finally:
            reader.stop(timeout=2.0)

    def test_terminal_restart_reanchors_from_a_recent_exact_frame(self):
        captures = []
        clock_calls = []
        states = []
        hold_replacement = threading.Event()
        fail_primary = threading.Event()
        release_fragment_match = threading.Event()
        lifecycle = []

        class ReanchorableClock(FakeMediaClock):
            def __init__(self):
                super().__init__()
                self.reanchors = []

            def reanchor_from_exact_match(
                self, previous_position_milliseconds,
                new_position_milliseconds,
            ):
                self.reanchors.append((
                    previous_position_milliseconds,
                    new_position_milliseconds,
                ))
                release_fragment_match.set()
                return self

        exact_clock = ReanchorableClock()

        def capture_factory(_source):
            if not captures:
                lifecycle.append("primary_open")
                capture = PositionedScriptedCapture(
                    ["shared-1", "shared-2", "shared-3"],
                    [0.0, 50.0, 100.0],
                    block_after_frames=fail_primary,
                )
                release = capture.release

                def release_primary():
                    lifecycle.append("primary_release")
                    release()

                capture.release = release_primary
            else:
                lifecycle.append("replacement_open")
                capture = PositionedScriptedCapture(
                    ["shared-1", "shared-2", "shared-3", "new-frame"],
                    [0.0, 50.0, 100.0, 150.0],
                    block_after_frames=hold_replacement,
                )
            captures.append(capture)
            return capture

        def media_clock_factory(*_args):
            if clock_calls:
                release_fragment_match.wait(1.0)
            clock_calls.append(_args)
            return exact_clock

        def validate_clock(*_args):
            return not fail_primary.is_set() or bool(exact_clock.reanchors)

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session-1",
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            media_clock_factory=media_clock_factory,
            media_clock_source_factory=lambda: "clock-session-1",
            media_clock_validator=validate_clock,
            capture_position_milliseconds=lambda cap: cap.get(0),
            terminal_read_failover_seconds=0.5,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            while first is not None and first["sequence"] < 3:
                first = reader.wait_for_frame(first["sequence"], timeout=1.0)
            self.assertEqual(first["frame"], "shared-3")
            fail_primary.set()
            replacement = reader.wait_for_frame(first["sequence"], timeout=2.0)
            self.assertEqual(replacement["frame"], "new-frame")
            self.assertEqual(exact_clock.reanchors, [(100.0, 100.0)])
            self.assertEqual(len(clock_calls), 1)
            terminal = next(
                event for event in states
                if event["state"] == "terminal_failover_succeeded"
            )
            self.assertEqual(terminal["stage"], "ready")
            self.assertEqual(
                terminal["evidence"], "recent_exact_sequence"
            )
            self.assertLess(
                lifecycle.index("primary_release"),
                lifecycle.index("replacement_open"),
            )
        finally:
            fail_primary.set()
            hold_replacement.set()
            release_fragment_match.set()
            reader.stop(timeout=2.0)

    def test_terminal_restart_reanchors_when_prior_clock_validation_fails(self):
        captures = []
        valid = "2026-07-10T03:57:23.388Z"
        invalid = "2026-07-10T03:57:20.000Z"
        class PositionSensitiveClock:
            def metadata_at(self, position_milliseconds):
                return {
                    "media_timestamp_utc": (
                        valid if position_milliseconds == 0.0 else invalid
                    ),
                    "media_clock": {
                        "source": "hls_ext_x_program_date_time",
                        "position_milliseconds": position_milliseconds,
                    },
                }

        clocks = [PositionSensitiveClock(), FakeMediaClock(valid)]
        clock_calls = []
        lower_bounds = []
        urgent_calls = []

        def capture_factory(source):
            if not captures:
                capture = ScriptedCapture([f"{source}-primary"])
                capture.get = lambda _property: 0.0
            else:
                capture = ContinuousCapture(source)
            captures.append(capture)
            return capture

        def media_clock_factory(
            *_args, not_before_media_time_utc=None, urgent=False
        ):
            clock_calls.append(_args)
            lower_bounds.append(not_before_media_time_utc)
            urgent_calls.append(urgent)
            return clocks[min(len(clock_calls) - 1, 1)]

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session-1",
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=media_clock_factory,
            media_clock_source_factory=lambda: "clock-session-1",
            media_clock_validator=lambda frame_clock, _epoch: (
                frame_clock["media_timestamp_utc"]
                == "2026-07-10T03:57:23.388Z"
            ),
            capture_position_milliseconds=lambda cap: cap.get(0),
            terminal_read_failover_seconds=0.5,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            replacement = reader.wait_for_frame(first["sequence"], timeout=2.0)
            self.assertIsNotNone(replacement)
            self.assertEqual(
                replacement["media_clock"]["media_timestamp_utc"], valid
            )
            self.assertEqual(len(clock_calls), 2)
            self.assertEqual(lower_bounds, [None, valid])
            self.assertEqual(urgent_calls, [False, True])
        finally:
            reader.stop(timeout=2.0)

    def test_terminal_read_failover_bound_must_be_finite_and_limited(self):
        for invalid in (-0.1, 10.1, float("inf")):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "between 0 and 10"):
                    LiveStreamReader(
                        source_factory=lambda: "signed-session",
                        capture_factory=lambda _source: ScriptedCapture([]),
                        recovery=StreamRecovery(0.1, 0.1),
                        terminal_read_failover_seconds=invalid,
                    )

    def test_failed_terminal_hot_failover_does_not_spin_session_mints(self):
        source_calls = []
        captures = []
        states = []
        reconnecting = threading.Event()

        def source_factory():
            source_calls.append(f"signed-session-{len(source_calls) + 1}")
            return source_calls[-1]

        def capture_factory(_source):
            capture = ScriptedCapture(
                ["primary"] if not captures else []
            )
            captures.append(capture)
            return capture

        def state_callback(**event):
            states.append(event)
            if event["state"] == "reconnecting":
                reconnecting.set()

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=capture_factory,
            recovery=StreamRecovery(1.0, 1.0),
            state_callback=state_callback,
            terminal_read_failover_seconds=0.5,
        )
        reader.start()
        try:
            self.assertIsNotNone(reader.wait_for_frame(0, timeout=1.0))
            self.assertTrue(reconnecting.wait(1.0))
            self.assertEqual(source_calls, [
                "signed-session-1",
                "signed-session-2",
            ])
            self.assertEqual(
                sum(
                    event["state"] == "terminal_failover_failed"
                    for event in states
                ),
                1,
            )
            failure = next(
                event for event in states
                if event["state"] == "terminal_failover_failed"
            )
            self.assertIn(
                failure["stage"],
                {"capture_open", "first_frame", "failed"},
            )
        finally:
            reader.stop(timeout=2.0)

    def test_proactive_renewal_rotates_source_without_reconnecting_state(self):
        source_calls = []
        states = []
        monotonic_value = [0.0]

        def monotonic():
            value = monotonic_value[0]
            monotonic_value[0] += 1.0
            return value

        def source_factory():
            source_calls.append(f"signed-session-{len(source_calls) + 1}")
            return source_calls[-1]

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=lambda source: ScriptedCapture(
                [f"{source}-frame-1", f"{source}-frame-2"]
            ),
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            monotonic=monotonic,
            connection_max_age_seconds=2.0,
        )
        reader.start()
        try:
            self.assertTrue(self.wait_until(lambda: any(
                event["state"] == "renewed" for event in states
            )))
            self.assertGreaterEqual(len(source_calls), 2)
            self.assertFalse(any(
                event["state"] == "reconnecting" for event in states
            ))
            self.assertEqual(sum(
                event["state"] == "connected" for event in states
            ), 1)
        finally:
            reader.stop(timeout=2.0)

    def test_proactive_preparations_are_serialized_process_wide(self):
        release_clock = threading.Event()
        entered_clock = threading.Event()
        lock = threading.Lock()
        active_replacement_clocks = 0
        maximum_active_replacement_clocks = 0
        readers = []
        states = {"left": [], "right": []}

        def make_reader(label):
            source_calls = []

            def source_factory():
                source = f"{label}-session-{len(source_calls) + 1}"
                source_calls.append(source)
                return source

            def media_clock_factory(source, *_args):
                nonlocal active_replacement_clocks
                nonlocal maximum_active_replacement_clocks
                if not source.endswith("-1"):
                    with lock:
                        active_replacement_clocks += 1
                        maximum_active_replacement_clocks = max(
                            maximum_active_replacement_clocks,
                            active_replacement_clocks,
                        )
                        entered_clock.set()
                    release_clock.wait(1.0)
                    with lock:
                        active_replacement_clocks -= 1
                return FakeMediaClock()

            return LiveStreamReader(
                source_factory=source_factory,
                capture_factory=lambda source: ContinuousCapture(source),
                recovery=StreamRecovery(0.1, 0.1),
                state_callback=lambda **event: states[label].append(event),
                media_clock_factory=media_clock_factory,
                media_clock_validator=lambda *_args: True,
                capture_position_milliseconds=lambda cap: cap.get(0),
                connection_max_age_seconds=0.15,
                connection_renewal_lead_seconds=0.05,
            )

        readers = [make_reader("left"), make_reader("right")]
        for reader in readers:
            reader.start()
        try:
            self.assertTrue(entered_clock.wait(1.0))
            time.sleep(0.05)
            with lock:
                self.assertEqual(maximum_active_replacement_clocks, 1)
            release_clock.set()
            self.assertTrue(self.wait_until(lambda: all(
                any(event["state"] == "renewed" for event in states[label])
                for label in states
            )))
            with lock:
                self.assertEqual(maximum_active_replacement_clocks, 1)
        finally:
            release_clock.set()
            for reader in readers:
                reader.stop(timeout=2.0)

    def test_proactive_renewal_age_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "must be positive"):
            LiveStreamReader(
                source_factory=lambda: "signed-session",
                capture_factory=lambda _source: ScriptedCapture([]),
                recovery=StreamRecovery(0.1, 0.1),
                connection_max_age_seconds=0,
            )

    def test_proactive_handover_drains_replacement_until_clock_is_ready(self):
        source_calls = []
        captures = []
        states = []
        release_replacement_clock = threading.Event()

        def source_factory():
            source = f"signed-session-{len(source_calls) + 1}"
            source_calls.append(source)
            return source

        def capture_factory(source):
            capture = ContinuousCapture(source)
            captures.append(capture)
            return capture

        def media_clock_factory(source, *_args):
            if source == "signed-session-2":
                release_replacement_clock.wait(1.0)
            return FakeMediaClock()

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            media_clock_factory=media_clock_factory,
            capture_position_milliseconds=lambda cap: cap.get(0),
            connection_max_age_seconds=0.15,
            connection_renewal_lead_seconds=0.05,
        )
        reader.start()
        try:
            self.assertTrue(self.wait_until(lambda: len(captures) >= 2))
            self.assertTrue(self.wait_until(lambda: captures[1].count >= 5))
            release_replacement_clock.set()
            self.assertTrue(self.wait_until(lambda: any(
                event["state"] == "renewed" for event in states
            )))
            snapshot = reader.snapshot(0)
            self.assertIsNotNone(snapshot)
            self.assertIsNotNone(snapshot["media_clock"])
            replacement_frame_number = int(
                snapshot["frame"].rsplit("-", 1)[1]
            )
            self.assertGreaterEqual(replacement_frame_number, 5)
            self.assertTrue(captures[0].released)
            self.assertFalse(any(
                event["state"] == "reconnecting" for event in states
            ))
        finally:
            release_replacement_clock.set()
            reader.stop(timeout=2.0)

    def test_failed_proactive_preparation_keeps_active_reader_streaming(self):
        source_calls = []
        captures = []
        states = []

        def source_factory():
            source = f"signed-session-{len(source_calls) + 1}"
            source_calls.append(source)
            return source

        def capture_factory(source):
            if not captures:
                capture = ContinuousCapture(source)
            else:
                capture = ScriptedCapture([])
            captures.append(capture)
            return capture

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            connection_max_age_seconds=0.1,
            connection_renewal_lead_seconds=0.05,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            self.assertTrue(self.wait_until(lambda: len(captures) >= 2))
            later = reader.wait_for_frame(first["sequence"], timeout=1.0)
            self.assertIsNotNone(later)
            self.assertGreater(later["sequence"], first["sequence"])
            self.assertFalse(captures[0].released)
            self.assertFalse(any(
                event["state"] == "reconnecting" for event in states
            ))
        finally:
            reader.stop(timeout=2.0)

    def test_snapshot_never_relabels_the_same_frame_as_new(self):
        release_read = threading.Event()
        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: ScriptedCapture(
                ["frame-1"], block_after_frames=release_read
            ),
            recovery=StreamRecovery(0.1, 0.1),
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            self.assertIsNone(reader.snapshot(first["sequence"]))
        finally:
            release_read.set()
            reader.stop(timeout=2.0)

    def test_frame_callback_receives_each_accepted_frame(self):
        release_read = threading.Event()
        callbacks = []
        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: ScriptedCapture(
                ["frame-1"], block_after_frames=release_read
            ),
            recovery=StreamRecovery(0.1, 0.1),
            wall_time=lambda: 1000.25,
            monotonic=lambda: 500.0,
            frame_callback=lambda *values: callbacks.append(values),
        )
        reader.start()
        try:
            self.assertIsNotNone(reader.wait_for_frame(0, timeout=1.0))
            self.assertTrue(self.wait_until(lambda: len(callbacks) == 1))
            self.assertEqual(callbacks[0], (
                "frame-1", 1000.25, 500.0, None
            ))
        finally:
            release_read.set()
            reader.stop(timeout=2.0)

    def test_snapshot_keeps_media_time_separate_from_decode_receipt_time(self):
        release_read = threading.Event()
        clock = FakeMediaClock()
        capture = ScriptedCapture(
            ["frame-1"], block_after_frames=release_read
        )
        capture.get = lambda _property: 250.5
        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            wall_time=lambda: 1_000.25,
            media_clock_factory=(
                lambda _source, _frame, _position, _identity: clock
            ),
            capture_position_milliseconds=lambda cap: cap.get(0),
        )
        reader.start()
        try:
            snapshot = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["source_epoch"], 1_000.25)
            self.assertEqual(
                snapshot["media_clock"]["media_timestamp_utc"],
                "2026-07-10T03:57:23.388Z",
            )
            self.assertEqual(clock.positions, [250.5])
        finally:
            release_read.set()
            reader.stop(timeout=2.0)

    def test_invalid_clock_mapping_is_discarded_and_reanchored(self):
        capture = ContinuousCapture("clock-validation")
        clock_calls = []

        def media_clock_factory(*_args):
            timestamp = (
                "2026-07-10T03:57:30.000Z"
                if not clock_calls
                else "2026-07-10T03:57:23.388Z"
            )
            clock_calls.append(timestamp)
            return FakeMediaClock(timestamp=timestamp)

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=media_clock_factory,
            media_clock_validator=lambda clock, _epoch: (
                clock["media_timestamp_utc"]
                == "2026-07-10T03:57:23.388Z"
            ),
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_retry_seconds=0.1,
            media_clock_invalid_grace_seconds=0.0,
        )
        reader.start()
        try:
            sequence = 0
            snapshot = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                candidate = reader.wait_for_frame(sequence, timeout=0.2)
                if candidate is None:
                    continue
                sequence = candidate["sequence"]
                if candidate.get("media_clock") is not None:
                    snapshot = candidate
                    break
            self.assertIsNotNone(snapshot)
            self.assertEqual(
                snapshot["media_clock"]["media_timestamp_utc"],
                "2026-07-10T03:57:23.388Z",
            )
            self.assertGreaterEqual(len(clock_calls), 2)
        finally:
            reader.stop(timeout=2.0)

    def test_reanchor_retains_last_trusted_frame_until_valid_clock(self):
        capture = ContinuousCapture("clock-hold")
        reanchor_started = threading.Event()
        release_reanchor = threading.Event()
        calls = []
        valid = "2026-07-10T03:57:23.388Z"
        invalid = "2026-07-10T03:57:30.000Z"

        def media_clock_factory(*_args):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                return SequencedMediaClock([valid, invalid])
            reanchor_started.set()
            release_reanchor.wait(1.0)
            return FakeMediaClock(timestamp=valid)

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=media_clock_factory,
            media_clock_validator=lambda clock, _epoch: (
                clock["media_timestamp_utc"] == valid
            ),
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_retry_seconds=0.1,
            media_clock_invalid_grace_seconds=0.0,
        )
        reader.start()
        try:
            sequence = 0
            trusted = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                candidate = reader.wait_for_frame(sequence, timeout=0.2)
                if candidate is None:
                    continue
                sequence = candidate["sequence"]
                if candidate.get("media_clock") is not None:
                    trusted = candidate
                    break
            self.assertIsNotNone(trusted)
            self.assertTrue(reanchor_started.wait(1.0))
            time.sleep(0.05)
            self.assertIsNone(reader.snapshot(trusted["sequence"]))

            release_reanchor.set()
            replacement = reader.wait_for_frame(
                trusted["sequence"], timeout=2.0
            )
            self.assertIsNotNone(replacement)
            self.assertEqual(
                replacement["media_clock"]["media_timestamp_utc"], valid
            )
        finally:
            release_reanchor.set()
            reader.stop(timeout=2.0)

    def test_transient_invalid_clock_is_discarded_without_reanchor(self):
        capture = ContinuousCapture("clock-glitch")
        calls = []
        valid = "2026-07-10T03:57:23.388Z"
        invalid = "2026-07-10T03:57:30.000Z"

        def media_clock_factory(*_args):
            calls.append(1)
            return SequencedMediaClock([valid, invalid, valid])

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=media_clock_factory,
            media_clock_validator=lambda clock, _epoch: (
                clock["media_timestamp_utc"] == valid
            ),
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_invalid_grace_seconds=1.0,
        )
        reader.start()
        try:
            sequence = 0
            trusted_sequences = []
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and len(trusted_sequences) < 2:
                snapshot = reader.wait_for_frame(sequence, timeout=0.2)
                if snapshot is None:
                    continue
                sequence = snapshot["sequence"]
                if snapshot.get("media_clock") is not None:
                    trusted_sequences.append(sequence)
            self.assertEqual(len(trusted_sequences), 2)
            self.assertEqual(calls, [1])
            self.assertGreater(trusted_sequences[1], trusted_sequences[0])
        finally:
            reader.stop(timeout=2.0)

    def test_persistent_invalid_clock_hot_prepares_a_fresh_session(self):
        source_calls = []
        captures = []
        states = []
        valid = "2026-07-10T03:57:23.388Z"
        invalid = "2026-07-10T03:57:30.000Z"

        def source_factory():
            source = f"signed-session-{len(source_calls) + 1}"
            source_calls.append(source)
            return source

        def capture_factory(source):
            capture = ContinuousCapture(source)
            captures.append(capture)
            return capture

        def media_clock_factory(source, *_args):
            if source == "signed-session-1":
                return SequencedMediaClock([valid, invalid])
            return FakeMediaClock(timestamp=valid)

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            media_clock_factory=media_clock_factory,
            media_clock_validator=lambda clock, _epoch: (
                clock["media_timestamp_utc"] == valid
            ),
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_invalid_grace_seconds=0.0,
            connection_max_age_seconds=60.0,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            self.assertIn("signed-session-1", first["frame"])
            self.assertTrue(self.wait_until(lambda: len(captures) >= 2))
            self.assertTrue(self.wait_until(lambda: any(
                event["state"] == "renewed" for event in states
            )))
            replacement = reader.wait_for_frame(
                first["sequence"], timeout=2.0
            )
            self.assertIsNotNone(replacement)
            self.assertIn("signed-session-2", replacement["frame"])
            self.assertEqual(
                replacement["media_clock"]["media_timestamp_utc"], valid
            )
            self.assertTrue(captures[0].released)
            self.assertFalse(any(
                event["state"] == "reconnecting" for event in states
            ))
        finally:
            reader.stop(timeout=2.0)

    def test_clock_resolution_uses_independent_connection_local_source(self):
        release_read = threading.Event()
        observed_sources = []

        def resolve(source, _frame, _position, _identity):
            observed_sources.append(source)
            return FakeMediaClock()

        reader = LiveStreamReader(
            source_factory=lambda: "capture-session",
            capture_factory=lambda source: ScriptedCapture(
                [f"{source}-frame"], block_after_frames=release_read
            ),
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=resolve,
            media_clock_source_factory=lambda: "clock-session",
            capture_position_milliseconds=lambda _cap: 0.0,
        )
        reader.start()
        try:
            snapshot = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(snapshot)
            self.assertIsNotNone(snapshot["media_clock"])
            self.assertEqual(observed_sources, ["clock-session"])
        finally:
            release_read.set()
            reader.stop(timeout=2.0)

    def test_media_clock_failure_does_not_hide_a_decodable_frame(self):
        release_read = threading.Event()
        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: ScriptedCapture(
                ["frame-1"], block_after_frames=release_read
            ),
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=(
                lambda _source, _frame, _position, _identity: (
                    _ for _ in ()
                ).throw(RuntimeError("playlist unavailable"))
            ),
            capture_position_milliseconds=lambda _cap: 0.0,
        )
        reader.start()
        try:
            snapshot = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(snapshot)
            self.assertIsNone(snapshot["media_clock"])
        finally:
            release_read.set()
            reader.stop(timeout=2.0)

    def test_slow_media_clock_resolution_does_not_pause_live_capture(self):
        allow_next = threading.Event()
        release_clock = threading.Event()
        capture = GatedPositionCapture(
            ["frame-1", "frame-2"], [0.0, 100.0], allow_next
        )

        def slow_clock(*_args):
            release_clock.wait(1.0)
            return FakeMediaClock()

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=slow_clock,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_initial_wait_seconds=0.0,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            self.assertIsNone(first["media_clock"])
            allow_next.set()
            second = reader.wait_for_frame(first["sequence"], timeout=0.5)
            self.assertIsNotNone(second)
            self.assertIsNone(second["media_clock"])
        finally:
            release_clock.set()
            allow_next.set()
            reader.stop(timeout=2.0)

    def test_unmatched_clock_retries_on_a_later_frame(self):
        allow_next = threading.Event()
        capture = GatedPositionCapture(
            ["frame-1", "frame-2", "frame-3"],
            [0.0, 100.0, 200.0],
            allow_next,
        )
        calls = []
        recovered_clock = FakeMediaClock("2026-07-10T03:57:24.000Z")

        def media_clock_factory(*_args):
            calls.append(len(calls) + 1)
            return None if len(calls) == 1 else recovered_clock

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=media_clock_factory,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_retry_seconds=0.1,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNone(first["media_clock"])
            time.sleep(0.11)
            allow_next.set()
            self.assertTrue(self.wait_until(lambda: len(calls) == 2))
            second = reader.snapshot(0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(
                second["media_clock"]["media_timestamp_utc"],
                "2026-07-10T03:57:24.000Z",
            )
        finally:
            allow_next.set()
            reader.stop(timeout=2.0)

    def test_unmatched_startup_clock_retries_with_three_frame_sequence(self):
        calls = []
        recovered_clock = FakeMediaClock("2026-07-10T03:57:24.000Z")

        def media_clock_factory(_source, _frame, _position, _identity, **kwargs):
            calls.append(kwargs.get("reference_sequence"))
            return None if len(calls) == 1 else recovered_clock

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda source: ContinuousCapture(source),
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=media_clock_factory,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_retry_seconds=0.1,
        )
        reader.start()
        try:
            self.assertTrue(self.wait_until(lambda: len(calls) >= 2))
            self.assertIsNone(calls[0])
            self.assertEqual(len(calls[1]), 3)
            positions = tuple(position for _frame, position in calls[1])
            self.assertEqual(positions, tuple(sorted(positions)))
            self.assertGreater(positions[-1], positions[0])
            self.assertTrue(self.wait_until(lambda: (
                (reader.snapshot(0) or {}).get("media_clock") is not None
            )))
        finally:
            reader.stop(timeout=2.0)

    def test_sequence_fallback_preserves_raw_duplicate_contiguity(self):
        release_read = threading.Event()
        calls = []

        class PacedCapture(PositionedScriptedCapture):
            def read(self):
                time.sleep(0.06)
                return super().read()

        capture = PacedCapture(
            ["A", "B", "B", "C"],
            [50.0, 100.0, 150.0, 200.0],
            block_after_frames=release_read,
        )

        def resolve_clock(_source, _frame, _position, _identity, **kwargs):
            calls.append(kwargs.get("reference_sequence"))
            return None if len(calls) == 1 else FakeMediaClock()

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            frame_identity=lambda frame: frame,
            media_clock_factory=resolve_clock,
            capture_position_milliseconds=lambda cap: cap.get(0),
            media_clock_retry_seconds=0.1,
        )
        reader.start()
        try:
            self.assertTrue(self.wait_until(lambda: len(calls) >= 2))
            self.assertIsNone(calls[0])
            self.assertEqual(calls[1], (
                ("B", 100.0), ("B", 150.0), ("C", 200.0)
            ))
            self.assertNotEqual(
                tuple(frame for frame, _position in calls[1]),
                ("A", "B", "C"),
            )
        finally:
            release_read.set()
            reader.stop(timeout=2.0)

    def test_capture_position_reset_forces_an_exact_reanchor(self):
        allow_next = threading.Event()
        capture = GatedPositionCapture(
            ["before-reset", "after-reset"], [1000.0, 0.0], allow_next
        )
        clocks = [
            FakeMediaClock("2026-07-10T03:57:23.000Z"),
            FakeMediaClock("2026-07-10T03:57:25.000Z"),
        ]

        def media_clock_factory(*_args):
            return clocks.pop(0)

        reader = LiveStreamReader(
            source_factory=lambda: "signed-session",
            capture_factory=lambda _source: capture,
            recovery=StreamRecovery(0.1, 0.1),
            media_clock_factory=media_clock_factory,
            capture_position_milliseconds=lambda cap: cap.get(0),
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertEqual(
                first["media_clock"]["media_timestamp_utc"],
                "2026-07-10T03:57:23.000Z",
            )
            allow_next.set()
            second = reader.wait_for_frame(first["sequence"], timeout=1.0)
            self.assertEqual(
                second["media_clock"]["media_timestamp_utc"],
                "2026-07-10T03:57:25.000Z",
            )
            self.assertEqual(clocks, [])
        finally:
            allow_next.set()
            reader.stop(timeout=2.0)

    def test_blocked_camera_does_not_delay_a_healthy_camera(self):
        release_blocked = threading.Event()
        release_healthy = threading.Event()
        blocked = LiveStreamReader(
            source_factory=lambda: "blocked-session",
            capture_factory=lambda _source: ScriptedCapture(
                [], block_after_frames=release_blocked
            ),
            recovery=StreamRecovery(0.1, 0.1),
        )
        healthy = LiveStreamReader(
            source_factory=lambda: "healthy-session",
            capture_factory=lambda _source: ScriptedCapture(
                ["healthy-frame"], block_after_frames=release_healthy
            ),
            recovery=StreamRecovery(0.1, 0.1),
        )
        blocked.start()
        healthy.start()
        try:
            frame = healthy.wait_for_frame(0, timeout=0.5)
            self.assertIsNotNone(frame)
            self.assertEqual(frame["frame"], "healthy-frame")
            self.assertIsNone(blocked.snapshot(0))
        finally:
            release_blocked.set()
            release_healthy.set()
            blocked.stop(timeout=2.0)
            healthy.stop(timeout=2.0)

    def test_identical_content_after_reconnect_does_not_advance_freshness(self):
        release_reads = [threading.Event() for _ in range(3)]
        captures = []
        states = []
        wall_time_calls = []

        def source_factory():
            return f"renewed-session-{len(captures) + 1}"

        def capture_factory(_source):
            index = len(captures)
            frame = "terminal-frame" if index < 2 else "changed-frame"
            capture = ScriptedCapture(
                [frame], block_after_frames=release_reads[index]
            )
            captures.append(capture)
            return capture

        event_times = iter((100.0, 200.0))

        def wall_time():
            value = next(event_times)
            wall_time_calls.append(value)
            return value

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            wall_time=wall_time,
            duplicate_frame_limit=5,
            terminal_read_failover_seconds=0.0,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            self.assertEqual(first["source_epoch"], 100.0)

            release_reads[0].set()
            self.assertTrue(self.wait_until(lambda: len(captures) >= 2))
            # The first frame from the renewed session is byte-identical to the
            # terminal frame from the prior session. It must not get a sequence
            # number or consume a new event timestamp.
            self.assertIsNone(reader.snapshot(first["sequence"]))
            self.assertEqual(wall_time_calls, [100.0])

            release_reads[1].set()
            changed = reader.wait_for_frame(first["sequence"], timeout=2.0)
            self.assertIsNotNone(changed)
            self.assertEqual(changed["sequence"], first["sequence"] + 1)
            self.assertEqual(changed["source_epoch"], 200.0)
            self.assertEqual(wall_time_calls, [100.0, 200.0])
            self.assertGreaterEqual(
                sum(event["state"] == "reconnecting" for event in states), 2
            )
        finally:
            for event in release_reads:
                event.set()
            reader.stop(timeout=2.0)

    def test_terminal_repeats_force_reconnect_without_new_sequence(self):
        release_changed = threading.Event()
        captures = []
        states = []

        def capture_factory(_source):
            if not captures:
                capture = ScriptedCapture(["same", "same", "same"])
            else:
                capture = ScriptedCapture(
                    ["different"], block_after_frames=release_changed
                )
            captures.append(capture)
            return capture

        reader = LiveStreamReader(
            source_factory=lambda: "renewed-session",
            capture_factory=capture_factory,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=lambda **event: states.append(event),
            duplicate_frame_limit=2,
        )
        reader.start()
        try:
            first = reader.wait_for_frame(0, timeout=1.0)
            self.assertIsNotNone(first)
            changed = reader.wait_for_frame(first["sequence"], timeout=2.0)
            self.assertIsNotNone(changed)
            self.assertEqual(changed["sequence"], first["sequence"] + 1)
            reconnect_errors = [
                event["error"]
                for event in states
                if event["state"] == "reconnecting"
            ]
            self.assertIn(
                "RuntimeError: repeated frame content", reconnect_errors
            )
        finally:
            release_changed.set()
            reader.stop(timeout=2.0)

    def test_source_exception_is_sanitized_before_callback(self):
        state_event = threading.Event()
        states = []

        def state_callback(**event):
            states.append(event)
            state_event.set()

        def source_factory():
            raise RuntimeError(
                "expired https://video.example/live.m3u8?"
                "SessionToken=secret&X-Amz-Signature=also-secret"
            )

        reader = LiveStreamReader(
            source_factory=source_factory,
            capture_factory=lambda _source: None,
            recovery=StreamRecovery(0.1, 0.1),
            state_callback=state_callback,
        )
        reader.start()
        try:
            self.assertTrue(state_event.wait(1.0))
            error = states[0]["error"]
            self.assertIn("details redacted", error)
            for forbidden in (
                "https://",
                "video.example",
                "SessionToken",
                "secret",
                "X-Amz-Signature",
            ):
                self.assertNotIn(forbidden, error)
        finally:
            reader.stop(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
