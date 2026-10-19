"""
J2735 (2024) message decoding for the V2X message viewer.

Wraps the ``j2735_202409`` package (USDOT FHWA STOL, Apache-2.0; a pycrate
generated ASN.1 module).  pycrate objects are process-wide singletons and not
thread safe, so every decode runs on one dedicated thread under a lock.  The
server never decodes on the event loop: callers use :meth:`Codec.decode_async`
or :meth:`Codec.summarize_async`.

If the package is missing the codec degrades: ``available`` is False, raw
frames are still routed (``peek_message_id`` and ``peek_bsm_id`` are pure bit
reads) and the viewer shows hex only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# J2735 messageId values (DSRCmsgID) for the types the viewer labels.
MSG_TYPES = {
    18: "MAP",
    19: "SPaT",
    20: "BSM",
    22: "EVA",
    27: "RSA",
    29: "SRM",
    30: "SSM",
    31: "TIM",
    32: "PSM",
    41: "SDSM",
}

# coreData "unavailable" sentinels (J2735 2024)
_LAT_UNAVAILABLE = 900000001
_LONG_UNAVAILABLE = 1800000001
_ELEV_UNAVAILABLE = -4096
_SPEED_UNAVAILABLE = 8191
_HEADING_UNAVAILABLE = 28800


@dataclass
class DecodedFrame:
    msg_type: str
    decoded: Optional[dict]
    summary: Optional[dict]
    error: Optional[str] = None
    raw: bytes = field(default=b"", repr=False)


class _BitReader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def read(self, nbits: int) -> int:
        value = 0
        for _ in range(nbits):
            byte = self.data[self.pos >> 3]
            bit = (byte >> (7 - (self.pos & 7))) & 1
            value = (value << 1) | bit
            self.pos += 1
        return value


def peek_message_id(raw: bytes) -> Optional[int]:
    """Read the MessageFrame.messageId without a full decode.

    UPER MessageFrame: 1 extension bit, then messageId INTEGER (0..32767) in 15
    bits, so the id is the low 15 bits of the first two bytes.
    """
    if len(raw) < 2:
        return None
    return ((raw[0] << 8) | raw[1]) & 0x7FFF


def peek_bsm_id(raw: bytes) -> Optional[str]:
    """Read BSM coreData.id (4 octets, hex) without a full decode.

    Layout after the 2-byte messageId: the open-type length determinant (1 byte
    for frames under 128 bytes, 2 bytes with the top bit set otherwise), then
    BasicSafetyMessage: 1 extension bit, 2 optional bits (partII, regional),
    coreData.msgCnt INTEGER (0..127) in 7 bits, then id OCTET STRING SIZE(4).
    Returns None for anything that is not a BSM or is too short.
    """
    try:
        if peek_message_id(raw) != 20 or len(raw) < 9:
            return None
        pos = 2
        length_byte = raw[pos]
        pos += 1
        if length_byte & 0x80:
            pos += 1  # two-byte length determinant
        reader = _BitReader(raw, pos * 8)
        reader.read(1 + 2 + 7)  # extension, optionals, msgCnt
        ident = reader.read(32)
        return "%08x" % ident
    except (IndexError, ValueError):
        return None


def _enum_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _bitstring_any(value: Any, lo: int, hi: int) -> Optional[bool]:
    """True if any bit in [lo, hi) of a pycrate JSON bit string is set.

    pycrate renders BIT STRING values as a hex string ("80") in to_json().
    """
    if value is None:
        return None
    try:
        if isinstance(value, str):
            bits = bin(int(value, 16))[2:].zfill(len(value) * 4)
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            bits = bin(int(value[0]))[2:].zfill(int(value[1]))
        else:
            return None
        return any(b == "1" for b in bits[lo:hi])
    except (ValueError, TypeError):
        return None


def summarize_bsm(decoded: dict) -> dict:
    """Flatten a decoded BSM into the viewer's summary (scaled SI units)."""
    body = decoded.get("value", {}) if isinstance(decoded, dict) else {}
    core = body.get("coreData", {}) or {}
    lat_raw = core.get("lat")
    lon_raw = core.get("long")
    lat = lat_raw / 1e7 if isinstance(lat_raw, (int, float)) and lat_raw != _LAT_UNAVAILABLE else None
    lon = lon_raw / 1e7 if isinstance(lon_raw, (int, float)) and lon_raw != _LONG_UNAVAILABLE else None
    elev_raw = core.get("elev")
    speed_raw = core.get("speed")
    heading_raw = core.get("heading")
    size = core.get("size") or {}
    brakes = core.get("brakes") or {}
    summary = {
        "id": core.get("id"),
        "msg_cnt": core.get("msgCnt"),
        "sec_mark": core.get("secMark"),
        "lat": lat,
        "lon": lon,
        "elev_m": elev_raw / 10.0 if isinstance(elev_raw, (int, float)) and elev_raw != _ELEV_UNAVAILABLE else None,
        "speed_mps": speed_raw * 0.02 if isinstance(speed_raw, (int, float)) and speed_raw != _SPEED_UNAVAILABLE else None,
        "heading_deg": heading_raw * 0.0125 if isinstance(heading_raw, (int, float)) and heading_raw != _HEADING_UNAVAILABLE else None,
        "brakes": _bitstring_any(brakes.get("wheelBrakes"), 1, 5),
        "transmission": _enum_text(core.get("transmission")),
        "size_m": {"length": size.get("length"), "width": size.get("width")} if size else None,
        "lights": None,
        "coords": "unknown",
    }
    if lat is not None and lon is not None:
        # The PATH adapter (2.4.1) encodes positions relative to the scenario
        # origin (0, 0); real WGS-84 fixes are far from the null island.
        summary["coords"] = "origin-relative" if (abs(lat) < 1.0 and abs(lon) < 1.0) else "wgs84"
    lights = {"siren": None, "lights": None, "multi": None, "role": None, "response_type": None, "events": None}
    for ext in body.get("partII", []) or []:
        pid = ext.get("partII-Id")
        val = ext.get("partII-Value") or {}
        if pid == 1:  # SpecialVehicleExtensions
            alerts = val.get("vehicleAlerts") or {}
            lights["siren"] = _enum_text(alerts.get("sirenUse"))
            lights["lights"] = _enum_text(alerts.get("lightsUse"))
            lights["multi"] = _enum_text(alerts.get("multi"))
            lights["response_type"] = _enum_text(alerts.get("responseType"))
            lights["events"] = alerts.get("events")
        elif pid == 2:  # SupplementalVehicleExtensions
            details = val.get("classDetails") or {}
            lights["role"] = _enum_text(details.get("role"))
            summary["classification"] = val.get("classification")
    if any(v is not None for v in lights.values()):
        summary["lights"] = lights
        summary["emergency"] = lights.get("siren") == "inUse" and lights.get("lights") == "inUse"
    return summary


