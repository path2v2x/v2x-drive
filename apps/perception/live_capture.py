"""Thread-isolated capture for live HLS sources.

OpenCV/FFmpeg reads can block until their configured timeout.  One worker per
camera keeps an unhealthy HLS source from delaying every healthy camera in the
perception pipeline.  The worker retains only the newest frame and assigns a
sequence number so consumers never mistake a cached frame for new input.
"""

from collections import deque
import hashlib
import inspect
from itertools import islice
import math
import secrets
import threading
import time

from decoder_admission import (
    acquire_auxiliary_decoder_slot,
    begin_urgent_decoder_window,
)
from runtime_health import sanitize_source_error


_PROACTIVE_PREPARATION_SEMAPHORE = threading.Semaphore(1)
_PROACTIVE_PREPARATIONS = set()
_PROACTIVE_PREPARATIONS_LOCK = threading.Lock()
_TERMINAL_RECOVERIES = 0
_TERMINAL_CLEANUPS = {}
_TERMINAL_CLEANUPS_LOCK = threading.Lock()
_TERMINAL_CLEANUP_FAILURES = 0
_PROACTIVE_PREPARATION_STAGE_NAMES = frozenset({
    "capture_open",
    "capture_position",
    "clock_resolution",
    "clock_source",
    "clock_validation",
    "decoder_slot",
    "failed",
    "first_frame",
    "preparation_slot",
    "ready",
    "recent_exact_anchor",
    "source",
    "transport_clock_validation",
})
_TERMINAL_CLEANUP_KIND_NAMES = frozenset({
    "candidate",
    "capture",
    "claimed",
    "reader-owned",
})
_DIAGNOSTIC_LOCK_TIMEOUT_SECONDS = 0.01
_DIAGNOSTIC_PREPARATION_LIMIT = 16
_DIAGNOSTIC_CLEANUP_LIMIT = 32
_TRANSPORT_DIAGNOSTICS = {
    "starting",
    "matched",
    "position_unavailable",
    "position_invalid",
    "position_before_window",
    "position_after_window",
    "position_inside_gap",
    "mapping_empty",
    "mediator_unavailable",
    "packet_probe_failed",
    "fragment_clock_rejected",
    "discontinuity",
    "transport_disabled",
    "sidecar_unavailable",
    "sidecar_invalid",
    "sidecar_line_too_long",
    "sidecar_non_ascii",
    "sidecar_time_base_invalid",
    "sidecar_time_base_changed",
    "sidecar_record_invalid",
    "sidecar_index_nonsequential",
    "sidecar_missing_time_base",
    "sidecar_queue_overflow",
    "sidecar_pts_nonmonotonic",
    "sidecar_io_error",
    "sidecar_timeout",
    "sidecar_eof",
    "sidecar_waiting",
    "sidecar_ready",
    "overlap_replay_exceeded",
    "closed",
}


class _AsyncTerminalCleanup:
    """Run potentially blocking decoder teardown outside the failover clock."""

    def __init__(self, key, action, started_monotonic=None):
        self.key = key
        self._started_monotonic = (
            time.monotonic()
            if started_monotonic is None
            else float(started_monotonic)
        )
        self._action = action
        self._done = threading.Event()
        self._succeeded = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        global _TERMINAL_CLEANUP_FAILURES
        try:
            self._action()
            self._succeeded = True
        except Exception:
            self._succeeded = False
        finally:
            self._action = None
            with _TERMINAL_CLEANUPS_LOCK:
                if not self._succeeded:
                    _TERMINAL_CLEANUP_FAILURES += 1
                if _TERMINAL_CLEANUPS.get(self.key) is self:
                    _TERMINAL_CLEANUPS.pop(self.key, None)
            # A waiter observing completion must also observe sticky failure
            # accounting and global-registry removal from the same action.
            self._done.set()

    def wait(self, timeout=None):
        return self._done.wait(
            None if timeout is None else max(0.0, float(timeout))
        )

    def succeeded(self):
        return self._done.is_set() and self._succeeded

    def diagnostic_kind(self):
        kind = self.key[0] if isinstance(self.key, tuple) and self.key else None
        return kind if kind in _TERMINAL_CLEANUP_KIND_NAMES else "other"

    def age_seconds(self, now_monotonic=None):
        now = (
            time.monotonic()
            if now_monotonic is None
            else float(now_monotonic)
        )
        return max(0.0, now - self._started_monotonic)


def _start_terminal_cleanup(key, action, started_monotonic=None):
    with _TERMINAL_CLEANUPS_LOCK:
        cleanup = _TERMINAL_CLEANUPS.get(key)
        if cleanup is None:
            cleanup = _AsyncTerminalCleanup(
                key, action, started_monotonic=started_monotonic
            )
            _TERMINAL_CLEANUPS[key] = cleanup
            cleanup.start()
        return cleanup


def _start_reader_owned_cleanup(key, action):
    """Start one cleanup owned and awaited by a specific live reader."""
    cleanup = _AsyncTerminalCleanup(key, action)
    cleanup.start()
    return cleanup


def _promote_reader_owned_cleanup(cleanup):
    """Expose an in-flight reader cleanup to the process-wide stop gate."""
    def wait_for_owned_cleanup():
        cleanup.wait()
        # The reader-owned task already records its own failure in the sticky
        # process counter. The wrapper provides visibility only; re-raising here
        # would count one failed release twice.

    return _start_terminal_cleanup(
        ("reader-owned", id(cleanup)),
        wait_for_owned_cleanup,
        started_monotonic=cleanup._started_monotonic,
    )


def _cleanup_candidate(candidate):
    error = None
    try:
        candidate.discard()
    except Exception as exc:
        error = exc
    try:
        join = getattr(candidate, "join", None)
        if join is not None:
            join()
        wait_quiesced = getattr(candidate, "wait_quiesced", None)
        if wait_quiesced is not None:
            wait_quiesced()
    except Exception as exc:
        if error is None:
            error = exc
    if error is not None:
        raise error


def _start_candidate_cleanup(candidate):
    return _start_terminal_cleanup(
        ("candidate", id(candidate)),
        lambda: _cleanup_candidate(candidate),
    )


def wait_for_terminal_cleanups(timeout=None):
    """Wait for all tracked teardown tasks; return false on timeout/failure."""
    deadline = (
        None if timeout is None else time.monotonic() + max(0.0, float(timeout))
    )
    while True:
        with _TERMINAL_CLEANUPS_LOCK:
            cleanups = tuple(_TERMINAL_CLEANUPS.values())
            failures = _TERMINAL_CLEANUP_FAILURES
        if not cleanups:
            return failures == 0
        for cleanup in cleanups:
            remaining = (
                None
                if deadline is None
                else max(0.0, deadline - time.monotonic())
            )
            if not cleanup.wait(remaining):
                return False


def _register_proactive_preparation(preparation):
    with _PROACTIVE_PREPARATIONS_LOCK:
        if _TERMINAL_RECOVERIES > 0:
            return False
        _PROACTIVE_PREPARATIONS.add(preparation)
        return True


def _unregister_proactive_preparation(preparation):
    with _PROACTIVE_PREPARATIONS_LOCK:
        _PROACTIVE_PREPARATIONS.discard(preparation)


def capture_preparation_topology():
    """Return secret-free process-wide preparation counts for health evidence."""
    if _PROACTIVE_PREPARATIONS_LOCK.acquire(
        timeout=_DIAGNOSTIC_LOCK_TIMEOUT_SECONDS
    ):
        try:
            proactive_count = len(_PROACTIVE_PREPARATIONS)
            preparations = tuple(islice(
                _PROACTIVE_PREPARATIONS, _DIAGNOSTIC_PREPARATION_LIMIT
            ))
            terminal_recoveries = _TERMINAL_RECOVERIES
            proactive_snapshot = (
                "ok" if proactive_count == len(preparations) else "truncated"
            )
        finally:
            _PROACTIVE_PREPARATIONS_LOCK.release()
        preparation_states = tuple(
            preparation.diagnostic_state() for preparation in preparations
        )
        stage_counts = {}
        for state in preparation_states:
            stage = state["stage"]
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
    else:
        proactive_count = -1
        terminal_recoveries = -1
        proactive_snapshot = "lock_busy"
        preparation_states = ()
        stage_counts = {"lock_busy": 1}
    topology = {
        "proactive_preparations": proactive_count,
        "terminal_recoveries": terminal_recoveries,
        "proactive_preparation_snapshot": proactive_snapshot,
        "proactive_preparation_states": {
            "sampled_count": len(preparation_states),
            "stage_counts": dict(sorted(stage_counts.items())),
            "claimed_count": sum(
                int(state["claimed"] is True)
                for state in preparation_states
            ),
            "claimed_lock_busy_count": sum(
                int(state["claimed"] is None)
                for state in preparation_states
            ),
            "done_count": sum(
                int(state["done"]) for state in preparation_states
            ),
            "discarded_count": sum(
                int(state["discarded"]) for state in preparation_states
            ),
            "quiesced_count": sum(
                int(state["quiesced"]) for state in preparation_states
            ),
        },
    }
    if _TERMINAL_CLEANUPS_LOCK.acquire(
        timeout=_DIAGNOSTIC_LOCK_TIMEOUT_SECONDS
    ):
        try:
            terminal_cleanup_count = len(_TERMINAL_CLEANUPS)
            cleanups = tuple(islice(
                _TERMINAL_CLEANUPS.values(), _DIAGNOSTIC_CLEANUP_LIMIT
            ))
            cleanup_failures = _TERMINAL_CLEANUP_FAILURES
            cleanup_snapshot = (
                "ok"
                if terminal_cleanup_count == len(cleanups)
                else "truncated"
            )
        finally:
            _TERMINAL_CLEANUPS_LOCK.release()
        now = time.monotonic()
        cleanup_states = {}
        for cleanup in cleanups:
            kind = cleanup.diagnostic_kind()
            state = cleanup_states.setdefault(
                kind, {"count": 0, "oldest_age_seconds": 0.0}
            )
            state["count"] += 1
            state["oldest_age_seconds"] = max(
                state["oldest_age_seconds"], cleanup.age_seconds(now)
            )
        for state in cleanup_states.values():
            state["oldest_age_seconds"] = round(
                state["oldest_age_seconds"], 3
            )
    else:
        terminal_cleanup_count = -1
        cleanups = ()
        cleanup_failures = _TERMINAL_CLEANUP_FAILURES
        cleanup_snapshot = "lock_busy"
        cleanup_states = {
            "lock_busy": {"count": 1, "oldest_age_seconds": 0.0}
        }
    topology["terminal_cleanups"] = terminal_cleanup_count
    topology["terminal_cleanup_snapshot"] = cleanup_snapshot
    topology["terminal_cleanup_sampled_count"] = len(cleanups)
    topology["terminal_cleanup_states"] = dict(
        sorted(cleanup_states.items())
    )
    topology["terminal_cleanup_failures"] = cleanup_failures
    return topology


