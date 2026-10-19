"""The fleet: spawning, ticking with fake agents, events, operator control, pruning."""

import asyncio
import math
import types

import carla
import pytest

from digital_twin_bridge.v2x_behaviors.fleet import Fleet, ROLE_PREFIX


class FakeWaypoint:
    """A straight road along +x: lanes -1 (our direction) and 1 (oncoming)."""

    def __init__(self, x, y=0.0, lane_id=-1, road_id=7, width=3.5, junction=False):
        self.transform = carla.Transform(carla.Location(x=x, y=y, z=0.0), carla.Rotation(yaw=0.0 if lane_id < 0 else 180.0))
        self.lane_id = lane_id
        self.road_id = road_id
        self.section_id = 0
        self.lane_width = width
        self.is_junction = junction
        self.junction_id = 99 if junction else -1
        self.lane_type = "Driving"
        self.s = x if lane_id < 0 else -x

    def next(self, step):
        return [FakeWaypoint(self.transform.location.x + step, self.transform.location.y, self.lane_id, self.road_id, self.lane_width)]

    def previous(self, step):
        return [FakeWaypoint(self.transform.location.x - step, self.transform.location.y, self.lane_id, self.road_id, self.lane_width)]

    def get_left_lane(self):
        if self.lane_id == -1:
            return FakeWaypoint(self.transform.location.x, 3.5, 1, self.road_id, self.lane_width)
        return None

    def get_right_lane(self):
        return None

    def get_landmarks(self, distance, stop_at_junction=False):
        return []


class FakeMap:
    def __init__(self):
        self.name = "FakeRoad"

    def get_spawn_points(self):
        return [carla.Transform(carla.Location(x=float(x), y=0.0, z=0.0), carla.Rotation(yaw=0.0)) for x in (0, 300, 600, 900)]

    def get_waypoint(self, location, project_to_road=True, lane_type=None):
        lane = 1 if location.y > 1.75 else -1
        return FakeWaypoint(location.x, 3.5 if lane == 1 else 0.0, lane)


class FakePlanner:
    def __init__(self, carla_map):
        self._map = carla_map
        self.traces = []

    def grp(self):
        return None

    def _lift(self, transform):
        loc = transform.location
        return carla.Transform(carla.Location(x=loc.x, y=loc.y, z=loc.z + 0.5), carla.Rotation(yaw=transform.rotation.yaw))

    def transform_along_lane(self, ref_location, distance_m, backwards=False):
        dx = -distance_m if backwards else distance_m
        return carla.Transform(carla.Location(x=ref_location.x + dx, y=ref_location.y, z=0.5), carla.Rotation(yaw=0.0))

    def snapped_transform(self, location, yaw_deg=None):
        return carla.Transform(carla.Location(x=location.x, y=0.0, z=0.5), carla.Rotation(yaw=float(yaw_deg or 0.0)))

    def random_destination(self, from_location, min_m=150.0, tries=25):
        return carla.Location(x=from_location.x + 500.0, y=0.0, z=0.0)

    def splice_to_destination(self, path, destination):
        return list(path) + [("dest", None)]

    def opposing_pass_path(self, wp, pass_length_m, **kw):
        return [(wp, "LANEFOLLOW"), ("opp", "CHANGELANELEFT")]

    def lane_change_path(self, agent, wp, direction, speed_mps):
        return [(wp, "LANEFOLLOW"), ("side", "CHANGELANERIGHT")] if direction == "right" else []


class FakeAgent:
    def __init__(self, actor, carla_map, planner, speed_mps, offset_m):
        self.actor = actor
        self.speed = speed_mps
        self.offset = offset_m
        self.plans = []
        self.destination = None
        self.stops = []

    def plan_to(self, destination):
        self.destination = destination
        self.plans.append(("to", destination))
        return 10

    def set_plan(self, plan):
        self.plans.append(("plan", plan))

    def set_target_speed_mps(self, v):
        self.speed = v

    def set_lateral_offset(self, o):
        self.offset = o

    def remaining(self):
        return 5

    def done(self):
        return False

    def run_control(self, stop=False, vehicle_list=None, hard_stop=False):
        self.stops.append((stop, hard_stop, len(vehicle_list or [])))
        return carla.VehicleControl(throttle=0.0 if stop else 0.5, brake=1.0 if hard_stop else (0.5 if stop else 0.0))


