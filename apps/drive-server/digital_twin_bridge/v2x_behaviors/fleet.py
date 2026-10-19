"""Server-owned fleet of V2X-reactive vehicles (civilians and ERVs).

The fleet lives in the drive server runtime, survives browser reconnects
and ticks right after ``world.tick()``: one vehicle table per tick (every
CARLA vehicle, ours or not), the ERV tracker fed from the viewer's TENA
feed and from CARLA light states, one behaviour decision per vehicle at
10 Hz (round robin), one control per vehicle at 20 Hz applied as a batch.
Vehicles carry ``drive_civ_<n>`` / ``drive_erv_<n>`` role names, so the
drive server's ownership rules treat them as ours.  A vehicle spawned under
a VOICES scenario slot (``role="PATH-M-1"``) carries that name instead, is
published by the DT adapter, and is parked rather than destroyed when it is
removed, because the adapter binds a name to one actor per DT session
(see ``voices_slots``).
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from digital_twin_bridge.v2x_behaviors import road_context as rc
from digital_twin_bridge.v2x_behaviors.bsm_types import light_mask
from digital_twin_bridge.v2x_behaviors.civilian_fsm import CivilianFsm, CivilianObservation, CivState, ErvRelation
from digital_twin_bridge.v2x_behaviors.erv_fsm import ErvFsm, ErvObservation, ErvState
from digital_twin_bridge.v2x_behaviors.erv_tracker import ErvTracker
from digital_twin_bridge.v2x_behaviors.feed_sources import HubErvSource, LightStateErvSource
from digital_twin_bridge.v2x_behaviors.geometry import INF, relative_position, same_direction
from digital_twin_bridge.v2x_behaviors.profiles import civilian_profile, erv_profile, profile_to_dict
from digital_twin_bridge.voices_slots import park_actor, reset_adopted_actor

logger = logging.getLogger(__name__)

ROLE_PREFIX = {"civilian": "drive_civ_", "erv": "drive_erv_"}
ERV_KINDS = {"erv", "emergency", "ambulance", "fire", "police"}
DEFAULT_SPEED_LIMIT_MPS = 11.1


@dataclass
class VehicleSnap:
    actor_id: int
    actor: object
    x: float
    y: float
    yaw: float
    speed: float
    ext_x: float
    ext_y: float
    wp: object = None
    is_fleet: bool = False
    kind: str = ""


@dataclass
class FleetVehicle:
    actor_id: int
    actor: object
    kind: str
    name: str
    blueprint: str
    profile: object
    agent: object
    fsm: object
    destination: object
    spawned_at: float
    decision: object = None
    stop: bool = False
    hard_stop: bool = False
    light_mask: int = 0
    lights_applied: Optional[int] = None
    doors_open: bool = False
    lane_change: Optional[dict] = None
    pass_job: Optional[dict] = None
    pass_block_until: float = 0.0
    pass_lead_id: Optional[int] = None
    junction_handling: Optional[int] = None
    arrived: bool = False
    transitions_sent: int = 0
    last_snap: Optional[VehicleSnap] = None
    last_obs: object = None
    last_control: object = None
    stuck_since: Optional[float] = None
    stuck_recoveries: int = 0
    slot: str = ""              # VOICES scenario slot this car publishes under, if any
    parked: bool = False        # a slot car taken off the road but kept for the adapter's binding
    parked_at: float = 0.0
    # After a teleport (adoption, stuck recovery) the client snapshot lags a tick or two: the
    # controller is built only after this tick and once the snapshot shows the car near the pose.
    bind_not_before: int = 0
    bind_pose: object = None

    def state(self) -> str:
        return "PARKED" if self.parked else self.fsm.state.value

    def summary(self, now: float) -> dict:
        d = self.decision
        snap = self.last_snap
        dest = self.destination
        dist = None
        if dest is not None and snap is not None:
            dist = round(math.hypot(dest.x - snap.x, dest.y - snap.y), 1)
        out = {
            "actor_id": self.actor_id,
            "name": self.name,
            "kind": self.kind,
            "profile": getattr(self.profile, "name", ""),
            "blueprint": self.blueprint,
            "state": self.state(),
            "reason": getattr(self.fsm, "last_reason", ""),
            "since_s": round(max(0.0, now - getattr(self.fsm, "t_state", now)), 1),
            "pos": [round(snap.x, 2), round(snap.y, 2)] if snap else None,
            "yaw": round(snap.yaw, 1) if snap else None,
            "speed_mps": round(snap.speed, 2) if snap else None,
            "target_speed_mps": round(d.target_speed_mps, 2) if d else None,
            "offset_m": round(d.lateral_offset_m, 2) if d else 0.0,
            "stop": bool(d.stop) if d else False,
            "destination": [round(dest.x, 1), round(dest.y, 1)] if dest is not None else None,
            "dist_to_destination_m": dist,
            "arrived": self.arrived,
            "lane_change": self.lane_change.get("direction") if self.lane_change else None,
            "passing": self.pass_job is not None,
            "spawned_at": self.spawned_at,
            "slot": self.slot,
            "parked": self.parked,
        }
        if self.kind == "erv":
            out.update({
                "siren": (bool(d.siren) if d else True) and not self.parked,
                "lights": (bool(d.lights) if d else True) and not self.parked,
                "response_type": d.response_type if d else "",
                "manual": self.fsm.state == ErvState.MANUAL,
                "doors_open": self.doors_open,
            })
        else:
            out.update({
                "lights": list(d.lights) if d else [],
                "erv_key": d.erv_key if d else None,
            })
        out["debug"] = self.debug()
        return out

    def debug(self) -> dict:
        """Why is this car doing what it does: plan length, obstacle stop, lane observation."""
        info = {"bound": self.agent is not None}
        try:
            info["plan_remaining"] = self.agent.remaining() if self.agent is not None else None
        except Exception:
            info["plan_remaining"] = None
        info["hazard"] = getattr(self.agent, "last_hazard", None)
        info["hazard_actor"] = getattr(self.agent, "last_hazard_actor", None)
        info["stop"] = self.stop
        info["hard_stop"] = self.hard_stop
        ctrl = getattr(self, "last_control", None)
        if ctrl is not None:
            info["sent"] = [round(float(getattr(ctrl, "throttle", 0.0)), 2), round(float(getattr(ctrl, "brake", 0.0)), 2),
                            round(float(getattr(ctrl, "steer", 0.0)), 2)]
        try:
            applied = self.actor.get_control()
            info["applied"] = [round(float(applied.throttle), 2), round(float(applied.brake), 2),
                               round(float(applied.steer), 2), bool(applied.hand_brake), int(getattr(applied, "gear", 0))]
        except Exception:
            pass
        try:
            lp = self.agent.get_local_planner()
            tw = lp.target_waypoint
            snap = self.last_snap
            if tw is not None and snap is not None:
                along, lateral = relative_position(snap.x, snap.y, snap.yaw, tw.transform.location.x, tw.transform.location.y)
                info["target_wp"] = [round(along, 1), round(lateral, 1), round(tw.transform.location.z - 0.0, 1)]
                info["z"] = round(float(self.actor.get_transform().location.z), 2)
        except Exception:
            pass
        obs = self.last_obs
        if obs is not None:
            for key in ("lead_gap_m", "lead_speed_mps", "dist_to_junction_m", "in_junction", "junction_control",
                        "junction_cleared", "conflicting_clear", "two_way", "opposing_clear", "sight_m",
                        "dist_to_destination_m", "same_dir_lanes", "lane_width_m", "speed_limit_mps"):
                if hasattr(obs, key):
                    val = getattr(obs, key)
                    info[key] = (round(val, 1) if isinstance(val, float) and val != INF else val)
            ervs = getattr(obs, "ervs", None)
            if ervs:
                info["ervs"] = [{"key": r.key, "gap": round(r.gap_m, 1), "lat": round(r.lateral_m, 1), "lane": r.same_lane,
                                 "road": r.same_road, "active": r.active, "stat": r.stationary} for r in ervs[:3]]
        snap = self.last_snap
        if snap is not None and snap.wp is not None:
            try:
                info["road"] = [int(snap.wp.road_id), int(snap.wp.lane_id), bool(getattr(snap.wp, "is_junction", False))]
            except Exception:
                pass
        return info


class Fleet:
    def __init__(self, world, carla_map, config=None, hub=None, codec=None, registry=None,
                 planner=None, agent_factory=None, client_factory=None, rng=None, now_fn=time.time,
                 slots=None):
        self._world = world
        self._map = carla_map
        self._config = config
        self._slots = slots  # voices_slots.SlotService; None = scenario slots unavailable
        self.max_vehicles = int(getattr(config, "FLEET_MAX_VEHICLES", 12) or 12)
        self.decision_every = max(1, int(getattr(config, "FLEET_DECISION_EVERY_N_TICKS", 2) or 2))
        self._base_decision_every = self.decision_every
        self.budget_ms = float(getattr(config, "FLEET_TICK_BUDGET_MS", 20.0) or 20.0)
        self.light_scan_every = max(1, int(getattr(config, "FLEET_LIGHT_SCAN_EVERY_N_TICKS", 10) or 10))
        sources = getattr(config, "fleet_erv_sources", None)
        if sources is None:
            raw = getattr(config, "FLEET_ERV_SOURCES", "bsm,lights,fleet")
            sources = frozenset(s.strip().lower() for s in str(raw).split(",") if s.strip())
        self.erv_sources = frozenset(sources)
        self._host = getattr(config, "CARLA_HOST", "localhost")
        self._port = int(getattr(config, "CARLA_PORT", 2000) or 2000)
        self._planner = planner
        self._agent_factory = agent_factory
        self._client_factory = client_factory
        self._client = None
        self._hub_source = HubErvSource(hub, codec, registry) if (hub is not None and codec is not None) else None
        self._light_source = LightStateErvSource()
        self.tracker = ErvTracker()
        self.gps_to_xy: Optional[Callable[[float, float], Tuple[float, float]]] = None
        self._vehicles: Dict[int, FleetVehicle] = {}
        self._lock = threading.RLock()
        self._counter = 0
        self._tick_i = 0
        self._actors_cache: list = []
        self._actors_cache_tick = -10_000
        self._ext_cache: Dict[int, Tuple[float, float]] = {}
        self._tick_ms: deque = deque(maxlen=200)
        self.degraded = False
        self._subs: List[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._now = now_fn
        self._rng = rng or random.Random()
        self.events_total = 0

    # ── wiring helpers ──

    def planner(self):
        if self._planner is None:
            from digital_twin_bridge.v2x_behaviors.route_planner import RoutePlanner
            self._planner = RoutePlanner(self._map, rng=self._rng)
        return self._planner

    def _make_agent(self, actor, speed_mps: float, offset_m: float):
        if self._agent_factory is not None:
            return self._agent_factory(actor, self._map, self.planner(), speed_mps, offset_m)
        from digital_twin_bridge.v2x_behaviors.controllers import make_agent
        return make_agent(actor, self._map, self.planner().grp(), speed_mps, offset_m)

    def update_world(self, world, carla_map) -> None:
        """After a map switch: forget everything bound to the old world."""
        with self._lock:
            self._vehicles.clear()
            self._world = world
            self._map = carla_map
            self._planner = None
            self._actors_cache = []
            self._actors_cache_tick = -10_000
            self._ext_cache.clear()
            self.tracker = ErvTracker()

    # ── pub/sub (events pushed to every connected browser) ──

    def subscribe(self) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._loop = loop
            self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def _broadcast(self, event: dict) -> None:
        self.events_total += 1
        with self._lock:
            loop, subs = self._loop, list(self._subs)
        if loop is None or not subs:
            return
        for q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except RuntimeError:
                pass

    # ── queries ──

    @property
    def has_vehicles(self) -> bool:
        """Any vehicle on the road (parked slot cars do not count)."""
        return any(not fv.parked for fv in self._vehicles.values())

    def active_count(self) -> int:
        return sum(1 for fv in self._vehicles.values() if not fv.parked)

    def actor_ids(self) -> set:
        return set(self._vehicles.keys())

    def kind_of(self, actor_id: int) -> Optional[str]:
        fv = self._vehicles.get(actor_id)
        return fv.kind if fv else None

    def erv_tracks(self, now: Optional[float] = None) -> List[dict]:
        now = now or self._now()
        return [t.to_dict(now) for t in self.tracker.tracks() if t.positioned]

    def compact_list(self) -> List[dict]:
        out = []
        for fv in list(self._vehicles.values()):
            s = fv.last_snap
            out.append({
                "actor_id": fv.actor_id, "kind": fv.kind, "name": fv.name, "state": fv.state(),
                "pos": [round(s.x, 2), round(s.y, 2)] if s else None, "yaw": round(s.yaw, 1) if s else None,
                "speed_mps": round(s.speed, 2) if s else None, "parked": fv.parked,
            })
        return out

    def stats(self) -> dict:
        ms = sorted(self._tick_ms)
        p95 = ms[int(len(ms) * 0.95) - 1] if len(ms) >= 20 else (ms[-1] if ms else 0.0)
        active = self.active_count()
        return {
            "count": active,
            "parked": len(self._vehicles) - active,
            "max": self.max_vehicles,
            "tick_ms_avg": round(sum(ms) / len(ms), 2) if ms else 0.0,
            "tick_ms_p95": round(p95, 2),
            "budget_ms": self.budget_ms,
            "degraded": self.degraded,
            "decision_hz": round(20.0 / self.decision_every, 1),
            "tracks": len(self.tracker.tracks()),
            "events": self.events_total,
            "erv_sources": sorted(self.erv_sources),
        }

    def list(self) -> dict:
        now = self._now()
        with self._lock:
            return {
                "type": "v2x_vehicles",
                "vehicles": [fv.summary(now) for fv in self._vehicles.values()],
                "tracks": self.erv_tracks(now),
                "stats": self.stats(),
            }

    def status(self) -> dict:
        return self.stats()

    # ── spawning ──

    def spawn(self, kind: str = "civilian", blueprint: Optional[str] = None, profile: Optional[str] = None,
              spawn=None, destination="random", params: Optional[dict] = None, ego_actor=None,
              role: Optional[str] = None) -> dict:
        """Spawn a civilian or an ERV.

        ``role`` names a VOICES scenario slot (e.g. ``PATH-M-1``) the car publishes under:
        the slot is validated, and a car parked under that name earlier (by the fleet or by
        a browser session) is adopted and moved to the requested pose instead of spawning.
        """
        with self._lock:
            role = str(role or "").strip()
            kind_key = str(kind or "civilian").lower()
            is_erv = kind_key in ERV_KINDS
            kind = "erv" if is_erv else "civilian"
            if is_erv:
                prof = erv_profile(profile or ("ambulance" if kind_key in ("erv", "emergency") else kind_key), params)
            else:
                prof = civilian_profile(profile or "civilian", params)
            parked = None
            adopt = None
            if role:
                parked = next((fv for fv in self._vehicles.values() if fv.parked and fv.slot == role), None)
                if self._slots is not None:
                    adopt = self._slots.claim(self._world, role, by="fleet")
                if adopt is None and parked is not None:
                    adopt = parked.actor
            if self.active_count() >= self.max_vehicles:
                raise RuntimeError("fleet is full (%d vehicles)" % self.max_vehicles)
            bp_id = str(blueprint or prof.blueprint)
            bp_lib = self._world.get_blueprint_library()
            bp = bp_lib.find(bp_id) or bp_lib.find(prof.blueprint)
            if bp is None:
                fallback = bp_lib.filter("vehicle.lincoln") or bp_lib.filter("vehicle.")
                bp = fallback[0] if fallback else None
            if bp is None and adopt is None:
                raise RuntimeError("no vehicle blueprint for %s" % bp_id)
            if bp is not None:
                bp_id = getattr(bp, "id", bp_id)
            if role:
                name = role
            else:
                self._counter += 1
                name = ROLE_PREFIX[kind] + str(self._counter)
            if bp is not None:
                bp.set_attribute("role_name", name)
                if not is_erv and bp.has_attribute("color"):
                    colors = bp.get_attribute("color").recommended_values
                    if colors:
                        bp.set_attribute("color", self._rng.choice(list(colors)))
            base = self._resolve_spawn(spawn, ego_actor)
            actor = None
            cand = base
            for attempt in range(6):
                cand = base
                if attempt > 0:
                    cand = self.planner().transform_along_lane(base.location, 6.0 * attempt) or base
                if self._occupied(cand.location, 6.0, ignore_id=(adopt.id if adopt is not None else None)):
                    continue
                if adopt is not None:
                    reset_adopted_actor(adopt, cand)
                    actor = adopt
                    break
                actor = self._world.try_spawn_actor(bp, cand)
                if actor is not None:
                    break
            if actor is None:
                raise RuntimeError("could not spawn %s: no free spot near the requested pose" % bp_id)
            if adopt is not None:
                bp_id = str(getattr(actor, "type_id", "") or bp_id)
            now = self._now()
            # The client reports a freshly spawned actor at the origin until the
            # next world tick, so use the spawn pose here and bind the agent
            # (which reads the vehicle's location) on the first tick that lists it.
            try:
                dest = self._resolve_destination(destination, cand.location)
            except Exception:
                if adopt is None:
                    try:
                        actor.destroy()
                    except Exception:
                        pass
                raise
            if parked is not None:
                self._vehicles.pop(parked.actor_id, None)
            fsm = ErvFsm(prof) if is_erv else CivilianFsm(prof)
            fsm.t_state = now
            fv = FleetVehicle(actor_id=actor.id, actor=actor, kind=kind, name=name, blueprint=bp_id, profile=prof,
                              agent=None, fsm=fsm, destination=dest, spawned_at=now, slot=role)
            fv.last_snap = VehicleSnap(actor.id, actor, cand.location.x, cand.location.y, cand.rotation.yaw, 0.0,
                                       *self._extent(actor), None, True, kind)
            if adopt is not None:
                fv.bind_not_before = self._tick_i + 3
                fv.bind_pose = cand.location
            self._vehicles[actor.id] = fv
            self._actors_cache_tick = -10_000  # pick the new actor up next tick
            if role and self._slots is not None:
                self._slots.hold(role, "fleet", marker=actor.id, actor_id=actor.id)
                # remembered while driven too: after a server restart the car can still be adopted
                self._slots.keeper.remember(role, actor, "fleet")
            reason = "adopted" if adopt is not None else "spawned"
            logger.info("Fleet: %s %s %s (%s, actor %d) -> %s", reason, kind, name, bp_id, actor.id,
                        "(%.0f, %.0f)" % (dest.x, dest.y) if dest is not None else "roaming")
            self._broadcast({"type": "v2x_vehicle_event", "actor_id": actor.id, "name": name, "kind": kind,
                             "from": "PARKED" if parked is not None else None, "to": fsm.state.value,
                             "reason": reason, "t": now})
            return {"type": "v2x_vehicle_spawned", "vehicle": fv.summary(now), "count": self.active_count()}

    def _resolve_spawn(self, spec, ego_actor):
        import carla
        planner = self.planner()
        if spec is None or spec == "random":
            # only spawn points a route leads away from: 14 of the 95 RFS points sit on
            # road fragments with no way out, and a car placed there can never plan
            points = list(planner.routable_spawn_points())
            self._rng.shuffle(points)
            for sp in points:
                if not self._occupied(sp.location, 8.0):
                    return planner._lift(sp)
            raise RuntimeError("no free spawn point")
        if not isinstance(spec, dict):
            raise ValueError("spawn must be an object")
        if "ahead_of" in spec or "behind" in spec:
            key = "ahead_of" if "ahead_of" in spec else "behind"
            ref = spec[key]
            if ref == "ego":
                if ego_actor is None:
                    raise ValueError("no ego vehicle in this session to spawn relative to")
                ref_actor = ego_actor
            else:
                ref_actor = self._world.get_actor(int(ref))
                if ref_actor is None:
                    raise ValueError("actor %s not found" % ref)
            distance = float(spec.get("distance_m", 60.0))
            tf = planner.transform_along_lane(ref_actor.get_transform().location, max(8.0, distance),
                                              backwards=(key == "behind"))
            if tf is None:
                raise ValueError("the lane ends before %.0f m" % distance)
            return tf
        if "spawn_index" in spec:
            points = list(self._map.get_spawn_points())
            idx = int(spec["spawn_index"])
            if not points or idx < 0 or idx >= len(points):
                raise ValueError("spawn_index out of range")
            return planner._lift(points[idx])
        if "x" in spec and "y" in spec:
            loc = carla.Location(x=float(spec["x"]), y=float(spec["y"]), z=float(spec.get("z", 0.0)))
            tf = planner.snapped_transform(loc, spec.get("yaw"))
            if tf is None:
                raise ValueError("no road near (%.1f, %.1f)" % (loc.x, loc.y))
            return tf
        if "lat" in spec and "lon" in spec:
            if self.gps_to_xy is None:
                raise ValueError("GPS spawn is unavailable on this map")
            x, y = self.gps_to_xy(float(spec["lat"]), float(spec["lon"]))
            tf = planner.snapped_transform(carla.Location(x=x, y=y, z=0.0), spec.get("yaw"))
            if tf is None:
                raise ValueError("no road near that GPS position")
            return tf
        raise ValueError("spawn needs ahead_of/behind, spawn_index, x/y or lat/lon")

    def _resolve_destination(self, spec, from_location):
        import carla
        if spec in (None, "none", False):
            return None
        if spec == "random" or spec is True:
            dest = self.planner().random_destination(from_location, strict=True)
            if dest is None:
                raise ValueError("no route leads away from that spot (an isolated road fragment); "
                                 "spawn somewhere else")
            return dest
        if isinstance(spec, dict):
            if "x" in spec and "y" in spec:
                loc = carla.Location(x=float(spec["x"]), y=float(spec["y"]), z=float(spec.get("z", 0.0)))
            elif "lat" in spec and "lon" in spec and self.gps_to_xy is not None:
                x, y = self.gps_to_xy(float(spec["lat"]), float(spec["lon"]))
                loc = carla.Location(x=x, y=y, z=0.0)
            else:
                raise ValueError("destination needs x/y or lat/lon")
            wp = self._map.get_waypoint(loc, project_to_road=True)
            return wp.transform.location if wp is not None else loc
        raise ValueError("destination must be 'random', 'none' or an object")

    def _occupied(self, location, radius_m: float, ignore_id: Optional[int] = None) -> bool:
        r2 = radius_m * radius_m
        for actor in self._actor_list(refresh=True):
            if ignore_id is not None and getattr(actor, "id", None) == ignore_id:
                continue
            try:
                loc = actor.get_transform().location
            except Exception:
                continue
            if (loc.x - location.x) ** 2 + (loc.y - location.y) ** 2 < r2:
                return True
        return False

    # ── removal ──

    PARK_REASON = "parked: the scenario slot stays bound to this car"

    def despawn(self, actor_id: int, release: bool = False) -> dict:
        """Remove a vehicle.  A slot car is parked instead (``release`` destroys it anyway)."""
        with self._lock:
            fv = self._vehicles.get(int(actor_id))
            if fv is None:
                raise ValueError("fleet vehicle %s not found" % actor_id)
            if fv.slot and not release:
                if not fv.parked:
                    self._park(fv, self.PARK_REASON)
                return {"type": "v2x_vehicle_despawned", "actor_id": fv.actor_id, "count": self.active_count(),
                        "parked": True}
            self._vehicles.pop(fv.actor_id, None)
            self._remove(fv, "despawned")
            return {"type": "v2x_vehicle_despawned", "actor_id": fv.actor_id, "count": self.active_count(),
                    "parked": False}

    def despawn_all(self, release: bool = False) -> dict:
        with self._lock:
            removed = parked = 0
            for actor_id in list(self._vehicles.keys()):
                fv = self._vehicles[actor_id]
                if fv.slot and not release:
                    if not fv.parked:
                        self._park(fv, self.PARK_REASON)
                        parked += 1
                    continue
                self._vehicles.pop(actor_id)
                self._remove(fv, "despawned")
                removed += 1
            return {"type": "v2x_vehicles_despawned", "count": removed + parked, "parked": parked}

    def _remove(self, fv: FleetVehicle, reason: str) -> None:
        now = self._now()
        if reason == "despawned":
            try:
                fv.actor.destroy()
            except Exception as exc:
                logger.debug("fleet destroy failed for %d: %s", fv.actor_id, exc)
        self.tracker.remove("actor:%d" % fv.actor_id)
        if self._slots is not None and fv.slot:
            self._slots.release(fv.slot, marker=fv.actor_id)
            self._slots.keeper.forget(fv.slot)
        self._broadcast({"type": "v2x_vehicle_event", "actor_id": fv.actor_id, "name": fv.name, "kind": fv.kind,
                         "from": fv.state(), "to": None, "reason": reason, "t": now})
        logger.info("Fleet: %s %s (actor %d)", reason, fv.name, fv.actor_id)

    def _park(self, fv: FleetVehicle, reason: str) -> None:
        """Take a slot car off the road but keep it in the world: the adapter's binding must survive."""
        now = self._now()
        previous = fv.state()
        fv.agent = None
        fv.decision = None
        fv.last_obs = None
        fv.lane_change = None
        fv.pass_job = None
        fv.stop = fv.hard_stop = False
        fv.stuck_since = None
        fv.stuck_recoveries = 0
        fv.arrived = False
        fv.light_mask = 0
        self._apply_lights(fv)
        info = {}
        try:
            info = park_actor(self._map, fv.actor)
        except Exception as exc:
            logger.debug("fleet park failed for %d: %s", fv.actor_id, exc)
        fv.parked = True
        fv.parked_at = now
        if info.get("moved") and fv.last_snap is not None:
            s = fv.last_snap
            fv.last_snap = VehicleSnap(s.actor_id, s.actor, float(info["x"]), float(info["y"]), s.yaw, 0.0,
                                       s.ext_x, s.ext_y, None, True, s.kind)
        self.tracker.remove("actor:%d" % fv.actor_id)
        if self._slots is not None and fv.slot:
            self._slots.keeper.remember(fv.slot, fv.actor, "fleet")
            self._slots.release(fv.slot, marker=fv.actor_id)
        self._broadcast({"type": "v2x_vehicle_event", "actor_id": fv.actor_id, "name": fv.name, "kind": fv.kind,
                         "from": previous, "to": "PARKED", "reason": reason, "t": now})
        logger.info("Fleet: parked %s (actor %d)%s: %s", fv.name, fv.actor_id,
                    " off the road" if info.get("moved") else "", reason)

    # ── operator control of an ERV ──

    def set_erv_state(self, actor_id: int, siren=None, lights=None, arrived=None, response_type=None,
                      manual=None, destination=None) -> dict:
        with self._lock:
            fv = self._vehicles.get(int(actor_id))
            if fv is None:
                raise ValueError("fleet vehicle %s not found" % actor_id)
            if fv.kind != "erv":
                raise ValueError("vehicle %s is not an emergency vehicle" % actor_id)
            if fv.parked:
                raise ValueError("%s is parked; spawn it again as %s to use it" % (fv.name, fv.slot))
            now = self._now()
            if destination is not None:
                snap = fv.last_snap
                import carla
                from_loc = carla.Location(x=snap.x, y=snap.y, z=0.0) if snap is not None else fv.actor.get_transform().location
                dest = self._resolve_destination(destination, from_loc)
                fv.destination = dest
                fv.arrived = False
                fv.doors_open = False
                if dest is not None and fv.agent is not None:
                    fv.agent.plan_to(dest)
                if fv.fsm.state in (ErvState.ARRIVED, ErvState.MANUAL) and manual is None and arrived is None:
                    fv.fsm.set_manual(now, manual=False, arrived=False)
            fv.fsm.set_manual(now, manual=manual, siren=siren, lights=lights, arrived=arrived,
                              response_type=response_type)
            return {"type": "v2x_vehicle_state", "vehicle": fv.summary(now)}

    # ── tick ──

    def tick(self) -> None:
        t0 = time.perf_counter()
        with self._lock:
            self._tick_i += 1
            now = self._now()
            refresh = self._tick_i % self.light_scan_every == 0 or self._tick_i - self._actors_cache_tick > self.light_scan_every
            actors = self._actor_list(refresh=refresh)
            if not self._vehicles and not refresh:
                return
            self._prune_dead(actors)
            table = self._snapshot_table(actors)
            self._bind_agents(table, now)
            self._update_tracker(now, table, refresh)
            if not self._vehicles:
                return
            phase = self._tick_i % self.decision_every
            others_by_id = {aid: s.actor for aid, s in table.items()}
            cmds = []
            for i, fv in enumerate(list(self._vehicles.values())):
                fv.last_snap = table.get(fv.actor_id, fv.last_snap)
                if fv.parked or fv.agent is None:
                    continue
                if i % self.decision_every == phase:
                    try:
                        self._decide(fv, now, table)
                    except Exception:
                        logger.warning("Fleet decision failed for %s", fv.name, exc_info=True)
                try:
                    vehicle_list = [a for aid, a in others_by_id.items() if aid != fv.actor_id]
                    control = fv.agent.run_control(stop=fv.stop, vehicle_list=vehicle_list, hard_stop=fv.hard_stop)
                    fv.last_control = control
                    cmds.append((fv.actor, control))
                    self._watch_stuck(fv, control, now)
                except Exception:
                    logger.debug("Fleet control failed for %s", fv.name, exc_info=True)
                self._apply_lights(fv)
            self._apply_controls(cmds)
            self._flush_events(now)
        ms = (time.perf_counter() - t0) * 1000.0
        self._tick_ms.append(ms)
        self._update_budget()

    def _update_budget(self) -> None:
        if len(self._tick_ms) < 40:
            return
        ms = sorted(self._tick_ms)
        p95 = ms[int(len(ms) * 0.95) - 1]
        if not self.degraded and p95 > self.budget_ms:
            self.degraded = True
            self.decision_every = self._base_decision_every * 2
            logger.warning("Fleet tick p95 %.1f ms over the %.0f ms budget: decisions at %.0f Hz",
                           p95, self.budget_ms, 20.0 / self.decision_every)
        elif self.degraded and p95 < self.budget_ms / 2.0:
            self.degraded = False
            self.decision_every = self._base_decision_every
            logger.info("Fleet tick p95 %.1f ms: decisions back at %.0f Hz", p95, 20.0 / self.decision_every)

    STUCK_AFTER_S = 6.0
    STUCK_MAX_RECOVERIES = 3

    def _watch_stuck(self, fv: FleetVehicle, control, now: float) -> None:
        """A car pushing the throttle without moving is wedged against map geometry.

        The RFS mesh has gates and kerbs the OpenDRIVE lanes run through.  First
        re-route, then move the car to a free spawn point, finally give up.
        """
        snap = fv.last_snap
        if snap is None:
            return
        pushing = float(getattr(control, "throttle", 0.0)) > 0.3 and not fv.stop and not fv.hard_stop
        if pushing and snap.speed < 0.2:
            if fv.stuck_since is None:
                fv.stuck_since = now
            elif now - fv.stuck_since >= self.STUCK_AFTER_S:
                self._recover_stuck(fv, snap, now)
        else:
            fv.stuck_since = None

    def _recover_stuck(self, fv: FleetVehicle, snap: VehicleSnap, now: float) -> None:
        import carla
        fv.stuck_since = None
        fv.stuck_recoveries += 1
        fv.lane_change = None
        fv.pass_job = None
        if fv.stuck_recoveries > self.STUCK_MAX_RECOVERIES:
            if fv.slot:
                self._park(fv, "stuck repeatedly, parked")
                return
            self._vehicles.pop(fv.actor_id, None)
            self._remove(fv, "despawned")
            logger.warning("Fleet: %s stuck repeatedly, removed", fv.name)
            return
        if fv.stuck_recoveries == 1:
            try:
                dest = self.planner().random_destination(carla.Location(x=snap.x, y=snap.y, z=0.0))
                fv.destination = dest
                fv.agent.plan_to(dest)
                reason = "stuck: re-routing"
            except Exception as exc:
                logger.debug("stuck re-route failed: %s", exc)
                reason = "stuck: re-route failed"
        else:
            points = list(self.planner().routable_spawn_points())
            self._rng.shuffle(points)
            moved = False
            for sp in points:
                if math.hypot(sp.location.x - snap.x, sp.location.y - snap.y) < 30.0 or self._occupied(sp.location, 8.0):
                    continue
                try:
                    tf = self.planner()._lift(sp)
                    fv.actor.set_transform(tf)
                    fv.actor.set_target_velocity(carla.Vector3D(0.0, 0.0, 0.0))
                    fv.agent = None  # rebound once the snapshot shows the new pose
                    fv.bind_not_before = self._tick_i + 3
                    fv.bind_pose = tf.location
                    fv.last_snap = VehicleSnap(fv.actor_id, fv.actor, tf.location.x, tf.location.y, tf.rotation.yaw, 0.0,
                                               snap.ext_x, snap.ext_y, None, True, fv.kind)
                    moved = True
                    break
                except Exception as exc:
                    logger.debug("stuck teleport failed: %s", exc)
            reason = "stuck: moved to a free spawn point" if moved else "stuck: no free spawn point"
        logger.info("Fleet: %s %s (recovery %d)", fv.name, reason, fv.stuck_recoveries)
        self._broadcast({"type": "v2x_vehicle_event", "actor_id": fv.actor_id, "name": fv.name, "kind": fv.kind,
                         "from": fv.state(), "to": fv.state(), "reason": reason, "t": now})

    def _bind_agents(self, table: Dict[int, VehicleSnap], now: float) -> None:
        """Build the controller for vehicles the world now reports (valid transform)."""
        for fv in list(self._vehicles.values()):
            if fv.parked or fv.agent is not None or fv.actor_id not in table:
                continue
            if self._tick_i < fv.bind_not_before:
                continue
            if fv.bind_pose is not None:
                s = table[fv.actor_id]
                far = math.hypot(s.x - fv.bind_pose.x, s.y - fv.bind_pose.y) > 8.0
                if far and self._tick_i < fv.bind_not_before + 40:
                    continue  # the teleport has not reached the snapshot yet
                fv.bind_pose = None
            try:
                offset = fv.profile.centerline_offset_m if fv.kind == "erv" else 0.0
                agent = self._make_agent(fv.actor, DEFAULT_SPEED_LIMIT_MPS, offset)
                if fv.destination is not None:
                    try:
                        agent.plan_to(fv.destination)
                    except Exception as exc:
                        # the car's reported position may have no route to that destination
                        # (RFS has dead road fragments): once, try one routed from where it is
                        import carla
                        s = table[fv.actor_id]
                        dest = self.planner().random_destination(carla.Location(x=s.x, y=s.y, z=0.0), strict=True)
                        if dest is None:
                            raise
                        logger.info("Fleet: %s re-routed at start (%s)", fv.name, exc)
                        fv.destination = dest
                        agent.plan_to(dest)
                fv.agent = agent
                fv.fsm.t_state = now
            except Exception:
                if fv.slot:
                    logger.warning("Fleet: controller setup failed for %s, parking it", fv.name, exc_info=True)
                    self._park(fv, "controller setup failed, parked")
                    continue
                logger.warning("Fleet: controller setup failed for %s, removing it", fv.name, exc_info=True)
                self._vehicles.pop(fv.actor_id, None)
                self._remove(fv, "despawned")

    def _actor_list(self, refresh: bool) -> list:
        if refresh or self._tick_i - self._actors_cache_tick > self.light_scan_every:
            try:
                self._actors_cache = list(self._world.get_actors().filter("vehicle.*"))
                self._actors_cache_tick = self._tick_i
            except Exception as exc:
                logger.debug("get_actors failed: %s", exc)
        return self._actors_cache

    def _prune_dead(self, actors: list) -> None:
        """Drop vehicles someone else destroyed (clear NPCs, the audit, a map reload).

        A freshly spawned actor only shows up in ``get_actors()`` after the next
        world tick, so the actor-list check waits a couple of seconds; CARLA's
        ``is_alive`` catches destroyed actors right away.
        """
        now = self._now()
        present = {getattr(a, "id", None) for a in actors}
        for actor_id, fv in list(self._vehicles.items()):
            alive = getattr(fv.actor, "is_alive", True)
            missing = actor_id not in present and now - fv.spawned_at > 2.0
            if missing or not alive:
                del self._vehicles[actor_id]
                self._remove(fv, "destroyed")

    def _extent(self, actor) -> Tuple[float, float]:
        aid = getattr(actor, "id", 0)
        ext = self._ext_cache.get(aid)
        if ext is None:
            try:
                bb = actor.bounding_box.extent
                ext = (float(bb.x), float(bb.y))
            except Exception:
                ext = (2.3, 1.0)
            self._ext_cache[aid] = ext
        return ext

    def _snapshot_table(self, actors: list) -> Dict[int, VehicleSnap]:
        table: Dict[int, VehicleSnap] = {}
        for actor in actors:
            aid = getattr(actor, "id", None)
            if aid is None:
                continue
            try:
                tf = actor.get_transform()
                v = actor.get_velocity()
            except Exception:
                continue
            ex, ey = self._extent(actor)
            try:
                wp = self._map.get_waypoint(tf.location, project_to_road=True)
            except Exception:
                wp = None
            fv = self._vehicles.get(aid)
            table[aid] = VehicleSnap(aid, actor, tf.location.x, tf.location.y, tf.rotation.yaw,
                                     math.hypot(v.x, v.y), ex, ey, wp, fv is not None, fv.kind if fv else "")
        return table

    def _update_tracker(self, now: float, table: Dict[int, VehicleSnap], refresh: bool) -> None:
        hub_actor_ids = set()
        # A fleet ERV under a scenario slot publishes real BSMs through the DT adapter; once the
        # hub tracks those, the fleet's own announcement would be a second ERV for the civilians.
        bsm_actor_ids = {t.actor_id for t in self.tracker.tracks() if t.source == "bsm" and t.actor_id is not None}
        for fv in self._vehicles.values():
            if fv.kind != "erv" or fv.parked or "fleet" not in self.erv_sources:
                continue
            if fv.slot and fv.actor_id in bsm_actor_ids:
                self.tracker.remove("actor:%d" % fv.actor_id)
                continue
            s = table.get(fv.actor_id)
            if s is None:
                continue
            d = fv.decision
            # before the first decision an ERV is en route with siren and lights on
            lights = bool(d.lights) if d is not None else True
            siren = bool(d.siren) if d is not None else True
            state = d.state if d is not None else fv.state()
            response = d.response_type if d is not None else fv.profile.response_type
            self.tracker.update_from_lights("actor:%d" % fv.actor_id, fv.actor_id, s.x, s.y, s.yaw, s.speed,
                                            lights, now, role=fv.profile.role, source="fleet",
                                            fleet_state=state, siren=siren, response_type=response)
        if self._hub_source is not None and "bsm" in self.erv_sources and self._tick_i % self.decision_every == 0:
            def pose_of(actor_id):
                s = table.get(actor_id)
                return (s.x, s.y, s.yaw) if s is not None else None
            try:
                self._hub_source.update(self.tracker, now, pose_of, self.gps_to_xy)
            except Exception as exc:
                logger.debug("hub ERV source failed: %s", exc)
            hub_actor_ids = {t.actor_id for t in self.tracker.tracks() if t.source == "bsm" and t.actor_id is not None}
        if refresh and "lights" in self.erv_sources:
            vehicles = [(s.actor, s.x, s.y, s.yaw, s.speed) for s in table.values() if not s.is_fleet]
            try:
                self._light_source.update(self.tracker, now, vehicles, exclude_ids=hub_actor_ids | self.actor_ids())
            except Exception as exc:
                logger.debug("light-state ERV source failed: %s", exc)
        self.tracker.expire(now)

    # ── observations ──

    def _speed_limit(self, actor) -> float:
        try:
            limit = float(actor.get_speed_limit()) / 3.6
            return limit if limit > 1.0 else DEFAULT_SPEED_LIMIT_MPS
        except Exception:
            return DEFAULT_SPEED_LIMIT_MPS

    def _lead_vehicle(self, snap: VehicleSnap, table: Dict[int, VehicleSnap], max_m: float = 60.0):
        best = None
        for other in table.values():
            if other.actor_id == snap.actor_id:
                continue
            along, lateral = relative_position(snap.x, snap.y, snap.yaw, other.x, other.y)
            if along <= 0 or along > max_m:
                continue
            in_lane = False
            if snap.wp is not None and other.wp is not None and snap.wp.road_id == other.wp.road_id:
                in_lane = other.wp.lane_id == snap.wp.lane_id
            if not in_lane:
                width = float(getattr(snap.wp, "lane_width", 3.5) or 3.5) if snap.wp is not None else 3.5
                in_lane = abs(lateral) <= width / 2.0 + 0.5 and same_direction(snap.yaw, other.yaw, 60.0)
            if not in_lane:
                continue
            gap = along - snap.ext_x - other.ext_x
            if best is None or gap < best[0]:
                best = (max(0.0, gap), other.speed, other)
        return best

    def _erv_relations(self, fv: FleetVehicle, snap: VehicleSnap, table: Dict[int, VehicleSnap], now: float) -> List[ErvRelation]:
        rels = []
        width = float(getattr(snap.wp, "lane_width", 3.5) or 3.5) if snap.wp is not None else 3.5
        for track in self.tracker.tracks():
            if not track.positioned or track.actor_id == fv.actor_id:
                continue
            if now - track.t_update > self.tracker.timeout_s:
                continue
            along, lateral = relative_position(snap.x, snap.y, snap.yaw, track.x, track.y)
            if abs(along) > 450.0:
                continue
            erv_snap = table.get(track.actor_id) if track.actor_id is not None else None
            aligned = same_direction(snap.yaw, track.yaw_deg, 45.0) and abs(lateral) <= 8.0
            if erv_snap is not None and erv_snap.wp is not None and snap.wp is not None:
                same_road = rc.same_road_same_direction(snap.wp, erv_snap.wp) or aligned
                same_lane = (snap.wp.road_id == erv_snap.wp.road_id and snap.wp.lane_id == erv_snap.wp.lane_id) \
                    or (aligned and abs(lateral) <= width / 2.0 + 0.3)
            else:
                same_road = aligned
                same_lane = aligned and abs(lateral) <= width / 2.0 + 0.3
            rels.append(ErvRelation(key=track.key, gap_m=-along, lateral_m=lateral, erv_speed_mps=track.speed_mps,
                                    my_speed_mps=snap.speed, same_lane=same_lane, same_road=same_road,
                                    active=track.active, stationary=track.is_stationary(now)))
        return rels

    def _opposing_status(self, snap: VehicleSnap, table: Dict[int, VehicleSnap], pass_len: float) -> Tuple[bool, bool, float]:
        """(opposing_clear, two_way, oncoming_ttc_s) for a pass of ``pass_len`` metres."""
        if snap.wp is None:
            return False, False, INF
        opp = rc.opposing_lane(snap.wp)
        if opp is None:
            return False, False, INF
        clear, ttc_min = True, INF
        sign = rc.lane_sign(snap.wp)
        for other in table.values():
            if other.actor_id == snap.actor_id or other.wp is None:
                continue
            if other.wp.road_id != snap.wp.road_id or rc.lane_sign(other.wp) == sign:
                continue
            along, _ = relative_position(snap.x, snap.y, snap.yaw, other.x, other.y)
            if along <= 0:
                continue
            closing = snap.speed + other.speed
            ttc = along / max(closing, 0.1)
            ttc_min = min(ttc_min, ttc)
            if along < pass_len + other.speed * 5.0 + 10.0:
                clear = False
        return clear, True, ttc_min

    def _lane_change_done(self, fv: FleetVehicle, snap: VehicleSnap, now: float) -> bool:
        lc = fv.lane_change
        if lc is None:
            return False
        done = lc.get("failed", False) or now - lc["started"] > 8.0
        if not done and snap.wp is not None and not getattr(snap.wp, "is_junction", False):
            done = snap.wp.lane_id != lc.get("from_lane") and snap.wp.road_id == lc.get("from_road")
        if done:
            fv.lane_change = None
        return done

    def _pass_done(self, fv: FleetVehicle, snap: VehicleSnap, table: Dict[int, VehicleSnap], now: float) -> bool:
        job = fv.pass_job
        if job is None:
            if fv.pass_lead_id is not None and fv.kind == "erv":
                lead = table.get(fv.pass_lead_id)
                if lead is None:
                    return True
                along, _ = relative_position(snap.x, snap.y, snap.yaw, lead.x, lead.y)
                return along < -5.0
            return False
        if job.get("failed"):
            fv.pass_job = None
            return True
        sign = rc.lane_sign(snap.wp) if snap.wp is not None else job["from_sign"]
        if sign != job["from_sign"]:
            job["left_lane"] = True
        done = (job.get("left_lane") and sign == job["from_sign"]) or now - job["started"] > 25.0
        try:
            done = done or fv.agent.remaining() == 0
        except Exception:
            pass
        if done:
            fv.pass_job = None
            fv.pass_lead_id = None
        return done

    def _observe_civilian(self, fv: FleetVehicle, snap: VehicleSnap, table: Dict[int, VehicleSnap], now: float) -> CivilianObservation:
        wp = snap.wp
        info = rc.lane_info(wp, with_control=False)
        lead = self._lead_vehicle(snap, table)
        ervs = self._erv_relations(fv, snap, table, now)
        two_way, opposing_clear = info.two_way, False
        if fv.fsm.state in (CivState.WAIT_BEHIND, CivState.OPPOSING_PASS):
            target = next((r for r in ervs if r.key == fv.fsm.target_erv), None)
            pass_len = max(30.0, (-target.gap_m if target else 20.0) + 25.0)
            opposing_clear, two_way, _ = self._opposing_status(snap, table, pass_len)
        return CivilianObservation(
            t=now, speed_mps=snap.speed, speed_limit_mps=self._speed_limit(snap.actor),
            lane_width_m=info.lane_width_m, half_width_m=snap.ext_y, shoulder_m=info.shoulder_m,
            same_dir_lanes=info.same_dir_lanes, two_way=two_way, dist_to_junction_m=info.dist_to_junction_m,
            in_junction=info.is_junction, lead_gap_m=lead[0] if lead else None, lead_speed_mps=lead[1] if lead else None,
            ervs=ervs, opposing_clear=opposing_clear and now >= fv.pass_block_until,
            lane_change_done=self._lane_change_done(fv, snap, now), pass_done=self._pass_done(fv, snap, table, now),
        )

    def _observe_erv(self, fv: FleetVehicle, snap: VehicleSnap, table: Dict[int, VehicleSnap], now: float) -> ErvObservation:
        wp = snap.wp
        info = rc.lane_info(wp, with_control=True)
        dist_j, jwp = rc.distance_to_junction(wp)
        cur_jid = None
        if info.is_junction:
            cur_jid = getattr(wp, "junction_id", None)
        elif jwp is not None:
            cur_jid = getattr(jwp, "junction_id", None)
        cleared = False
        if fv.junction_handling is not None and cur_jid != fv.junction_handling:
            cleared = True
            fv.junction_handling = None
        if fv.junction_handling is None and cur_jid is not None and dist_j <= fv.profile.junction_approach_m:
            fv.junction_handling = cur_jid
        # conflicting traffic at the junction: a moving vehicle that reaches it within the
        # crossing time and is not simply following me (vehicles that stopped are yielding)
        conflicting_clear = True
        if jwp is not None or info.is_junction:
            jx = jwp.transform.location.x if jwp is not None else snap.x
            jy = jwp.transform.location.y if jwp is not None else snap.y
            for other in table.values():
                if other.actor_id == snap.actor_id or other.speed <= 1.0:
                    continue
                dist = math.hypot(other.x - jx, other.y - jy)
                if dist > 40.0:
                    continue
                along, lateral = relative_position(snap.x, snap.y, snap.yaw, other.x, other.y)
                if along < 0 and abs(lateral) < 4.0 and same_direction(snap.yaw, other.yaw, 45.0):
                    continue  # behind me in my lane
                if along > 8.0 and abs(lateral) < 2.5 and same_direction(snap.yaw, other.yaw, 45.0):
                    continue  # ahead of me, leaving the junction my way
                # is it heading toward the junction?
                oyaw = math.radians(other.yaw)
                closing = ((jx - other.x) * math.cos(oyaw) + (jy - other.y) * math.sin(oyaw)) / max(dist, 0.1)
                if closing < 0.3 and dist > 8.0:
                    continue  # driving away from it
                if dist / max(other.speed, 0.1) > fv.profile.crossing_ttc_s:
                    continue  # far enough away in time
                conflicting_clear = False
                break
        lead = self._lead_vehicle(snap, table)
        lead_moved_right = False
        if lead is not None:
            lead_snap = lead[2]
            if fv.fsm.state == ErvState.WAIT_FOR_YIELD:
                fv.pass_lead_id = lead_snap.actor_id
            if lead_snap.wp is not None:
                lat = rc.lateral_in_lane(lead_snap.wp, lead_snap.x, lead_snap.y)
                room_left = float(getattr(lead_snap.wp, "lane_width", 3.5) or 3.5) / 2.0 + lat - lead_snap.ext_y
                lead_moved_right = room_left >= 2.0 * snap.ext_y + fv.profile.pass_clearance_m
        two_way, opposing_clear, ttc = info.two_way, False, INF
        if fv.fsm.state in (ErvState.WAIT_FOR_YIELD, ErvState.OPPOSING_LANE_PASS, ErvState.PASS_LEFT_IN_LANE):
            pass_len = max(30.0, (lead[0] if lead else 15.0) + 2.0 * (lead[2].ext_x if lead else 2.5) + 20.0)
            opposing_clear, two_way, ttc = self._opposing_status(snap, table, pass_len)
        dist_dest = INF
        if fv.destination is not None:
            dist_dest = math.hypot(fv.destination.x - snap.x, fv.destination.y - snap.y)
            try:
                if fv.agent.done() and dist_dest < 25.0:
                    dist_dest = 0.0
            except Exception:
                pass
        return ErvObservation(
            t=now, speed_mps=snap.speed, speed_limit_mps=self._speed_limit(snap.actor),
            dist_to_junction_m=info.dist_to_junction_m, in_junction=info.is_junction,
            junction_control=info.junction_control, junction_cleared=cleared, conflicting_clear=conflicting_clear,
            lead_gap_m=lead[0] if lead else None, lead_speed_mps=lead[1] if lead else None,
            lead_moved_right=lead_moved_right, lane_width_m=info.lane_width_m, half_width_m=snap.ext_y,
            two_way=two_way, opposing_clear=opposing_clear and now >= fv.pass_block_until,
            sight_m=min(info.dist_to_junction_m, 150.0), oncoming_ttc_s=ttc, dist_to_destination_m=dist_dest,
            pass_done=self._pass_done(fv, snap, table, now),
        )

    # ── decisions ──

    def _roam_if_arrived(self, fv: FleetVehicle, snap: VehicleSnap, now: float) -> None:
        """Civilians keep driving: a new random destination once the current one is reached."""
        if fv.lane_change is not None or fv.pass_job is not None:
            return
        arrived = False
        try:
            arrived = fv.agent.remaining() <= 2
        except Exception:
            pass
        if not arrived and fv.destination is not None:
            arrived = math.hypot(fv.destination.x - snap.x, fv.destination.y - snap.y) < 10.0
        if not arrived:
            return
        try:
            import carla
            dest = self.planner().random_destination(carla.Location(x=snap.x, y=snap.y, z=0.0))
            fv.agent.plan_to(dest)
            fv.destination = dest
            self._broadcast({"type": "v2x_vehicle_event", "actor_id": fv.actor_id, "name": fv.name, "kind": fv.kind,
                             "from": fv.state(), "to": fv.state(), "reason": "new destination", "t": now})
        except Exception as exc:
            logger.debug("re-plan for %s failed: %s", fv.name, exc)

    def _decide(self, fv: FleetVehicle, now: float, table: Dict[int, VehicleSnap]) -> None:
        snap = table.get(fv.actor_id)
        if snap is None or snap.wp is None:
            return
        if fv.kind == "civilian":
            self._roam_if_arrived(fv, snap, now)
            obs = self._observe_civilian(fv, snap, table, now)
            d = fv.fsm.step(obs)
            self._apply_civilian(fv, snap, obs, d, now)
        else:
            obs = self._observe_erv(fv, snap, table, now)
            d = fv.fsm.step(obs)
            self._apply_erv(fv, snap, obs, d, now, table)
        fv.decision = d
        fv.last_obs = obs

    def _apply_common(self, fv: FleetVehicle, snap: VehicleSnap, target_speed: float, offset: float, stop: bool) -> None:
        fv.agent.set_target_speed_mps(target_speed)
        fv.agent.set_lateral_offset(offset)
        fv.stop = bool(stop)
        fv.hard_stop = bool(stop) and snap.speed < 0.3

    def _apply_civilian(self, fv: FleetVehicle, snap: VehicleSnap, obs: CivilianObservation, d, now: float) -> None:
        self._apply_common(fv, snap, d.target_speed_mps, d.lateral_offset_m, d.stop)
        if d.lane_change and fv.lane_change is None:
            self._start_lane_change(fv, snap, d.lane_change, now)
        if d.pass_path == "opposing" and fv.pass_job is None:
            target = next((r for r in obs.ervs if r.key == d.erv_key), None)
            length = max(30.0, (-target.gap_m if target else 20.0) + 25.0)
            self._start_opposing_pass(fv, snap, length, now)
        fv.light_mask = light_mask(left="left" in d.lights, right="right" in d.lights,
                                   hazard="hazard" in d.lights, brake="brake" in d.lights)

    def _apply_erv(self, fv: FleetVehicle, snap: VehicleSnap, obs: ErvObservation, d, now: float,
                   table: Dict[int, VehicleSnap]) -> None:
        self._apply_common(fv, snap, d.target_speed_mps, d.lateral_offset_m, d.stop)
        if d.pass_path == "opposing" and fv.pass_job is None:
            lead_len = 5.0
            lead = self._lead_vehicle(snap, table)
            if lead is not None:
                lead_len = lead[0] + 2.0 * lead[2].ext_x
                fv.pass_lead_id = lead[2].actor_id
            self._start_opposing_pass(fv, snap, max(30.0, lead_len + 20.0), now)
        if d.doors_open and not fv.doors_open:
            from digital_twin_bridge.v2x_behaviors.controllers import open_rear_doors
            fv.doors_open = open_rear_doors(fv.actor)
        fv.arrived = d.state == ErvState.ARRIVED.value
        fv.light_mask = light_mask(lights=d.lights, siren=d.siren, low_beam=d.lights, brake=d.stop)

    def _start_lane_change(self, fv: FleetVehicle, snap: VehicleSnap, direction: str, now: float) -> None:
        path = []
        try:
            path = self.planner().lane_change_path(fv.agent, snap.wp, direction, snap.speed)
        except Exception as exc:
            logger.debug("lane change path failed: %s", exc)
        if path:
            fv.agent.set_plan(self.planner().splice_to_destination(path, fv.destination))
            fv.lane_change = {"direction": direction, "started": now, "from_lane": snap.wp.lane_id,
                              "from_road": snap.wp.road_id, "failed": False}
            reason = "lane change %s" % direction
        else:
            fv.lane_change = {"direction": direction, "started": now, "failed": True}
            reason = "lane change %s not possible here" % direction
        self._broadcast({"type": "v2x_vehicle_event", "actor_id": fv.actor_id, "name": fv.name, "kind": fv.kind,
                         "from": fv.state(), "to": fv.state(), "reason": reason, "t": now})

    def _start_opposing_pass(self, fv: FleetVehicle, snap: VehicleSnap, length_m: float, now: float) -> None:
        path = []
        try:
            path = self.planner().opposing_pass_path(snap.wp, length_m)
        except Exception as exc:
            logger.debug("opposing pass path failed: %s", exc)
        if path:
            fv.agent.set_plan(self.planner().splice_to_destination(path, fv.destination))
            fv.pass_job = {"started": now, "from_sign": rc.lane_sign(snap.wp), "failed": False, "left_lane": False}
            reason = "passing via the opposing lane (%.0f m)" % length_m
        else:
            fv.pass_job = {"started": now, "failed": True}
            fv.pass_block_until = now + 5.0
            reason = "opposing-lane pass not possible here"
        self._broadcast({"type": "v2x_vehicle_event", "actor_id": fv.actor_id, "name": fv.name, "kind": fv.kind,
                         "from": fv.state(), "to": fv.state(), "reason": reason, "t": now})

    # ── outputs ──

    def _apply_lights(self, fv: FleetVehicle) -> None:
        if fv.lights_applied == fv.light_mask:
            return
        from digital_twin_bridge.v2x_behaviors.controllers import set_vehicle_lights
        fv.lights_applied = set_vehicle_lights(fv.actor, fv.light_mask, fv.lights_applied)

    def _apply_controls(self, cmds: list) -> None:
        if not cmds:
            return
        client = self._get_client()
        command = None
        try:
            import carla
            command = getattr(carla, "command", None)
        except Exception:
            command = None
        if client is not None and command is not None and hasattr(command, "ApplyVehicleControl"):
            try:
                client.apply_batch([command.ApplyVehicleControl(actor.id, control) for actor, control in cmds])
                return
            except Exception as exc:
                logger.debug("apply_batch failed, falling back: %s", exc)
        for actor, control in cmds:
            try:
                actor.apply_control(control)
            except Exception as exc:
                logger.debug("apply_control failed: %s", exc)

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import carla
                client = carla.Client(self._host, self._port)
                client.set_timeout(5.0)
                self._client = client
        except Exception as exc:
            logger.debug("fleet client unavailable: %s", exc)
            self._client = None
        return self._client

    def _flush_events(self, now: float) -> None:
        for fv in self._vehicles.values():
            trs = fv.fsm.transitions
            while fv.transitions_sent < len(trs):
                tr = trs[fv.transitions_sent]
                fv.transitions_sent += 1
                self._broadcast({"type": "v2x_vehicle_event", "actor_id": fv.actor_id, "name": fv.name,
                                 "kind": fv.kind, "from": tr.src, "to": tr.dst, "reason": tr.reason, "t": tr.t})
