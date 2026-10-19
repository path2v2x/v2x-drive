"""
V2X message feed for the viewer: where the J2735 messages come from.

Two taps, both local to the RFS host:

* ``JsonStreamSource`` — the DT JSON streamer (a TENA subscriber inside the
  dt-core container) connects to us as a TCP client and writes JSON records,
  each prefixed with the record separator byte 0x1E.  Records carry
  ``metadata.type_name`` and ``attributes``; we keep ``VUG::TV2XMsg::V2X``
  (raw UPER bytes + ``senderIdentifier``), ``VUG::Entities::LandVehicle``
  (position, ``specialLightsOn``) and ``VUG::Configuration::Scenario``.
* ``UdpUperSource`` — the TENA V2X adapter's raw UDP datagrams (one UPER
  MessageFrame each, no sender).  Used per sender only when the JSON stream is
  silent for that vehicle, matched to a name through the scenario roster.

``V2xHub`` fans messages out to per-connection subscriptions with bounded
queues, tracks per-sender liveness and rates, and can relaunch the DT streamer
when it is missing (``StreamerKeeper``).
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Set

from digital_twin_bridge.j2735_codec import MSG_TYPES, peek_bsm_id, peek_message_id

logger = logging.getLogger(__name__)

RECORD_SEPARATOR = b"\x1e"
TYPE_V2X = "VUG::TV2XMsg::V2X"
TYPE_SECURED_V2X = "VUG::TV2XMsg::SecuredV2X"
TYPE_LAND_VEHICLE = "VUG::Entities::LandVehicle"
TYPE_SCENARIO = "VUG::Configuration::Scenario"


@dataclass
class FeedMessage:
    key: str                 # vehicle name (senderIdentifier / role_name) or "bsm:<hex>"
    msg_type: str            # "BSM", "SDSM", ...
    raw: bytes
    source: str              # "tena" | "udp"
    t_rx: float
    uuid: Optional[str] = None
    sender: Optional[str] = None
    meta: dict = field(default_factory=dict)
    seq: int = 0


@dataclass
class VehicleState:
    """Last LandVehicle object state seen for a name (from the JSON stream)."""
    key: str
    t_rx: float
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    speed_mps: Optional[float] = None
    special_lights: Optional[bool] = None
    org: str = ""
    removed: bool = False


def _dig(obj, *path):
    cur = obj
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def binary_content_bytes(value) -> bytes:
    """The streamer renders ``binaryContent`` as a list of {"index": byte} dicts."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        try:
            return bytes.fromhex(value)
        except ValueError:
            return b""
    out = bytearray()
    for item in value or []:
        if isinstance(item, dict):
            item = next(iter(item.values()), 0)
        try:
            out.append(int(item) & 0xFF)
        except (TypeError, ValueError):
            out.append(0)
    return bytes(out)


def parse_v2x_record(record: dict, t_rx: float) -> Optional[FeedMessage]:
    attrs = record.get("attributes") or {}
    raw = binary_content_bytes(attrs.get("binaryContent"))
    if not raw:
        return None
    meta = record.get("metadata") or {}
    msg_id = peek_message_id(raw)
    msg_type = attrs.get("messageType") or MSG_TYPES.get(msg_id, "unknown")
    sender = attrs.get("senderIdentifier") or None
    key = sender or ("bsm:%s" % (peek_bsm_id(raw) or "unknown"))
    return FeedMessage(
        key=key, msg_type=str(msg_type), raw=raw, source="tena", t_rx=t_rx,
        uuid=attrs.get("uuid"), sender=sender,
        meta={
            "publisher_endpoint": meta.get("publisher_endpoint"),
            "publisher_application_id": meta.get("publisher_application_id"),
            "time_of_transmission": meta.get("time_of_transmission"),
            "time_of_receipt": meta.get("time_of_receipt"),
        },
    )


