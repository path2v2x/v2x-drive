"""Civilian driver state machine (pure).

How a law-abiding driver reacts to emergency vehicles, adapted to the RFS
roads (mostly two-way single-lane, narrow shoulders):

* an active ERV approaching from behind: monitor from 300 m / 60 s, signal
  right, pull to the right edge of the lane (or stop in the lane when it is
  too narrow), hold with hazards until the ERV is 30 m past, wait 3 s, merge
  back with the left blinker and stay 91 m behind it;
* the ERV coming up in *my* lane with no time to pull over: slow in lane,
  hazards on; on a road with two same-direction lanes and a 72 km/h limit:
  move over one lane instead of stopping (CARMA style);
* a stopped ERV ahead with lights on: slow to limit minus 32 km/h (never
  below 8 km/h) and shift half a metre away while passing, move to a
  non-adjacent lane when there is one; a fully blocked single lane: wait
  behind, pass via the opposing lane when it is clear.

Inputs are plain numbers in :class:`CivilianObservation` (the CARLA binding
fills them); outputs are a :class:`CivilianDecision` the controller turns
into target speed, lateral offset, lights and lane changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from digital_twin_bridge.v2x_behaviors.geometry import INF, pull_over_offset, time_to_pass
from digital_twin_bridge.v2x_behaviors.profiles import CivilianProfile, kmh_to_mps


class CivState(str, Enum):
    NORMAL = "NORMAL"
    MONITOR = "MONITOR"
    YIELD_PREP = "YIELD_PREP"
    PULL_OVER = "PULL_OVER"
    STOPPED_HOLD = "STOPPED_HOLD"
    WAIT_CLEAR = "WAIT_CLEAR"
    MERGE_BACK = "MERGE_BACK"
    SLOW_IN_LANE = "SLOW_IN_LANE"
    MOVE_OVER = "MOVE_OVER"
    PASS_STOPPED_ERV = "PASS_STOPPED_ERV"
    WAIT_BEHIND = "WAIT_BEHIND"
    OPPOSING_PASS = "OPPOSING_PASS"


@dataclass
class ErvRelation:
    """One tracked ERV as seen from this vehicle."""
    key: str
    gap_m: float               # along-track distance: positive = ERV behind me, negative = ahead
    lateral_m: float = 0.0     # ERV offset in my frame, positive right
    erv_speed_mps: float = 0.0
    my_speed_mps: float = 0.0
    same_lane: bool = False
    same_road: bool = True     # same road, same direction
    active: bool = True        # siren + lights
    stationary: bool = False

    def ttp_s(self) -> float:
        return time_to_pass(self.gap_m, self.erv_speed_mps, self.my_speed_mps)


@dataclass
class CivilianObservation:
    t: float
    speed_mps: float
    speed_limit_mps: float
    lane_width_m: float = 3.5
    half_width_m: float = 1.0
    shoulder_m: float = 0.0
    same_dir_lanes: int = 1
    two_way: bool = True
    dist_to_junction_m: float = INF
    in_junction: bool = False
    lead_gap_m: Optional[float] = None
    lead_speed_mps: Optional[float] = None
    ervs: List[ErvRelation] = field(default_factory=list)
    opposing_clear: bool = False
    lane_change_done: bool = False
    pass_done: bool = False


@dataclass
class CivilianDecision:
    state: str
    target_speed_mps: float
    lateral_offset_m: float = 0.0
    stop: bool = False
    lights: Tuple[str, ...] = ()        # 'left' | 'right' | 'hazard' | 'brake'
    lane_change: Optional[str] = None   # 'left' | 'right' (request, once)
    pass_path: Optional[str] = None     # 'opposing' (request, once)
    reason: str = ""
    erv_key: Optional[str] = None


@dataclass
class Transition:
    t: float
    src: str
    dst: str
    reason: str


class CivilianFsm:
    def __init__(self, profile: Optional[CivilianProfile] = None):
        self.p = profile or CivilianProfile()
        self.state = CivState.NORMAL
        self.t_state = 0.0
        self.t_signal: Optional[float] = None
        self.t_stop: Optional[float] = None
        self.t_clear: Optional[float] = None
        self.target_erv: Optional[str] = None
        self.pull_offset = 0.0
        self.pending_lane_change: Optional[str] = None
        self.pending_pass: Optional[str] = None
        self.pass_side = -1.0
        self.transitions: List[Transition] = []
        self.last_reason = ""

    # ── helpers ──

    def _enter(self, state: CivState, t: float, reason: str) -> None:
        if state != self.state:
            self.transitions.append(Transition(t, self.state.value, state.value, reason))
        self.state = state
        self.t_state = t
        self.last_reason = reason

    def _cruise(self, obs: CivilianObservation) -> float:
        if self.p.cruise_kmh is not None:
            return min(kmh_to_mps(self.p.cruise_kmh), obs.speed_limit_mps)
        return obs.speed_limit_mps

    def _approaching(self, obs: CivilianObservation, exclude: Optional[str] = None) -> Optional[ErvRelation]:
        """Nearest active ERV behind me inside the monitoring envelope."""
        best = None
        for r in obs.ervs:
            if r.key == exclude or not r.active or r.stationary or not r.same_road or r.gap_m <= 0:
                continue
            if r.gap_m <= self.p.monitor_distance_m or r.ttp_s() <= self.p.monitor_ttp_s:
                if best is None or r.gap_m < best.gap_m:
                    best = r
        return best

    def _stopped_ahead(self, obs: CivilianObservation) -> Optional[ErvRelation]:
        best = None
        for r in obs.ervs:
            if not r.stationary or not r.same_road or r.gap_m >= 0:
                continue
            if -r.gap_m <= self.p.stopped_erv_zone_before_m:
                if best is None or r.gap_m > best.gap_m:
                    best = r
        return best

    @staticmethod
    def _passed(rel: Optional[ErvRelation], clear_past_m: float) -> bool:
        return rel is not None and rel.gap_m < 0 and -rel.gap_m >= clear_past_m

    def _imminent(self, rel: ErvRelation) -> bool:
        return rel.gap_m <= self.p.alert_distance_m or rel.ttp_s() <= self.p.alert_ttp_s

    def _move_over_possible(self, obs: CivilianObservation) -> bool:
        return (obs.same_dir_lanes >= self.p.move_over_min_lanes
                and obs.speed_limit_mps >= kmh_to_mps(self.p.move_over_min_limit_kmh))

    def _away_from(self, rel: ErvRelation) -> str:
        """Lane change direction that gives the ERV room (ERV on my left/behind -> right)."""
        return "right" if rel.lateral_m <= 0.5 else "left"

    def _begin_stopped_erv(self, obs: CivilianObservation, rel: ErvRelation, t: float) -> None:
        self.target_erv = rel.key
        # the ERV sits right or left of my path: shift the other way while passing
        self.pass_side = -1.0 if rel.lateral_m >= 0 else 1.0
        if obs.same_dir_lanes >= 2 and rel.same_lane:
            self.pending_lane_change = self._away_from(rel)
            self._enter(CivState.PASS_STOPPED_ERV, t, "stopped ERV in my lane: change lane")
        elif rel.same_lane:
            self._enter(CivState.WAIT_BEHIND, t, "stopped ERV blocks my lane")
        else:
            self._enter(CivState.PASS_STOPPED_ERV, t, "stopped ERV ahead: slow and shift")

    # ── main step ──

    def step(self, obs: CivilianObservation) -> CivilianDecision:
        p = self.p
        t = obs.t
        cruise = self._cruise(obs)
        rels = {r.key: r for r in obs.ervs}
        target = rels.get(self.target_erv) if self.target_erv else None
        approaching = self._approaching(obs)
        stopped_ahead = self._stopped_ahead(obs)
        slow_delta = kmh_to_mps(p.slow_in_lane_delta_kmh)
        s = self.state

        # ── transitions ──
        if s == CivState.NORMAL:
            if approaching is not None:
                self.target_erv = approaching.key
                self._enter(CivState.MONITOR, t, "active ERV approaching from behind")
            elif stopped_ahead is not None:
                self._begin_stopped_erv(obs, stopped_ahead, t)

        elif s == CivState.MONITOR:
            if target is None or not target.active or target.stationary:
                self.target_erv = None
                self._enter(CivState.NORMAL, t, "ERV track lost or inactive")
            elif self._passed(target, p.clear_past_m):
                self.target_erv = None
                self._enter(CivState.NORMAL, t, "ERV passed")
            elif not target.same_road:
                self.target_erv = None
                self._enter(CivState.NORMAL, t, "ERV left my road")
            elif target.gap_m > p.monitor_distance_m and target.ttp_s() > p.monitor_ttp_s:
                self.target_erv = None
                self._enter(CivState.NORMAL, t, "ERV out of range")
            elif target.same_lane and target.gap_m > 0 and target.ttp_s() < p.slow_in_lane_ttp_s:
                self._enter(CivState.SLOW_IN_LANE, t, "ERV in my lane, no time to pull over")
            elif self._imminent(target):
                if self._move_over_possible(obs):
                    self.pending_lane_change = self._away_from(target)
                    self._enter(CivState.MOVE_OVER, t, "multi-lane road: move over one lane")
                else:
                    self.t_signal = t
                    self._enter(CivState.YIELD_PREP, t, "ERV within 150 m / 25 s: signal and prepare")

        elif s == CivState.YIELD_PREP:
            if target is None or not target.active or self._passed(target, p.clear_past_m) or not target.same_road:
                self.target_erv = None
                self._enter(CivState.NORMAL, t, "ERV gone before pulling over")
            elif target.same_lane and target.gap_m > 0 and target.ttp_s() < p.slow_in_lane_ttp_s:
                self._enter(CivState.SLOW_IN_LANE, t, "ERV in my lane, no time to pull over")
            else:
                signalled = self.t_signal is not None and t - self.t_signal >= p.signal_before_pullover_s
                junction_near = obs.in_junction or obs.dist_to_junction_m < max(10.0, obs.speed_mps * p.junction_clear_s)
                if signalled and not junction_near:
                    off = pull_over_offset(obs.lane_width_m, obs.half_width_m, obs.shoulder_m,
                                           p.edge_margin_m, p.shoulder_use_m)
                    self.pull_offset = off if off is not None else 0.0
                    self._enter(CivState.PULL_OVER, t,
                                "pulling to the right edge" if off is not None else "lane too narrow: stopping in lane")

        elif s == CivState.PULL_OVER:
            if obs.speed_mps <= p.stop_speed_mps:
                self.t_stop = t
                self._enter(CivState.STOPPED_HOLD, t, "stopped")
            elif target is None or not target.active or self._passed(target, p.clear_past_m):
                self.t_clear = t
                self._enter(CivState.WAIT_CLEAR, t, "ERV passed while slowing")

        elif s == CivState.STOPPED_HOLD:
            another = self._approaching(obs, exclude=self.target_erv)
            done_with_target = target is None or not target.active or self._passed(target, p.clear_past_m)
            if done_with_target and another is not None:
                self.target_erv = another.key
                self.last_reason = "another ERV approaching: keep holding"
            elif target is None or not target.active:
                self.t_clear = t
                self._enter(CivState.WAIT_CLEAR, t, "ERV track lost or siren off")
            elif self._passed(target, p.clear_past_m):
                self.t_clear = t
                self._enter(CivState.WAIT_CLEAR, t, "ERV passed")
            elif self.t_stop is not None and t - self.t_stop > p.hold_max_s:
                self.t_clear = t
                self._enter(CivState.WAIT_CLEAR, t, "held the maximum time")

        elif s == CivState.WAIT_CLEAR:
            another = self._approaching(obs, exclude=self.target_erv)
            if another is not None:
                self.target_erv = another.key
                self.t_stop = t
                self._enter(CivState.STOPPED_HOLD, t, "another ERV approaching: keep holding")
            elif self.t_clear is not None and t - self.t_clear >= p.merge_wait_s:
                self.t_signal = t
                self._enter(CivState.MERGE_BACK, t, "road clear: merging back")

        elif s == CivState.MERGE_BACK:
            signal_done = self.t_signal is not None and t - self.t_signal >= p.merge_signal_s
            if approaching is not None and approaching.key != self.target_erv:
                self.target_erv = approaching.key
                self._enter(CivState.MONITOR, t, "new ERV approaching")
            elif signal_done and t - self.t_state >= p.merge_settle_s:
                self.target_erv = None
                self._enter(CivState.NORMAL, t, "back in lane")

        elif s == CivState.SLOW_IN_LANE:
            if target is None or not target.active or self._passed(target, p.clear_past_m):
                self.target_erv = None
                self._enter(CivState.NORMAL, t, "ERV passed")
            elif not target.same_road:
                self.target_erv = None
                self._enter(CivState.NORMAL, t, "ERV left my road")
            elif not target.same_lane and not self._imminent(target):
                self._enter(CivState.MONITOR, t, "ERV left my lane")
            elif (target.gap_m > 0 and target.ttp_s() >= p.slow_in_lane_ttp_s
                  and t - self.t_state >= 2.0 and not obs.in_junction):
                # the ERV is holding behind us instead of passing: get out of its way
                self.t_signal = t
                self._enter(CivState.YIELD_PREP, t, "ERV holding behind: pulling over")

        elif s == CivState.MOVE_OVER:
            if obs.lane_change_done or t - self.t_state > 8.0:
                self.pending_lane_change = None
            if target is None or not target.active or self._passed(target, p.clear_past_m):
                self.target_erv = None
                self.pending_lane_change = None
                self._enter(CivState.NORMAL, t, "ERV passed")

        elif s == CivState.PASS_STOPPED_ERV:
            if obs.lane_change_done or t - self.t_state > 8.0:
                self.pending_lane_change = None
            if target is None or target.gap_m > p.stopped_erv_zone_after_m:
                self.target_erv = None
                self.pending_lane_change = None
                self._enter(CivState.NORMAL, t, "past the stopped ERV")
            elif approaching is not None:
                self.target_erv = approaching.key
                self.pending_lane_change = None
                self._enter(CivState.MONITOR, t, "active ERV approaching from behind")

        elif s == CivState.WAIT_BEHIND:
            if target is None or not target.stationary or target.gap_m > p.stopped_erv_zone_after_m:
                self.target_erv = None
                self._enter(CivState.NORMAL, t, "stopped ERV gone")
            elif obs.two_way and obs.opposing_clear:
                self.pending_pass = "opposing"
                self._enter(CivState.OPPOSING_PASS, t, "opposing lane clear: passing")

        elif s == CivState.OPPOSING_PASS:
            if obs.pass_done or target is None or target.gap_m > p.stopped_erv_zone_after_m:
                self.target_erv = None
                self.pending_pass = None
                self._enter(CivState.NORMAL, t, "pass complete")

        return self._decide(obs, cruise, rels.get(self.target_erv) if self.target_erv else None, slow_delta)

    # ── outputs ──

    def _decide(self, obs: CivilianObservation, cruise: float, target: Optional[ErvRelation],
                slow_delta: float) -> CivilianDecision:
        p = self.p
        t = obs.t
        s = self.state
        d = CivilianDecision(state=s.value, target_speed_mps=cruise, reason=self.last_reason, erv_key=self.target_erv)
        if s == CivState.MONITOR:
            pass
        elif s == CivState.YIELD_PREP:
            d.target_speed_mps = max(cruise - slow_delta, 3.0)
            d.lights = ("right",)
        elif s == CivState.PULL_OVER:
            d.target_speed_mps = 0.0
            d.lateral_offset_m = self.pull_offset
            d.lights = ("right", "brake")
            d.stop = obs.speed_mps < 1.5
        elif s in (CivState.STOPPED_HOLD, CivState.WAIT_CLEAR):
            d.target_speed_mps = 0.0
            d.lateral_offset_m = self.pull_offset
            d.lights = ("hazard", "brake")
            d.stop = True
        elif s == CivState.MERGE_BACK:
            d.lateral_offset_m = 0.0
            if self.t_signal is not None and t - self.t_signal < p.merge_signal_s:
                d.lights = ("left",)
            if target is not None and target.gap_m < 0 and -target.gap_m < p.follow_erv_gap_m:
                d.target_speed_mps = min(cruise, max(target.erv_speed_mps - 0.5, 2.0))
        elif s == CivState.SLOW_IN_LANE:
            d.target_speed_mps = max(cruise - slow_delta, 2.0)
            d.lights = ("hazard",)
        elif s == CivState.MOVE_OVER:
            d.target_speed_mps = max(cruise - kmh_to_mps(p.move_over_delta_kmh), 3.0)
            if self.pending_lane_change is not None:
                d.lane_change = self.pending_lane_change
                d.lights = (self.pending_lane_change,)
        elif s == CivState.PASS_STOPPED_ERV:
            d.target_speed_mps = max(kmh_to_mps(p.stopped_erv_min_kmh), cruise - kmh_to_mps(p.stopped_erv_delta_kmh))
            d.lateral_offset_m = self.pass_side * p.stopped_erv_shift_m
            if self.pending_lane_change is not None:
                d.lane_change = self.pending_lane_change
                d.lights = (self.pending_lane_change,)
                d.lateral_offset_m = 0.0
        elif s == CivState.WAIT_BEHIND:
            gap_ahead = -target.gap_m if target is not None else INF
            if gap_ahead <= p.wait_behind_gap_m + 5.0:
                d.target_speed_mps = 0.0
                d.stop = True
                d.lights = ("hazard", "brake")
            else:
                d.target_speed_mps = min(cruise, 3.0)
        elif s == CivState.OPPOSING_PASS:
            d.target_speed_mps = max(kmh_to_mps(p.stopped_erv_min_kmh), cruise - kmh_to_mps(p.stopped_erv_delta_kmh))
            if self.pending_pass is not None:
                d.pass_path = self.pending_pass
                self.pending_pass = None
            d.lights = ("left",)
        # car-following keeps the model from rear-ending whatever is ahead
        if obs.lead_gap_m is not None and obs.lead_speed_mps is not None and not d.stop:
            from digital_twin_bridge.v2x_behaviors.geometry import car_following_speed
            d.target_speed_mps = car_following_speed(obs.lead_gap_m, obs.lead_speed_mps, d.target_speed_mps)
        d.target_speed_mps = max(0.0, d.target_speed_mps)
        return d

    def status(self) -> dict:
        return {
            "state": self.state.value,
            "reason": self.last_reason,
            "erv_key": self.target_erv,
            "offset_m": self.pull_offset if self.state in (CivState.PULL_OVER, CivState.STOPPED_HOLD, CivState.WAIT_CLEAR) else 0.0,
            "since": self.t_state,
        }