class Codec:
    """Thread-confined J2735 decoder with a per-frame cache."""

    def __init__(self, cache_size: int = 4096):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="j2735")
        self._lock = threading.Lock()
        self._cache: "OrderedDict[bytes, DecodedFrame]" = OrderedDict()
        self._cache_size = max(16, int(cache_size))
        self._frame_cls = None
        self._import_tried = False
        self.error: Optional[str] = None

    # ── availability ──

    def _ensure_loaded(self) -> bool:
        if self._frame_cls is not None:
            return True
        if self._import_tried:
            return False
        self._import_tried = True
        try:
            import j2735_202409 as j2735  # noqa: WPS433 (runtime import on purpose)

            self._frame_cls = j2735.MessageFrame.MessageFrame
            logger.info("J2735 decoder loaded (%s)", getattr(j2735, "__file__", "j2735_202409"))
            return True
        except Exception as exc:  # ImportError or a broken install
            self.error = "%s: %s" % (type(exc).__name__, exc)
            logger.warning("J2735 decoder unavailable: %s", self.error)
            return False

    @property
    def available(self) -> bool:
        with self._lock:
            return self._ensure_loaded()

    def status(self) -> dict:
        return {"available": self.available, "error": self.error}

    # ── decoding ──

    def decode(self, raw: bytes) -> DecodedFrame:
        """Decode synchronously (call from the codec thread or tests only)."""
        with self._lock:
            cached = self._cache.get(raw)
            if cached is not None:
                self._cache.move_to_end(raw)
                return cached
            result = self._decode_locked(raw)
            self._cache[raw] = result
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return result

    def _decode_locked(self, raw: bytes) -> DecodedFrame:
        msg_id = peek_message_id(raw)
        msg_type = MSG_TYPES.get(msg_id, "unknown") if msg_id is not None else "unknown"
        if not self._ensure_loaded():
            return DecodedFrame(msg_type, None, None, "decoder unavailable", raw)
        try:
            frame = self._frame_cls
            frame.from_uper(raw)
            decoded = json.loads(frame.to_json())
        except Exception as exc:
            return DecodedFrame(msg_type, None, None, "%s: %s" % (type(exc).__name__, str(exc)[:160]), raw)
        summary = summarize_bsm(decoded) if msg_type == "BSM" else {"message_id": decoded.get("messageId")}
        return DecodedFrame(msg_type, decoded, summary, None, raw)

    async def decode_async(self, raw: bytes) -> DecodedFrame:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.decode, raw)

    async def decode_many_async(self, frames: list) -> list:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: [self.decode(r) for r in frames])

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