def parse_land_vehicle_record(record: dict, t_rx: float) -> Optional[VehicleState]:
    attrs = record.get("attributes") or {}
    key = attrs.get("identifier")
    if not key:
        return None
    pos = _dig(attrs, "tspi", "attributes", "position", "attributes", "ltpENU_asTransmitted", "attributes") or {}
    vel = _dig(attrs, "tspi", "attributes", "velocity", "attributes", "ltpENU_asTransmitted", "attributes") or {}
    speed = None
    try:
        vx, vy = float(vel.get("vxInMetersPerSecond", 0.0)), float(vel.get("vyInMetersPerSecond", 0.0))
        speed = (vx * vx + vy * vy) ** 0.5
    except (TypeError, ValueError):
        pass
    lights = _dig(attrs, "landVehicleData", "attributes", "exteriorLights", "attributes") or {}
    meta = record.get("metadata") or {}
    return VehicleState(
        key=key, t_rx=t_rx,
        x=pos.get("xInMeters"), y=pos.get("yInMeters"), z=pos.get("zInMeters"),
        speed_mps=speed,
        special_lights=lights.get("specialLightsOn"),
        org=str(attrs.get("sourceIdentifier") or ""),
        removed=bool(meta.get("is_removed")) or meta.get("event_type") == "Destruction",
    )


class _RateWindow:
    """Message count over the last second, cheap enough for every message."""
    __slots__ = ("times",)

    def __init__(self):
        self.times: collections.deque = collections.deque()

    def add(self, t: float) -> None:
        self.times.append(t)
        cutoff = t - 1.0
        while self.times and self.times[0] < cutoff:
            self.times.popleft()

    def rate(self, now: float) -> float:
        cutoff = now - 1.0
        while self.times and self.times[0] < cutoff:
            self.times.popleft()
        return float(len(self.times))


@dataclass
class _SenderStats:
    count: int = 0
    last_rx: float = 0.0
    last_tena: float = 0.0
    last_udp: float = 0.0
    last_raw: bytes = b""
    last_raw_t: float = 0.0
    window: _RateWindow = field(default_factory=_RateWindow)
    msg_types: Dict[str, int] = field(default_factory=dict)


class V2xSubscription:
    def __init__(self, maxsize: int, detail: str = "compact"):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max(16, int(maxsize)))
        self.keys: Set[str] = set()
        self.detail = detail
        self.dropped = 0

    def push(self, msg: FeedMessage) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            self.dropped += 1


class V2xHub:
    """Fan-out point for feed messages; lives on the event loop thread."""

    def __init__(self, roster=None, stale_s: float = 3.0, queue_max: int = 2000):
        self.roster = roster
        self.stale_s = float(stale_s)
        self.queue_max = int(queue_max)
        self._subs: Set[V2xSubscription] = set()
        self._stats: Dict[str, _SenderStats] = {}
        self._vehicles: Dict[str, VehicleState] = {}
        self._seq = 0
        self.total = 0
        self.version = 0            # bumps when the set of senders changes
        self._sources: dict = {}

    # ── subscriptions ──

    def subscribe(self, detail: str = "compact") -> V2xSubscription:
        sub = V2xSubscription(self.queue_max, detail)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: V2xSubscription) -> None:
        self._subs.discard(sub)

    def wanted_keys(self) -> Set[str]:
        keys: Set[str] = set()
        for sub in self._subs:
            keys |= sub.keys
        return keys

    # ── ingest ──

    def publish(self, msg: FeedMessage) -> bool:
        """Route one message; returns False when it was suppressed as a duplicate."""
        stats = self._stats.get(msg.key)
        if stats is None:
            stats = self._stats[msg.key] = _SenderStats()
            self.version += 1
        if msg.source == "udp":
            if msg.t_rx - stats.last_tena < self.stale_s:
                stats.last_udp = msg.t_rx
                return False  # the JSON stream covers this sender
            if msg.raw == stats.last_raw and msg.t_rx - stats.last_raw_t < 0.05:
                return False
            stats.last_udp = msg.t_rx
        else:
            stats.last_tena = msg.t_rx
        self._seq += 1
        msg.seq = self._seq
        stats.count += 1
        stats.last_rx = msg.t_rx
        stats.last_raw = msg.raw
        stats.last_raw_t = msg.t_rx
        stats.window.add(msg.t_rx)
        stats.msg_types[msg.msg_type] = stats.msg_types.get(msg.msg_type, 0) + 1
        self.total += 1
        for sub in self._subs:
            if msg.key in sub.keys:
                sub.push(msg)
        return True

    def update_vehicle(self, state: VehicleState) -> None:
        if state.removed:
            self._vehicles.pop(state.key, None)
            return
        self._vehicles[state.key] = state

    def on_record(self, record: dict, t_rx: Optional[float] = None) -> None:
        """Dispatch one JSON-stream record (called on the loop thread)."""
        t_rx = t_rx or time.time()
        tname = (record.get("metadata") or {}).get("type_name")
        if tname in (TYPE_V2X, TYPE_SECURED_V2X):
            msg = parse_v2x_record(record, t_rx)
            if msg is not None:
                self.publish(msg)
        elif tname == TYPE_LAND_VEHICLE:
            state = parse_land_vehicle_record(record, t_rx)
            if state is not None:
                self.update_vehicle(state)
        elif tname == TYPE_SCENARIO and self.roster is not None:
            try:
                self.roster.merge_live(record.get("attributes") or {})
            except Exception as exc:
                logger.debug("Scenario merge failed: %s", exc)

    def on_datagram(self, raw: bytes, t_rx: Optional[float] = None) -> None:
        t_rx = t_rx or time.time()
        msg_id = peek_message_id(raw)
        msg_type = MSG_TYPES.get(msg_id, "unknown") if msg_id is not None else "unknown"
        key = None
        if msg_type == "BSM":
            bsm_id = peek_bsm_id(raw)
            entry = self.roster.by_bsm_id(bsm_id) if (self.roster is not None and bsm_id) else None
            key = entry.name if entry is not None else ("bsm:%s" % (bsm_id or "unknown"))
        else:
            key = "udp:%s" % msg_type
        self.publish(FeedMessage(key=key, msg_type=msg_type, raw=raw, source="udp", t_rx=t_rx))

    # ── queries ──

    def sender_feed(self, key: str, now: Optional[float] = None) -> dict:
        now = now or time.time()
        stats = self._stats.get(key)
        if stats is None or stats.last_rx == 0.0:
            return {"source": "none", "last_rx": None, "rate_hz": 0.0, "count": 0, "age_s": None}
        if now - stats.last_tena < self.stale_s:
            source = "tena"
        elif now - stats.last_udp < self.stale_s:
            source = "udp"
        else:
            source = "stale"
        return {
            "source": source,
            "last_rx": stats.last_rx,
            "age_s": round(now - stats.last_rx, 2),
            "rate_hz": round(stats.window.rate(now), 1),
            "count": stats.count,
            "types": dict(stats.msg_types),
        }

    def senders(self) -> list:
        return list(self._stats.keys())

    def vehicle_state(self, key: str) -> Optional[VehicleState]:
        return self._vehicles.get(key)

    def vehicle_states(self) -> Dict[str, VehicleState]:
        return dict(self._vehicles)

    def register_source(self, name: str, source) -> None:
        self._sources[name] = source

    def status(self) -> dict:
        return {
            "total": self.total,
            "senders": len(self._stats),
            "subscriptions": len(self._subs),
            "sources": {name: src.status() for name, src in self._sources.items()},
        }


