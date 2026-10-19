"""Outliner for the Editor mode: every actor of the world, grouped and described, read-only.

Groups, in the order the panel shows them:

* ``ego``        a browser session's car
* ``voices``     our organisation's scenario vehicles (PATH-M-1, PATH-BG-n), whoever drives them
* ``ghost``      the other organisations' scenario vehicles the DT adapter clones into our world
* ``fleet``      the V2X driving models' civilians and emergency vehicles
* ``traffic``    the Traffic panel's cars
* ``dynamic``    Add Actor cars with their geofences
* ``trajectory`` the trajectory playback car
* ``foreign``    vehicles nobody here created (HIL_Tool traffic, other clients)
* ``walker``, ``prop``, ``sensor``, ``light`` (traffic lights and signs), ``other``

The owner says who may touch an actor: ``session``, ``fleet``, ``drive`` (this
server), ``dt`` (the DT stack), ``world`` (map furniture) or ``other``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from digital_twin_bridge.actor_ownership import DRIVE_ROLE_PREFIXES

GROUP_ORDER = ["ego", "voices", "ghost", "fleet", "traffic", "dynamic", "trajectory", "foreign",
               "walker", "prop", "sensor", "light", "other"]

LIGHT_BITS = (
    (1, "Position"), (2, "LowBeam"), (4, "HighBeam"), (8, "Brake"), (16, "RightBlinker"),
    (32, "LeftBlinker"), (64, "Reverse"), (128, "Fog"), (256, "Interior"), (512, "Special1"), (1024, "Special2"),
)


@dataclass
class OutlinerContext:
    session_egos: dict = field(default_factory=dict)      # actor id -> session marker
    traffic_ids: set = field(default_factory=set)
    dynamic_ids: set = field(default_factory=set)
    kept_ids: set = field(default_factory=set)            # parked slot cars this server created
    editor_roles: set = field(default_factory=set)        # role names of editor cameras
    fleet_kind_of: Optional[Callable[[int], Optional[str]]] = None
    roster: object = None                                 # ScenarioRoster-like: get(name), is_local(entry)


def role_of(actor) -> str:
    try:
        attrs = actor.attributes
        return str(attrs.get("role_name", "") or "") if attrs else ""
    except Exception:
        return ""


def display_name(type_id: str) -> str:
    """``vehicle.tesla.model3`` -> ``Tesla Model3`` (manual_control.py's get_actor_display_name)."""
    return " ".join(str(type_id or "").replace("_", ".").title().split(".")[1:])


def decode_lights(mask) -> list:
    try:
        value = int(mask)
    except (TypeError, ValueError):
        return []
    return [name for bit, name in LIGHT_BITS if value & bit]


def classify(actor, ctx: OutlinerContext):
    """(group, owner) of an actor; see the module docstring."""
    type_id = str(getattr(actor, "type_id", "") or "")
    role = role_of(actor)
    actor_id = getattr(actor, "id", None)
    drive_role = any(role.startswith(prefix) for prefix in DRIVE_ROLE_PREFIXES)
    if type_id.startswith("sensor."):
        if role in ctx.editor_roles or drive_role:
            return "sensor", "drive"
        parent = getattr(actor, "parent", None)
        if parent is not None and getattr(parent, "id", None) in ctx.session_egos:
            return "sensor", "session"
        return "sensor", "other"
    if type_id.startswith("static.prop."):
        return "prop", "drive"
    if type_id.startswith("traffic."):
        return "light", "world"
    if type_id.startswith("walker."):
        return "walker", "drive" if drive_role else "other"
    if type_id.startswith("vehicle."):
        if actor_id in ctx.session_egos:
            return "ego", "session"
        kind = ctx.fleet_kind_of(actor_id) if (ctx.fleet_kind_of is not None and actor_id is not None) else None
        entry = None
        if ctx.roster is not None and role:
            try:
                entry = ctx.roster.get(role)
            except Exception:
                entry = None
        if entry is not None:
            local = False
            try:
                local = bool(ctx.roster.is_local(entry))
            except Exception:
                pass
            if not local:
                return "ghost", "dt"
            if kind:
                return "voices", "fleet"
            if actor_id in ctx.kept_ids:
                return "voices", "drive"
            return "voices", "dt"
        if kind:
            return "fleet", "fleet"
        if actor_id in ctx.traffic_ids:
            return "traffic", "drive"
        if actor_id in ctx.dynamic_ids:
            return "dynamic", "drive"
        if role.startswith("trajectory"):
            return "trajectory", "drive"
        if drive_role:
            return "other", "drive"
        return "foreign", "other"
    if type_id == "spectator" or not type_id:
        return "", ""
    return "other", "other"


def describe(actor, group: str, owner: str) -> dict:
    pos, yaw = None, None
    try:
        t = actor.get_transform()
        pos = [round(float(t.location.x), 2), round(float(t.location.y), 2), round(float(t.location.z), 2)]
        yaw = round(float(t.rotation.yaw), 1)
    except Exception:
        pass
    speed = None
    try:
        v = actor.get_velocity()
        speed = round(math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z), 2)
    except Exception:
        pass
    parent = getattr(actor, "parent", None)
    type_id = str(getattr(actor, "type_id", "") or "")
    return {
        "id": getattr(actor, "id", None),
        "type_id": type_id,
        "name": display_name(type_id) or type_id,
        "role": role_of(actor),
        "group": group,
        "owner": owner,
        "pos": pos,
        "yaw": yaw,
        "speed_mps": speed,
        "parent": getattr(parent, "id", None) if parent is not None else None,
        "alive": bool(getattr(actor, "is_alive", True)),
    }


