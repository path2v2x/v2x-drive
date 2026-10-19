"""VOICES scenario slots: names, holders, kept cars, claim rules and parking."""

import json
import types

import carla
import pytest

from digital_twin_bridge.scenario_roster import ScenarioRoster
from digital_twin_bridge.voices_slots import (
    SlotClaimError,
    SlotKeeper,
    SlotService,
    off_road_transform,
    park_actor,
    reset_adopted_actor,
)
from tests.conftest import MockWorld

XML = (
    '<s><vehicles>'
    '<vehicle name="PATH-M-1" bsmid="11111110" organization="PATH" type="vehicle.ambulance.ford"/>'
    '<vehicle name="PATH-BG-5" bsmid="11111115" organization="PATH" type="vehicle.lincoln.mkz"/>'
    '<vehicle name="VW-M-1" bsmid="00000000" organization="VW" type="vehicle.lincoln.mkz"/>'
    '</vehicles></s>'
)


def roster(tmp_path):
    xml = tmp_path / "scn.xml"
    xml.write_text(XML)
    return ScenarioRoster(xml_path=str(xml))


def car(world, role, type_id="vehicle.lincoln.mkz", x=0.0, y=0.0):
    bp = types.SimpleNamespace(id=type_id, _attributes={"role_name": role})
    return world.try_spawn_actor(bp, carla.Transform(carla.Location(x=x, y=y, z=0.5), carla.Rotation(yaw=0.0)))


def vehicles(world):
    return list(world.get_actors().filter("vehicle.*"))


def test_keeper_remembers_cars_across_restarts(tmp_path):
    path = str(tmp_path / "slots.json")
    world = MockWorld()
    kept_car = car(world, "PATH-BG-5")
    keeper = SlotKeeper(path)
    keeper.remember("PATH-BG-5", kept_car, "session")
    with open(path, "r", encoding="utf-8") as f:
        assert json.load(f)["PATH-BG-5"]["actor_id"] == kept_car.id
    restarted = SlotKeeper(path)  # a restarted drive server reads the file
    assert restarted.match("PATH-BG-5", vehicles(world)) is kept_car
    other = car(world, "PATH-BG-5", "vehicle.ambulance.ford")
    assert restarted.match("PATH-BG-5", [other]) is None  # same name, a different car: not ours
    assert restarted.match("PATH-M-1", vehicles(world)) is None
    restarted.forget("PATH-BG-5")
    assert SlotKeeper(path).kept() == {}
    assert SlotKeeper(path="").kept() == {}  # in-memory only


def test_slot_names_states_and_claim_rules(tmp_path):
    svc = SlotService(roster=roster(tmp_path), keeper=SlotKeeper(path=""), config_roles=("PATH-X-9",))
    assert set(svc.names()) == {"PATH-X-9", "PATH-M-1", "PATH-BG-5"}  # config roles plus our roster entries
    assert svc.is_emergency("PATH-M-1") and not svc.is_emergency("PATH-BG-5")
    world = MockWorld()
    states = {s["name"]: s for s in svc.states(world)}
    assert states["PATH-BG-5"]["state"] == "free" and states["PATH-M-1"]["bsm_id"] == "11111110"
    assert states["PATH-M-1"]["emergency"] is True and states["PATH-M-1"]["vehicle_type"] == "vehicle.ambulance.ford"

    # another organisation's name or an unknown one is refused
    for name in ("VW-M-1", "PATH-BG-7", ""):
        with pytest.raises(SlotClaimError) as e:
            svc.claim(world, name, by="session")
        assert e.value.code == "unknown_slot"

    # a free slot: nothing to adopt, the caller spawns and then holds it
    assert svc.claim(world, "PATH-BG-5", by="session", marker="s1") is None
    ego = car(world, "PATH-BG-5")
    svc.hold("PATH-BG-5", "session", marker="s1", actor_id=ego.id)
    assert {s["name"]: s["state"] for s in svc.states(world)}["PATH-BG-5"] == "session"
    with pytest.raises(SlotClaimError) as e:
        svc.claim(world, "PATH-BG-5", by="fleet")
    assert e.value.code == "slot_busy"

    # the session ends and parks the car: kept, adoptable by the next holder (even the fleet)
    svc.release("PATH-BG-5", marker="s1")
    svc.keeper.remember("PATH-BG-5", ego, "session")
    state = {s["name"]: s for s in svc.states(world)}["PATH-BG-5"]
    assert state["state"] == "kept" and state["holder"] == "session" and state["actor_id"] == ego.id
    assert svc.claim(world, "PATH-BG-5", by="fleet") is ego

    # a car we did not create under a slot name is taken and never adopted
    car(world, "PATH-M-1", "vehicle.lincoln.mkz")
    assert {s["name"]: s["state"] for s in svc.states(world)}["PATH-M-1"] == "taken"
    with pytest.raises(SlotClaimError) as e:
        svc.claim(world, "PATH-M-1", by="fleet")
    assert e.value.code == "slot_taken"

    # a stale record (that car is gone) is dropped and the slot is free again
    world._actors.pop(ego.id)
    assert svc.claim(world, "PATH-BG-5", by="session", marker="s2") is None
    assert svc.keeper.record("PATH-BG-5") is None
    status = svc.status(world)
    assert status["available"] and status["default"] == "PATH-BG-5" and len(status["slots"]) == 3


def test_default_slot_falls_back_when_missing(tmp_path):
    svc = SlotService(roster=roster(tmp_path), keeper=SlotKeeper(path=""), default_slot="PATH-BG-9")
    assert svc.status(MockWorld())["default"] == "PATH-M-1" or svc.status(MockWorld())["default"] in svc.names()
    empty = SlotService(keeper=SlotKeeper(path=""))
    assert empty.names() == () and empty.status(MockWorld()) == {
        "slots": [], "local_org": "PATH", "default": "", "available": False, "scenario": ""}


class VergeMap:
    """One lane along +x at y = 0 (3.5 m wide); everything else is off the road."""

    def get_waypoint(self, location, project_to_road=True, lane_type=None):
        if project_to_road:
            return types.SimpleNamespace(transform=carla.Transform(
                carla.Location(x=location.x, y=0.0, z=0.0), carla.Rotation(yaw=0.0)))
        return types.SimpleNamespace() if abs(location.y) <= 1.75 else None


def test_park_moves_a_car_beside_the_lane_when_the_map_knows_the_verge():
    world = MockWorld()
    a = car(world, "PATH-BG-5", x=50.0, y=0.5)
    info = park_actor(VergeMap(), a)
    assert info == {"moved": True, "x": 50.0, "y": 5.0}  # 5 m to the right of the lane
    assert a.get_transform().location.y == 5.0
    assert a.control_history[-1].brake == 1.0 and a.control_history[-1].hand_brake
    # already off the road: left where it is
    assert off_road_transform(VergeMap(), carla.Transform(carla.Location(x=1.0, y=9.0, z=0.0), carla.Rotation())) is None
    # a map that cannot tell (every point projects onto a lane): the car stays put, hand brake on
    b = car(world, "PATH-M-1", x=3.0)
    info = park_actor(world.get_map(), b)
    assert info["moved"] is False and b.get_transform().location.x == 3.0 and b.control_history[-1].hand_brake
    # adoption puts the car at the requested pose with neutral controls
    pose = carla.Transform(carla.Location(x=80.0, y=0.0, z=0.5), carla.Rotation(yaw=0.0))
    reset_adopted_actor(b, pose)
    assert b.get_transform().location.x == 80.0 and b.control_history[-1].hand_brake is False
