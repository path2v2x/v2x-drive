"""Civilian driver state machine against the yield / move-over playbook."""

import pytest

from digital_twin_bridge.v2x_behaviors.civilian_fsm import CivilianFsm, CivilianObservation, CivState, ErvRelation
from digital_twin_bridge.v2x_behaviors.profiles import CivilianProfile, kmh_to_mps

LIMIT = kmh_to_mps(50)


def obs(t, speed=LIMIT, ervs=(), **kw):
    return CivilianObservation(t=t, speed_mps=speed, speed_limit_mps=LIMIT, ervs=list(ervs), **kw)


def erv(gap, speed=25.0, my_speed=LIMIT, active=True, stationary=False, same_lane=False, lateral=-2.0, key="ERV"):
    return ErvRelation(key=key, gap_m=gap, lateral_m=lateral, erv_speed_mps=speed, my_speed_mps=my_speed,
                       same_lane=same_lane, active=active, stationary=stationary)


def run(fsm, t, *args, **kw):
    return fsm.step(obs(t, *args, **kw))


def test_full_pull_over_cycle():
    fsm = CivilianFsm(CivilianProfile())
    d = run(fsm, 0.0)
    assert d.state == "NORMAL" and d.target_speed_mps == pytest.approx(LIMIT)
    # an ERV 400 m back, closing at 11 m/s -> TTP 36 s: monitor
    d = run(fsm, 1.0, ervs=[erv(400)])
    assert d.state == "MONITOR"
    # within 150 m: signal right and slow a little
    d = run(fsm, 2.0, ervs=[erv(140)])
    assert d.state == "YIELD_PREP" and d.lights == ("right",) and d.target_speed_mps < LIMIT
    # keeps signalling for 3 s before pulling over, and never right before a junction
    d = run(fsm, 4.0, ervs=[erv(100)], dist_to_junction_m=5.0)
    assert d.state == "YIELD_PREP"
    d = run(fsm, 5.5, ervs=[erv(80)], shoulder_m=1.0, lane_width_m=3.5, half_width_m=1.0)
    # 1.75 m half lane - 1.0 m half car - 0.3 m margin = 0.45 m inside the lane, plus 1 m of shoulder
    assert d.state == "PULL_OVER" and d.lateral_offset_m == pytest.approx(1.45) and d.target_speed_mps == 0.0
    assert d.lights == ("right", "brake")
    # stopped: hold with hazards until the ERV is 30 m past
    d = run(fsm, 8.0, speed=0.1, ervs=[erv(30)])
    assert d.state == "STOPPED_HOLD" and d.stop and "hazard" in d.lights and d.lateral_offset_m == pytest.approx(1.45)
    d = run(fsm, 10.0, speed=0.0, ervs=[erv(-10)])
    assert d.state == "STOPPED_HOLD"
    d = run(fsm, 11.0, speed=0.0, ervs=[erv(-35)])
    assert d.state == "WAIT_CLEAR" and d.stop
    # 3 s later: merge back with the left blinker, staying 91 m behind the ERV
    d = run(fsm, 13.5, speed=0.0, ervs=[erv(-60, speed=20.0)])
    assert d.state == "WAIT_CLEAR"
    d = run(fsm, 14.1, speed=0.0, ervs=[erv(-70, speed=20.0)])
    assert d.state == "MERGE_BACK" and d.lights == ("left",) and d.lateral_offset_m == 0.0
    assert d.target_speed_mps == pytest.approx(min(LIMIT, 19.5))
    d = run(fsm, 16.0, speed=5.0, ervs=[erv(-120, speed=20.0)])
    assert d.state == "MERGE_BACK" and d.lights == ("left",)
    d = run(fsm, 18.5, speed=8.0, ervs=[])
    assert d.state == "NORMAL" and d.target_speed_mps == pytest.approx(LIMIT)
    assert [tr.dst for tr in fsm.transitions] == [
        "MONITOR", "YIELD_PREP", "PULL_OVER", "STOPPED_HOLD", "WAIT_CLEAR", "MERGE_BACK", "NORMAL"]


def test_narrow_lane_stops_in_lane_and_hold_times_out():
    fsm = CivilianFsm(CivilianProfile(hold_max_s=20.0))
    run(fsm, 0.0, ervs=[erv(120)])
    run(fsm, 1.0, ervs=[erv(110)])
    d = run(fsm, 4.5, ervs=[erv(90)], lane_width_m=2.4, half_width_m=1.0)
    assert d.state == "PULL_OVER" and d.lateral_offset_m == 0.0 and "narrow" in d.reason
    d = run(fsm, 6.0, speed=0.0, ervs=[erv(80, speed=0.0)])
    assert d.state == "STOPPED_HOLD"
    d = run(fsm, 27.0, speed=0.0, ervs=[erv(80, speed=0.0)])
    assert d.state == "WAIT_CLEAR" and "maximum" in d.reason


def test_erv_in_my_lane_without_time_slows_in_lane():
    fsm = CivilianFsm()
    run(fsm, 0.0, ervs=[erv(250, speed=30.0, same_lane=True)])
    d = run(fsm, 1.0, ervs=[erv(60, speed=30.0, same_lane=True)])  # TTP ~3.7 s
    assert d.state == "SLOW_IN_LANE" and d.lights == ("hazard",) and d.target_speed_mps == pytest.approx(LIMIT - kmh_to_mps(16))
    d = run(fsm, 3.0, ervs=[erv(-40, speed=30.0, same_lane=True)])
    assert d.state == "NORMAL"


