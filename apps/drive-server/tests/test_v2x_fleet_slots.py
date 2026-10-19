"""Fleet vehicles under VOICES scenario slots: adoption, parking instead of destroying, release."""

import asyncio
import types

import carla
import pytest

from digital_twin_bridge.v2x_behaviors.fleet import Fleet
from digital_twin_bridge.voices_slots import SlotClaimError, SlotKeeper, SlotService
from tests.test_v2x_fleet import Clock, FakeAgent, FakeMap, FakePlanner


class VergeMap(FakeMap):
    """FakeMap where points more than 1.75 m from the lane centre count as off the road."""

    def get_waypoint(self, location, project_to_road=True, lane_type=None):
        if not project_to_road and abs(location.y) > 1.75:
            return None
        return super().get_waypoint(location, project_to_road, lane_type)


@pytest.fixture
def slots():
    return SlotService(keeper=SlotKeeper(path=""), config_roles=("PATH-M-1", "PATH-BG-5"))


@pytest.fixture
def fleet(mock_world, slots):
    clock = Clock()
    fmap = VergeMap()
    cfg = types.SimpleNamespace(FLEET_MAX_VEHICLES=3, FLEET_DECISION_EVERY_N_TICKS=1, FLEET_TICK_BUDGET_MS=20.0,
                                FLEET_LIGHT_SCAN_EVERY_N_TICKS=2, CARLA_HOST="localhost", CARLA_PORT=2000)
    f = Fleet(mock_world, fmap, cfg, planner=FakePlanner(fmap), agent_factory=FakeAgent, client_factory=lambda: None,
              now_fn=clock, slots=slots)
    f.clock = clock
    return f


async def drain(q):
    await asyncio.sleep(0)
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_slot_car_is_parked_not_destroyed_and_adopted_again(fleet, mock_world, slots):
    q = fleet.subscribe()
    reply = fleet.spawn("ambulance", spawn={"x": 10, "y": 0}, role="PATH-M-1")
    v = reply["vehicle"]
    actor = mock_world.get_actor(v["actor_id"])
    assert v["name"] == "PATH-M-1" and v["slot"] == "PATH-M-1" and v["parked"] is False
    assert actor.attributes["role_name"] == "PATH-M-1" and reply["count"] == 1
    assert slots.holder("PATH-M-1").by == "fleet" and slots.holder("PATH-M-1").actor_id == actor.id
    fleet.tick()
    fv = fleet._vehicles[v["actor_id"]]
    assert fv.agent is not None and fleet.tracker.get("actor:%d" % actor.id) is not None
    initial_state = v["state"]

    # the same name cannot be spawned twice while it drives
    with pytest.raises(SlotClaimError) as e:
        fleet.spawn("ambulance", spawn={"x": 50, "y": 0}, role="PATH-M-1")
    assert e.value.code == "slot_busy"

    # "remove" parks it beside the lane with the lights off and keeps the actor for the adapter's binding
    reply = fleet.despawn(v["actor_id"])
    assert reply["parked"] is True and reply["count"] == 0
    assert not actor.is_destroyed and fv.parked and fv.state() == "PARKED" and fv.agent is None
    assert actor.get_transform().location.y == 5.0 and actor.control_history[-1].hand_brake
    assert fv.light_mask == 0 and fv.lights_applied == 0
    assert fleet.stats()["count"] == 0 and fleet.stats()["parked"] == 1 and fleet.has_vehicles is False
    assert slots.holder("PATH-M-1") is None and slots.keeper.record("PATH-M-1")["actor_id"] == actor.id
    listed = fleet.list()["vehicles"]
    assert listed[0]["state"] == "PARKED" and listed[0]["siren"] is False and listed[0]["lights"] is False
    assert fleet.compact_list()[0]["parked"] is True
    with pytest.raises(ValueError):
        fleet.set_erv_state(v["actor_id"], siren=True)
    fleet.tick()  # a parked car gets no controller and no ERV track
    assert fv.agent is None and fleet.tracker.get("actor:%d" % actor.id) is None

    # spawning the slot again adopts the parked car: same actor, back on the road at the new pose
    reply = fleet.spawn("ambulance", spawn={"x": 100, "y": 0}, role="PATH-M-1")
    assert reply["vehicle"]["actor_id"] == actor.id and reply["vehicle"]["parked"] is False
    assert reply["vehicle"]["state"] == initial_state and reply["count"] == 1
    assert actor.get_transform().location.x == 100.0 and actor.get_transform().location.y == 0.0
    assert slots.holder("PATH-M-1").by == "fleet" and slots.keeper.record("PATH-M-1")["actor_id"] == actor.id
    assert len(fleet._vehicles) == 1
    # the controller waits a few ticks for the teleport to show up in the world snapshot
    fleet.tick()
    fleet.tick()
    assert fleet._vehicles[actor.id].agent is None
    fleet.tick()
    assert fleet._vehicles[actor.id].agent is not None and fleet._vehicles[actor.id].bind_pose is None

    # release destroys it for good
    reply = fleet.despawn(actor.id, release=True)
    assert reply["parked"] is False and actor.is_destroyed and fleet.list()["vehicles"] == []
    assert slots.holder("PATH-M-1") is None and slots.keeper.record("PATH-M-1") is None
    fleet.tick()
    events = [(e["reason"], e["from"], e["to"]) for e in await drain(q)]
    assert events == [
        ("spawned", None, initial_state),
        (Fleet.PARK_REASON, initial_state, "PARKED"),
        ("adopted", "PARKED", initial_state),
        ("despawned", initial_state, None),
    ]


