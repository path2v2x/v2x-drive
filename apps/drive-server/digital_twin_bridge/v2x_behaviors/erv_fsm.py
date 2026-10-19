"""Emergency vehicle (ERV) state machine, ambulance profile first (pure).

Behaviours from fire / EMS / police driving policies: limit plus 10 mph with
siren and lights, drive toward the centerline so civilians read the pass on
the left, slow to the limit near junctions, come to a full stop at stop
signs (and at signals, treated as red) and cross at walking pace only when
the conflicting traffic has stopped or is far away, wait a moment behind a
slow lead for it to yield, pass on the left inside the lane when it moved
right, otherwise use the opposing lane when it is clear and sight is good,
never pass on the right, switch the siren off when stuck or parked, open the
rear doors on arrival.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from digital_twin_bridge.v2x_behaviors.geometry import INF, car_following_speed
from digital_twin_bridge.v2x_behaviors.profiles import ErvProfile, kmh_to_mps


class ErvState(str, Enum):
    EN_ROUTE = "EN_ROUTE"
    APPROACH_JUNCTION = "APPROACH_JUNCTION"
    STOP_AT_CONTROL = "STOP_AT_CONTROL"
    CROSSING = "CROSSING"
    FOLLOW_SLOW = "FOLLOW_SLOW"
    WAIT_FOR_YIELD = "WAIT_FOR_YIELD"
    PASS_LEFT_IN_LANE = "PASS_LEFT_IN_LANE"
    OPPOSING_LANE_PASS = "OPPOSING_LANE_PASS"
    NO_PROGRESS = "NO_PROGRESS"
    ARRIVED = "ARRIVED"
    MANUAL = "MANUAL"


@dataclass
class ErvObservation:
    t: float
    speed_mps: float
    speed_limit_mps: float
    dist_to_junction_m: float = INF
    in_junction: bool = False
    junction_control: str = "none"      # 'stop' | 'signal' | 'blind' | 'none'
    junction_cleared: bool = False      # the junction being handled is behind us now
    conflicting_clear: bool = True      # every conflicting vehicle stopped or TTC > 5 s
    lead_gap_m: Optional[float] = None
    lead_speed_mps: Optional[float] = None
    lead_moved_right: bool = False      # the lead left enough room on its left
    lane_width_m: float = 3.5
    half_width_m: float = 1.1
    two_way: bool = True
    opposing_clear: bool = False
    sight_m: float = INF
    oncoming_ttc_s: float = INF
    dist_to_destination_m: float = INF
    pass_done: bool = False


@dataclass
class ErvDecision:
    state: str
    target_speed_mps: float
    lateral_offset_m: float = 0.0
    siren: bool = True
    lights: bool = True
    stop: bool = False
    pass_path: Optional[str] = None     # 'opposing' (request, once)
    doors_open: bool = False
    response_type: str = "emergency"
    reason: str = ""


@dataclass
class Transition:
    t: float
    src: str
    dst: str
    reason: str


class ErvFsm:
    def __init__(self, profile: Optional[ErvProfile] = None):
        self.p = profile or ErvProfile()
        self.state = ErvState.EN_ROUTE
        self.t_state = 0.0
        self.t_stopped_since: Optional[float] = None
        self.t_follow: Optional[float] = None
        self.t_slow_since: Optional[float] = None
        self.pending_pass: Optional[str] = None
        self.manual: Optional[dict] = None
        self.transitions: List[Transition] = []
        self.last_reason = ""
        self._state_before_manual: Optional[ErvState] = None

    def _enter(self, state: ErvState, t: float, reason: str) -> None:
        if state != self.state:
            self.transitions.append(Transition(t, self.state.value, state.value, reason))
        self.state = state
        self.t_state = t
        self.last_reason = reason
        if state != ErvState.STOP_AT_CONTROL:
            self.t_stopped_since = None

    # ── operator overrides ──

    def set_manual(self, t: float, manual: Optional[bool] = None, siren: Optional[bool] = None,
                   lights: Optional[bool] = None, arrived: Optional[bool] = None,
                   response_type: Optional[str] = None) -> None:
        """Operator control (``set_erv_state``): toggle manual mode, siren, lights, arrival."""
        if arrived:
            self.manual = None
            self._enter(ErvState.ARRIVED, t, "operator: arrived")
            return
        if manual is True and self.state != ErvState.MANUAL:
            self._state_before_manual = self.state
            self.manual = {"siren": True, "lights": True, "response_type": self.p.response_type}
            self._enter(ErvState.MANUAL, t, "operator: manual hold")
        elif manual is False and self.state == ErvState.MANUAL:
            self.manual = None
            self._enter(ErvState.EN_ROUTE, t, "operator: resume")
        if self.manual is not None:
            if siren is not None:
                self.manual["siren"] = bool(siren)
            if lights is not None:
                self.manual["lights"] = bool(lights)
            if response_type:
                self.manual["response_type"] = str(response_type)
        elif self.state == ErvState.ARRIVED and arrived is False:
            self._enter(ErvState.EN_ROUTE, t, "operator: departing")

    # ── main step ──

    def step(self, obs: ErvObservation) -> ErvDecision:
        p = self.p
        t = obs.t
        limit = obs.speed_limit_mps
        fast = limit + kmh_to_mps(p.over_limit_kmh)
        s = self.state

        if s == ErvState.MANUAL:
            m = self.manual or {}
            return ErvDecision(state=s.value, target_speed_mps=0.0, stop=True,
                               siren=bool(m.get("siren", True)), lights=bool(m.get("lights", True)),
                               response_type=str(m.get("response_type", p.response_type)), reason=self.last_reason)

        if s != ErvState.ARRIVED and obs.dist_to_destination_m <= p.arrive_m:
            self._enter(ErvState.ARRIVED, t, "destination reached")
            s = self.state

        # track time stuck behind traffic (for NO_PROGRESS)
        if obs.speed_mps < p.no_progress_speed_mps and s in (ErvState.FOLLOW_SLOW, ErvState.WAIT_FOR_YIELD):
            if self.t_slow_since is None:
                self.t_slow_since = t
        else:
            self.t_slow_since = None
        stuck = self.t_slow_since is not None and t - self.t_slow_since >= p.no_progress_s

        needs_stop = (
            (obs.junction_control == "stop" and p.stop_at_stop_signs)
            or (obs.junction_control == "signal" and p.treat_signals_as_red)
            or (obs.junction_control == "blind" and p.blind_junction_stop)
        )
        lead_slow = (obs.lead_gap_m is not None and obs.lead_gap_m <= p.follow_gap_m
                     and (obs.lead_speed_mps is None or obs.lead_speed_mps < min(obs.speed_mps, limit) - 1.0
                          or obs.lead_speed_mps < limit * 0.6))
        lead_gone = obs.lead_gap_m is None or obs.lead_gap_m > p.follow_gap_m + 10.0

        # ── transitions ──
        if s == ErvState.EN_ROUTE:
            if lead_slow:
                self.t_follow = t
                self._enter(ErvState.FOLLOW_SLOW, t, "slow vehicle ahead")
            elif obs.dist_to_junction_m <= p.junction_approach_m or obs.in_junction:
                self._enter(ErvState.APPROACH_JUNCTION, t, "junction ahead")

        elif s == ErvState.APPROACH_JUNCTION:
            if needs_stop and obs.dist_to_junction_m <= p.junction_stop_m and not obs.in_junction:
                self._enter(ErvState.STOP_AT_CONTROL, t, "stopping at the %s" % obs.junction_control)
            elif obs.junction_cleared:
                self._enter(ErvState.EN_ROUTE, t, "junction cleared")
            elif not needs_stop and (obs.in_junction or obs.dist_to_junction_m <= 2.0):
                self._enter(ErvState.CROSSING, t, "crossing an uncontrolled junction")
            elif obs.dist_to_junction_m > p.junction_approach_m + 5.0 and not obs.in_junction:
                self._enter(ErvState.EN_ROUTE, t, "junction no longer ahead")

        elif s == ErvState.STOP_AT_CONTROL:
            if obs.speed_mps < 0.1:
                if self.t_stopped_since is None:
                    self.t_stopped_since = t
                if t - self.t_stopped_since >= p.stop_hold_s and obs.conflicting_clear:
                    self._enter(ErvState.CROSSING, t, "stopped, conflicting traffic clear")
            else:
                self.t_stopped_since = None

        elif s == ErvState.CROSSING:
            blocked = (lead_slow and obs.speed_mps < 1.0 and (obs.lead_speed_mps or 0.0) < 1.0
                       and t - self.t_state >= 3.0)
            if obs.junction_cleared or (not obs.in_junction and obs.dist_to_junction_m > p.junction_approach_m):
                self._enter(ErvState.EN_ROUTE, t, "junction cleared")
            elif blocked:
                self.t_follow = t
                self._enter(ErvState.FOLLOW_SLOW, t, "stopped vehicle blocks the junction exit")

        elif s == ErvState.FOLLOW_SLOW:
            if lead_gone:
                self._enter(ErvState.EN_ROUTE, t, "lead vehicle gone")
            elif stuck:
                self._enter(ErvState.NO_PROGRESS, t, "no progress for %.0f s" % p.no_progress_s)
            elif self.t_follow is not None and t - self.t_follow >= p.wait_for_yield_s:
                self._enter(ErvState.WAIT_FOR_YIELD, t, "waiting for the lead to yield")

        elif s == ErvState.WAIT_FOR_YIELD:
            if lead_gone:
                self._enter(ErvState.EN_ROUTE, t, "lead vehicle gone")
            elif obs.lead_moved_right:
                self._enter(ErvState.PASS_LEFT_IN_LANE, t, "lead moved right: passing on the left")
            elif (p.opposing_pass and obs.two_way and obs.opposing_clear
                  and obs.sight_m >= p.opposing_sight_m and obs.oncoming_ttc_s >= p.opposing_ttc_s):
                self.pending_pass = "opposing"
                self._enter(ErvState.OPPOSING_LANE_PASS, t, "opposing lane clear: passing")
            elif stuck:
                self._enter(ErvState.NO_PROGRESS, t, "no progress for %.0f s" % p.no_progress_s)

        elif s in (ErvState.PASS_LEFT_IN_LANE, ErvState.OPPOSING_LANE_PASS):
            if obs.pass_done or lead_gone:
                self.pending_pass = None
                self._enter(ErvState.EN_ROUTE, t, "pass complete")

        elif s == ErvState.NO_PROGRESS:
            if obs.lead_gap_m is None or obs.lead_gap_m > p.resume_gap_m:
                self._enter(ErvState.EN_ROUTE, t, "traffic moving again")

        return self._decide(obs, fast, limit)

    # ── outputs ──

    def _decide(self, obs: ErvObservation, fast: float, limit: float) -> ErvDecision:
        p = self.p
        s = self.state
        d = ErvDecision(state=s.value, target_speed_mps=fast, lateral_offset_m=p.centerline_offset_m,
                        response_type=p.response_type, reason=self.last_reason)
        if s == ErvState.APPROACH_JUNCTION:
            d.target_speed_mps = min(limit, fast)
        elif s == ErvState.STOP_AT_CONTROL:
            d.target_speed_mps = 0.0
            d.stop = True
            d.lateral_offset_m = 0.0
        elif s == ErvState.CROSSING:
            # walking pace through a controlled junction; an uncontrolled one (most of the
            # RFS map is junction-internal road) only slows while traffic is near
            d.target_speed_mps = kmh_to_mps(p.crossing_kmh) if obs.junction_control != "none" else min(limit, fast)
            d.lateral_offset_m = 0.0
            if not obs.conflicting_clear:
                d.target_speed_mps = 0.0
                d.stop = True
        elif s in (ErvState.FOLLOW_SLOW, ErvState.WAIT_FOR_YIELD):
            d.target_speed_mps = car_following_speed(obs.lead_gap_m, obs.lead_speed_mps, fast, min_gap_m=10.0)
        elif s == ErvState.PASS_LEFT_IN_LANE:
            d.target_speed_mps = kmh_to_mps(p.pass_kmh)
            d.lateral_offset_m = -min(max(obs.lane_width_m / 2.0 - obs.half_width_m + 0.3, 0.3), 1.5)
            if obs.oncoming_ttc_s < 2.0:
                d.stop = True
                d.target_speed_mps = 0.0
        elif s == ErvState.OPPOSING_LANE_PASS:
            d.target_speed_mps = kmh_to_mps(p.pass_kmh)
            d.lateral_offset_m = 0.0
            if self.pending_pass is not None:
                d.pass_path = self.pending_pass
                self.pending_pass = None
            if obs.oncoming_ttc_s < 2.0 or (obs.dist_to_junction_m <= p.junction_stop_m
                                           and obs.junction_control != "none" and not obs.conflicting_clear):
                d.stop = True
                d.target_speed_mps = 0.0
        elif s == ErvState.NO_PROGRESS:
            d.target_speed_mps = 0.0
            d.stop = True
            d.siren = False
            d.lateral_offset_m = 0.0
        elif s == ErvState.ARRIVED:
            d.target_speed_mps = 0.0
            d.stop = True
            d.siren = False
            d.lights = True
            d.doors_open = p.doors_open_on_arrival
            d.response_type = "stationary"
            d.lateral_offset_m = 0.0
        d.target_speed_mps = max(0.0, d.target_speed_mps)
        return d

    def status(self) -> dict:
        return {
            "state": self.state.value,
            "reason": self.last_reason,
            "since": self.t_state,
            "manual": self.manual,
        }
