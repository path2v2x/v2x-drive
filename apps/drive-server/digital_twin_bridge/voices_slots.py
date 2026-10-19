"""VOICES scenario slots: which scenario name is carried by which CARLA actor.

A *slot* is a vehicle entry of the DT scenario file that belongs to our organization
(PATH-M-1 and PATH-BG-1..5 in ch2_erv.xml).  The TENA adapter binds a name to the first
CARLA actor it sees carrying that ``role_name`` after the adapter started and never
re-binds, so a slot's actor has to be created after DT is up and then kept alive for the
whole DT session.  Two things in this server create slot actors: a browser session whose
ego "publishes to VOICES" and the fleet when an emergency vehicle is spawned under
PATH-M-1.  Both park the car instead of destroying it when they are done, and the next
holder adopts the same actor, so the adapter's binding survives.

``SlotService`` is the one place that knows

* which names are slots (the roster's local entries plus ``DTB_VOICES_EGO_ROLE``),
* who holds a slot right now (a session or the fleet),
* which parked actors were created by us and may be adopted (``SlotKeeper``, a small
  JSON file so a parked car survives a drive-server restart),
* how a request for a slot is validated (``claim``).

A name carried by an actor we did not create (DT's manual car, a SUMO slot car) is
``taken`` and is never touched.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_KEEPER_PATH = os.path.join(os.path.expanduser("~"), ".cache", "v2x-drive", "voices_slots.json")
EMERGENCY_TYPE_HINTS = ("ambulance", "firetruck", "police", "cop")
SLOT_STATES = ("free", "kept", "session", "fleet", "taken")


class SlotClaimError(RuntimeError):
    """A slot request that cannot be honoured; ``code`` is for the client."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _role_of(actor) -> str:
    try:
        attrs = actor.attributes
        return str(attrs.get("role_name", "")) if attrs else ""
    except Exception:
        return ""


def vehicle_actors(world, tries: int = 3, pause_s: float = 0.15) -> list:
    """Vehicle actors; the 0.10 server returns an empty list now and then, so retry briefly."""
    actors: list = []
    for i in range(max(1, int(tries))):
        try:
            actors = list(world.get_actors().filter("vehicle.*"))
        except Exception:
            actors = []
        if actors:
            break
        if i + 1 < tries and pause_s > 0:
            time.sleep(pause_s)
    return actors


