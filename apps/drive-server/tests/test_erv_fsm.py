"""Emergency vehicle state machine (ambulance profile)."""

import pytest

from digital_twin_bridge.v2x_behaviors.erv_fsm import ErvFsm, ErvObservation
from digital_twin_bridge.v2x_behaviors.profiles import ErvProfile, erv_profile, kmh_to_mps

LIMIT = kmh_to_mps(40)


def obs(t, speed=LIMIT, **kw):
    return ErvObservation(t=t, speed_mps=speed, speed_limit_mps=LIMIT, **kw)


def test_en_route_speed_lights_and_centerline_offset():
    fsm = ErvFsm(erv_profile("ambulance"))
    d = fsm.step(obs(0.0))
    assert d.state == "EN_ROUTE" and d.siren and d.lights and d.lateral_offset_m == -0.3
    assert d.target_speed_mps == pytest.approx(LIMIT + kmh_to_mps(16))
    assert d.response_type == "emergency"


def test_stop_sign_junction_full_stop_then_crossing():
    fsm = ErvFsm()
    d = fsm.step(obs(0.0, dist_to_junction_m=35.0, junction_control="stop"))
    assert d.state == "APPROACH_JUNCTION" and d.target_speed_mps == pytest.approx(LIMIT)
    d = fsm.step(obs(2.0, speed=5.0, dist_to_junction_m=5.0, junction_control="stop"))
    assert d.state == "STOP_AT_CONTROL" and d.stop
    d = fsm.step(obs(3.0, speed=0.0, dist_to_junction_m=4.0, junction_control="stop", conflicting_clear=False))
    assert d.state == "STOP_AT_CONTROL"
    d = fsm.step(obs(4.5, speed=0.0, dist_to_junction_m=4.0, junction_control="stop", conflicting_clear=True))
    assert d.state == "CROSSING" and d.target_speed_mps == pytest.approx(kmh_to_mps(10))
    d = fsm.step(obs(5.0, speed=2.0, in_junction=True, junction_control="stop", conflicting_clear=False))
    assert d.stop  # a car entered the junction: hold
    d = fsm.step(obs(8.0, speed=2.0, junction_cleared=True, dist_to_junction_m=200.0))
    assert d.state == "EN_ROUTE"
    assert [tr.dst for tr in fsm.transitions] == ["APPROACH_JUNCTION", "STOP_AT_CONTROL", "CROSSING", "EN_ROUTE"]


def test_a_stopped_car_at_the_junction_exit_turns_into_a_follow_and_pass():
    fsm = ErvFsm()
    fsm.step(obs(0.0, dist_to_junction_m=20.0, junction_control="none"))
    fsm.step(obs(1.0, in_junction=True, dist_to_junction_m=0.0, junction_control="none"))
    d = fsm.step(obs(2.0, speed=0.0, in_junction=True, dist_to_junction_m=0.0, lead_gap_m=6.0, lead_speed_mps=0.0))
    assert d.state == "CROSSING"
    d = fsm.step(obs(4.5, speed=0.0, in_junction=True, dist_to_junction_m=0.0, lead_gap_m=6.0, lead_speed_mps=0.0))
    assert d.state == "FOLLOW_SLOW" and "blocks" in d.reason
    d = fsm.step(obs(6.5, speed=0.0, in_junction=True, lead_gap_m=6.0, lead_speed_mps=0.0))
    assert d.state == "WAIT_FOR_YIELD"


def test_uncontrolled_junction_is_crossed_without_stopping():
    fsm = ErvFsm()
    fsm.step(obs(0.0, dist_to_junction_m=20.0, junction_control="none"))
    d = fsm.step(obs(1.0, in_junction=True, dist_to_junction_m=0.0, junction_control="none"))
    assert d.state == "CROSSING" and not d.stop
    assert d.target_speed_mps == pytest.approx(LIMIT)  # no control: keep the limit unless traffic is near
    d = fsm.step(obs(1.5, in_junction=True, dist_to_junction_m=0.0, junction_control="none", conflicting_clear=False))
    assert d.stop
    d = fsm.step(obs(3.0, dist_to_junction_m=60.0, junction_cleared=True))
    assert d.state == "EN_ROUTE"