def place(actor, x, y=0.0, yaw=0.0, speed=0.0):
    actor.set_transform(carla.Transform(carla.Location(x=x, y=y, z=0.5), carla.Rotation(yaw=yaw)))
    actor.set_target_velocity(carla.Vector3D(x=speed * math.cos(math.radians(yaw)), y=speed * math.sin(math.radians(yaw)), z=0.0))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def fleet(mock_world):
    clock = Clock()
    fmap = FakeMap()
    cfg = types.SimpleNamespace(FLEET_MAX_VEHICLES=3, FLEET_DECISION_EVERY_N_TICKS=1, FLEET_TICK_BUDGET_MS=20.0,
                                FLEET_LIGHT_SCAN_EVERY_N_TICKS=2, CARLA_HOST="localhost", CARLA_PORT=2000)
    f = Fleet(mock_world, fmap, cfg, planner=FakePlanner(fmap), agent_factory=FakeAgent, client_factory=lambda: None,
              now_fn=clock)
    f.clock = clock
    return f


def spawn_ego(mock_world, x=0.0):
    bp = types.SimpleNamespace(id="vehicle.tesla.model3", _attributes={"role_name": "ego_vehicle_1"})
    ego = mock_world.try_spawn_actor(bp, carla.Transform(carla.Location(x=x, y=0.0, z=0.5), carla.Rotation(yaw=0.0)))
    return ego


def test_spawn_relative_to_the_ego_and_list(fleet, mock_world):
    ego = spawn_ego(mock_world)
    reply = fleet.spawn("civilian", spawn={"ahead_of": "ego", "distance_m": 60}, ego_actor=ego)
    v = reply["vehicle"]
    assert reply["type"] == "v2x_vehicle_spawned" and v["kind"] == "civilian" and v["name"] == ROLE_PREFIX["civilian"] + "1"
    assert v["state"] == "NORMAL" and v["pos"] == [60.0, 0.0] and v["destination"] == [560.0, 0.0]
    actor = mock_world.get_actor(v["actor_id"])
    assert actor.attributes["role_name"] == v["name"]
    erv = fleet.spawn("ambulance", spawn={"behind": v["actor_id"], "distance_m": 100}, ego_actor=None)["vehicle"]
    assert erv["kind"] == "erv" and erv["profile"] == "ambulance" and erv["pos"] == [-40.0, 0.0]
    assert erv["siren"] is True and erv["manual"] is False
    listing = fleet.list()
    assert listing["type"] == "v2x_vehicles" and len(listing["vehicles"]) == 2 and listing["stats"]["count"] == 2
    assert fleet.kind_of(erv["actor_id"]) == "erv" and fleet.actor_ids() == {v["actor_id"], erv["actor_id"]}
    assert sorted(a["kind"] for a in fleet.compact_list()) == ["civilian", "erv"]


def test_spawn_validation_and_capacity(fleet, mock_world):
    with pytest.raises(ValueError):
        fleet.spawn("civilian", spawn={"ahead_of": "ego"}, ego_actor=None)
    with pytest.raises(ValueError):
        fleet.spawn("civilian", spawn={"behind": 12345})
    with pytest.raises(ValueError):
        fleet.spawn("civilian", spawn={"nonsense": 1})
    fleet.spawn("civilian", spawn={"x": 10, "y": 0})
    fleet.spawn("civilian", spawn={"spawn_index": 1})
    fleet.spawn("civilian", spawn=None, destination="none")
    with pytest.raises(RuntimeError):
        fleet.spawn("civilian", spawn={"x": 200, "y": 0})
    # a spot already occupied is skipped (the second car lands further along the lane)
    assert len({tuple(v["pos"]) for v in fleet.list()["vehicles"]}) == 3