def test_slot_held_by_a_foreign_car_is_refused(fleet, mock_world):
    bp = types.SimpleNamespace(id="vehicle.lincoln.mkz", _attributes={"role_name": "PATH-M-1"})
    foreign = mock_world.try_spawn_actor(bp, carla.Transform(carla.Location(x=0.0, y=0.0, z=0.5), carla.Rotation(yaw=0.0)))
    with pytest.raises(SlotClaimError) as e:
        fleet.spawn("ambulance", spawn={"x": 50, "y": 0}, role="PATH-M-1")
    assert e.value.code == "slot_taken" and fleet.list()["vehicles"] == [] and not foreign.is_destroyed
    with pytest.raises(SlotClaimError) as e:
        fleet.spawn("civilian", spawn={"x": 50, "y": 0}, role="PATH-BG-1")
    assert e.value.code == "unknown_slot"
    # without a slot service a role is just a name
    fleet._slots = None
    v = fleet.spawn("civilian", spawn={"x": 60, "y": 0}, role="PATH-BG-5")["vehicle"]
    assert v["name"] == "PATH-BG-5" and mock_world.get_actor(v["actor_id"]).attributes["role_name"] == "PATH-BG-5"


def test_a_repeatedly_stuck_slot_car_is_parked_not_removed(fleet, mock_world, slots):
    v = fleet.spawn("ambulance", spawn={"x": 10, "y": 0}, role="PATH-M-1")["vehicle"]
    fleet.tick()
    fv = fleet._vehicles[v["actor_id"]]
    fv.stuck_recoveries = fleet.STUCK_MAX_RECOVERIES
    fleet._recover_stuck(fv, fv.last_snap, fleet.clock.t)
    assert fv.parked and v["actor_id"] in fleet._vehicles and not mock_world.get_actor(v["actor_id"]).is_destroyed
    assert slots.keeper.record("PATH-M-1") is not None and slots.holder("PATH-M-1") is None
    # a car without a slot is removed instead
    c = fleet.spawn("civilian", spawn={"x": 200, "y": 0})["vehicle"]
    fleet.tick()
    cv = fleet._vehicles[c["actor_id"]]
    cv.stuck_recoveries = fleet.STUCK_MAX_RECOVERIES
    fleet._recover_stuck(cv, cv.last_snap, fleet.clock.t)
    assert c["actor_id"] not in fleet._vehicles and mock_world.get_actor(c["actor_id"]).is_destroyed


def test_despawn_all_parks_slot_cars_and_removes_the_rest(fleet, mock_world, slots):
    a = fleet.spawn("ambulance", spawn={"x": 10, "y": 0}, role="PATH-M-1")["vehicle"]
    c = fleet.spawn("civilian", spawn={"x": 100, "y": 0})["vehicle"]
    reply = fleet.despawn_all()
    assert reply == {"type": "v2x_vehicles_despawned", "count": 2, "parked": 1}
    assert not mock_world.get_actor(a["actor_id"]).is_destroyed and mock_world.get_actor(c["actor_id"]).is_destroyed
    assert fleet.has_vehicles is False and len(fleet._vehicles) == 1
    # a browser session may adopt the parked ambulance too
    assert slots.claim(mock_world, "PATH-M-1", by="session", marker="s") is mock_world.get_actor(a["actor_id"])
    # releasing everything destroys the parked car as well
    assert fleet.despawn_all(release=True) == {"type": "v2x_vehicles_despawned", "count": 1, "parked": 0}
    assert mock_world.get_actor(a["actor_id"]).is_destroyed and slots.keeper.record("PATH-M-1") is None