class SlotKeeper:
    """Actors this server parked under a slot name, remembered across restarts.

    ``path=""`` keeps the records in memory only (tests).  A record is matched
    against the world by actor id *and* blueprint, so a CARLA restart (ids start
    again from 1) cannot make a foreign car look like ours.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = DEFAULT_KEEPER_PATH if path is None else path
        self._kept: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._kept = {str(k): v for k, v in data.items() if isinstance(v, dict) and "actor_id" in v}
        except FileNotFoundError:
            self._kept = {}
        except Exception:
            logger.warning("Could not read %s; starting with no kept slot cars", self.path, exc_info=True)
            self._kept = {}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._kept, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception:
            logger.warning("Could not write %s", self.path, exc_info=True)

    def kept(self) -> Dict[str, dict]:
        return dict(self._kept)

    def record(self, role: str) -> Optional[dict]:
        return self._kept.get(role)

    def remember(self, role: str, actor, by: str) -> None:
        self._kept[role] = {
            "actor_id": int(actor.id),
            "type_id": str(getattr(actor, "type_id", "") or ""),
            "by": by,
            "t": time.time(),
        }
        self._save()

    def forget(self, role: str) -> None:
        if self._kept.pop(role, None) is not None:
            self._save()

    def match(self, role: str, actors: Iterable):
        """The kept actor for ``role`` when it is still in the world under that name."""
        rec = self._kept.get(role)
        if not rec:
            return None
        for actor in actors:
            if getattr(actor, "id", None) != rec.get("actor_id") or _role_of(actor) != role:
                continue
            if rec.get("type_id") and str(getattr(actor, "type_id", "") or "") != rec["type_id"]:
                return None
            return actor
        return None


@dataclass
class SlotHolder:
    by: str                     # "session" | "fleet"
    marker: object = None       # the session, or the fleet actor id
    actor_id: Optional[int] = None


class SlotService:
    """Slot names, their holders, the kept-car records and claim validation."""

    def __init__(self, roster=None, keeper: Optional[SlotKeeper] = None, local_org: str = "PATH",
                 config_roles: Iterable[str] = (), default_slot: str = "PATH-BG-5"):
        self.roster = roster
        self.keeper = keeper if keeper is not None else SlotKeeper(path="")
        self.local_org = local_org
        self.config_roles = tuple(r for r in config_roles if r)
        self.default_slot = default_slot
        self._holders: Dict[str, SlotHolder] = {}

    # ── names ──

    def names(self) -> tuple:
        names = list(self.config_roles)
        if self.roster is not None:
            try:
                for name in self.roster.names():
                    entry = self.roster.get(name)
                    if entry is not None and self.roster.is_local(entry) and name not in names:
                        names.append(name)
            except Exception:
                logger.debug("roster unavailable for slot names", exc_info=True)
        return tuple(names)

    def entry(self, role: str):
        if self.roster is None:
            return None
        try:
            return self.roster.get(role)
        except Exception:
            return None

    def is_emergency(self, role: str) -> bool:
        entry = self.entry(role)
        vehicle_type = (getattr(entry, "vehicle_type", "") or "").lower() if entry is not None else ""
        return any(hint in vehicle_type for hint in EMERGENCY_TYPE_HINTS)

    # ── holders ──

    def hold(self, role: str, by: str, marker=None, actor_id: Optional[int] = None) -> None:
        self._holders[role] = SlotHolder(by=by, marker=marker, actor_id=actor_id)

    def release(self, role: str, marker=None) -> None:
        holder = self._holders.get(role)
        if holder is not None and (marker is None or holder.marker == marker):
            del self._holders[role]

    def holder(self, role: str) -> Optional[SlotHolder]:
        return self._holders.get(role)

    def held_by(self, by: str) -> List[str]:
        return [role for role, holder in self._holders.items() if holder.by == by]

    # ── state ──

    def state_of(self, role: str, actors: list) -> dict:
        entry = self.entry(role)
        out = {
            "name": role,
            "vehicle_type": getattr(entry, "vehicle_type", "") if entry is not None else "",
            "bsm_id": getattr(entry, "bsm_id", "") if entry is not None else "",
            "emergency": self.is_emergency(role),
            "state": "free",
            "holder": "",
            "actor_id": None,
        }
        carriers = [a for a in actors if _role_of(a) == role]
        holder = self._holders.get(role)
        if holder is not None:
            actor_id = holder.actor_id
            if actor_id is None and carriers:
                actor_id = getattr(carriers[0], "id", None)
            out.update(state=holder.by, holder=holder.by, actor_id=actor_id)
            return out
        kept = self.keeper.match(role, carriers)
        if kept is not None:
            rec = self.keeper.record(role) or {}
            out.update(state="kept", holder=str(rec.get("by", "")), actor_id=kept.id)
        elif carriers:
            out.update(state="taken", holder="foreign", actor_id=getattr(carriers[0], "id", None))
        return out

    def states(self, world) -> List[dict]:
        actors = vehicle_actors(world, tries=1)
        return [self.state_of(role, actors) for role in self.names()]

    def status(self, world) -> dict:
        names = self.names()
        scenario = ""
        if self.roster is not None:
            try:
                scenario = str(self.roster.status().get("scenario", "") or "")
            except Exception:
                scenario = ""
        return {
            "slots": self.states(world),
            "local_org": self.local_org,
            "default": self.default_slot if self.default_slot in names else (names[-1] if names else ""),
            "available": bool(names),
            "scenario": scenario,
        }

    # ── claims ──

    def claim(self, world, role: str, by: str, marker=None):
        """Validate ``role`` for ``by``; return the parked actor to adopt, or None to spawn a new one.

        Raises ``SlotClaimError`` with code ``unknown_slot`` (not one of our names),
        ``slot_busy`` (a session or the fleet holds it) or ``slot_taken`` (an actor we
        did not create carries the name).
        """
        role = str(role or "").strip()
        names = self.names()
        if not role:
            raise SlotClaimError("unknown_slot", "no scenario slot given")
        if role not in names:
            raise SlotClaimError(
                "unknown_slot",
                "%s is not one of our scenario slots (%s)" % (role, ", ".join(names) or "none loaded"))
        holder = self._holders.get(role)
        if holder is not None:
            who = "a drive session" if holder.by == "session" else "the fleet"
            raise SlotClaimError("slot_busy", "%s is in use by %s" % (role, who))
        actors = vehicle_actors(world, tries=3)
        carriers = [a for a in actors if _role_of(a) == role]
        if not carriers:
            self.keeper.forget(role)  # a stale record: that car is gone
            return None
        kept = self.keeper.match(role, carriers)
        if kept is None:
            self.keeper.forget(role)
            raise SlotClaimError(
                "slot_taken",
                "%s is already in the world as actor %s, created by another program (the DT manual car or "
                "background traffic)" % (role, getattr(carriers[0], "id", "?")))
        return kept


# ── parking helpers shared by sessions and the fleet ──

def _driving_waypoint(carla_map, location):
    """``get_waypoint`` restricted to driving lanes without projection; None when off the road."""
    try:
        import carla
        lane_type = getattr(carla, "LaneType", None)
        if lane_type is not None and hasattr(lane_type, "Driving"):
            return carla_map.get_waypoint(location, project_to_road=False, lane_type=lane_type.Driving)
    except TypeError:
        pass
    except Exception:
        return None
    try:
        return carla_map.get_waypoint(location, project_to_road=False)
    except Exception:
        return None


def off_road_transform(carla_map, transform, offsets=(5.0, 6.0, 7.0, 8.0, 9.0, 10.0), half_length_m: float = 2.6):
    """A spot beside the lane at ``transform`` where the whole car is off every driving lane, or None.

    Tries the right side of the lane first (the verge in right-hand traffic), then the left.
    """
    import carla
    loc = transform.location
    if _driving_waypoint(carla_map, loc) is None:
        return None  # already off the road
    try:
        wp = carla_map.get_waypoint(loc, project_to_road=True)
        base = wp.transform if wp is not None else transform
    except Exception:
        base = transform
    yaw = math.radians(float(base.rotation.yaw))
    fx, fy = math.cos(yaw), math.sin(yaw)
    rx, ry = -fy, fx  # right-hand side in CARLA's left-handed frame
    for side in (1.0, -1.0):
        for d in offsets:
            px = base.location.x + rx * d * side
            py = base.location.y + ry * d * side
            spots = [carla.Location(x=px + fx * s, y=py + fy * s, z=loc.z) for s in (0.0, -half_length_m, half_length_m)]
            if all(_driving_waypoint(carla_map, s) is None for s in spots):
                return carla.Transform(carla.Location(x=px, y=py, z=loc.z + 0.3), carla.Rotation(yaw=float(base.rotation.yaw)))
    return None


def park_actor(carla_map, actor, move_off_road: bool = True) -> dict:
    """Stop ``actor`` with the hand brake and lights off; move it beside the lane when the map allows."""
    import carla
    info = {"moved": False, "x": None, "y": None}
    spot = None
    if move_off_road:
        try:
            spot = off_road_transform(carla_map, actor.get_transform())
        except Exception as exc:
            logger.debug("off-road search failed: %s", exc)
    if spot is not None:
        try:
            actor.set_transform(spot)
            info.update(moved=True, x=round(spot.location.x, 1), y=round(spot.location.y, 1))
        except Exception as exc:
            logger.debug("park teleport failed: %s", exc)
    try:
        actor.set_target_velocity(carla.Vector3D(0.0, 0.0, 0.0))
    except Exception:
        pass
    try:
        actor.apply_control(carla.VehicleControl(throttle=0.0, brake=1.0, hand_brake=True))
    except Exception:
        pass
    try:
        actor.set_light_state(carla.VehicleLightState(0))
    except Exception:
        pass
    return info


def reset_adopted_actor(actor, transform) -> None:
    """Put a parked slot car at ``transform``, standing still with neutral controls."""
    import carla
    for step in (
        lambda: actor.set_transform(transform),
        lambda: actor.set_target_velocity(carla.Vector3D(0.0, 0.0, 0.0)),
        lambda: actor.apply_control(carla.VehicleControl()),
        lambda: actor.set_light_state(carla.VehicleLightState(0)),
    ):
        try:
            step()
        except Exception as exc:
            logger.debug("adopted actor reset step failed: %s", exc)
