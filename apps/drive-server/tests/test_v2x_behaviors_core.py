"""Pure driving-model pieces: J2735 values, profiles, geometry, the ERV tracker."""

import math

import pytest

from digital_twin_bridge.v2x_behaviors import bsm_types, geometry
from digital_twin_bridge.v2x_behaviors.erv_tracker import ErvTracker
from digital_twin_bridge.v2x_behaviors.profiles import (
    CivilianProfile,
    ErvProfile,
    civilian_profile,
    erv_profile,
    kmh_to_mps,
    mps_to_kmh,
    profile_to_dict,
)


def test_bsm_enums_accept_names_and_numbers():
    assert bsm_types.siren_active("inUse") and bsm_types.siren_active(2)
    assert not bsm_types.siren_active("unavailable") and not bsm_types.siren_active(None)
    assert bsm_types.lights_active("inUse") and bsm_types.lights_active("yellowCautionLights")
    assert bsm_types.response_stationary("stationary") and bsm_types.response_stationary(4)
    assert bsm_types.role_is_emergency("ambulance") and bsm_types.role_is_emergency(13)
    assert not bsm_types.role_is_emergency("basicVehicle") and not bsm_types.role_is_emergency(0)
    assert bsm_types.role_name(14) == "ambulance" and bsm_types.role_name("fire") == "fire"
    mask = bsm_types.light_mask(siren=True, hazard=True, brake=True)
    assert mask & bsm_types.LIGHT_SPECIAL1 and mask & bsm_types.LIGHT_SPECIAL2
    assert mask & bsm_types.LIGHT_LEFT_BLINKER and mask & bsm_types.LIGHT_RIGHT_BLINKER and mask & bsm_types.LIGHT_BRAKE
    assert bsm_types.light_mask(lights=True) == bsm_types.LIGHT_SPECIAL1


def test_profiles_and_overrides():
    assert kmh_to_mps(36) == pytest.approx(10.0) and mps_to_kmh(10) == pytest.approx(36.0)
    amb = erv_profile("ambulance")
    assert amb.blueprint == "vehicle.ambulance.ford" and amb.opposing_pass is True
    fire = erv_profile("FIRE", {"over_limit_kmh": "20", "opposing_pass": 1, "name": "x", "bogus": 3})
    assert fire.name == "fire" and fire.over_limit_kmh == 20.0 and fire.opposing_pass is True
    assert erv_profile("nope").name == "ambulance"
    civ = civilian_profile("civilian", {"cruise_kmh": 40, "monitor_distance_m": "bad"})
    assert civ.cruise_kmh == 40.0 and civ.monitor_distance_m == CivilianProfile().monitor_distance_m
    assert profile_to_dict(ErvProfile())["arrive_m"] == 8.0


def test_geometry_frames_and_relations():
    assert geometry.wrap_deg(190) == -170 and geometry.wrap_deg(-180) == 180
    assert geometry.heading_diff_deg(350, 10) == 20
    assert geometry.same_direction(0, 30) and not geometry.same_direction(0, 90)
    # yaw 90: forward is +y, right is -x
    along, lateral = geometry.relative_position(0, 0, 90, 0, 10)
    assert along == pytest.approx(10) and lateral == pytest.approx(0)
    along, lateral = geometry.relative_position(0, 0, 90, -3, 10)
    assert lateral == pytest.approx(3)   # -x is to the right when facing +y
    behind, gap, lat = geometry.is_behind(100, 0, 0, 40, 1, 5)
    assert behind and gap == pytest.approx(60) and lat == pytest.approx(1)
    behind, gap, _ = geometry.is_behind(100, 0, 0, 160, 0, 0)
    assert not behind and gap == pytest.approx(-60)
    assert not geometry.is_behind(100, 0, 0, 40, 0, 180)[0]  # oncoming
    assert geometry.time_to_pass(60, 20, 10) == pytest.approx(6.0)
    assert geometry.time_to_pass(60, 10, 10) == math.inf and geometry.time_to_pass(-5, 20, 0) == 0.0
    assert geometry.time_to_collision(30, 10) == 3.0
    assert geometry.brake_distance(10, 2) == pytest.approx(25)


def test_pull_over_offset_and_following():
    assert geometry.pull_over_offset(3.5, 1.0, shoulder_m=1.8) == pytest.approx(1.45)
    assert geometry.pull_over_offset(3.5, 1.0, shoulder_m=0.0) == pytest.approx(0.45)
    assert geometry.pull_over_offset(2.4, 1.0) is None  # too narrow: stop in lane
    assert geometry.car_following_speed(None, None, 13.0) == 13.0
    assert geometry.car_following_speed(5.0, 8.0, 13.0) == pytest.approx(7.0)
    assert geometry.car_following_speed(20.0, 8.0, 13.0) == pytest.approx(13.0)
    assert geometry.car_following_speed(11.0, 8.0, 13.0) == pytest.approx(10.0)


