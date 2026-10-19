"""Tracks of emergency vehicles the models react to (pure).

A track exists per sender (a VOICES name such as PATH-M-1, or ``actor:<id>``
for a CARLA vehicle observed through its light state).  It is *active* when
both the siren and the light bar are in use (J2735 Part II
``vehicleAlerts``; the DT adapter sets both from CARLA's ``Special1`` light),
and *stationary* when the BSM says ``responseType stationary`` or the vehicle
has been slower than 5 km/h for a minute.  Tracks expire after
``timeout_s`` without an update.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from digital_twin_bridge.v2x_behaviors.bsm_types import (
    lights_active,
    response_stationary,
    role_is_emergency,
    role_name,
    siren_active,
)
from digital_twin_bridge.v2x_behaviors.geometry import compass_to_carla_yaw


@dataclass
class ErvTrack:
    key: str
    actor_id: Optional[int] = None
    x: Optional[float] = None
    y: Optional[float] = None
    yaw_deg: Optional[float] = None
    speed_mps: float = 0.0
    siren: bool = False
    lights: bool = False
    role: str = ""
    response_type: Optional[str] = None
    source: str = "bsm"            # bsm | lights | fleet
    t_update: float = 0.0
    t_slow_since: Optional[float] = None
    fleet_state: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.siren and self.lights

    @property
    def positioned(self) -> bool:
        return self.x is not None and self.y is not None and self.yaw_deg is not None

    def is_stationary(self, now: float, slow_after_s: float = 60.0) -> bool:
        if response_stationary(self.response_type):
            return True
        return self.t_slow_since is not None and now - self.t_slow_since >= slow_after_s

    def to_dict(self, now: float) -> dict:
        return {
            "key": self.key, "actor_id": self.actor_id, "x": self.x, "y": self.y, "yaw": self.yaw_deg,
            "speed_mps": round(self.speed_mps, 2), "siren": self.siren, "lights": self.lights,
            "active": self.active, "stationary": self.is_stationary(now), "role": self.role,
            "response_type": self.response_type, "source": self.source,
            "age_s": round(max(0.0, now - self.t_update), 2), "fleet_state": self.fleet_state,
        }


class ErvTracker:
    def __init__(self, timeout_s: float = 15.0, stationary_speed_mps: float = 1.39, stationary_after_s: float = 60.0):
        self.timeout_s = float(timeout_s)
        self.stationary_speed_mps = float(stationary_speed_mps)
        self.stationary_after_s = float(stationary_after_s)
        self._tracks: Dict[str, ErvTrack] = {}

    # ── updates ──

    def _touch(self, track: ErvTrack, now: float) -> None:
        if track.speed_mps <= self.stationary_speed_mps:
            if track.t_slow_since is None:
                track.t_slow_since = now
        else:
            track.t_slow_since = None
        track.t_update = now

    def update_from_bsm(self, key: str, summary: dict, now: float,
                        actor_pose: Optional[Tuple[float, float, float]] = None,
                        actor_id: Optional[int] = None,
                        gps_to_xy: Optional[Callable[[float, float], Tuple[float, float]]] = None) -> Optional[ErvTrack]:
        """Update a track from a decoded BSM summary (see j2735_codec.summarize_bsm).

        Only vehicles that look like emergency vehicles are tracked: an
        emergency ``role`` or any siren / light bar report.  Position comes
        from the mapped CARLA actor when known, else from WGS-84 coordinates
        through ``gps_to_xy``; origin-relative PATH coordinates are not usable.
        """
        if not isinstance(summary, dict):
            return None
        lights = summary.get("lights") or {}
        role = role_name(lights.get("role"))
        siren = siren_active(lights.get("siren"))
        bar = lights_active(lights.get("lights"))
        emergency = role_is_emergency(role) or siren or bar or lights.get("siren") is not None
        track = self._tracks.get(key)
        if not emergency and track is None:
            return None
        if track is None:
            track = ErvTrack(key=key, source="bsm")
            self._tracks[key] = track
        track.siren = siren
        track.lights = bar
        if role:
            track.role = role
        rt = lights.get("response_type")
        track.response_type = str(rt) if rt is not None else None
        speed = summary.get("speed_mps")
        if isinstance(speed, (int, float)):
            track.speed_mps = float(speed)
        if actor_pose is not None:
            track.x, track.y, track.yaw_deg = actor_pose
            track.actor_id = actor_id if actor_id is not None else track.actor_id
        else:
            lat, lon = summary.get("lat"), summary.get("lon")
            if (gps_to_xy is not None and summary.get("coords") == "wgs84"
                    and isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
                try:
                    track.x, track.y = gps_to_xy(float(lat), float(lon))
                except Exception:
                    pass
            heading = summary.get("heading_deg")
            if isinstance(heading, (int, float)):
                track.yaw_deg = compass_to_carla_yaw(float(heading))
        track.source = "bsm"
        self._touch(track, now)
        return track

    def update_from_lights(self, key: str, actor_id: Optional[int], x: float, y: float, yaw_deg: float,
                           speed_mps: float, special_on: bool, now: float, role: str = "",
                           source: str = "lights", fleet_state: Optional[str] = None,
                           siren: Optional[bool] = None, response_type: Optional[str] = None) -> ErvTrack:
        """Update a track from a CARLA vehicle's light state (Special1 = bar + siren)."""
        track = self._tracks.get(key)
        if track is None:
            track = ErvTrack(key=key, source=source)
            self._tracks[key] = track
        track.actor_id = actor_id
        track.x, track.y, track.yaw_deg = float(x), float(y), float(yaw_deg)
        track.speed_mps = float(speed_mps)
        track.lights = bool(special_on)
        track.siren = bool(special_on) if siren is None else bool(siren)
        if role:
            track.role = role
        track.source = source
        track.fleet_state = fleet_state
        track.response_type = response_type
        self._touch(track, now)
        return track

    def remove(self, key: str) -> None:
        self._tracks.pop(key, None)

    def expire(self, now: float) -> List[str]:
        gone = [k for k, t in self._tracks.items() if now - t.t_update > self.timeout_s]
        for k in gone:
            del self._tracks[k]
        return gone

    # ── queries ──

    def get(self, key: str) -> Optional[ErvTrack]:
        return self._tracks.get(key)

    def tracks(self) -> List[ErvTrack]:
        return list(self._tracks.values())

    def active_tracks(self, now: Optional[float] = None) -> List[ErvTrack]:
        return [t for t in self._tracks.values() if t.active and t.positioned
                and (now is None or now - t.t_update <= self.timeout_s)]

    def by_actor(self, actor_id: int) -> Optional[ErvTrack]:
        for t in self._tracks.values():
            if t.actor_id == actor_id:
                return t
        return None

    def nearest(self, x: float, y: float, max_m: float = math.inf) -> Optional[ErvTrack]:
        best, best_d = None, max_m
        for t in self._tracks.values():
            if not t.positioned:
                continue
            d = math.hypot(t.x - x, t.y - y)
            if d <= best_d:
                best, best_d = t, d
        return best

    def snapshot(self, now: float) -> List[dict]:
        return [t.to_dict(now) for t in self._tracks.values()]