def test_slow_lead_wait_for_yield_then_pass_left_in_lane():
    fsm = ErvFsm()
    d = fsm.step(obs(0.0, lead_gap_m=25.0, lead_speed_mps=6.0))
    assert d.state == "FOLLOW_SLOW"
    d = fsm.step(obs(1.0, speed=8.0, lead_gap_m=15.0, lead_speed_mps=6.0))
    assert d.state == "FOLLOW_SLOW" and d.target_speed_mps < LIMIT  # closing in: car-following caps the speed
    d = fsm.step(obs(2.0, speed=7.0, lead_gap_m=14.0, lead_speed_mps=6.0))
    assert d.state == "WAIT_FOR_YIELD"
    d = fsm.step(obs(3.0, speed=7.0, lead_gap_m=14.0, lead_speed_mps=4.0, lead_moved_right=True))
    assert d.state == "PASS_LEFT_IN_LANE" and d.lateral_offset_m < 0 and d.target_speed_mps == pytest.approx(kmh_to_mps(32))
    d = fsm.step(obs(4.0, speed=8.0, lead_gap_m=5.0, lead_speed_mps=4.0, oncoming_ttc_s=1.5))
    assert d.stop  # oncoming car too close: hold
    d = fsm.step(obs(6.0, speed=8.0, pass_done=True))
    assert d.state == "EN_ROUTE"


def test_opposing_lane_pass_when_the_lead_does_not_yield():
    fsm = ErvFsm()
    fsm.step(obs(0.0, lead_gap_m=25.0, lead_speed_mps=6.0))
    fsm.step(obs(2.0, speed=7.0, lead_gap_m=14.0, lead_speed_mps=6.0))
    d = fsm.step(obs(3.0, speed=7.0, lead_gap_m=14.0, lead_speed_mps=6.0, two_way=True, opposing_clear=True, sight_m=30.0))
    assert d.state == "WAIT_FOR_YIELD"  # sight too short
    d = fsm.step(obs(3.5, speed=7.0, lead_gap_m=14.0, lead_speed_mps=6.0, two_way=True, opposing_clear=True, sight_m=60.0))
    assert d.state == "OPPOSING_LANE_PASS" and d.pass_path == "opposing" and d.lateral_offset_m == 0.0
    d = fsm.step(obs(4.0, speed=8.0, lead_gap_m=10.0, lead_speed_mps=6.0))
    assert d.pass_path is None
    d = fsm.step(obs(7.0, speed=9.0, pass_done=True))
    assert d.state == "EN_ROUTE"
    fire = ErvFsm(erv_profile("fire"))
    fire.step(obs(0.0, lead_gap_m=25.0, lead_speed_mps=6.0))
    fire.step(obs(2.0, speed=7.0, lead_gap_m=14.0, lead_speed_mps=6.0))
    d = fire.step(obs(3.0, speed=7.0, lead_gap_m=14.0, lead_speed_mps=6.0, two_way=True, opposing_clear=True, sight_m=200.0))
    assert d.state == "WAIT_FOR_YIELD"  # fire trucks never use the opposing lane


def test_no_progress_turns_the_siren_off_and_resumes():
    fsm = ErvFsm(ErvProfile(no_progress_s=5.0))
    fsm.step(obs(0.0, lead_gap_m=12.0, lead_speed_mps=0.0))
    for t in (1.0, 2.0, 3.0, 4.0, 5.0):
        d = fsm.step(obs(t, speed=0.0, lead_gap_m=12.0, lead_speed_mps=0.0))
    assert d.state == "WAIT_FOR_YIELD"
    d = fsm.step(obs(6.5, speed=0.0, lead_gap_m=12.0, lead_speed_mps=0.0))
    assert d.state == "NO_PROGRESS" and not d.siren and d.lights and d.stop
    d = fsm.step(obs(9.0, speed=0.0, lead_gap_m=20.0, lead_speed_mps=5.0))
    assert d.state == "EN_ROUTE" and d.siren


def test_arrival_and_operator_overrides():
    fsm = ErvFsm()
    d = fsm.step(obs(0.0, dist_to_destination_m=6.0))
    assert d.state == "ARRIVED" and d.stop and not d.siren and d.lights and d.doors_open
    assert d.response_type == "stationary"
    fsm.set_manual(1.0, arrived=False)
    assert fsm.step(obs(1.0, dist_to_destination_m=500.0)).state == "EN_ROUTE"
    fsm.set_manual(2.0, manual=True, siren=False)
    d = fsm.step(obs(2.0))
    assert d.state == "MANUAL" and d.stop and not d.siren and d.lights
    fsm.set_manual(3.0, lights=False, response_type="nonEmergency")
    d = fsm.step(obs(3.0))
    assert not d.lights and d.response_type == "nonEmergency"
    fsm.set_manual(4.0, manual=False)
    assert fsm.step(obs(4.0)).state == "EN_ROUTE"
    fsm.set_manual(5.0, arrived=True)
    assert fsm.step(obs(5.0)).state == "ARRIVED" and fsm.status()["state"] == "ARRIVED"
    police = ErvFsm(erv_profile("police"))
    assert police.step(obs(0.0)).response_type == "pursuit"