def test_compass_and_polylines():
    assert geometry.compass_to_carla_yaw(90) == pytest.approx(0.0)      # east
    assert geometry.compass_to_carla_yaw(0) == pytest.approx(-90.0)     # north = -y
    assert geometry.carla_yaw_to_compass(-90) == pytest.approx(0.0)
    assert geometry.carla_yaw_to_compass(geometry.compass_to_carla_yaw(201.9)) == pytest.approx(201.9)
    line = [(0, 0), (10, 0), (10, 10)]
    assert geometry.polyline_length(line) == 20
    s, lateral, idx = geometry.project_on_polyline(line, 5, 1)
    assert s == pytest.approx(5) and lateral == pytest.approx(1) and idx == 0
    s, lateral, idx = geometry.project_on_polyline(line, 11, 5)
    assert s == pytest.approx(15) and lateral == pytest.approx(-1) and idx == 1
    assert geometry.point_along_polyline(line, 15) == pytest.approx((10, 5))
    assert geometry.point_along_polyline(line, 99) == (10, 10)
    assert geometry.min_pairwise_distance([(0, 0), (3, 4), (10, 10)]) == 5.0


def test_erv_tracker_from_bsm_and_lights():
    tr = ErvTracker(timeout_s=15.0, stationary_after_s=60.0)
    amb = {"id": "11111110", "speed_mps": 12.0, "heading_deg": 90.0, "lat": 37.9, "lon": -122.3, "coords": "wgs84",
           "lights": {"siren": "inUse", "lights": "inUse", "multi": "unavailable", "role": "ambulance", "response_type": None, "events": None}}
    track = tr.update_from_bsm("PATH-M-1", amb, 100.0, gps_to_xy=lambda lat, lon: (10.0, 20.0))
    assert track.active and track.role == "ambulance" and (track.x, track.y) == (10.0, 20.0)
    assert track.yaw_deg == pytest.approx(0.0) and track.positioned
    # a mapped CARLA actor wins over the BSM position
    tr.update_from_bsm("PATH-M-1", amb, 100.5, actor_pose=(1.0, 2.0, 45.0), actor_id=77)
    assert tr.get("PATH-M-1").actor_id == 77 and tr.by_actor(77).x == 1.0
    # a plain car with no alerts is not tracked
    assert tr.update_from_bsm("PATH-BG-1", {"speed_mps": 3.0, "lights": None}, 100.0) is None
    # siren off: still tracked, no longer active
    quiet = dict(amb, lights=dict(amb["lights"], siren="notInUse"))
    tr.update_from_bsm("PATH-M-1", quiet, 101.0, actor_pose=(1.0, 2.0, 45.0))
    assert not tr.get("PATH-M-1").active and len(tr.active_tracks(101.0)) == 0
    # stationary: responseType or a minute below 5 km/h
    parked = dict(amb, speed_mps=0.0, lights=dict(amb["lights"], response_type="stationary"))
    tr.update_from_bsm("PATH-M-1", parked, 102.0, actor_pose=(1.0, 2.0, 45.0))
    assert tr.get("PATH-M-1").is_stationary(102.0)
    slow = dict(amb, speed_mps=0.5)
    tr.update_from_bsm("PATH-M-1", slow, 103.0, actor_pose=(1.0, 2.0, 45.0))
    assert not tr.get("PATH-M-1").is_stationary(103.0)
    tr.update_from_bsm("PATH-M-1", slow, 170.0, actor_pose=(1.0, 2.0, 45.0))
    assert tr.get("PATH-M-1").is_stationary(170.0)
    # light-state source (no DT): Special1 means siren and bar together
    t2 = tr.update_from_lights("actor:9", 9, 50.0, 60.0, 180.0, 8.0, True, 170.0, role="fire")
    assert t2.active and t2.source == "lights" and tr.nearest(49, 60).key == "actor:9"
    assert {d["key"] for d in tr.snapshot(170.0)} == {"PATH-M-1", "actor:9"}
    assert tr.expire(190.0) == ["PATH-M-1", "actor:9"] and tr.tracks() == []


def test_erv_tracker_origin_relative_position_needs_an_actor():
    tr = ErvTracker()
    rel = {"speed_mps": 1.0, "heading_deg": 180.0, "lat": 0.0008, "lon": 0.0004, "coords": "origin-relative",
           "lights": {"siren": "inUse", "lights": "inUse", "multi": None, "role": "ambulance", "response_type": None, "events": None}}
    track = tr.update_from_bsm("PATH-M-1", rel, 1.0, gps_to_xy=lambda lat, lon: (999.0, 999.0))
    assert track.active and not track.positioned and track.yaw_deg == pytest.approx(90.0)
    assert tr.active_tracks(1.0) == []
