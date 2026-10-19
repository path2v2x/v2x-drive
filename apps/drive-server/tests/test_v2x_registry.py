"""Vehicle registry: classification of world actors and the feed join."""

import time
import types

import carla
import pytest

from digital_twin_bridge.scenario_roster import ScenarioRoster
from digital_twin_bridge.v2x_feed import FeedMessage, V2xHub, VehicleState
from digital_twin_bridge.v2x_registry import VehicleRegistry

XML = (
    '<scenario><vehicles>'
    '<vehicle name="PATH-M-1" bsmid="11111110" organization="PATH" type="vehicle.ambulance.ford"/>'
    '<vehicle name="PATH-BG-1" bsmid="11111111" organization="PATH" type="vehicle.lincoln.mkz"/>'
    '<vehicle name="PATH-BG-5" bsmid="11111115" organization="PATH" type="vehicle.lincoln.mkz"/>'
    '<vehicle name="VW-M-1" bsmid="00000000" organization="VW" type="vehicle.lincoln.mkz"/>'
    '</vehicles></scenario>'
)


def spawn(world, type_id, role=None, x=0.0, y=0.0, yaw=0.0, speed=(0.0, 0.0)):
    bp = types.SimpleNamespace(id=type_id, _attributes=({"role_name": role} if role else {}))
    actor = world.try_spawn_actor(bp, carla.Transform(carla.Location(x=x, y=y, z=0.5), carla.Rotation(yaw=yaw)))
    actor.set_target_velocity(carla.Vector3D(x=speed[0], y=speed[1], z=0.0))
    return actor


@pytest.fixture
def roster(tmp_path):
    path = tmp_path / "scn.xml"
    path.write_text(XML)
    return ScenarioRoster(xml_path=str(path))


def test_classification(mock_world, roster):
    hub = V2xHub(roster=roster)
    reg = VehicleRegistry(roster, hub, local_org="PATH")
    ego = spawn(mock_world, "vehicle.lincoln.mkz_2020", role="ego_vehicle_ab12", x=1, y=2, yaw=90, speed=(3, 4))
    amb = spawn(mock_world, "vehicle.ambulance.ford", role="PATH-M-1")
    spawn(mock_world, "vehicle.lincoln.mkz", role="VW-M-1")
    spawn(mock_world, "vehicle.audi.tt", role="autopilot")
    spawn(mock_world, "vehicle.audi.tt", role="dynamic_geofence_3")
    spawn(mock_world, "vehicle.audi.tt", role="trajectory_1")
    spawn(mock_world, "vehicle.audi.tt", role="drive_civ_1")
    spawn(mock_world, "vehicle.lincoln.mkz", role="bkg")
    anon = spawn(mock_world, "vehicle.lincoln.mkz")
    spawn(mock_world, "walker.pedestrian.0001", role="drive_walker")  # not a vehicle
    reg.refresh(mock_world, session_egos={ego.id: 1})
    kinds = {e.key: (e.kind, e.subkind) for e in reg.all_entries()}
    assert kinds["ego_vehicle_ab12"] == ("drive", "ego")
    assert kinds["PATH-M-1"] == ("voices", "scenario")
    assert kinds["VW-M-1"] == ("ghost", "scenario")
    assert kinds["autopilot"] == ("drive", "traffic")
    assert kinds["dynamic_geofence_3"] == ("drive", "dynamic")
    assert kinds["trajectory_1"] == ("drive", "trajectory")
    assert kinds["drive_civ_1"] == ("drive", "other")
    assert kinds["bkg"] == ("other", "")
    assert kinds["actor:%d" % anon.id] == ("other", "")
    assert "drive_walker" not in kinds
    e = reg.get("ego_vehicle_ab12")
    assert e.pos == [1.0, 2.0, 0.5] and e.yaw == 90.0 and e.speed_mps == pytest.approx(5.0)
    assert e.org == "PATH" and e.ego_of == 1 and e.in_world is True
    assert reg.get("PATH-M-1").emergency is True and reg.get("PATH-M-1").bsm_id == "11111110"
    assert reg.get("VW-M-1").org == "VW" and reg.get("VW-M-1").emergency is False
    assert reg.actor_for_key("PATH-M-1") == amb.id and reg.actor_for_key("nope") is None
    # nothing has a feed yet: only scenario vehicles are listed
    assert [v.key for v in reg.listed()] == ["PATH-M-1", "VW-M-1"]
    assert reg.get("PATH-M-1").feed["source"] == "none"
    assert reg.get("PATH-M-1").to_dict()["kind"] == "voices"


def test_ego_that_claimed_a_voices_slot_is_voices(mock_world, roster):
    reg = VehicleRegistry(roster, V2xHub(roster=roster))
    ego = spawn(mock_world, "vehicle.lincoln.mkz", role="PATH-BG-5")
    reg.refresh(mock_world, session_egos={ego.id: 7})
    e = reg.get("PATH-BG-5")
    assert (e.kind, e.subkind) == ("voices", "ego") and e.bsm_id == "11111115" and e.ego_of == 7


