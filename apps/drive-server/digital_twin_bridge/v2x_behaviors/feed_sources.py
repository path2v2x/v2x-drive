"""Where the models learn about emergency vehicles.

* :class:`HubErvSource` — the viewer's TENA/UDP feed: the latest BSM per
  sender is decoded (cached) and its Part II siren / light bar flags feed
  the tracker.  Positions come from the mapped CARLA actor (ghosts and our
  scenario cars exist in the world) or from WGS-84 coordinates.
* :class:`LightStateErvSource` — no DT needed: an emergency-blueprint
  vehicle (ambulance, fire truck, police car) with CARLA's ``Special1`` light
  on counts as siren and lights in use.  This is what a human driving
  PATH-M-1 with Ctrl+L produces, and what our own fleet ERVs set.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Iterable, Optional, Tuple

from digital_twin_bridge.v2x_behaviors.bsm_types import LIGHT_SPECIAL1

logger = logging.getLogger(__name__)

EMERGENCY_HINTS = {"ambulance": "ambulance", "firetruck": "fire", "dodgecop": "police", "police": "police"}


def emergency_role_of(type_id: str) -> Optional[str]:
    tid = str(type_id or "").lower()
    for hint, role in EMERGENCY_HINTS.items():
        if hint in tid:
            return role
    return None


class HubErvSource:
    def __init__(self, hub, codec, registry=None, stale_s: float = 3.0):
        self.hub = hub
        self.codec = codec
        self.registry = registry
        self.stale_s = float(stale_s)

    def update(self, tracker, now: float, pose_of_actor: Callable[[int], Optional[Tuple[float, float, float]]],
               gps_to_xy: Optional[Callable[[float, float], Tuple[float, float]]] = None) -> int:
        if self.hub is None or self.codec is None:
            return 0
        updated = 0
        for key in self.hub.senders():
            if key.startswith("udp:"):
                continue
            latest = self.hub.latest_raw(key)
            if latest is None:
                continue
            raw, t_rx = latest
            if now - t_rx > self.stale_s:
                continue
            try:
                frame = self.codec.decode(raw)
            except Exception as exc:
                logger.debug("decode for tracker failed: %s", exc)
                continue
            if frame.msg_type != "BSM" or not frame.summary:
                continue
            actor_id = self.registry.actor_for_key(key) if self.registry is not None else None
            pose = pose_of_actor(actor_id) if actor_id is not None else None
            if tracker.update_from_bsm(key, frame.summary, now, actor_pose=pose, actor_id=actor_id,
                                       gps_to_xy=gps_to_xy) is not None:
                updated += 1
        return updated


class LightStateErvSource:
    def update(self, tracker, now: float, vehicles: Iterable, exclude_ids: Iterable[int] = ()) -> int:
        """``vehicles``: iterable of (actor, x, y, yaw_deg, speed_mps)."""
        excluded = set(exclude_ids)
        updated = 0
        for actor, x, y, yaw, speed in vehicles:
            actor_id = getattr(actor, "id", None)
            if actor_id in excluded:
                continue
            role = emergency_role_of(getattr(actor, "type_id", ""))
            if role is None:
                continue
            try:
                state = actor.get_light_state()
                special = bool(int(state) & LIGHT_SPECIAL1)
            except Exception:
                special = False
            tracker.update_from_lights("actor:%s" % actor_id, actor_id, x, y, yaw, speed, special, now, role=role)
            updated += 1
        return updated