class _TerminalRecoveryWindow:
    """Block new proactive decoders and normal fragment work until release."""

    def __init__(self, preparations, decoder_window):
        self.preparations = preparations
        self._decoder_window = decoder_window
        self._lock = threading.Lock()
        self._released = False

    def release(self):
        global _TERMINAL_RECOVERIES
        with self._lock:
            if self._released:
                return
            self._released = True
        with _PROACTIVE_PREPARATIONS_LOCK:
            if _TERMINAL_RECOVERIES < 1:
                raise RuntimeError("terminal recovery window underflow")
            _TERMINAL_RECOVERIES -= 1
        self._decoder_window.release()


def _begin_terminal_recovery():
    global _TERMINAL_RECOVERIES
    decoder_window = begin_urgent_decoder_window()
    with _PROACTIVE_PREPARATIONS_LOCK:
        _TERMINAL_RECOVERIES += 1
        preparations = tuple(_PROACTIVE_PREPARATIONS)
    return _TerminalRecoveryWindow(preparations, decoder_window)


def _cancel_proactive_preparations(timeout=0.0, preparations=None):
    """Cancel every off-path capture before terminal GPU work is admitted."""
    owned_window = None
    if preparations is None:
        owned_window = _begin_terminal_recovery()
        preparations = owned_window.preparations
    try:
        cleanups = [
            _start_candidate_cleanup(preparation)
            for preparation in preparations
        ]
        deadline = time.monotonic() + max(0.0, float(timeout))
        for cleanup in cleanups:
            cleanup.wait(max(0.0, deadline - time.monotonic()))
        return all(cleanup.succeeded() for cleanup in cleanups)
    finally:
        if owned_window is not None:
            owned_window.release()


def _call_with_supported_kwargs(factory, *args, **kwargs):
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_keywords = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    supported = {
        key: value for key, value in kwargs.items()
        if accepts_keywords or key in parameters
    }
    return factory(*args, **supported)


def _capture_transport_media_clock(capture):
    """Return same-session transport evidence exposed by a capture adapter.

    The method is intentionally optional so the existing OpenCV and exact
    decoded-pixel paths remain unchanged.  Capture-layer failures are additive:
    they provide no clock and the established exact matcher may still recover.
    """
    accessor = getattr(capture, "transport_media_clock", None)
    if accessor is None:
        return None
    try:
        clock = accessor()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    if getattr(clock, "evidence_method", None) != "exact_same_session_pts":
        return None
    return clock


def _capture_transport_diagnostic(capture):
    accessor = getattr(capture, "transport_clock_diagnostic", None)
    if accessor is None:
        return None
    try:
        diagnostic = accessor()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return "transport_disabled"
    return (
        diagnostic
        if diagnostic in _TRANSPORT_DIAGNOSTICS
        else "transport_disabled"
    )


class _ProactiveRenewal(Exception):
    """Internal control flow for rotating a signed source before expiry."""


class _AsyncMediaClockResolution:
    """Resolve one exact media-clock anchor without pausing live capture."""

    def __init__(self, factory, args, kwargs=None):
        self._factory = factory
        self._args = args
        self._kwargs = dict(kwargs or {})
        self._cancelled = threading.Event()
        self._kwargs.setdefault("cancel_event", self._cancelled)
        self._done = threading.Event()
        self._result = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            kwargs = self._kwargs
            if kwargs:
                try:
                    parameters = inspect.signature(self._factory).parameters
                except (TypeError, ValueError):
                    parameters = {}
                accepts_keywords = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                kwargs = {
                    key: value for key, value in kwargs.items()
                    if accepts_keywords or key in parameters
                }
            self._result = self._factory(*self._args, **kwargs)
        except Exception:
            # Clock evidence is additive and fail-closed. The live reader must
            # keep decoding while a bounded metadata fetch/match fails.
            self._result = None
        finally:
            # Drop the signed source URL and reference frame as soon as the
            # resolver finishes; neither value is published or logged.
            self._factory = None
            self._args = None
            self._kwargs = None
            self._done.set()

    def poll(self, timeout=0.0):
        if not self._done.wait(max(0.0, float(timeout))):
            return False, None
        return True, self._result

    def discard(self):
        self._cancelled.set()

    def join(self, timeout=None):
        self._thread.join(
            None if timeout is None else max(0.0, float(timeout))
        )

    def is_alive(self):
        return self._thread.is_alive()


