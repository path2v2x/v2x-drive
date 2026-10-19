"""Behavior parameters for the civilian and emergency vehicle (ERV) models.

Numbers come from the sources collected for the plan: CARMA's approaching
ERV plugin, state yield / move-over statutes (limit minus 10 mph, 300 ft
follow gap), FHWA-HRT-24-063 pull-over guidance and fire / police driving
policies (limit plus 10 mph, full stops at controls, no passing on the
right).  Everything is in SI internally; the ``*_kmh`` fields are converted
with :func:`kmh_to_mps`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Optional


def kmh_to_mps(v: float) -> float:
    return float(v) / 3.6


def mps_to_kmh(v: float) -> float:
    return float(v) * 3.6


@dataclass
class CivilianProfile:
    name: str = "civilian"
    blueprint: str = "vehicle.lincoln.mkz"
    cruise_kmh: Optional[float] = None       # None: drive at the posted limit
    # approaching ERV (behind, same direction)
    monitor_distance_m: float = 300.0
    monitor_ttp_s: float = 60.0
    alert_distance_m: float = 150.0
    alert_ttp_s: float = 25.0
    signal_before_pullover_s: float = 3.0
    junction_clear_s: float = 2.0            # do not stop within this many seconds of a junction
    edge_margin_m: float = 0.3               # stay this far inside the lane line
    shoulder_use_m: float = 1.0              # usable shoulder / parking width
    stop_speed_mps: float = 0.3
    hold_max_s: float = 90.0
    clear_past_m: float = 30.0               # ERV counts as passed this far ahead
    merge_wait_s: float = 3.0
    merge_signal_s: float = 3.0
    merge_settle_s: float = 4.0
    follow_erv_gap_m: float = 91.0           # 300 ft behind an ERV
    slow_in_lane_ttp_s: float = 8.0
    slow_in_lane_delta_kmh: float = 16.0
    move_over_min_lanes: int = 2
    move_over_min_limit_kmh: float = 72.0
    move_over_delta_kmh: float = 16.0
    # stopped ERV ahead
    stopped_erv_zone_before_m: float = 150.0
    stopped_erv_zone_after_m: float = 50.0
    stopped_erv_delta_kmh: float = 32.0
    stopped_erv_min_kmh: float = 8.0
    stopped_erv_shift_m: float = 0.5
    wait_behind_gap_m: float = 10.0
    track_timeout_s: float = 15.0


@dataclass
class ErvProfile:
    name: str = "ambulance"
    blueprint: str = "vehicle.ambulance.ford"
    role: str = "ambulance"
    over_limit_kmh: float = 16.0
    centerline_offset_m: float = -0.3        # negative = toward the centerline (left)
    junction_approach_m: float = 40.0
    junction_stop_m: float = 6.0
    stop_hold_s: float = 1.0
    crossing_kmh: float = 10.0
    crossing_ttc_s: float = 5.0
    follow_gap_m: float = 30.0
    wait_for_yield_s: float = 1.6
    pass_kmh: float = 32.0
    pass_clearance_m: float = 0.5
    opposing_pass: bool = True
    opposing_sight_m: float = 45.0           # RFS junctions are dense; 80 m of clear road is rare
    opposing_ttc_s: float = 5.0
    no_progress_s: float = 10.0
    no_progress_speed_mps: float = 0.5
    resume_gap_m: float = 15.0
    arrive_m: float = 8.0
    stop_at_stop_signs: bool = True
    treat_signals_as_red: bool = True
    blind_junction_stop: bool = False
    response_type: str = "emergency"
    doors_open_on_arrival: bool = True


ERV_PROFILES = {
    "ambulance": ErvProfile(),
    "fire": ErvProfile(name="fire", blueprint="vehicle.firetruck.actors", role="fire",
                       over_limit_kmh=16.0, stop_hold_s=2.0, opposing_pass=False,
                       doors_open_on_arrival=False),
    "police": ErvProfile(name="police", blueprint="vehicle.dodgecop.charger", role="police",
                         over_limit_kmh=24.0, response_type="pursuit", doors_open_on_arrival=False),
}

CIVILIAN_PROFILES = {
    "civilian": CivilianProfile(),
    "cautious": CivilianProfile(name="cautious", monitor_distance_m=400.0, alert_distance_m=200.0,
                                alert_ttp_s=35.0, signal_before_pullover_s=2.0),
}


def _apply_overrides(profile, overrides: Optional[dict]):
    if not overrides:
        return profile
    fields = {f.name: f for f in dataclasses.fields(profile)}
    clean = {}
    for key, value in overrides.items():
        fld = fields.get(str(key))
        if fld is None or fld.name in ("name",):
            continue
        current = getattr(profile, fld.name)
        try:
            if isinstance(current, bool):
                clean[fld.name] = bool(value)
            elif isinstance(current, int) and not isinstance(current, bool):
                clean[fld.name] = int(value)
            elif isinstance(current, float) or current is None:
                clean[fld.name] = None if value is None else float(value)
            else:
                clean[fld.name] = str(value)
        except (TypeError, ValueError):
            continue
    return dataclasses.replace(profile, **clean)


def erv_profile(name: str = "ambulance", overrides: Optional[dict] = None) -> ErvProfile:
    base = ERV_PROFILES.get(str(name or "ambulance").lower(), ERV_PROFILES["ambulance"])
    return _apply_overrides(base, overrides)


def civilian_profile(name: str = "civilian", overrides: Optional[dict] = None) -> CivilianProfile:
    base = CIVILIAN_PROFILES.get(str(name or "civilian").lower(), CIVILIAN_PROFILES["civilian"])
    return _apply_overrides(base, overrides)


def profile_to_dict(profile) -> dict:
    return dataclasses.asdict(profile)