async def test_tick_drives_decisions_controls_lights_and_events(fleet, mock_world):
    q = fleet.subscribe()
    civ = fleet.spawn("civilian", spawn={"x": 100, "y": 0})["vehicle"]
    erv = fleet.spawn("ambulance", spawn={"x": 0, "y": 0})["vehicle"]
    civ_actor, erv_actor = mock_world.get_actor(civ["actor_id"]), mock_world.get_actor(erv["actor_id"])
    place(civ_actor, 100.0, speed=10.0)
    place(erv_actor, -100.0, speed=12.0)   # 200 m back, closing slowly: monitored, nothing imminent yet
    fleet.tick()
    await asyncio.sleep(0)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert [e["reason"] for e in events[:2]] == ["spawned", "spawned"]
    civ_fv, erv_fv = fleet._vehicles[civ["actor_id"]], fleet._vehicles[erv["actor_id"]]
    # the ERV announced itself to the tracker and the civilian started monitoring it
    assert fleet.tracker.get("actor:%d" % erv["actor_id"]).active
    fleet.tick()
    assert civ_fv.state() == "MONITOR" and civ_fv.decision.erv_key == "actor:%d" % erv["actor_id"]
    assert erv_fv.agent.speed == pytest.approx(11.1 + 16 / 3.6) and erv_fv.agent.offset == -0.3
    assert erv_fv.light_mask & (1 << 9) and erv_fv.lights_applied == erv_fv.light_mask
    assert civ_fv.agent.stops[-1][2] == 1  # the other vehicle was handed to obstacle detection
    # ERV closing in (40 m, 20 s to pass): the civilian signals, then pulls over and stops
    place(erv_actor, 60.0, speed=12.0)
    fleet.clock.t += 1.0
    fleet.tick()
    assert civ_fv.state() == "YIELD_PREP" and civ_fv.light_mask & (1 << 4)
    fleet.clock.t += 3.5
    place(erv_actor, 70.0, speed=12.0)
    fleet.tick()
    assert civ_fv.state() == "PULL_OVER" and civ_fv.agent.offset > 0 and civ_fv.agent.speed == 0.0
    place(civ_actor, 100.0, speed=0.0)
    fleet.clock.t += 2.0
    fleet.tick()
    assert civ_fv.state() == "STOPPED_HOLD" and civ_fv.hard_stop
    assert civ_actor.control_history[-1].brake == 1.0
    await asyncio.sleep(0)
    reasons = []
    while not q.empty():
        reasons.append(q.get_nowait())
    civ_events = [e["to"] for e in reasons if e["actor_id"] == civ["actor_id"] and e["from"] != e["to"]]
    assert civ_events[-3:] == ["YIELD_PREP", "PULL_OVER", "STOPPED_HOLD"]
    # the ambulance meanwhile treated the stopped car as a slow lead
    assert [e["to"] for e in reasons if e["actor_id"] == erv["actor_id"] and e["from"] != e["to"]][:1] == ["FOLLOW_SLOW"]
    stats = fleet.list()["stats"]
    assert stats["count"] == 2 and stats["tracks"] == 1 and stats["events"] >= 5
    fleet.unsubscribe(q)


def test_set_erv_state_and_despawn(fleet, mock_world):
    erv = fleet.spawn("police", spawn={"x": 0, "y": 0})["vehicle"]
    fv = fleet._vehicles[erv["actor_id"]]
    assert fv.agent is None and erv["debug"]["bound"] is False   # bound on the first tick that lists the actor
    fleet.tick()
    assert fv.agent is not None and fv.agent.plans[0] == ("to", fv.destination)
    reply = fleet.set_erv_state(erv["actor_id"], manual=True, siren=False)
    assert reply["vehicle"]["state"] == "MANUAL" and reply["vehicle"]["manual"] is True
    fleet.tick()
    assert fv.decision.siren is False and fv.stop and not (fv.light_mask & (1 << 10))
    reply = fleet.set_erv_state(erv["actor_id"], manual=False, destination={"x": 50, "y": 0})
    assert reply["vehicle"]["state"] == "EN_ROUTE" and reply["vehicle"]["destination"] == [50.0, 0.0]
    assert fv.agent.plans[-1][0] == "to"
    with pytest.raises(ValueError):
        fleet.set_erv_state(999)
    civ = fleet.spawn("civilian", spawn={"x": 200, "y": 0})["vehicle"]
    with pytest.raises(ValueError):
        fleet.set_erv_state(civ["actor_id"], siren=True)
    reply = fleet.despawn(erv["actor_id"])
    assert reply["type"] == "v2x_vehicle_despawned" and reply["count"] == 1
    assert mock_world.get_actor(erv["actor_id"]).is_destroyed
    assert fleet.despawn_all() == {"type": "v2x_vehicles_despawned", "count": 1} and not fleet.has_vehicles


def test_destroyed_actors_are_pruned_and_world_updates_reset(fleet, mock_world):
    civ = fleet.spawn("civilian", spawn={"x": 0, "y": 0})["vehicle"]
    actor = mock_world.get_actor(civ["actor_id"])
    del mock_world._actors[actor.id]   # someone else destroyed it (clear NPCs)
    fleet.tick()
    fleet.tick()
    assert fleet.has_vehicles           # a just-spawned actor is not in get_actors() until the next tick
    fleet.clock.t += 3.0
    fleet.tick()
    fleet.tick()
    assert not fleet.has_vehicles
    civ = fleet.spawn("civilian", spawn={"x": 0, "y": 0})["vehicle"]
    mock_world.get_actor(civ["actor_id"])._destroyed = True
    type(mock_world.get_actor(civ["actor_id"])).is_alive = property(lambda self: not self._destroyed)
    fleet.tick()
    assert not fleet.has_vehicles       # is_alive catches it at once
    del type(actor).is_alive
    fleet.spawn("civilian", spawn={"x": 0, "y": 0})
    fleet.update_world(mock_world, FakeMap())
    assert not fleet.has_vehicles and fleet.tracker.tracks() == []