class _AsyncCapturePreparation:
    """Open, clock, and continuously drain a replacement capture off-thread."""

    def __init__(
        self,
        source_factory,
        clock_source_factory,
        capture_factory,
        media_clock_factory,
        media_clock_validator,
        capture_position_milliseconds,
        media_frame_identity,
        stop_event,
        wall_time,
        monotonic,
        serialize_preparation=True,
        reserve_decoder_slot=False,
    ):
        self._source_factory = source_factory
        self._clock_source_factory = clock_source_factory
        self._capture_factory = capture_factory
        self._media_clock_factory = media_clock_factory
        self._media_clock_validator = media_clock_validator
        self._capture_position_milliseconds = capture_position_milliseconds
        self._media_frame_identity = media_frame_identity
        self._stop_event = stop_event
        self._wall_time = wall_time
        self._monotonic = monotonic
        self._serialize_preparation = bool(serialize_preparation)
        self._reserve_decoder_slot = bool(reserve_decoder_slot)
        self._decoder_lease_lock = threading.Lock()
        self._decoder_lease = None
        self._registration_lock = threading.Lock()
        self._registered_proactive = False
        self._quiesced = threading.Event()
        self._claimed = False
        self._clock_resolution_lock = threading.Lock()
        self._clock_resolution = None
        self._done = threading.Event()
        self._discarded = threading.Event()
        self._result_lock = threading.Lock()
        self._result = None
        self._failed = False
        self._success_evidence = None
        self._stage_lock = threading.Lock()
        self._stage = "source"
        self._thread = threading.Thread(target=self._run, daemon=True)
        if self._reserve_decoder_slot:
            registered = _register_proactive_preparation(self)
            with self._registration_lock:
                self._registered_proactive = registered
            if not registered:
                self._discarded.set()
                self._quiesced.set()
        else:
            self._quiesced.set()
        self._thread.start()

    def _set_decoder_lease(self, lease):
        with self._decoder_lease_lock:
            self._decoder_lease = lease

    def _release_decoder_lease(self):
        with self._decoder_lease_lock:
            lease, self._decoder_lease = self._decoder_lease, None
        if lease is not None:
            lease.release()

    def _unregister(self):
        with self._registration_lock:
            registered = self._registered_proactive
            self._registered_proactive = False
        if registered:
            _unregister_proactive_preparation(self)
        self._quiesced.set()

    def _track_clock_resolution(self, resolution):
        with self._clock_resolution_lock:
            self._clock_resolution = resolution

    def _cancel_clock_resolution(self):
        with self._clock_resolution_lock:
            resolution = self._clock_resolution
        if resolution is not None:
            resolution.discard()

    def _quiesce_clock_resolution(self):
        with self._clock_resolution_lock:
            resolution = self._clock_resolution
        if resolution is not None:
            resolution.discard()
            resolution.join()
            with self._clock_resolution_lock:
                if self._clock_resolution is resolution:
                    self._clock_resolution = None

    def _set_stage(self, stage):
        with self._stage_lock:
            self._stage = str(stage)

    def stage(self):
        with self._stage_lock:
            return self._stage

    def diagnostic_state(self):
        """Return bounded, secret-free state used only for aggregate counts."""
        if self._stage_lock.acquire(timeout=_DIAGNOSTIC_LOCK_TIMEOUT_SECONDS):
            try:
                stage = self._stage
            finally:
                self._stage_lock.release()
        else:
            stage = "lock_busy"
        if stage not in _PROACTIVE_PREPARATION_STAGE_NAMES:
            stage = "lock_busy" if stage == "lock_busy" else "other"
        if self._result_lock.acquire(timeout=_DIAGNOSTIC_LOCK_TIMEOUT_SECONDS):
            try:
                claimed = self._claimed
            finally:
                self._result_lock.release()
        else:
            claimed = None
        return {
            "stage": stage,
            "claimed": None if claimed is None else bool(claimed),
            "done": self._done.is_set(),
            "discarded": self._discarded.is_set(),
            "quiesced": self._quiesced.is_set(),
        }

    def evidence(self):
        return self._success_evidence

    def _run(self):
        capture = None
        clock_resolution = None
        unclocked_frame_sequence = deque(maxlen=3)
        next_clock_retry = 0.0
        clock_attempted = False
        resolution_clock_source = None
        preparation_slot_acquired = False
        try:
            if self._serialize_preparation:
                self._set_stage("preparation_slot")
                while not (
                    self._stop_event.is_set() or self._discarded.is_set()
                ):
                    if _PROACTIVE_PREPARATION_SEMAPHORE.acquire(timeout=0.05):
                        preparation_slot_acquired = True
                        break
                if not preparation_slot_acquired:
                    raise RuntimeError("capture preparation stopped")
            self._set_stage("source")
            source = self._source_factory()
            capture_source = source
            self._set_stage("clock_source")
            clock_source = (
                source if self._clock_source_factory is None else None
            )
            # Until an exact clock succeeds, the only source known to belong
            # to this capture connection is the capture URL itself. Never
            # retain/promote a failed auxiliary clock session.
            prepared_clock_source = capture_source
            if self._reserve_decoder_slot:
                self._set_stage("decoder_slot")
                self._set_decoder_lease(acquire_auxiliary_decoder_slot(
                    urgent=False,
                    cancelled=lambda: (
                        self._stop_event.is_set()
                        or self._discarded.is_set()
                    ),
                ))
            self._set_stage("capture_open")
            capture = _call_with_supported_kwargs(
                self._capture_factory,
                source,
                cancel_event=self._discarded,
            )
            if self._clock_source_factory is not None:
                source = None
            if capture is None or not capture.isOpened():
                raise RuntimeError("capture open failed")

            latest = None
            self._set_stage("first_frame")
            while not (
                self._stop_event.is_set() or self._discarded.is_set()
            ):
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise RuntimeError("frame read failed")
                source_epoch = self._wall_time()
                source_monotonic = self._monotonic()
                position = None
                if self._capture_position_milliseconds is not None:
                    try:
                        position = self._capture_position_milliseconds(capture)
                    except (AttributeError, TypeError, ValueError):
                        position = None
                latest = (frame, source_epoch, source_monotonic, position)

                transport_clock = _capture_transport_media_clock(capture)
                if transport_clock is not None and position is not None:
                    self._set_stage("transport_clock_validation")
                    frame_clock = transport_clock.metadata_at(position)
                    if (
                        frame_clock is not None
                        and (
                            self._media_clock_validator is None
                            or self._media_clock_validator(
                                frame_clock, source_epoch
                            )
                        )
                    ):
                        with self._result_lock:
                            if self._discarded.is_set():
                                return
                            self._result = (
                                capture,
                                *latest,
                                transport_clock,
                                capture_source,
                                capture_source,
                            )
                            capture = None
                        self._set_stage("ready")
                        self._success_evidence = "exact_same_session_pts"
                        return

                if self._media_clock_factory is None:
                    with self._result_lock:
                        if self._discarded.is_set():
                            return
                        self._result = (
                            capture,
                            *latest,
                            None,
                            capture_source,
                            prepared_clock_source,
                        )
                        capture = None
                    self._success_evidence = "no_media_clock"
                    return
                if position is None:
                    self._set_stage("capture_position")
                    continue
                unclocked_frame_sequence.append((frame, position))
                if (
                    clock_resolution is None
                    and self._monotonic() >= next_clock_retry
                    and (
                        not clock_attempted
                        or len(unclocked_frame_sequence) == 3
                    )
                ):
                    if clock_source is None:
                        clock_source = (
                            self._clock_source_factory()
                            if self._clock_source_factory is not None
                            else source
                        )
                    clock_resolution = _AsyncMediaClockResolution(
                        self._media_clock_factory,
                        (
                            clock_source,
                            frame,
                            position,
                            self._media_frame_identity,
                        ),
                        {
                            "reference_sequence": tuple(
                                unclocked_frame_sequence
                            )
                            if len(unclocked_frame_sequence) == 3
                            else None,
                        },
                    )
                    resolution_clock_source = clock_source
                    self._track_clock_resolution(clock_resolution)
                    clock_attempted = True
                    clock_source = None
                    self._set_stage("clock_resolution")
                if clock_resolution is None:
                    # A failed exact match is retried after a short backoff.
                    # Keep draining frames without dereferencing an absent
                    # resolver while that bounded retry delay is active.
                    continue
                resolved, media_clock = clock_resolution.poll()
                if not resolved:
                    # Keep draining the replacement FIFO while its first exact
                    # frame is matched, so the prepared reader cannot build a
                    # hidden decoder backlog before handover.
                    continue
                if media_clock is None:
                    self._quiesce_clock_resolution()
                    clock_resolution = None
                    resolution_clock_source = None
                    next_clock_retry = self._monotonic() + 1.0
                    continue
                self._set_stage("clock_validation")
                frame, source_epoch, source_monotonic, position = latest
                frame_clock = media_clock.metadata_at(position)
                if frame_clock is None:
                    raise RuntimeError("media clock metadata failed")
                if (
                    self._media_clock_validator is not None
                    and not self._media_clock_validator(
                        frame_clock, source_epoch
                    )
                ):
                    # A decoder PTS discontinuity can occasionally map a
                    # genuine frame outside the fixed receipt-time trust
                    # window. Keep draining and establish a new exact anchor;
                    # never hand an invalid clock to the active reader.
                    self._quiesce_clock_resolution()
                    clock_resolution = None
                    resolution_clock_source = None
                    next_clock_retry = self._monotonic() + 1.0
                    continue
                prepared_clock_source = (
                    resolution_clock_source or capture_source
                )
                resolution_clock_source = None
                with self._result_lock:
                    if self._discarded.is_set():
                        return
                    self._result = (
                        capture,
                        frame,
                        source_epoch,
                        source_monotonic,
                        position,
                        media_clock,
                        capture_source,
                        prepared_clock_source,
                    )
                    capture = None
                self._set_stage("ready")
                self._success_evidence = (
                    "exact_fragment_sequence"
                    if getattr(media_clock, "anchor_match_frame_count", 1) == 3
                    else "exact_fragment_match"
                )
                return
            raise RuntimeError("capture preparation stopped")
        except Exception:
            self._failed = True
            self._set_stage("failed")
        finally:
            self._source_factory = None
            self._clock_source_factory = None
            self._capture_factory = None
            self._media_clock_factory = None
            self._media_clock_validator = None
            self._capture_position_milliseconds = None
            self._media_frame_identity = None
            if clock_resolution is not None:
                self._quiesce_clock_resolution()
            if capture is not None:
                capture.release()
            if preparation_slot_acquired:
                _PROACTIVE_PREPARATION_SEMAPHORE.release()
            with self._result_lock:
                retain_lease = (
                    self._result is not None
                    and not self._discarded.is_set()
                    and not self._failed
                )
                # Completion publication and retained ownership are one state
                # transition. take() and discard() both serialize on this same
                # lock, so neither can clear the result between the decision
                # and making _done visible.
                self._done.set()
            if not retain_lease:
                self._release_decoder_lease()
                self._unregister()

    def poll(self):
        if not self._done.is_set():
            return False, None, False
        with self._result_lock:
            return True, self._result, self._failed

    def take(self):
        with self._result_lock:
            result, self._result = self._result, None
            self._claimed = result is not None
            return result

    def adopt(self):
        """Promote a prepared capture after the old active reader is closed."""
        with self._result_lock:
            self._claimed = False
        self._release_decoder_lease()
        self._unregister()

    def discard(self):
        self._discarded.set()
        self._cancel_clock_resolution()
        capture = None
        with self._result_lock:
            if self._result is not None:
                capture = self._result[0]
                self._result = None
            claimed = self._claimed
        if capture is not None:
            capture.release()
        if self._done.is_set() and not claimed:
            self._release_decoder_lease()
            self._unregister()

    def join(self, timeout=None):
        self._thread.join(
            None if timeout is None else max(0.0, float(timeout))
        )

    def is_alive(self):
        return self._thread.is_alive()

    def wait_quiesced(self, timeout=None):
        return self._quiesced.wait(
            None if timeout is None else max(0.0, float(timeout))
        )

    def is_quiesced(self):
        return self._quiesced.is_set()


