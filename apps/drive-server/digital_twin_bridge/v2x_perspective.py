"""Per-vehicle V2X perspective: what each connected vehicle sends and what it hears.

A real onboard unit receives every BSM broadcast within radio range and keeps the latest
state per sender; it never sees a log.  This module rebuilds that view for every vehicle
the viewer lists: its own latest BSM (what it transmits) and the latest BSM of every other
transmitting vehicle within ``radius_m`` of it, with the distance and where that sender is
relative to its heading.  Positions come from CARLA (our cars, and the ghosts the adapter
spawns for the other sites), so the distance is the ground truth at the moment of the
snapshot; the BSM fields are what the receiver would decode.  The viewer connection pushes
a snapshot every ``interval_s`` and the client replaces its state in place.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, Optional, Tuple

DEFAULT_RADIUS_M = 300.0
MIN_RADIUS_M = 10.0
MAX_RADIUS_M = 5000.0
DEFAULT_INTERVAL_S = 0.5
MIN_INTERVAL_S = 0.2
MAX_INTERVAL_S = 5.0


def clamp_radius(value, default: float = DEFAULT_RADIUS_M) -> float:
    """A usable reception radius in metres: ``default`` when the value is not a number."""
    try:
        radius = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(radius):
        return float(default)
    return min(MAX_RADIUS_M, max(MIN_RADIUS_M, radius))


def clamp_interval(value, default: float = DEFAULT_INTERVAL_S) -> float:
    """A usable push interval in seconds."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(seconds):
        return float(default)
    return min(MAX_INTERVAL_S, max(MIN_INTERVAL_S, seconds))


def _wrap_deg(angle: float) -> float:
    return (angle + 180.0) % 360.0 - 180.0


def distance_and_bearing(from_pos, from_yaw, to_pos) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """2-D distance in metres, the bearing to ``to_pos`` in CARLA's yaw convention, and that
    bearing relative to ``from_yaw`` (0 = ahead, +90 = right, -90 = left, +-180 = behind).

    CARLA's frame is left-handed with y to the right of x, so a positive relative bearing is
    on the right-hand side.  Parts are None when a position is missing; a coincident point
    has distance 0 and no bearing.
    """
    if not from_pos or not to_pos:
        return None, None, None
    try:
        dx = float(to_pos[0]) - float(from_pos[0])
        dy = float(to_pos[1]) - float(from_pos[1])
    except (TypeError, ValueError, IndexError):
        return None, None, None
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        return 0.0, None, None
    bearing = _wrap_deg(math.degrees(math.atan2(dy, dx)))
    rel = _wrap_deg(bearing - float(from_yaw)) if isinstance(from_yaw, (int, float)) else None
    return round(dist, 1), round(bearing, 1), (round(rel, 1) if rel is not None else None)


def tx_payload(entry, latest, decoded, now: float) -> Optional[dict]:
    """What a vehicle transmits: its latest frame as the receiver would see it, or None."""
    if latest is None:
        return None
    raw, t_rx = latest
    feed = entry.feed or {}
    msg_type = "BSM"
    if decoded is not None and getattr(decoded, "msg_type", "unknown") != "unknown":
        msg_type = decoded.msg_type
    return {
        "msg_type": msg_type,
        "t_rx": round(float(t_rx), 3),
        "age_s": round(max(0.0, now - float(t_rx)), 2),
        "rate_hz": feed.get("rate_hz", 0.0),
        "count": feed.get("count", 0),
        "source": feed.get("source", "none"),
        "size": len(raw),
        "summary": getattr(decoded, "summary", None) if decoded is not None else None,
        "error": getattr(decoded, "error", None) if decoded is not None else None,
    }


def build_perspective(entries: Iterable, latest_by_key: Dict[str, tuple], decoded_by_key: Dict[str, object],
                      radius_m: float, now: float, interval_s: float = DEFAULT_INTERVAL_S) -> dict:
    """The snapshot message for the client.

    ``entries`` are the registry's listed vehicles (VehicleEntry), ``latest_by_key`` the hub's
    latest ``(raw, t_rx)`` per key and ``decoded_by_key`` the DecodedFrame per key where the
    frame could be decoded.  Every placed vehicle is a receiver; only vehicles with a frame
    are senders.  A vehicle never hears itself.
    """
    placed = [e for e in entries if getattr(e, "in_world", True) and e.pos]
    transmitting = {e.key: e for e in placed if latest_by_key.get(e.key)}
    vehicles = []
    for entry in placed:
        tx = tx_payload(entry, latest_by_key.get(entry.key), decoded_by_key.get(entry.key), now)
        heard = []
        for other in transmitting.values():
            if other.key == entry.key:
                continue
            dist, bearing, rel = distance_and_bearing(entry.pos, entry.yaw, other.pos)
            if dist is None or dist > radius_m:
                continue
            other_tx = tx_payload(other, latest_by_key.get(other.key), decoded_by_key.get(other.key), now) or {}
            heard.append({
                "key": other.key,
                "name": other.role or other.key,
                "actor_id": other.actor_id,
                "kind": other.kind,
                "org": other.org,
                "emergency": bool(other.emergency),
                "distance_m": dist,
                "bearing_deg": bearing,
                "rel_bearing_deg": rel,
                "msg_type": other_tx.get("msg_type", "BSM"),
                "age_s": other_tx.get("age_s"),
                "rate_hz": other_tx.get("rate_hz", 0.0),
                "source": other_tx.get("source", "none"),
                "summary": other_tx.get("summary"),
            })
        heard.sort(key=lambda r: (r["distance_m"], r["key"]))
        vehicles.append({
            "key": entry.key,
            "actor_id": entry.actor_id,
            "name": entry.role or entry.key,
            "kind": entry.kind,
            "subkind": entry.subkind,
            "org": entry.org,
            "emergency": bool(entry.emergency),
            "pos": entry.pos,
            "yaw": entry.yaw,
            "speed_mps": entry.speed_mps,
            "tx": tx,
            "rx": heard,
            "rx_count": len(heard),
        })
    return {
        "type": "v2x_perspective",
        "active": True,
        "ts": round(now, 3),
        "radius_m": float(radius_m),
        "interval_ms": int(round(interval_s * 1000)),
        "vehicles": vehicles,
        "count": len(vehicles),
        "transmitting": len(transmitting),
    }