def test_a_slot_erv_heard_through_its_own_bsms_is_not_announced_twice(fleet, mock_world):
    v = fleet.spawn("ambulance", spawn={"x": 10, "y": 0}, role="PATH-M-1")["vehicle"]
    aid = v["actor_id"]
    fleet.tick()
    assert fleet.tracker.get("actor:%d" % aid) is not None  # the fleet announces it until BSMs arrive
    summary = {"lights": {"siren": "inUse", "lights": "inUse", "role": "ambulance"}, "speed_mps": 10.0}

    class FakeHub:
        def update(self, tracker, now, pose_of, gps_to_xy=None):
            return 1 if tracker.update_from_bsm("PATH-M-1", summary, now, actor_pose=pose_of(aid), actor_id=aid) else 0

    fleet._hub_source = FakeHub()
    fleet.tick()
    fleet.tick()
    assert sorted(t.key for t in fleet.tracker.tracks()) == ["PATH-M-1"]  # one ERV, heard through the adapter
    track = fleet.tracker.get("PATH-M-1")
    assert track.source == "bsm" and track.actor_id == aid and track.active
    # the BSMs stop (DT down): the track expires and the fleet announces the car itself again
    fleet._hub_source = None
    fleet.clock.t += 20.0
    fleet.tick()
    fleet.tick()
    assert sorted(t.key for t in fleet.tracker.tracks()) == ["actor:%d" % aid]


def fleet_with(mock_world, planner, agent_factory):
    fmap = planner._map
    cfg = types.SimpleNamespace(FLEET_MAX_VEHICLES=3, FLEET_DECISION_EVERY_N_TICKS=1, FLEET_TICK_BUDGET_MS=20.0,
                                FLEET_LIGHT_SCAN_EVERY_N_TICKS=2, CARLA_HOST="localhost", CARLA_PORT=2000)
    return Fleet(mock_world, fmap, cfg, planner=planner, agent_factory=agent_factory, client_factory=lambda: None,
                 now_fn=Clock())


def test_unroutable_spots_are_refused_and_binding_retries_once(mock_world):
    class NoRoutePlanner(FakePlanner):
        def random_destination(self, from_location, min_m=150.0, tries=25, strict=False):
            return None if strict else super().random_destination(from_location, min_m, tries)

    f = fleet_with(mock_world, NoRoutePlanner(FakeMap()), FakeAgent)
    with pytest.raises(ValueError):
        f.spawn("civilian", spawn={"x": 10, "y": 0})          # no routed destination from there
    assert f.list()["vehicles"] == []
    assert all(a.is_destroyed for a in mock_world.spawned_actors if a.type_id.startswith("vehicle"))

    class FlakyAgent(FakeAgent):
        def plan_to(self, destination):
            if not self.plans:   # the first plan fails: no path from the reported position
                self.plans.append(("failed", destination))
                raise RuntimeError("Node 179 not reachable from 10")
            return super().plan_to(destination)

    f2 = fleet_with(mock_world, FakePlanner(FakeMap()), FlakyAgent)
    v = f2.spawn("civilian", spawn={"x": 10, "y": 0})["vehicle"]
    f2.tick()
    fv = f2._vehicles[v["actor_id"]]
    assert fv.agent is not None and [p[0] for p in fv.agent.plans] == ["failed", "to"]
    assert fv.destination.x == fv.last_snap.x + 500.0  # routed again from where the car stands


def test_a_slot_car_driven_at_a_restart_is_adoptable_by_the_next_server(fleet, mock_world, tmp_path):
    path = str(tmp_path / "voices_slots.json")
    fleet._slots = SlotService(keeper=SlotKeeper(path=path), config_roles=("PATH-M-1",))
    v = fleet.spawn("ambulance", spawn={"x": 10, "y": 0}, role="PATH-M-1")["vehicle"]
    actor = mock_world.get_actor(v["actor_id"])
    rec = fleet._slots.keeper.record("PATH-M-1")
    assert rec["actor_id"] == actor.id and rec["by"] == "fleet"
    # a new server (fresh holders, the same file) finds the car kept, not taken by a foreign program
    restarted = SlotService(keeper=SlotKeeper(path=path), config_roles=("PATH-M-1",))
    assert {s["name"]: s["state"] for s in restarted.states(mock_world)}["PATH-M-1"] == "kept"
    assert restarted.claim(mock_world, "PATH-M-1", by="fleet") is actor
