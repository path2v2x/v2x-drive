"""
Per-connection glue for the V2X message viewer.

Protocol (JSON over the drive WebSocket; replies are returned by
``handle``, pushes are sent by ``pump``):

  -> {"type": "v2x_list_vehicles"}
  <- {"type": "v2x_vehicle_list", "version": n, "feed": {...}, "vehicles": [...]}
     (also pushed whenever the list changes while the client watches)
  -> {"type": "v2x_subscribe", "keys": ["PATH-M-1", ...], "detail": "compact"|"full"}
  <- {"type": "v2x_subscribed", "keys": [...], "detail": "compact"}
  -> {"type": "v2x_unsubscribe", "keys": [...]}      (no keys = everything)
  <- {"type": "v2x_subscribed", "keys": [...]}
  -> {"type": "v2x_decode", "raw_hex": "0014..."}
  <- {"type": "v2x_decoded", "raw_hex": "...", "msg_type": "BSM", "decoded": {...}|null, "error": null|"..."}
  <- {"type": "v2x_messages", "dropped": 0, "batch": [{...}, ...]}   pushed, <= 10 per second

Per-vehicle perspective (what each connected vehicle sends and hears, see v2x_perspective.py):

  -> {"type": "v2x_perspective_subscribe", "radius_m": 300, "interval_ms": 500, "detail": "simple"|"full"}
  -> {"type": "v2x_perspective_set", "radius_m": 150, "detail": "full"}   (full adds the decoded J2735 body)
  <- {"type": "v2x_perspective", "active": true, "radius_m": 300, "interval_ms": 500, "vehicles": [...]}
     (the reply, and then pushed every interval while subscribed)
  -> {"type": "v2x_perspective_unsubscribe"}
  <- {"type": "v2x_perspective", "active": false, "vehicles": []}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from digital_twin_bridge.v2x_perspective import (
    DEFAULT_INTERVAL_S, DEFAULT_RADIUS_M, build_perspective, clamp_interval, clamp_radius,
)

logger = logging.getLogger(__name__)

WATCH_LEASE_S = 60.0
MAX_BATCH = 400
MAX_KEYS = 64


class V2xContext:
    """Server-wide objects the viewer needs; built once in drive_main."""

    def __init__(self, hub, registry, codec, roster, push_interval_s: float = 0.1, keeper=None,
                 json_source=None, udp_source=None):
        self.hub = hub
        self.registry = registry
        self.codec = codec
        self.roster = roster
        self.push_interval_s = max(0.02, float(push_interval_s))
        self.keeper = keeper
        self.json_source = json_source
        self.udp_source = udp_source

    def feed_status(self) -> dict:
        return {
            "decoder": self.codec.status() if self.codec is not None else {"available": False, "error": "no codec"},
            "json_stream": self.json_source.status() if self.json_source is not None else {"enabled": False},
            "udp": self.udp_source.status() if self.udp_source is not None else {"enabled": False},
            "roster": self.roster.status() if self.roster is not None else {},
            "hub": self.hub.status() if self.hub is not None else {},
            "keeper": self.keeper.status() if self.keeper is not None else None,
        }

    def vehicle_list_message(self) -> dict:
        reg = self.registry
        return {
            "type": "v2x_vehicle_list",
            "version": reg.version if reg is not None else 0,
            "ts": time.time(),
            "feed": self.feed_status(),
            "vehicles": [e.to_dict() for e in reg.listed()] if reg is not None else [],
        }


def message_payload(msg, decoded, detail: str) -> dict:
    """One viewer row for a feed message; ``decoded`` is a DecodedFrame or None."""
    row = {
        "key": msg.key,
        "seq": msg.seq,
        "t_rx": round(msg.t_rx, 3),
        "msg_type": msg.msg_type,
        "source": msg.source,
        "sender": msg.sender,
        "uuid": msg.uuid,
        "raw_hex": msg.raw.hex(),
        "size": len(msg.raw),
        "summary": None,
        "error": None,
        "tx": msg.meta.get("time_of_transmission") if msg.meta else None,
    }
    if decoded is not None:
        row["msg_type"] = decoded.msg_type if decoded.msg_type != "unknown" else msg.msg_type
        row["summary"] = decoded.summary
        row["error"] = decoded.error
        if detail == "full":
            row["decoded"] = decoded.decoded
    return row


class V2xViewerConn:
    def __init__(self, ctx: V2xContext, websocket):
        self.ctx = ctx
        self.ws = websocket
        self.sub = ctx.hub.subscribe() if (ctx is not None and ctx.hub is not None) else None
        self.detail = "compact"
        self.lease_until = 0.0
        self.sent_version = -1
        self._closed = False
        # per-vehicle perspective: None until the client subscribes
        self.persp_radius_m: Optional[float] = None
        self.persp_interval_s = DEFAULT_INTERVAL_S
        self.persp_full = False
        self._persp_last = 0.0

    async def perspective_snapshot(self) -> dict:
        """The current per-vehicle view: latest frame per listed vehicle, decoded, within the radius."""
        registry, hub = self.ctx.registry, self.ctx.hub
        entries = registry.listed() if registry is not None else []
        latest = {}
        for e in entries:
            got = hub.latest_raw(e.key)
            if got:
                latest[e.key] = got
        decoded_by_key = {}
        if latest and self.ctx.codec is not None:
            frames = list({raw: None for raw, _ in latest.values()}.keys())
            try:
                results = await self.ctx.codec.decode_many_async(frames)
                by_raw = dict(zip(frames, results))
                decoded_by_key = {k: by_raw.get(raw) for k, (raw, _) in latest.items()}
            except Exception as exc:
                logger.debug("perspective decode failed: %s", exc)
        radius = self.persp_radius_m if self.persp_radius_m is not None else DEFAULT_RADIUS_M
        return build_perspective(entries, latest, decoded_by_key, radius, time.time(), self.persp_interval_s,
                                 full=self.persp_full)

    # ── request/reply ──

    async def handle(self, msg: dict) -> Optional[dict]:
        mtype = msg.get("type", "")
        if self.ctx is None or self.ctx.hub is None:
            return {"type": "error", "message": "V2X feed is disabled on this server"}
        if mtype == "v2x_list_vehicles":
            self.lease_until = time.time() + WATCH_LEASE_S
            reply = self.ctx.vehicle_list_message()
            self.sent_version = reply["version"]
            return reply
        if mtype == "v2x_subscribe":
            keys = [str(k) for k in (msg.get("keys") or []) if str(k)]
            detail = "full" if msg.get("detail") == "full" else "compact"
            self.detail = detail
            self.sub.detail = detail
            for k in keys:
                if len(self.sub.keys) >= MAX_KEYS:
                    break
                self.sub.keys.add(k)
            self.lease_until = time.time() + WATCH_LEASE_S
            known = set(self.ctx.hub.senders()) | {e.key for e in self.ctx.registry.all_entries()}
            return {"type": "v2x_subscribed", "keys": sorted(self.sub.keys), "detail": detail,
                    "unknown": sorted(k for k in keys if k not in known)}
        if mtype == "v2x_unsubscribe":
            keys = [str(k) for k in (msg.get("keys") or [])]
            if keys:
                for k in keys:
                    self.sub.keys.discard(k)
            else:
                self.sub.keys.clear()
            return {"type": "v2x_subscribed", "keys": sorted(self.sub.keys), "detail": self.detail}
        if mtype in ("v2x_perspective_subscribe", "v2x_perspective_set"):
            if "radius_m" in msg or self.persp_radius_m is None:
                current = self.persp_radius_m if self.persp_radius_m is not None else DEFAULT_RADIUS_M
                self.persp_radius_m = clamp_radius(msg.get("radius_m"), current)
            if "interval_ms" in msg:
                try:
                    self.persp_interval_s = clamp_interval(float(msg["interval_ms"]) / 1000.0, self.persp_interval_s)
                except (TypeError, ValueError):
                    pass
            if "detail" in msg:
                self.persp_full = str(msg.get("detail")) == "full"
            self.lease_until = time.time() + WATCH_LEASE_S
            self._persp_last = time.time()
            return await self.perspective_snapshot()
        if mtype == "v2x_perspective_unsubscribe":
            self.persp_radius_m = None
            return {"type": "v2x_perspective", "active": False, "vehicles": [], "count": 0, "radius_m": None}
        if mtype == "v2x_decode":
            raw_hex = str(msg.get("raw_hex") or "")
            try:
                raw = bytes.fromhex(raw_hex)
            except ValueError:
                return {"type": "v2x_decoded", "raw_hex": raw_hex, "msg_type": "unknown", "decoded": None,
                        "error": "raw_hex is not hexadecimal"}
            if not raw:
                return {"type": "v2x_decoded", "raw_hex": raw_hex, "msg_type": "unknown", "decoded": None,
                        "error": "empty frame"}
            dec = await self.ctx.codec.decode_async(raw)
            return {"type": "v2x_decoded", "raw_hex": raw_hex, "msg_type": dec.msg_type,
                    "decoded": dec.decoded, "summary": dec.summary, "error": dec.error}
        return None

    # ── push loop ──

    async def pump(self) -> None:
        """Batch subscribed messages (<= 10 Hz), push perspective snapshots while subscribed, and
        push vehicle-list changes while watched."""
        if self.sub is None:
            return
        interval = self.ctx.push_interval_s
        last_list_check = 0.0
        while not self._closed:
            wait_s = 0.5 if self.persp_radius_m is None else min(0.5, self.persp_interval_s / 2.0)
            try:
                first = await asyncio.wait_for(self.sub.queue.get(), timeout=wait_s)
            except asyncio.TimeoutError:
                first = None
            if first is not None:
                await asyncio.sleep(interval)  # let a batch accumulate
                batch = [first]
                while len(batch) < MAX_BATCH:
                    try:
                        batch.append(self.sub.queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                frames = list({m.raw: None for m in batch}.keys())
                decoded_by_raw = {}
                if self.ctx.codec is not None:
                    try:
                        results = await self.ctx.codec.decode_many_async(frames)
                        decoded_by_raw = dict(zip(frames, results))
                    except Exception as exc:
                        logger.debug("decode batch failed: %s", exc)
                rows = [message_payload(m, decoded_by_raw.get(m.raw), self.detail) for m in batch]
                dropped, self.sub.dropped = self.sub.dropped, 0
                try:
                    await self.ws.send(json.dumps({"type": "v2x_messages", "dropped": dropped, "batch": rows}))
                except Exception:
                    break
            now = time.time()
            if self.persp_radius_m is not None and now - self._persp_last >= self.persp_interval_s:
                self._persp_last = now
                try:
                    await self.ws.send(json.dumps(await self.perspective_snapshot()))
                except Exception:
                    break
            if now - last_list_check >= 1.0:
                last_list_check = now
                watching = now < self.lease_until or bool(self.sub.keys)
                if watching and self.ctx.registry is not None and self.ctx.registry.version != self.sent_version:
                    reply = self.ctx.vehicle_list_message()
                    self.sent_version = reply["version"]
                    try:
                        await self.ws.send(json.dumps(reply))
                    except Exception:
                        break

    def close(self) -> None:
        self._closed = True
        if self.sub is not None and self.ctx is not None and self.ctx.hub is not None:
            self.ctx.hub.unsubscribe(self.sub)