def test_civilians_keep_roaming_after_reaching_their_destination(fleet, mock_world):
    civ = fleet.spawn("civilian", spawn={"x": 0, "y": 0})["vehicle"]
    fv = fleet._vehicles[civ["actor_id"]]
    first_dest = fv.destination
    fleet.tick()                     # binds the agent
    fv.agent.remaining = lambda: 0   # plan consumed
    fleet.tick()
    assert fv.destination is not first_dest and fv.agent.plans[-1][0] == "to"
    assert fv.destination.x == pytest.approx(500.0)


def test_stuck_vehicles_are_rerouted_then_moved_then_removed(fleet, mock_world):
    civ = fleet.spawn("civilian", spawn={"x": 0, "y": 0})["vehicle"]
    fv = fleet._vehicles[civ["actor_id"]]
    actor = mock_world.get_actor(civ["actor_id"])
    fleet.tick()
    first_dest = fv.destination
    # the fake agent asks for throttle 0.5 while the car never moves
    for _ in range(3):
        fleet.clock.t += 3.5
        fleet.tick()
    assert fv.stuck_recoveries == 1 and fv.destination is not first_dest and fv.agent.plans[-1][0] == "to"
    for _ in range(3):
        fleet.clock.t += 3.5
        fleet.tick()
    assert fv.stuck_recoveries == 2 and fv.agent is not None   # moved to a free spawn point and rebound
    assert actor.get_transform().location.x >= 300.0
    for _ in range(3):
        fleet.clock.t += 3.5
        fleet.tick()
    assert fv.stuck_recoveries == 3
    for _ in range(3):
        fleet.clock.t += 3.5
        fleet.tick()
    assert not fleet.has_vehicles and actor.is_destroyed


def test_bsm_only_sources_ignore_lights_and_fleet_ervs(mock_world):
    fmap = FakeMap()
    cfg = types.SimpleNamespace(FLEET_MAX_VEHICLES=3, FLEET_DECISION_EVERY_N_TICKS=1, FLEET_TICK_BUDGET_MS=20.0,
                                FLEET_LIGHT_SCAN_EVERY_N_TICKS=2, FLEET_ERV_SOURCES="bsm", CARLA_HOST="localhost", CARLA_PORT=2000)
    f = Fleet(mock_world, fmap, cfg, planner=FakePlanner(fmap), agent_factory=FakeAgent, client_factory=lambda: None, now_fn=Clock())
    assert f.erv_sources == frozenset({"bsm"}) and f.stats()["erv_sources"] == ["bsm"]
    bp = types.SimpleNamespace(id="vehicle.ambulance.ford", _attributes={"role_name": "PATH-M-1"})
    amb = mock_world.try_spawn_actor(bp, carla.Transform(carla.Location(x=-200, y=0, z=0.5), carla.Rotation(yaw=0.0)))
    amb.get_light_state = lambda: 1 << 9
    place(amb, -200.0, speed=10.0)
    f.spawn("ambulance", spawn={"x": -100, "y": 0})
    civ = f.spawn("civilian", spawn={"x": 120, "y": 0})["vehicle"]
    for _ in range(4):
        f.tick()
    assert f.erv_tracks() == []                                   # neither light states nor our own ERV count
    assert f._vehicles[civ["actor_id"]].state() == "NORMAL"


def test_external_emergency_lights_feed_the_tracker_and_advisories(fleet, mock_world):
    bp = types.SimpleNamespace(id="vehicle.ambulance.ford", _attributes={"role_name": "PATH-M-1"})
    amb = mock_world.try_spawn_actor(bp, carla.Transform(carla.Location(x=0, y=0, z=0.5), carla.Rotation(yaw=0.0)))
    amb.get_light_state = lambda: 1 << 9
    place(amb, -200.0, speed=10.0)   # 320 m behind, 32 s to pass: monitored but not yet imminent
    civ = fleet.spawn("civilian", spawn={"x": 120, "y": 0})["vehicle"]
    fleet.tick()
    fleet.tick()
    tracks = fleet.erv_tracks()
    assert len(tracks) == 1 and tracks[0]["key"] == "actor:%d" % amb.id and tracks[0]["active"] and tracks[0]["role"] == "ambulance"
    assert fleet._vehicles[civ["actor_id"]].state() == "MONITOR"
    amb.get_light_state = lambda: 0
    fleet.clock.t += 1.0
    fleet.tick()
    fleet.tick()
    assert fleet.erv_tracks()[0]["active"] is False