def test_an_erv_holding_behind_a_slowed_car_makes_it_pull_over():
    fsm = CivilianFsm()
    run(fsm, 0.0, ervs=[erv(250, speed=30.0, same_lane=True)])
    d = run(fsm, 1.0, ervs=[erv(60, speed=30.0, same_lane=True)])
    assert d.state == "SLOW_IN_LANE"
    # the ERV settles 12 m behind at our speed (no longer closing)
    d = run(fsm, 2.0, speed=4.0, ervs=[erv(12, speed=4.0, my_speed=4.0, same_lane=True)])
    assert d.state == "SLOW_IN_LANE"
    d = run(fsm, 3.5, speed=4.0, ervs=[erv(12, speed=4.0, my_speed=4.0, same_lane=True)])
    assert d.state == "YIELD_PREP" and "holding behind" in d.reason and d.lights == ("right",)
    d = run(fsm, 7.0, speed=4.0, ervs=[erv(12, speed=4.0, my_speed=4.0, same_lane=True)])
    assert d.state == "PULL_OVER"


def test_an_erv_that_turns_off_my_road_releases_the_car():
    fsm = CivilianFsm()
    run(fsm, 0.0, ervs=[erv(250, speed=30.0, same_lane=True)])
    d = run(fsm, 1.0, ervs=[erv(60, speed=30.0, same_lane=True)])
    assert d.state == "SLOW_IN_LANE"
    gone = erv(50, speed=30.0, same_lane=False)
    gone.same_road = False
    d = run(fsm, 2.0, ervs=[gone])
    assert d.state == "NORMAL" and "left my road" in d.reason
    fsm2 = CivilianFsm()
    run(fsm2, 0.0, ervs=[erv(250)])
    off = erv(200)
    off.same_road = False
    assert run(fsm2, 1.0, ervs=[off]).state == "NORMAL"


def test_multi_lane_fast_road_moves_over_instead_of_stopping():
    fsm = CivilianFsm()
    fast = kmh_to_mps(80)
    o = CivilianObservation(t=0.0, speed_mps=fast, speed_limit_mps=fast, same_dir_lanes=2,
                            ervs=[erv(200, speed=35.0, my_speed=fast, lateral=0.0)])
    assert fsm.step(o).state == "MONITOR"
    o.t = 1.0
    o.ervs = [erv(140, speed=35.0, my_speed=fast, lateral=0.0)]
    d = fsm.step(o)
    assert d.state == "MOVE_OVER" and d.lane_change == "right" and d.lights == ("right",)
    o.t = 1.5
    d = fsm.step(o)
    assert d.lane_change == "right"  # still pending until the binding reports completion
    o.t = 2.0
    o.lane_change_done = True
    d = fsm.step(o)
    assert d.lane_change is None and d.state == "MOVE_OVER"
    o.t = 5.0
    o.ervs = [erv(-50, speed=35.0, my_speed=fast)]
    assert fsm.step(o).state == "NORMAL"


def test_stopped_erv_ahead_slow_and_shift_then_resume():
    fsm = CivilianFsm()
    d = run(fsm, 0.0, ervs=[erv(-120, speed=0.0, active=False, stationary=True, lateral=2.5)])
    assert d.state == "PASS_STOPPED_ERV"
    assert d.target_speed_mps == pytest.approx(max(kmh_to_mps(8), LIMIT - kmh_to_mps(32)))
    assert d.lateral_offset_m == pytest.approx(-0.5)  # ERV on my right: shift left
    d = run(fsm, 5.0, ervs=[erv(40, speed=0.0, active=False, stationary=True, lateral=2.5)])
    assert d.state == "PASS_STOPPED_ERV"
    d = run(fsm, 6.0, ervs=[erv(60, speed=0.0, active=False, stationary=True, lateral=2.5)])
    assert d.state == "NORMAL"


def test_blocked_single_lane_waits_then_passes_on_the_opposing_lane():
    fsm = CivilianFsm()
    blocked = erv(-60, speed=0.0, active=False, stationary=True, same_lane=True, lateral=0.0)
    d = run(fsm, 0.0, ervs=[blocked])
    assert d.state == "WAIT_BEHIND" and d.target_speed_mps == 3.0 and not d.stop
    d = run(fsm, 1.0, ervs=[erv(-12, speed=0.0, active=False, stationary=True, same_lane=True, lateral=0.0)])
    assert d.stop and "hazard" in d.lights
    d = run(fsm, 2.0, ervs=[erv(-12, speed=0.0, active=False, stationary=True, same_lane=True, lateral=0.0)],
            two_way=True, opposing_clear=True)
    assert d.state == "OPPOSING_PASS" and d.pass_path == "opposing" and d.lights == ("left",)
    d = run(fsm, 2.5, ervs=[erv(-10, speed=0.0, active=False, stationary=True, same_lane=True, lateral=0.0)])
    assert d.pass_path is None  # requested once
    d = run(fsm, 6.0, ervs=[erv(20, speed=0.0, active=False, stationary=True, same_lane=True)], pass_done=True)
    assert d.state == "NORMAL"


def test_second_erv_keeps_the_car_parked_and_car_following_caps_speed():
    fsm = CivilianFsm()
    run(fsm, 0.0, ervs=[erv(120, key="A")])
    run(fsm, 1.0, ervs=[erv(100, key="A")])
    run(fsm, 4.5, ervs=[erv(60, key="A")])
    run(fsm, 6.0, speed=0.0, ervs=[erv(20, key="A")])
    d = run(fsm, 7.0, speed=0.0, ervs=[erv(-40, key="A"), erv(250, key="B")])
    assert d.state == "STOPPED_HOLD" and d.erv_key == "B"
    fsm2 = CivilianFsm()
    d = run(fsm2, 0.0, lead_gap_m=6.0, lead_speed_mps=5.0)
    assert d.state == "NORMAL" and d.target_speed_mps == pytest.approx(4.0)
    assert fsm2.status()["state"] == "NORMAL"
