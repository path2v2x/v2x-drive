"""
Vehicle registry for the V2X message viewer.

Once every few ticks the registry lists the vehicles in the CARLA world and
classifies them:

* ``voices``  — a scenario vehicle owned by our organization (PATH-M-1, the
  PATH-BG background cars, or the browser ego when it opted into a slot);
* ``ghost``   — a scenario vehicle of another organization, cloned into our
  world by the DT adapter (VW-M-1, AMG-BG-n, ...);
* ``drive``   — this server's own actors (egos, traffic, dynamic, trajectory);
* ``other``   — anything else (HIL_Tool ``bkg`` cars, foreign sidecar clients).

Only vehicles that are part of the VOICES scenario (in the roster) or that have
a live message feed are listed in the viewer; the rest have no V2X messages
and are hidden on purpose.  Scenario senders that publish without a CARLA
actor in our world (a ghost the adapter has not spawned yet) are listed too,
from the feed alone.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from digital_twin_bridge.actor_ownership import DRIVE_ROLE_PREFIXES

logger = logging.getLogger(__name__)

EMERGENCY_BLUEPRINT_HINTS = ("ambulance", "firetruck", "police", "dodgecop")
# A sender that went silent stays listed as "stale" this long, then drops out
# (unless its CARLA actor is still a scenario vehicle in our world).
STALE_LIST_S = 60.0


def _feed_is_listed(feed: dict) -> bool:
    source = feed.get("source")
    if source in ("tena", "udp"):
        return True
    if source == "stale":
        age = feed.get("age_s")
        return age is None or age <= STALE_LIST_S
    return False


@dataclass
class VehicleEntry:
    key: str
    actor_id: Optional[int]
    role: str
    type_id: str
    kind: str
    subkind: str = ""
    org: str = ""
    bsm_id: str = ""
    pos: Optional[list] = None
    yaw: Optional[float] = None
    speed_mps: Optional[float] = None
    emergency: bool = False
    special_lights: Optional[bool] = None
    feed: dict = field(default_factory=dict)
    in_world: bool = True
    ego_of: Optional[int] = None   # id(session) when this is a browser ego

    def to_dict(self) -> dict:
        return {
            "key": self.key, "actor_id": self.actor_id, "role": self.role, "type_id": self.type_id,
            "kind": self.kind, "subkind": self.subkind, "org": self.org, "bsm_id": self.bsm_id,
            "pos": self.pos, "yaw": self.yaw, "speed_mps": self.speed_mps,
            "emergency": self.emergency, "special_lights": self.special_lights,
            "feed": self.feed, "in_world": self.in_world,
        }


def _org_from_key(key: str) -> str:
    """Scenario names are ORG-KIND-N (PATH-BG-1); synthetic keys have no org."""
    if ":" in key or "-" not in key:
        return ""
    return key.split("-")[0]


def _role_of(actor) -> str:
    try:
        return str(actor.attributes.get("role_name", "") or "")
    except Exception:
        return ""


def _speed_of(actor) -> Optional[float]:
    try:
        v = actor.get_velocity()
        return math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
    except Exception:
        return None


def _pose_of(actor):
    try:
        t = actor.get_transform()
        return [round(t.location.x, 2), round(t.location.y, 2), round(t.location.z, 2)], round(t.rotation.yaw, 1)
    except Exception:
        return None, None


class VehicleRegistry:
    def __init__(self, roster, hub, local_org: str = "PATH"):
        self.roster = roster
        self.hub = hub
        self.local_org = local_org
        self._entries: Dict[str, VehicleEntry] = {}
        self._signature: tuple = ()
        self.version = 0
        self.updated_at: float = 0.0
        self.last_refresh_ms: float = 0.0

    # ── refresh (runs in the tick executor, off the event loop) ──

    def refresh(self, world, session_egos: Optional[Dict[int, int]] = None,
                traffic_ids: Iterable[int] = (), dynamic_ids: Iterable[int] = ()) -> None:
        """Rebuild the table from the world.  ``session_egos`` maps actor id -> session marker."""
        started = time.perf_counter()
        session_egos = session_egos or {}
        traffic_ids = set(traffic_ids)
        dynamic_ids = set(dynamic_ids)
        try:
            actors = list(world.get_actors().filter("vehicle.*"))
        except Exception as exc:
            logger.debug("Registry refresh: get_actors failed: %s", exc)
            return
        now = time.time()
        entries: Dict[str, VehicleEntry] = {}
        seen_roles: Dict[str, int] = {}
        for actor in actors:
            role = _role_of(actor)
            type_id = str(getattr(actor, "type_id", "") or "")
            actor_id = int(getattr(actor, "id", 0) or 0)
            roster_entry = self.roster.get(role) if (self.roster is not None and role) else None
            if actor_id in session_egos:
                kind, subkind = ("voices", "ego") if roster_entry is not None else ("drive", "ego")
            elif roster_entry is not None:
                kind = "voices" if self.roster.is_local(roster_entry) else "ghost"
                subkind = "scenario"
            elif actor_id in traffic_ids or role == "autopilot":
                kind, subkind = "drive", "traffic"
            elif actor_id in dynamic_ids or role.startswith("dynamic_geofence"):
                kind, subkind = "drive", "dynamic"
            elif role.startswith("trajectory"):
                kind, subkind = "drive", "trajectory"
            elif any(role.startswith(p) for p in DRIVE_ROLE_PREFIXES):
                kind, subkind = "drive", "other"
            else:
                kind, subkind = "other", ""
            key = role if role else "actor:%d" % actor_id
            if key in seen_roles:
                key = "%s#%d" % (key, actor_id)
            seen_roles[key] = actor_id
            pos, yaw = _pose_of(actor)
            entry = VehicleEntry(
                key=key, actor_id=actor_id, role=role, type_id=type_id, kind=kind, subkind=subkind,
                org=(roster_entry.org if roster_entry else (self.local_org if kind == "drive" else "")),
                bsm_id=(roster_entry.bsm_id if roster_entry else ""),
                pos=pos, yaw=yaw, speed_mps=_speed_of(actor),
                emergency=any(h in type_id for h in EMERGENCY_BLUEPRINT_HINTS),
                ego_of=session_egos.get(actor_id),
            )
            entries[key] = entry
        # feed-only senders (scenario vehicles publishing without an actor here)
        if self.hub is not None:
            for key in self.hub.senders():
                if key in entries or key.startswith("udp:"):
                    continue
                feed = self.hub.sender_feed(key, now)
                if not _feed_is_listed(feed):
                    continue
                roster_entry = self.roster.get(key) if self.roster is not None else None
                kind = "ghost"
                if roster_entry is not None and self.roster.is_local(roster_entry):
                    kind = "voices"
                elif roster_entry is None and key.startswith("bsm:"):
                    kind = "other"
                entries[key] = VehicleEntry(
                    key=key, actor_id=None, role=key, type_id=(roster_entry.vehicle_type if roster_entry else ""),
                    kind=kind, subkind="feed-only",
                    org=(roster_entry.org if roster_entry else _org_from_key(key)),
                    bsm_id=(roster_entry.bsm_id if roster_entry else ""),
                    in_world=False,
                )
        # join with the feed and the LandVehicle state
        for entry in entries.values():
            if self.hub is not None:
                entry.feed = self.hub.sender_feed(entry.key, now)
                state = self.hub.vehicle_state(entry.key)
                if state is not None:
                    entry.special_lights = state.special_lights
                    if not entry.org and state.org:
                        entry.org = state.org
                    if entry.pos is None and state.x is not None:
                        entry.pos = [round(state.x, 2), round(-state.y, 2), round(state.z or 0.0, 2)]
                    if entry.speed_mps is None:
                        entry.speed_mps = state.speed_mps
            else:
                entry.feed = {"source": "none", "last_rx": None, "rate_hz": 0.0, "count": 0, "age_s": None}
            if entry.special_lights:
                entry.emergency = True
        signature = tuple(sorted((e.key, e.kind, e.actor_id or 0, e.feed.get("source")) for e in entries.values()))
        if signature != self._signature:
            self._signature = signature
            self.version += 1
        self._entries = entries
        self.updated_at = now
        self.last_refresh_ms = (time.perf_counter() - started) * 1000.0

    # ── queries (event loop) ──

    def listed(self) -> List[VehicleEntry]:
        """Vehicles the viewer shows: scenario vehicles and anything with a feed."""
        out = []
        for e in self._entries.values():
            if (e.kind in ("voices", "ghost") and e.in_world) or _feed_is_listed(e.feed):
                out.append(e)
        order = {"voices": 0, "ghost": 1, "drive": 2, "other": 3}
        out.sort(key=lambda e: (order.get(e.kind, 9), e.key))
        return out

    def all_entries(self) -> List[VehicleEntry]:
        return list(self._entries.values())

    def get(self, key: str) -> Optional[VehicleEntry]:
        return self._entries.get(key)

    def actor_for_key(self, key: str) -> Optional[int]:
        e = self._entries.get(key)
        return e.actor_id if e else None

    def reset(self) -> None:
        self._entries = {}
        self._signature = ()
        self.version += 1