def list_actors(world, ctx: OutlinerContext, max_items: int = 400) -> dict:
    """Every listable actor, sorted by group then name, capped at ``max_items``."""
    entries = []
    try:
        actors = list(world.get_actors())
    except Exception:
        actors = []
    for actor in actors:
        group, owner = classify(actor, ctx)
        if not group:
            continue
        entries.append(describe(actor, group, owner))
    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    entries.sort(key=lambda e: (order.get(e["group"], len(order)), e["name"], e["id"] or 0))
    counts: dict = {}
    for e in entries:
        counts[e["group"]] = counts.get(e["group"], 0) + 1
    total = len(entries)
    max_items = max(1, int(max_items))
    return {"actors": entries[:max_items], "total": total, "truncated": total > max_items, "groups": counts}


def inspect(world, actor_id: int, ctx: OutlinerContext) -> Optional[dict]:
    """The outliner entry of one actor plus everything else cheap to read about it."""
    try:
        actor = world.get_actor(int(actor_id))
    except Exception:
        actor = None
    if actor is None:
        return None
    group, owner = classify(actor, ctx)
    details = describe(actor, group or "other", owner or "other")
    attrs: dict = {}
    try:
        for key, value in list(actor.attributes.items())[:24]:
            attrs[str(key)] = str(value)
    except Exception:
        pass
    details["attributes"] = attrs
    try:
        ext = actor.bounding_box.extent
        details["bbox"] = [round(float(ext.x), 2), round(float(ext.y), 2), round(float(ext.z), 2)]
    except Exception:
        details["bbox"] = None
    try:
        v = actor.get_velocity()
        details["velocity"] = [round(float(v.x), 2), round(float(v.y), 2), round(float(v.z), 2)]
    except Exception:
        details["velocity"] = None
    try:
        t = actor.get_transform()
        details["rot"] = [round(float(t.rotation.pitch), 1), round(float(t.rotation.yaw), 1), round(float(t.rotation.roll), 1)]
    except Exception:
        details["rot"] = None
    if details["type_id"].startswith("vehicle."):
        try:
            c = actor.get_control()
            details["control"] = {
                "throttle": round(float(getattr(c, "throttle", 0.0)), 2),
                "steer": round(float(getattr(c, "steer", 0.0)), 2),
                "brake": round(float(getattr(c, "brake", 0.0)), 2),
                "hand_brake": bool(getattr(c, "hand_brake", False)),
                "reverse": bool(getattr(c, "reverse", False)),
                "gear": int(getattr(c, "gear", 0)),
                "manual_gear_shift": bool(getattr(c, "manual_gear_shift", False)),
            }
        except Exception:
            details["control"] = None
        try:
            details["lights"] = decode_lights(actor.get_light_state())
        except Exception:
            details["lights"] = None
        try:
            details["speed_limit"] = round(float(actor.get_speed_limit()), 0)
        except Exception:
            details["speed_limit"] = None
    parent = getattr(actor, "parent", None)
    details["parent_type"] = str(getattr(parent, "type_id", "") or "") if parent is not None else None
    return details