class JsonStreamSource:
    """TCP listener for the DT JSON streamer (it connects to us)."""

    def __init__(self, host: str, port: int, loop: asyncio.AbstractEventLoop,
                 on_record: Callable[[dict, float], None]):
        self.host = host
        self.port = int(port)
        self._loop = loop
        self._on_record = on_record
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._server: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._state = {
            "listening": None, "connected": False, "peer": None, "since": None,
            "records": 0, "bytes": 0, "parse_errors": 0, "last_rx": None, "connections": 0,
        }
        self._window = _RateWindow()

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        self.port = srv.getsockname()[1]
        srv.listen(4)
        srv.settimeout(0.5)
        self._server = srv
        with self._lock:
            self._state["listening"] = "%s:%d" % (self.host, self.port)
        self._thread = threading.Thread(target=self._accept_loop, name="v2x-json-stream", daemon=True)
        self._thread.start()
        logger.info("V2X JSON stream: listening on %s:%d", self.host, self.port)

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass

    def status(self) -> dict:
        with self._lock:
            st = dict(self._state)
        st["records_per_s"] = round(self._window.rate(time.time()), 1)
        return st

    # ── thread side ──

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, peer = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._state["connections"] += 1
            threading.Thread(target=self._reader, args=(conn, peer), name="v2x-json-reader", daemon=True).start()

    def _reader(self, conn: socket.socket, peer) -> None:
        peer_text = "%s:%d" % (peer[0], peer[1])
        with self._lock:
            self._state.update({"connected": True, "peer": peer_text, "since": time.time()})
        logger.info("V2X JSON stream: streamer connected from %s", peer_text)
        conn.settimeout(1.0)
        buf = bytearray()
        try:
            while not self._stop.is_set():
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    # the streamer writes "<RS>{json}" per record, so the last
                    # record before a disconnect has no trailing separator
                    if buf.strip():
                        self._handle_part(bytes(buf), final=True)
                    break
                buf += data
                with self._lock:
                    self._state["bytes"] += len(data)
                if RECORD_SEPARATOR not in buf:
                    continue
                parts = buf.split(RECORD_SEPARATOR)
                buf = bytearray(parts.pop())  # last piece may be a partial record
                for part in parts:
                    self._handle_part(part)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                self._state.update({"connected": False, "peer": None})
            logger.info("V2X JSON stream: streamer %s disconnected", peer_text)

    def _handle_part(self, part: bytes, final: bool = False) -> None:
        text = part.strip()
        if not text:
            return
        try:
            record = json.loads(text.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            if final:
                logger.debug("V2X JSON stream: discarding %d bytes of a partial record at EOF", len(text))
                return
            with self._lock:
                self._state["parse_errors"] += 1
            return
        now = time.time()
        with self._lock:
            self._state["records"] += 1
            self._state["last_rx"] = now
        self._window.add(now)
        try:
            self._loop.call_soon_threadsafe(self._on_record, record, now)
        except RuntimeError:
            pass  # loop closed


class UdpUperSource(asyncio.DatagramProtocol):
    """Raw UPER datagrams from the TENA V2X adapter."""

    def __init__(self, host: str, port: int, on_datagram: Callable[[bytes, float], None]):
        self.host = host
        self.port = int(port)
        self._on_datagram = on_datagram
        self._transport = None
        self.count = 0
        self.last_rx: Optional[float] = None
        self._window = _RateWindow()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=(self.host, self.port))
        self.port = self._transport.get_extra_info("sockname")[1]
        logger.info("V2X UDP tap: listening on %s:%d", self.host, self.port)

    def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()

    def datagram_received(self, data: bytes, addr) -> None:
        now = time.time()
        self.count += 1
        self.last_rx = now
        self._window.add(now)
        try:
            self._on_datagram(bytes(data), now)
        except Exception as exc:
            logger.debug("UDP datagram handling failed: %s", exc)

    def status(self) -> dict:
        return {
            "bind": "%s:%d" % (self.host, self.port), "datagrams": self.count,
            "last_rx": self.last_rx, "datagrams_per_s": round(self._window.rate(time.time()), 1),
        }