class _AsyncCaptureRestart:
    """Restart FFmpeg against the active in-memory signed HLS session.

    A local FIFO/decoder can terminate while the server-side KVS HLS session
    remains valid. Reopening the same connection-local capture URL and
    re-anchoring through its paired connection-local clock URL avoids minting
    any additional KVS session at the account client limit. The first
    replacement frame must still obtain an exact clock and pass the unchanged
    receipt-time validator before handoff.
    """

    def __init__(
        self,
        source,
        clock_source,
        capture_factory,
        media_clock_factory,
        media_clock_validator,
        recent_exact_media_anchors,
        prior_media_clock,
        prior_capture_position,
        capture_position_milliseconds,
        media_frame_identity,
        stop_event,
        wall_time,
        monotonic,
    ):
        self._source = source
        self._clock_source = clock_source
        self._capture_factory = capture_factory
        self._media_clock_factory = media_clock_factory
        self._media_clock_validator = media_clock_validator
        self._recent_exact_media_anchors = tuple(
            recent_exact_media_anchors or ()
        )
        self._restart_exact_frames = deque(maxlen=3)
        self._restart_fragment_frames = deque(maxlen=3)
        self._prior_media_clock = prior_media_clock
        self._prior_media_time_utc = None
        if (
            prior_media_clock is not None
            and prior_capture_position is not None
        ):
            prior_frame_clock = prior_media_clock.metadata_at(
                prior_capture_position
            )
            if isinstance(prior_frame_clock, dict):
                self._prior_media_time_utc = prior_frame_clock.get(
                    "media_timestamp_utc"
                )
        self._capture_position_milliseconds = capture_position_milliseconds
        self._media_frame_identity = media_frame_identity
        self._stop_event = stop_event
        self._wall_time = wall_time
        self._monotonic = monotonic
        self._done = threading.Event()
        self._discarded = threading.Event()
        self._clock_resolution_lock = threading.Lock()
        self._clock_resolution = None
        self._result_lock = threading.Lock()
        self._result = None
        self._failed = False
        self._success_evidence = None
        self._stage_lock = threading.Lock()
        self._stage = "capture_open"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _set_stage(self, stage):
        with self._stage_lock:
            self._stage = str(stage)

    def stage(self):
        with self._stage_lock:
            return self._stage

    def evidence(self):
        return self._success_evidence

    def _track_clock_resolution(self, resolution):
        with self._clock_resolution_lock:
            self._clock_resolution = resolution

    def _cancel_clock_resolution(self):
        with self._clock_resolution_lock:
            resolution = self._clock_resolution
        if resolution is not None:
            resolution.discard()

    def _quiesce_clock_resolution(self):
        with self._clock_resolution_lock:
            resolution = self._clock_resolution
        if resolution is not None:
            resolution.discard()
            resolution.join()
            with self._clock_resolution_lock:
                if self._clock_resolution is resolution:
                    self._clock_resolution = None

    def _run(self):
        capture = None
        clock_resolution = None
        source = self._source
        clock_source = self._clock_source
        resolution_sources = [source]
        if clock_source != source:
            resolution_sources.append(clock_source)
        resolution_source_index = 0
        try:
            self._set_stage("capture_open")
            capture = _call_with_supported_kwargs(
                self._capture_factory,
                source,
                cancel_event=self._discarded,
            )
            if capture is None or not capture.isOpened():
                raise RuntimeError("capture open failed")
            self._set_stage("first_frame")
            while not (
                self._stop_event.is_set() or self._discarded.is_set()
            ):
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise RuntimeError("frame read failed")
                source_epoch = self._wall_time()
                source_monotonic = self._monotonic()
                position = None
                if self._capture_position_milliseconds is not None:
                    try:
                        position = self._capture_position_milliseconds(capture)
                    except (AttributeError, TypeError, ValueError):
                        position = None
                if position is None:
                    self._set_stage("capture_position")
                    continue
                transport_clock = _capture_transport_media_clock(capture)
                if transport_clock is not None:
                    self._set_stage("transport_clock_validation")
                    frame_clock = transport_clock.metadata_at(position)
                    if (
                        frame_clock is not None
                        and (
                            self._media_clock_validator is None
                            or self._media_clock_validator(
                                frame_clock, source_epoch
                            )
                        )
                    ):
                        with self._result_lock:
                            if self._discarded.is_set():
                                return
                            self._result = (
                                capture,
                                frame,
                                source_epoch,
                                source_monotonic,
                                position,
                                transport_clock,
                                source,
                                source,
                            )
                            capture = None
                        self._set_stage("ready")
                        self._success_evidence = "exact_same_session_pts"
                        return
                self._restart_fragment_frames.append((frame, position))
                if (
                    self._recent_exact_media_anchors
                    and self._media_frame_identity is not None
                    and self._media_clock_validator is not None
                ):
                    self._set_stage("recent_exact_anchor")
                    target_identity = self._media_frame_identity(frame)
                    self._restart_exact_frames.append(
                        (target_identity, position)
                    )
                    restart_identities = tuple(
                        item[0] for item in self._restart_exact_frames
                    )
                    anchor_matches = []
                    if (
                        len(restart_identities) == 3
                        and len(set(restart_identities)) == 3
                    ):
                        anchors = self._recent_exact_media_anchors
                        for offset in range(max(0, len(anchors) - 2)):
                            window = anchors[offset:offset + 3]
                            if tuple(item[0] for item in window) != (
                                restart_identities
                            ):
                                continue
                            old_delta = float(window[-1][2]) - float(
                                window[0][2]
                            )
                            new_delta = float(
                                self._restart_exact_frames[-1][1]
                            ) - float(self._restart_exact_frames[0][1])
                            if (
                                old_delta > 0.0
                                and new_delta > 0.0
                                and math.isclose(
                                    old_delta,
                                    new_delta,
                                    rel_tol=0.0,
                                    abs_tol=1.0,
                                )
                            ):
                                anchor_matches.append(window)
                    if len(anchor_matches) == 1:
                        anchor_window = anchor_matches[0]
                        _, anchor_clock, anchor_position = anchor_window[-1]
                        reanchor = getattr(
                            anchor_clock,
                            "reanchor_from_exact_match",
                            None,
                        )
                        candidate_clock = (
                            None
                            if reanchor is None
                            else reanchor(anchor_position, position)
                        )
                        frame_clock = (
                            None
                            if candidate_clock is None
                            else candidate_clock.metadata_at(position)
                        )
                        if (
                            frame_clock is not None
                            and self._media_clock_validator(
                                frame_clock, source_epoch
                            )
                        ):
                            with self._result_lock:
                                if self._discarded.is_set():
                                    return
                                self._result = (
                                    capture,
                                    frame,
                                    source_epoch,
                                    source_monotonic,
                                    position,
                                    candidate_clock,
                                    source,
                                    clock_source,
                                )
                                capture = None
                            self._set_stage("ready")
                            self._success_evidence = "recent_exact_sequence"
                            return
                if self._media_clock_factory is None:
                    with self._result_lock:
                        if self._discarded.is_set():
                            return
                        self._result = (
                            capture,
                            frame,
                            source_epoch,
                            source_monotonic,
                            position,
                            None,
                            source,
                            clock_source,
                        )
                        capture = None
                    self._success_evidence = "no_media_clock"
                    return
                if (
                    clock_resolution is None
                    and (
                        not self._recent_exact_media_anchors
                        or len(self._restart_exact_frames) >= 3
                    )
                ):
                    clock_resolution = _AsyncMediaClockResolution(
                        self._media_clock_factory,
                        (
                            resolution_sources[resolution_source_index],
                            frame,
                            position,
                            self._media_frame_identity,
                        ),
                        {
                            "not_before_media_time_utc": (
                                self._prior_media_time_utc
                            ),
                            "urgent": True,
                            "reference_sequence": tuple(
                                self._restart_fragment_frames
                            )
                            if len(self._restart_fragment_frames) == 3
                            else None,
                        },
                    )
                    self._track_clock_resolution(clock_resolution)
                    self._set_stage("clock_resolution")
                if clock_resolution is None:
                    continue
                resolved, media_clock = clock_resolution.poll()
                if not resolved:
                    continue
                if media_clock is None:
                    self._quiesce_clock_resolution()
                    clock_resolution = None
                    if resolution_source_index + 1 < len(resolution_sources):
                        resolution_source_index += 1
                        continue
                    raise RuntimeError("media clock resolution failed")
                self._set_stage("clock_validation")
                frame_clock = media_clock.metadata_at(position)
                if frame_clock is None:
                    raise RuntimeError("media clock metadata failed")
                if (
                    self._media_clock_validator is not None
                    and not self._media_clock_validator(frame_clock, source_epoch)
                ):
                    continue
                with self._result_lock:
                    if self._discarded.is_set():
                        return
                    self._result = (
                        capture,
                        frame,
                        source_epoch,
                        source_monotonic,
                        position,
                        media_clock,
                        source,
                        resolution_sources[resolution_source_index],
                    )
                    capture = None
                self._set_stage("ready")
                self._success_evidence = (
                    "exact_fragment_sequence"
                    if getattr(media_clock, "anchor_match_frame_count", 1) == 3
                    else "exact_fragment_match"
                )
                return
            raise RuntimeError("capture restart stopped")
        except Exception:
            self._failed = True
            self._set_stage("failed")
        finally:
            self._source = None
            self._clock_source = None
            self._capture_factory = None
            self._media_clock_factory = None
            self._media_clock_validator = None
            self._recent_exact_media_anchors = None
            self._restart_exact_frames = None
            self._restart_fragment_frames = None
            self._prior_media_clock = None
            self._prior_media_time_utc = None
            self._capture_position_milliseconds = None
            self._media_frame_identity = None
            if clock_resolution is not None:
                self._quiesce_clock_resolution()
            if capture is not None:
                capture.release()
            self._done.set()

    def poll(self):
        if not self._done.is_set():
            return False, None, False
        with self._result_lock:
            return True, self._result, self._failed

    def take(self):
        with self._result_lock:
            result, self._result = self._result, None
            return result

    def discard(self):
        self._discarded.set()
        self._cancel_clock_resolution()
        capture = None
        with self._result_lock:
            if self._result is not None:
                capture = self._result[0]
                self._result = None
        if capture is not None:
            capture.release()

    def join(self, timeout=None):
        self._thread.join(
            None if timeout is None else max(0.0, float(timeout))
        )

    def is_alive(self):
        return self._thread.is_alive()


def _bounded_bytes(value, limit=32_768):
    """Return a bounded byte representation without copying a full frame."""
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, bytearray):
        raw = bytes(value)
    elif isinstance(value, memoryview):
        raw = value.tobytes()
    elif isinstance(value, str):
        raw = value.encode("utf-8", errors="replace")
    else:
        raw = repr(value).encode("utf-8", errors="replace")

    if len(raw) <= limit:
        return raw
    stride = max(1, math.ceil(len(raw) / limit))
    return raw[::stride][:limit]