def test_traffic_and_dynamic_ids_from_the_server(mock_world, roster):
    reg = VehicleRegistry(roster, None)
    t = spawn(mock_world, "vehicle.audi.tt", role="ego_vehicle_x")      # id sets win over the role prefix
    d = spawn(mock_world, "vehicle.audi.tt")
    reg.refresh(mock_world, traffic_ids=[t.id], dynamic_ids=[d.id])
    assert reg.get("ego_vehicle_x").subkind == "traffic"
    assert reg.get("actor:%d" % d.id).subkind == "dynamic"


def test_duplicate_roles_get_an_id_suffix(mock_world, roster):
    reg = VehicleRegistry(roster, None)
    spawn(mock_world, "vehicle.audi.tt", role="autopilot")
    b = spawn(mock_world, "vehicle.audi.tt", role="autopilot")
    reg.refresh(mock_world)
    keys = sorted(e.key for e in reg.all_entries())
    assert keys == ["autopilot", "autopilot#%d" % b.id]
    assert reg.get("autopilot").feed["source"] == "none"  # no hub -> empty feed
    assert reg.listed() == []


def test_feed_join_and_feed_only_senders(mock_world, roster):
    hub = V2xHub(roster=roster, stale_s=3.0)
    reg = VehicleRegistry(roster, hub)
    spawn(mock_world, "vehicle.ambulance.ford", role="PATH-M-1")
    ego = spawn(mock_world, "vehicle.lincoln.mkz", role="ego_vehicle_1")
    now = time.time()
    hub.publish(FeedMessage("PATH-M-1", "BSM", b"\x00\x14\x01", "tena", now))
    hub.publish(FeedMessage("AMG-BG-1", "BSM", b"\x00\x14\x02", "tena", now))     # ghost without an actor here
    hub.publish(FeedMessage("bsm:deadbeef", "BSM", b"\x00\x14\x03", "udp", now))  # unknown UDP sender
    hub.publish(FeedMessage("udp:SPaT", "SPaT", b"\x00\x13\x00", "udp", now))     # not a vehicle
    hub.update_vehicle(VehicleState("AMG-BG-1", now, x=10.0, y=5.0, z=1.0, speed_mps=2.0,
                                    special_lights=True, org="AMG"))
    reg.refresh(mock_world, session_egos={ego.id: 1})
    listed = [e.key for e in reg.listed()]
    assert listed == ["PATH-M-1", "AMG-BG-1", "bsm:deadbeef"]
    amg = reg.get("AMG-BG-1")
    assert amg.kind == "ghost" and amg.subkind == "feed-only" and amg.in_world is False and amg.actor_id is None
    assert amg.org == "AMG" and amg.pos == [10.0, -5.0, 1.0] and amg.speed_mps == 2.0
    assert amg.special_lights is True and amg.emergency is True
    assert reg.get("PATH-M-1").feed["source"] == "tena" and reg.get("PATH-M-1").feed["count"] == 1
    other = reg.get("bsm:deadbeef")
    assert other.kind == "other" and other.org == "" and other.feed["source"] == "udp"
    assert reg.get("udp:SPaT") is None
    assert reg.get("ego_vehicle_1") is not None and "ego_vehicle_1" not in listed


def test_version_bumps_only_when_the_table_changes(mock_world, roster):
    reg = VehicleRegistry(roster, V2xHub(roster=roster))
    spawn(mock_world, "vehicle.ambulance.ford", role="PATH-M-1")
    reg.refresh(mock_world)
    v = reg.version
    reg.refresh(mock_world)
    assert reg.version == v
    spawn(mock_world, "vehicle.lincoln.mkz", role="VW-M-1")
    reg.refresh(mock_world)
    assert reg.version == v + 1
    assert reg.last_refresh_ms >= 0.0 and reg.updated_at > 0
    reg.reset()
    assert reg.version == v + 2 and reg.all_entries() == []


def test_refresh_survives_a_failing_world(roster):
    reg = VehicleRegistry(roster, None)
    broken = types.SimpleNamespace(get_actors=lambda: (_ for _ in ()).throw(RuntimeError("rpc down")))
    reg.refresh(broken)
    assert reg.all_entries() == [] and reg.version == 0


def test_silent_senders_drop_out_after_a_minute(mock_world, roster):
    hub = V2xHub(roster=roster, stale_s=3.0)
    reg = VehicleRegistry(roster, hub)
    now = time.time()
    hub.publish(FeedMessage("AMG-BG-1", "BSM", b"\x00\x14\x01", "tena", now - 30.0))   # stale, still listed
    hub.publish(FeedMessage("AMG-BG-2", "BSM", b"\x00\x14\x02", "tena", now - 120.0))  # silent for 2 min: gone
    spawn(mock_world, "vehicle.lincoln.mkz", role="VW-M-1")                              # scenario actor, no feed
    reg.refresh(mock_world)
    listed = {e.key: e.feed["source"] for e in reg.listed()}
    assert listed == {"AMG-BG-1": "stale", "VW-M-1": "none"}
    assert reg.get("AMG-BG-2") is None