class StreamerKeeper:
    """Relaunch the DT JSON streamer inside dt-core when DT runs without it.

    The streamer is a TCP client that exits when it cannot reach the listener,
    so a drive-server restart leaves a DT session without a streamer.  Every
    ``interval_s`` we check ``docker exec <container> pgrep`` and relaunch via
    DT's own launcher script when needed (never more than once a minute).
    """

    def __init__(self, container: str = "dt-core", interval_s: float = 20.0,
                 launcher: str = "$HOME/distributed-testing/scripts/run_scripts/start-json-streamer.sh"):
        self.container = container
        self.interval_s = float(interval_s)
        self.launcher = launcher
        self._last_launch = 0.0
        self.launches = 0
        self.last_check: Optional[dict] = None
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        if shutil.which("docker") is None:
            logger.info("StreamerKeeper: docker not available, disabled")
            return
        loop = asyncio.get_running_loop()
        while True:
            try:
                await loop.run_in_executor(None, self.check_once)
            except Exception as exc:
                logger.debug("StreamerKeeper check failed: %s", exc)
            await asyncio.sleep(self.interval_s)

    def _run_cmd(self, args, timeout=10.0):
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    def check_once(self) -> dict:
        result = {"dt_running": False, "streamer_running": False, "launched": False}
        ps = self._run_cmd(["docker", "ps", "--format", "{{.Names}}"])
        names = set(ps.stdout.split())
        result["dt_running"] = self.container in names
        if result["dt_running"]:
            pg = self._run_cmd(["docker", "exec", self.container, "pgrep", "-f", "jsonStreamer-VUG"])
            result["streamer_running"] = pg.returncode == 0
            if not result["streamer_running"] and time.time() - self._last_launch > 60.0:
                cmd = "source $HOME/start_scripts/setup-docker.sh >/dev/null 2>&1; %s" % self.launcher
                self._run_cmd(["docker", "exec", "-d", self.container, "bash", "-lc", cmd])
                self._last_launch = time.time()
                self.launches += 1
                result["launched"] = True
                logger.info("StreamerKeeper: relaunched the DT JSON streamer in %s", self.container)
        self.last_check = result
        return result

    def status(self) -> dict:
        return {"launches": self.launches, "last_check": self.last_check}