def bounded_frame_identity(frame, axis_samples=64):
    """Fingerprint bounded, spatially distributed frame content.

    A full 2560x1920 BGR hash on every decoded frame would add substantial CPU
    and memory bandwidth.  This samples at most roughly ``axis_samples ** 2``
    pixels plus five small anchor patches and hashes only that bounded payload.
    Shape and dtype are part of the identity.
    """
    digest = hashlib.blake2b(digest_size=16)
    shape = tuple(getattr(frame, "shape", ()) or ())
    dtype = str(getattr(frame, "dtype", ""))
    digest.update(repr((shape, dtype)).encode("ascii", errors="replace"))

    if len(shape) >= 2 and hasattr(frame, "__getitem__"):
        height = max(1, int(shape[0]))
        width = max(1, int(shape[1]))
        samples = max(8, int(axis_samples))
        y_step = max(1, math.ceil(height / samples))
        x_step = max(1, math.ceil(width / samples))
        try:
            lattice = frame[::y_step, ::x_step]
            digest.update(_bounded_bytes(lattice.tobytes(), 32_768))

            patch_radius = 3
            for y, x in (
                (0, 0),
                (0, width - 1),
                (height - 1, 0),
                (height - 1, width - 1),
                (height // 2, width // 2),
            ):
                y0 = max(0, y - patch_radius)
                y1 = min(height, y + patch_radius + 1)
                x0 = max(0, x - patch_radius)
                x1 = min(width, x + patch_radius + 1)
                patch = frame[y0:y1, x0:x1]
                digest.update(_bounded_bytes(patch.tobytes(), 4_096))
            return digest.digest()
        except (AttributeError, IndexError, TypeError, ValueError):
            # Tests and alternate capture adapters may use non-array frames.
            # Fall back to a bounded representation without weakening the
            # production NumPy-frame path above.
            pass

    digest.update(_bounded_bytes(frame))
    return digest.digest()


def exact_frame_identity(frame):
    """Fingerprint every byte for clock matching, where collisions are unsafe."""
    digest = hashlib.blake2b(digest_size=32)
    shape = tuple(getattr(frame, "shape", ()) or ())
    dtype = str(getattr(frame, "dtype", ""))
    digest.update(repr((shape, dtype)).encode("ascii", errors="replace"))
    try:
        digest.update(memoryview(frame))
    except (TypeError, ValueError, BufferError):
        try:
            digest.update(frame.tobytes())
        except AttributeError:
            digest.update(_bounded_bytes(frame, limit=1_048_576))
    return digest.digest()


class LiveStreamReader:
    """Continuously read one renewable live source in a daemon thread.

    ``source_factory`` is invoked for every connection attempt.  For signed HLS
    sessions this is the important boundary: a reconnect always receives a new
    URL rather than retrying an expired one.
    """

    def __init__(
        self,
        source_factory,
        capture_factory,
        recovery,
        state_callback=None,
        frame_callback=None,
        wall_time=None,
        monotonic=None,
        frame_identity=None,
        media_clock_factory=None,
        media_clock_validator=None,
        media_clock_source_factory=None,
        capture_position_milliseconds=None,
        media_frame_identity=None,
        media_clock_retry_seconds=2.0,
        media_clock_initial_wait_seconds=0.05,
        media_clock_invalid_grace_seconds=2.0,
        frame_identity_history_size=256,
        duplicate_frame_limit=90,
        connection_max_age_seconds=None,
        connection_renewal_lead_seconds=15.0,
        connection_initial_renewal_delay_seconds=0.0,
        terminal_read_failover_seconds=8.0,
        reserve_proactive_decoder_slot=False,
    ):
        self.source_factory = source_factory
        self.capture_factory = capture_factory
        self.recovery = recovery
        self.state_callback = state_callback
        self.frame_callback = frame_callback
        self.wall_time = wall_time or time.time
        self.monotonic = monotonic or time.monotonic
        self.frame_identity = frame_identity or bounded_frame_identity
        self.media_clock_factory = media_clock_factory
        self.media_clock_validator = media_clock_validator
        self.media_clock_source_factory = media_clock_source_factory
        self.capture_position_milliseconds = capture_position_milliseconds
        self.media_frame_identity = media_frame_identity or exact_frame_identity
        self.media_clock_retry_seconds = max(
            0.1, float(media_clock_retry_seconds)
        )
        self.media_clock_initial_wait_seconds = max(
            0.0, min(0.25, float(media_clock_initial_wait_seconds))
        )
        invalid_grace = float(media_clock_invalid_grace_seconds)
        if not math.isfinite(invalid_grace) or not 0.0 <= invalid_grace <= 5.0:
            raise ValueError(
                "media_clock_invalid_grace_seconds must be between 0 and 5"
            )
        self.media_clock_invalid_grace_seconds = invalid_grace
        self.frame_identity_history_size = max(
            1, int(frame_identity_history_size)
        )
        self.duplicate_frame_limit = max(1, int(duplicate_frame_limit))
        if connection_max_age_seconds is None:
            self.connection_max_age_seconds = None
            self.connection_renewal_lead_seconds = 0.0
            self.connection_initial_renewal_delay_seconds = 0.0
        else:
            maximum_age = float(connection_max_age_seconds)
            if not math.isfinite(maximum_age) or maximum_age <= 0.0:
                raise ValueError("connection_max_age_seconds must be positive")
            self.connection_max_age_seconds = maximum_age
            renewal_lead = float(connection_renewal_lead_seconds)
            if not math.isfinite(renewal_lead) or renewal_lead < 0.0:
                raise ValueError("connection_renewal_lead_seconds must be nonnegative")
            self.connection_renewal_lead_seconds = min(
                renewal_lead, maximum_age / 2.0
            )
            initial_delay = float(connection_initial_renewal_delay_seconds)
            if not math.isfinite(initial_delay) or not 0.0 <= initial_delay <= 30.0:
                raise ValueError(
                    "connection_initial_renewal_delay_seconds must be between 0 and 30"
                )
            self.connection_initial_renewal_delay_seconds = initial_delay
        failover_seconds = float(terminal_read_failover_seconds)
        if (
            not math.isfinite(failover_seconds)
            or not 0.0 <= failover_seconds <= 10.0
        ):
            raise ValueError(
                "terminal_read_failover_seconds must be between 0 and 10"
            )
        self.terminal_read_failover_seconds = failover_seconds
        self.reserve_proactive_decoder_slot = bool(
            reserve_proactive_decoder_slot
        )

        self._condition = threading.Condition()
        self._stop_event = threading.Event()
        self._shutdown_deadline_lock = threading.Lock()
        self._shutdown_deadline = None
        self._pending_terminal_cleanups = []
        self._thread = None
        self._sequence = 0
        self._latest = None
        self._recent_frame_identities = deque()
        self._recent_frame_identity_set = set()
        self._recent_exact_media_anchors = deque(
            maxlen=min(64, self.frame_identity_history_size)
        )
        self._consecutive_duplicate_frames = 0
        self._last_transport_diagnostic = None
        self._capture_session_counter = 0

    def _new_capture_session_id(self):
        """Issue an unguessable id for one actual decoder/source lifetime."""
        self._capture_session_counter += 1
        return (
            "capture-v1-"
            f"{self._capture_session_counter:016x}-"
            f"{time.monotonic_ns():016x}-"
            f"{secrets.token_hex(16)}"
        )

    @staticmethod
    def _bind_capture_session(frame_media_clock, capture_session_id):
        if not isinstance(frame_media_clock, dict):
            return frame_media_clock
        bound = dict(frame_media_clock)
        raw_clock = bound.get("media_clock")
        if not isinstance(raw_clock, dict):
            return None
        raw_clock = dict(raw_clock)
        raw_clock["session_id"] = capture_session_id
        bound["media_clock"] = raw_clock
        return bound

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        with self._shutdown_deadline_lock:
            self._shutdown_deadline = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout=1.0):
        self.request_stop()
        self.join(timeout)

    def request_stop(self, deadline=None):
        if deadline is not None:
            deadline = float(deadline)
            if not math.isfinite(deadline):
                raise ValueError("shutdown deadline must be finite")
        with self._shutdown_deadline_lock:
            if deadline is not None:
                self._shutdown_deadline = (
                    deadline
                    if self._shutdown_deadline is None
                    else min(self._shutdown_deadline, deadline)
                )
        self._stop_event.set()
        with self._condition:
            self._condition.notify_all()

    def _shutdown_remaining(self):
        with self._shutdown_deadline_lock:
            deadline = self._shutdown_deadline
        if deadline is None:
            return None
        return max(0.0, deadline - self.monotonic())

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(
                None if timeout is None else max(0.0, float(timeout))
            )

    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def snapshot(self, after_sequence=0):
        """Return the newest unconsumed frame, or ``None`` when unchanged."""
        with self._condition:
            if self._latest is None or self._sequence <= int(after_sequence):
                return None
            frame, source_epoch, source_monotonic, media_clock = self._latest
            return {
                "sequence": self._sequence,
                "frame": frame,
                "source_epoch": source_epoch,
                "source_monotonic": source_monotonic,
                "media_clock": media_clock,
            }

    def wait_for_frame(self, after_sequence=0, timeout=None):
        """Wait for a new frame; primarily useful for focused runtime tests."""
        with self._condition:
            self._condition.wait_for(
                lambda: self._stop_event.is_set()
                or (self._latest is not None and self._sequence > int(after_sequence)),
                timeout=timeout,
            )
        return self.snapshot(after_sequence)

    def _notify(
        self, state, error=None, delay_seconds=0.0, method=None, stage=None,
        evidence=None,
    ):
        if self.state_callback:
            self.state_callback(
                state=state,
                error=error,
                failures=self.recovery.failures,
                delay_seconds=float(delay_seconds),
                method=method,
                stage=stage,
                evidence=evidence,
            )

    def _remember_frame_identity(self, identity):
        if len(self._recent_frame_identities) >= self.frame_identity_history_size:
            expired = self._recent_frame_identities.popleft()
            self._recent_frame_identity_set.discard(expired)
        self._recent_frame_identities.append(identity)
        self._recent_frame_identity_set.add(identity)

    def _prepare_replacement_capture(self, *, urgent=False):
        return _AsyncCapturePreparation(
            self.source_factory,
            self.media_clock_source_factory,
            self.capture_factory,
            self.media_clock_factory,
            self.media_clock_validator,
            self.capture_position_milliseconds,
            self.media_frame_identity,
            self._stop_event,
            self.wall_time,
            self.monotonic,
            serialize_preparation=not urgent,
            reserve_decoder_slot=(
                self.reserve_proactive_decoder_slot and not urgent
            ),
        )

    def _prepare_same_session_restart(
        self, source, clock_source, prior_media_clock, prior_capture_position
    ):
        return _AsyncCaptureRestart(
            source,
            clock_source,
            self.capture_factory,
            self.media_clock_factory,
            self.media_clock_validator,
            tuple(self._recent_exact_media_anchors),
            prior_media_clock,
            prior_capture_position,
            self.capture_position_milliseconds,
            self.media_frame_identity,
            self._stop_event,
            self.wall_time,
            self.monotonic,
        )

    def _recover_terminal_read(
        self, preparation, source, clock_source, prior_media_clock,
        prior_capture_position, started=None, failed_capture=None,
        active_clock_resolution=None,
    ):
        """Return one fully validated replacement within a strict time bound.

        A live FFmpeg FIFO can close on a single HLS discontinuity while the
        source itself is still available.  Keep the last trusted frame aging
        normally while a new signed session and exact media clock are prepared.
        No unclocked frame is published, and failure to validate inside this
        bound falls through to the normal reconnect/staleness path.
        """
        started = self.monotonic() if started is None else float(started)
        deadline = started + self.terminal_read_failover_seconds
        if self.terminal_read_failover_seconds <= 0.0:
            self._cleanup_capture_until(failed_capture, deadline)
            self._cleanup_candidate_until(preparation, deadline)
            self._cleanup_candidate_until(active_clock_resolution, deadline)
            return None

        old_capture_cleanup = (
            None
            if failed_capture is None
            else _start_terminal_cleanup(
                ("capture", id(failed_capture)), failed_capture.release
            )
        )
        active_clock_cleanup = (
            None
            if active_clock_resolution is None
            else _start_candidate_cleanup(active_clock_resolution)
        )
        prior_cleanups, self._pending_terminal_cleanups = (
            tuple(self._pending_terminal_cleanups),
            [],
        )
        terminal_window = _begin_terminal_recovery()
        try:
            return self._recover_terminal_read_admitted(
                preparation,
                source,
                clock_source,
                prior_media_clock,
                prior_capture_position,
                started,
                deadline,
                terminal_window.preparations,
                old_capture_cleanup,
                active_clock_cleanup,
                prior_cleanups,
            )
        finally:
            terminal_window.release()

    @staticmethod
    def _discard_and_quiesce(candidate):
        if candidate is None:
            return True
        cleanup = _start_candidate_cleanup(candidate)
        cleanup.wait()
        return cleanup.succeeded()

    def _track_terminal_cleanup(self, cleanup):
        self._pending_terminal_cleanups = [
            pending for pending in self._pending_terminal_cleanups
            if not pending.succeeded()
        ]
        if cleanup.succeeded():
            return
        if cleanup not in self._pending_terminal_cleanups:
            self._pending_terminal_cleanups.append(cleanup)

    def _wait_for_terminal_cleanups(self, timeout=None):
        cleanups, self._pending_terminal_cleanups = (
            self._pending_terminal_cleanups,
            [],
        )
        deadline = (
            None
            if timeout is None
            else self.monotonic() + max(0.0, float(timeout))
        )
        succeeded = True
        for cleanup in cleanups:
            remaining = (
                None
                if deadline is None
                else max(0.0, deadline - self.monotonic())
            )
            if not cleanup.wait(remaining):
                self._pending_terminal_cleanups.append(cleanup)
                succeeded = False
            else:
                succeeded = cleanup.succeeded() and succeeded
        return succeeded

    def _cleanup_candidate_until(self, candidate, deadline):
        if candidate is None:
            return True
        cleanup = _start_candidate_cleanup(candidate)
        cleanup.wait(max(0.0, deadline - self.monotonic()))
        if cleanup.succeeded():
            return True
        self._track_terminal_cleanup(cleanup)
        return False

    def _cleanup_capture_until(self, capture, deadline):
        if capture is None:
            return True
        cleanup = _start_terminal_cleanup(
            ("capture", id(capture)), capture.release
        )
        cleanup.wait(max(0.0, deadline - self.monotonic()))
        if cleanup.succeeded():
            return True
        self._track_terminal_cleanup(cleanup)
        return False

    def _cleanup_claimed_until(self, candidate, prepared, deadline):
        cleanup = self._start_claimed_cleanup(candidate, prepared)
        cleanup.wait(max(0.0, deadline - self.monotonic()))
        if cleanup.succeeded():
            return True
        self._track_terminal_cleanup(cleanup)
        return False

    def _start_claimed_cleanup(self, candidate, prepared):
        preparation_candidate = isinstance(
            candidate, _AsyncCapturePreparation
        )

        def release_claimed():
            error = None
            try:
                prepared[0].release()
            except Exception as exc:
                error = exc
            finally:
                if preparation_candidate:
                    candidate.adopt()
            if error is not None:
                raise error

        cleanup = _start_terminal_cleanup(
            ("claimed", id(candidate)), release_claimed
        )
        return cleanup

    def _recover_terminal_read_admitted(
        self,
        preparation,
        source,
        clock_source,
        prior_media_clock,
        prior_capture_position,
        started,
        deadline,
        proactive_preparations,
        old_capture_cleanup,
        active_clock_cleanup,
        prior_cleanups,
    ):
        """Recover while new proactive and normal auxiliary work is blocked."""

        def finish(result, outcome, method, stage, evidence=None):
            elapsed = max(0.0, self.monotonic() - started)
            self._notify(
                f"terminal_failover_{outcome}",
                delay_seconds=elapsed,
                method=method,
                stage=stage,
                evidence=evidence,
            )
            return result

        # A terminal replacement can safely reuse the two auxiliary GPU permits
        # only after proactive captures release theirs. Cancellation is global
        # because another camera may own the single serialized preparation.
        quiesced = _cancel_proactive_preparations(
            timeout=min(1.0, max(0.0, deadline - self.monotonic())),
            preparations=proactive_preparations,
        )
        old_capture_released = True
        if old_capture_cleanup is not None:
            old_capture_cleanup.wait(
                max(0.0, deadline - self.monotonic())
            )
            old_capture_released = old_capture_cleanup.succeeded()
            if not old_capture_released:
                self._track_terminal_cleanup(old_capture_cleanup)
        active_clock_released = True
        if active_clock_cleanup is not None:
            active_clock_cleanup.wait(
                max(0.0, deadline - self.monotonic())
            )
            active_clock_released = active_clock_cleanup.succeeded()
            if not active_clock_released:
                self._track_terminal_cleanup(active_clock_cleanup)
        prior_cleanups_released = True
        for cleanup in prior_cleanups:
            cleanup.wait(max(0.0, deadline - self.monotonic()))
            if not cleanup.succeeded():
                prior_cleanups_released = False
                self._track_terminal_cleanup(cleanup)
        if not old_capture_released:
            return finish(
                None,
                "failed",
                "same_session_restart",
                "old_capture_release",
            )
        if not active_clock_released:
            return finish(
                None,
                "failed",
                "same_session_restart",
                "active_clock_cleanup",
            )
        if not prior_cleanups_released:
            return finish(
                None,
                "failed",
                "same_session_restart",
                "prior_terminal_cleanup",
            )
        if not quiesced:
            return finish(
                None,
                "failed",
                "same_session_restart",
                "proactive_quiescence",
            )
        if self.monotonic() >= deadline:
            return finish(
                None,
                "failed",
                "same_session_restart",
                "preparation_deadline",
            )

        if source is not None and clock_source is not None:
            if preparation is not None:
                if not self._cleanup_candidate_until(
                    preparation, deadline
                ):
                    return finish(
                        None,
                        "failed",
                        "same_session_restart",
                        "proactive_cleanup",
                    )
            candidate = self._prepare_same_session_restart(
                source, clock_source, prior_media_clock,
                prior_capture_position,
            )
            method = "same_session_restart"
            may_start_fresh_attempt = True
        else:
            candidate = preparation or self._prepare_replacement_capture(
                urgent=True
            )
            method = (
                "proactive_replacement"
                if preparation is not None
                else "fresh_session_replacement"
            )
            may_start_fresh_attempt = preparation is not None
        # If an already-running proactive preparation fails at the same moment
        # as the active reader, permit one clean connection-local attempt. If
        # the terminal read itself started the preparation, never spin on fast
        # source failures and overload the session-mint API.
        while not self._stop_event.is_set():
            done, result, failed = candidate.poll()
            if done:
                if self.monotonic() >= deadline:
                    late_stage = candidate.stage()
                    self._cleanup_candidate_until(candidate, deadline)
                    return finish(
                        None, "failed", method,
                        f"deadline_exceeded:{late_stage}",
                    )
                if not failed and result is not None:
                    prepared = candidate.take()
                    if prepared is None:
                        self._cleanup_candidate_until(candidate, deadline)
                        return finish(
                            None, "failed", method, "result_ownership"
                        )
                    preparation_candidate = isinstance(
                        candidate, _AsyncCapturePreparation
                    )
                    if self.monotonic() >= deadline:
                        self._cleanup_claimed_until(
                            candidate, prepared, deadline
                        )
                        return finish(
                            None, "failed", method,
                            "deadline_exceeded:handover",
                        )
                    if preparation_candidate:
                        # Terminal callers close the failed active reader before
                        # entering recovery, so this capture fills that vacated
                        # slot and no longer consumes the auxiliary budget.
                        candidate.adopt()
                    return finish(
                        prepared, "succeeded", method,
                        candidate.stage(), candidate.evidence(),
                    )
                failed_stage = candidate.stage()
                if not self._cleanup_candidate_until(candidate, deadline):
                    return finish(
                        None, "failed", method, "candidate_cleanup"
                    )
                if (
                    not may_start_fresh_attempt
                    or self.monotonic() >= deadline
                ):
                    return finish(None, "failed", method, failed_stage)
                may_start_fresh_attempt = False
                candidate = self._prepare_replacement_capture(urgent=True)
                method = "fresh_session_replacement"
                continue

            remaining = deadline - self.monotonic()
            if remaining <= 0.0:
                timed_out_stage = candidate.stage()
                self._cleanup_candidate_until(candidate, deadline)
                return finish(None, "failed", method, timed_out_stage)
            self._stop_event.wait(min(0.01, remaining))

        self._cleanup_candidate_until(candidate, deadline)
        return finish(None, "stopped", method, candidate.stage())

    def _run(self):
        while not self._stop_event.is_set():
            if not self._wait_for_terminal_cleanups():
                self._notify(
                    "reconnecting",
                    error="terminal decoder cleanup failed",
                    delay_seconds=0.0,
                )
                while not self._stop_event.wait(0.5):
                    pass
                break
            now = self.monotonic()
            if not self.recovery.can_retry(now):
                delay = max(0.0, self.recovery.next_retry_monotonic - now)
                self._stop_event.wait(min(delay, 0.5))
                continue

            cap = None
            capture_source = None
            capture_clock_source = None
            proactive_preparation = None
            clock_resolution = None
            pending_clock_source = None
            try:
                # Keep signed URLs only in the connection-local stack. The
                # optional longer clock window is independent of the shortest
                # safe live-edge capture window. Never publish, log, or retain
                # either URL in reader state; renew both on every outer attempt.
                source = self.source_factory()
                capture_source = source
                clock_source = (
                    source
                    if self.media_clock_source_factory is None
                    else None
                )
                capture_clock_source = capture_source
                media_clock = None
                transport_recovery_active = False
                clock_resolution = None
                next_media_clock_retry = 0.0
                last_capture_position = None
                unclocked_frame_sequence = deque(maxlen=3)
                media_clock_attempted = False
                cap = _call_with_supported_kwargs(
                    self.capture_factory,
                    source,
                    cancel_event=self._stop_event,
                )
                if cap is None or not cap.isOpened():
                    raise RuntimeError("capture open failed")
                capture_session_id = self._new_capture_session_id()
                if self.media_clock_source_factory is not None:
                    source = None
                if self.media_clock_factory is None:
                    source = None

                connected = False
                has_trusted_media_clock = self.media_clock_factory is None
                invalid_clock_started = None
                connection_started = self.monotonic()
                renewal_deadline = (
                    None
                    if self.connection_max_age_seconds is None
                    else connection_started
                    + self.connection_max_age_seconds
                    + self.connection_initial_renewal_delay_seconds
                    - self.connection_renewal_lead_seconds
                )
                while not self._stop_event.is_set():
                    if (
                        connected
                        and self.connection_max_age_seconds is not None
                        and proactive_preparation is None
                        and self.monotonic() >= renewal_deadline
                    ):
                        proactive_preparation = (
                            self._prepare_replacement_capture()
                        )

                    prepared = None
                    prepared_owner = None
                    if proactive_preparation is not None:
                        done, _result, failed = proactive_preparation.poll()
                        if done:
                            if failed or _result is None:
                                # Preparation is deliberately off-path. A
                                # transient source/clock failure must not tear
                                # down a still-readable active session. Drop
                                # the failed helper, keep draining the current
                                # capture, and let the normal deadline retry.
                                proactive_preparation = None
                                renewal_deadline = self.monotonic() + 1.0
                            else:
                                prepared_owner = proactive_preparation
                                prepared = prepared_owner.take()
                                proactive_preparation = None

                    if prepared is None:
                        ret, frame = cap.read()
                        if not ret or frame is None:
                            # SIGTERM can arrive while OpenCV is blocked in the
                            # FIFO read.  Once it returns, cooperative shutdown
                            # owns cleanup; starting terminal recovery here
                            # creates a fresh decoder during process teardown.
                            if self._stop_event.is_set():
                                break
                            terminal_started = self.monotonic()
                            failed_capture, cap = cap, None
                            active_clock_resolution = clock_resolution
                            clock_resolution = None
                            pending_clock_source = None
                            prepared = self._recover_terminal_read(
                                proactive_preparation,
                                capture_source,
                                capture_clock_source,
                                media_clock,
                                last_capture_position,
                                started=terminal_started,
                                failed_capture=failed_capture,
                                active_clock_resolution=(
                                    active_clock_resolution
                                ),
                            )
                            proactive_preparation = None
                            if prepared is None:
                                raise RuntimeError("frame read failed")
                    if prepared is None:
                        source_epoch = None
                        source_monotonic = None
                        prepared_position = None
                        prepared_media_clock = None
                    else:
                        (
                            replacement,
                            frame,
                            source_epoch,
                            source_monotonic,
                            prepared_position,
                            prepared_media_clock,
                            prepared_source,
                            prepared_clock_source,
                        ) = prepared
                        previous, cap = cap, None
                        previous_release_errors = []

                        def release_previous():
                            try:
                                if previous is not None:
                                    previous.release()
                            except Exception as exc:
                                previous_release_errors.append(exc)
                                raise

                        # Normal handover still waits for the old writer before
                        # adopt() releases the replacement's auxiliary lease.
                        # Running that release in a reader-owned daemon task lets
                        # process shutdown interrupt the wait and destroy both
                        # old and claimed replacement captures concurrently.
                        previous_cleanup = _start_reader_owned_cleanup(
                            ("handover", id(previous)), release_previous
                        )
                        while not previous_cleanup.wait(0.01):
                            if self._stop_event.is_set():
                                break
                        if self._stop_event.is_set():
                            previous_cleanup = (
                                _promote_reader_owned_cleanup(
                                    previous_cleanup
                                )
                            )
                            self._track_terminal_cleanup(previous_cleanup)
                            if prepared_owner is not None:
                                replacement_cleanup = (
                                    self._start_claimed_cleanup(
                                        prepared_owner, prepared
                                    )
                                )
                            else:
                                replacement_cleanup = _start_terminal_cleanup(
                                    ("capture", id(replacement)),
                                    replacement.release,
                                )
                            self._track_terminal_cleanup(replacement_cleanup)
                            prepared_owner = None
                            break
                        if not previous_cleanup.succeeded():
                            # The new decoder cannot outlive its admission lease
                            # when the old active reader failed to close.
                            try:
                                replacement.release()
                            finally:
                                if prepared_owner is not None:
                                    prepared_owner.adopt()
                            if previous_release_errors:
                                raise previous_release_errors[0]
                            raise RuntimeError("old capture release failed")
                        cap = replacement
                        capture_session_id = self._new_capture_session_id()
                        if prepared_owner is not None:
                            prepared_owner.adopt()
                        if clock_resolution is not None:
                            # A proactive handover can beat an active exact
                            # matcher. Quarantine that superseded resolver so
                            # its signed source/frame refs remain visible to
                            # cleanup telemetry and shutdown waits for it.
                            cleanup = _start_candidate_cleanup(
                                clock_resolution
                            )
                            self._track_terminal_cleanup(cleanup)
                            clock_resolution = None
                            pending_clock_source = None
                        media_clock = prepared_media_clock
                        transport_recovery_active = False
                        capture_source = prepared_source
                        capture_clock_source = prepared_clock_source
                        has_trusted_media_clock = (
                            prepared_media_clock is not None
                            or self.media_clock_factory is None
                        )
                        invalid_clock_started = None
                        clock_resolution = None
                        unclocked_frame_sequence.clear()
                        media_clock_attempted = True
                        last_capture_position = prepared_position
                        connection_started = self.monotonic()
                        renewal_deadline = (
                            None
                            if self.connection_max_age_seconds is None
                            else connection_started
                            + self.connection_max_age_seconds
                            - self.connection_renewal_lead_seconds
                        )
                        # The old connection remained healthy until this
                        # atomic swap. Report the lifecycle event without
                        # downgrading a published streaming state to the
                        # cold-start/recovery-only "connected" state.
                        self._notify("renewed")

                    # Clock fallback evidence must follow the decoder's actual
                    # contiguous frame sequence. Collect it before publication
                    # duplicate suppression; otherwise A,B,B,C could be
                    # misrepresented as A,B,C and anchor the wrong occurrence.
                    capture_position = prepared_position
                    if (
                        capture_position is None
                        and self.capture_position_milliseconds is not None
                    ):
                        try:
                            capture_position = (
                                self.capture_position_milliseconds(cap)
                            )
                        except (AttributeError, TypeError, ValueError):
                            capture_position = None
                    transport_clock = _capture_transport_media_clock(cap)
                    transport_diagnostic = _capture_transport_diagnostic(cap)
                    if (
                        transport_diagnostic is not None
                        and transport_diagnostic
                        != self._last_transport_diagnostic
                    ):
                        self._notify(
                            "transport_diagnostic",
                            stage=transport_diagnostic,
                        )
                        self._last_transport_diagnostic = transport_diagnostic
                    transport_clock_lost = False
                    if transport_clock is not None:
                        if clock_resolution is not None:
                            cleanup = _start_candidate_cleanup(
                                clock_resolution
                            )
                            self._track_terminal_cleanup(cleanup)
                        clock_resolution = None
                        pending_clock_source = None
                        media_clock = transport_clock
                        transport_recovery_active = False
                        capture_clock_source = capture_source
                        unclocked_frame_sequence.clear()
                        media_clock_attempted = True
                    elif (
                        getattr(media_clock, "evidence_method", None)
                        == "exact_same_session_pts"
                    ):
                        # Transport evidence is per decoded frame. Never carry
                        # yesterday's exact PTS mapping across a missing,
                        # delayed, or malformed current sidecar record. Drop
                        # it immediately so this very frame can enter the
                        # established exact-pixel fallback/recovery path.
                        media_clock = None
                        capture_clock_source = capture_source
                        unclocked_frame_sequence.clear()
                        media_clock_attempted = False
                        next_media_clock_retry = 0.0
                        transport_clock_lost = True
                        transport_recovery_active = True
                        if proactive_preparation is None:
                            # A failed sidecar is connection-scoped and cannot
                            # repair itself. Start one bounded fresh capture in
                            # parallel with the additive pixel fallback instead
                            # of freezing trusted publication until the normal
                            # multi-minute proactive-renewal deadline.
                            proactive_preparation = (
                                self._prepare_replacement_capture()
                            )
                    if (
                        media_clock is not None
                        and capture_position is not None
                        and last_capture_position is not None
                        and capture_position < last_capture_position - 0.5
                    ):
                        # A discontinuity/PTS reset invalidates the prior UTC
                        # mapping. Re-match this exact frame before trusting it.
                        media_clock = None
                        if clock_resolution is not None:
                            cleanup = _start_candidate_cleanup(
                                clock_resolution
                            )
                            self._track_terminal_cleanup(cleanup)
                        clock_resolution = None
                        pending_clock_source = None
                        unclocked_frame_sequence.clear()
                        # A new PTS epoch gets one immediate exact-frame
                        # attempt; only an ambiguous/missing result escalates
                        # to the three-frame sequence fallback.
                        media_clock_attempted = False
                        next_media_clock_retry = 0.0
                    if capture_position is not None:
                        last_capture_position = capture_position
                        if media_clock is None:
                            unclocked_frame_sequence.append(
                                (frame, capture_position)
                            )

                    identity = self.frame_identity(frame)
                    if (
                        identity in self._recent_frame_identity_set
                        and transport_clock is None
                        and not transport_clock_lost
                        and not transport_recovery_active
                    ):
                        self._consecutive_duplicate_frames += 1
                        if (
                            self._consecutive_duplicate_frames
                            >= self.duplicate_frame_limit
                        ):
                            raise RuntimeError("repeated frame content")
                        continue

                    if source_epoch is None:
                        source_epoch = self.wall_time()
                    if source_monotonic is None:
                        source_monotonic = self.monotonic()

                    frame_media_clock = None
                    if media_clock is None and clock_resolution is not None:
                        resolved, candidate = clock_resolution.poll()
                        if resolved:
                            clock_resolution = None
                            media_clock = candidate
                            if media_clock is not None:
                                transport_recovery_active = False
                                capture_clock_source = (
                                    pending_clock_source or capture_source
                                )
                                unclocked_frame_sequence.clear()
                            pending_clock_source = None
                            if media_clock is None:
                                next_media_clock_retry = (
                                    self.monotonic()
                                    + self.media_clock_retry_seconds
                                )
                    if (
                        media_clock is None
                        and clock_resolution is None
                        and (
                            proactive_preparation is None
                            or transport_recovery_active
                        )
                        and self.media_clock_factory is not None
                        and capture_position is not None
                        and source_monotonic >= next_media_clock_retry
                        and (
                            not media_clock_attempted
                            or len(unclocked_frame_sequence) == 3
                        )
                    ):
                        clock_source = (
                            clock_source
                            if clock_source is not None
                            else (
                                self.media_clock_source_factory()
                                if self.media_clock_source_factory is not None
                                else source
                            )
                        )
                        clock_resolution = _AsyncMediaClockResolution(
                            self.media_clock_factory,
                            (
                                clock_source,
                                frame,
                                capture_position,
                                self.media_frame_identity,
                            ),
                            {
                                "reference_sequence": tuple(
                                    unclocked_frame_sequence
                                )
                                if len(unclocked_frame_sequence) == 3
                                else None,
                            },
                        )
                        pending_clock_source = clock_source
                        media_clock_attempted = True
                        clock_source = None
                        resolved, candidate = clock_resolution.poll(
                            self.media_clock_initial_wait_seconds
                        )
                        if resolved:
                            clock_resolution = None
                            media_clock = candidate
                            if media_clock is not None:
                                transport_recovery_active = False
                                capture_clock_source = (
                                    pending_clock_source or capture_source
                                )
                                unclocked_frame_sequence.clear()
                            pending_clock_source = None
                            if media_clock is None:
                                next_media_clock_retry = (
                                    self.monotonic()
                                    + self.media_clock_retry_seconds
                                )
                    if (
                        media_clock is not None
                        and capture_position is not None
                    ):
                        try:
                            frame_media_clock = media_clock.metadata_at(
                                capture_position
                            )
                        except (AttributeError, TypeError, ValueError):
                            frame_media_clock = None
                    if frame_media_clock is not None:
                        frame_media_clock = self._bind_capture_session(
                            frame_media_clock, capture_session_id
                        )
                    if (
                        frame_media_clock is not None
                        and self.media_clock_validator is not None
                        and not self.media_clock_validator(
                            frame_media_clock, source_epoch
                        )
                    ):
                        invalid_now = self.monotonic()
                        if (
                            has_trusted_media_clock
                            and self.connection_max_age_seconds is not None
                            and proactive_preparation is None
                        ):
                            # Begin validating a replacement immediately while
                            # the short glitch grace still lets the current
                            # exact anchor self-correct. Waiting until grace
                            # expired consumed recovery time needed by the
                            # unchanged 15-second freshness gate.
                            proactive_preparation = (
                                self._prepare_replacement_capture()
                            )
                        if (
                            has_trusted_media_clock
                            and self.media_clock_invalid_grace_seconds > 0.0
                        ):
                            if invalid_clock_started is None:
                                invalid_clock_started = invalid_now
                            if (
                                invalid_now - invalid_clock_started
                                < self.media_clock_invalid_grace_seconds
                            ):
                                # HLS discontinuities can produce a brief PTS
                                # excursion that self-corrects on subsequent
                                # frames. Discard it without throwing away the
                                # still-valid exact anchor.
                                continue
                        # Discard only the frame carrying the invalid mapping
                        # and re-anchor from a later exact frame. The last
                        # trusted published frame remains subject to the normal
                        # freshness deadline, so a persistent fault still
                        # fails closed as stale.
                        if (
                            has_trusted_media_clock
                            and self.connection_max_age_seconds is not None
                            and proactive_preparation is None
                        ):
                            # Same-session exact re-anchoring can remain stuck
                            # behind a wedged HLS reader. Validate a new signed
                            # session off-thread while the old capture is still
                            # drained. Publication stays frozen on the last
                            # trusted frame and the normal stale gate remains
                            # unchanged if hot recovery cannot finish in time.
                            proactive_preparation = (
                                self._prepare_replacement_capture()
                            )
                        media_clock = None
                        if clock_resolution is not None:
                            cleanup = _start_candidate_cleanup(
                                clock_resolution
                            )
                            self._track_terminal_cleanup(cleanup)
                        clock_resolution = None
                        pending_clock_source = None
                        capture_clock_source = capture_source
                        unclocked_frame_sequence.clear()
                        media_clock_attempted = False
                        next_media_clock_retry = (
                            self.monotonic()
                            + self.media_clock_retry_seconds
                        )
                        invalid_clock_started = None
                        continue
                    if self.media_clock_factory is not None:
                        if frame_media_clock is None and has_trusted_media_clock:
                            # Once this reader has published a trusted clock,
                            # never replace it with an unclocked frame during
                            # re-anchor. The retained frame still ages normally
                            # and becomes stale if recovery exceeds the bound.
                            continue
                        if frame_media_clock is not None:
                            has_trusted_media_clock = True
                            invalid_clock_started = None
                    if (
                        frame_media_clock is not None
                        and media_clock is not None
                        and capture_position is not None
                        and getattr(
                            media_clock, "evidence_method", None
                        ) != "exact_same_session_pts"
                    ):
                        self._recent_exact_media_anchors.append(
                            (
                                self.media_frame_identity(frame),
                                media_clock,
                                capture_position,
                            )
                        )
                    if identity not in self._recent_frame_identity_set:
                        self._remember_frame_identity(identity)
                    self._consecutive_duplicate_frames = 0
                    self.recovery.record_success()
                    if not connected:
                        connected = True
                        self._notify("connected")

                    with self._condition:
                        self._sequence += 1
                        self._latest = (
                            frame,
                            source_epoch,
                            source_monotonic,
                            frame_media_clock,
                        )
                        self._condition.notify_all()
                    if self.frame_callback is not None:
                        try:
                            self.frame_callback(
                                frame,
                                source_epoch,
                                source_monotonic,
                                frame_media_clock,
                            )
                        except Exception:
                            # UI publication is observational and must never
                            # tear down a trusted capture reader. Health will
                            # naturally become stale if callbacks keep failing.
                            pass
            except _ProactiveRenewal:
                # Keep the broadcaster in its current streaming state and the
                # latest trusted frame available while a fresh signed session
                # opens. Normal freshness/clock gates still fail closed if the
                # rotation takes too long.
                continue
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                # Capture/source factories should raise sanitized messages.  Do
                # not include the source URL in this status or in service logs.
                error = sanitize_source_error(exc)
                delay = self.recovery.record_failure(error, self.monotonic())
                self._notify("reconnecting", error=error, delay_seconds=delay)
                self._stop_event.wait(delay)
            finally:
                if clock_resolution is not None:
                    cleanup = _start_candidate_cleanup(clock_resolution)
                    self._track_terminal_cleanup(cleanup)
                if proactive_preparation is not None:
                    cleanup = _start_candidate_cleanup(proactive_preparation)
                    self._track_terminal_cleanup(cleanup)
                if cap is not None:
                    if self._stop_event.is_set():
                        cleanup = _start_terminal_cleanup(
                            ("capture", id(cap)), cap.release
                        )
                        self._track_terminal_cleanup(cleanup)
                    else:
                        cap.release()
        self._wait_for_terminal_cleanups(self._shutdown_remaining())
