"""The Editor's outliner: grouping, ownership, listing and inspection of world actors."""

import types

import carla

from digital_twin_bridge.scenario_roster import ScenarioRoster
from digital_twin_bridge.world_outliner import OutlinerContext, classify, decode_lights, inspect, list_actors
from tests.conftest import MockActor, MockWorld

XML = (
    '<s><vehicles>'
    '<vehicle name="PATH-M-1" bsmid="11111110" organization="PATH" type="vehicle.ambulance.ford"/>'
    '<vehicle name="PATH-BG-1" bsmid="11111111" organization="PATH" type="vehicle.lincoln.mkz"/>'
    '<vehicle name="VW-M-1" bsmid="00000000" organization="VW" type="vehicle.lincoln.mkz"/>'
    '</vehicles></s>'
)


def spawn(world, type_id, role="", x=0.0, y=0.0):
    bp = types.SimpleNamespace(id=type_id, _attributes={"role_name": role} if role else {})
    return world.try_spawn_actor(bp, carla.Transform(carla.Location(x=x, y=y, z=0.5), carla.Rotation(yaw=0.0)))


def test_groups_and_owners(tmp_path):
    xml = tmp_path / "scn.xml"
    xml.write_text(XML)
    roster = ScenarioRoster(xml_path=str(xml))
    world = MockWorld()
    ego = spawn(world, "vehicle.tesla.model3", "ego_vehicle_1")
    browser_slot = spawn(world, "vehicle.lincoln.mkz", "PATH-BG-1")       # a session driving a slot
    ambulance = spawn(world, "vehicle.ambulance.ford", "PATH-M-1")        # the fleet's ERV under the slot
    parked = spawn(world, "vehicle.lincoln.mkz", "PATH-BG-1")             # a parked slot car we created
    manual = spawn(world, "vehicle.lincoln.mkz", "PATH-M-1")              # the DT manual car
    ghost = spawn(world, "vehicle.lincoln.mkz", "VW-M-1")
    civ = spawn(world, "vehicle.lincoln.mkz", "drive_civ_3")
    traffic = spawn(world, "vehicle.audi.tt", "autopilot")
    dynamic = spawn(world, "vehicle.audi.tt", "dynamic_geofence_1")
    playback = spawn(world, "vehicle.tesla.model3", "trajectory")
    stale = spawn(world, "vehicle.tesla.model3", "ego_vehicle_dead")
    bkg = spawn(world, "vehicle.nissan.patrol", "bkg")
    cone = spawn(world, "static.prop.trafficcone01")
    editor_cam = spawn(world, "sensor.camera.rgb", "drive_editor_abc")
    ego_cam = spawn(world, "sensor.camera.rgb")
    ego_cam.parent = ego
    dt_sensor = spawn(world, "sensor.other.gnss", "PATH-R-1")
    signal = spawn(world, "traffic.traffic_light")
    walker = spawn(world, "walker.pedestrian.0001")
    ctx = OutlinerContext(
        session_egos={ego.id: 1, browser_slot.id: 2},
        traffic_ids={traffic.id},
        dynamic_ids={dynamic.id},
        kept_ids={parked.id},
        editor_roles={"drive_editor_abc"},
        fleet_kind_of=lambda aid: "erv" if aid == ambulance.id else ("civilian" if aid == civ.id else None),
        roster=roster,
    )
    expected = {
        ego: ("ego", "session"), browser_slot: ("ego", "session"), ambulance: ("voices", "fleet"),
        parked: ("voices", "drive"), manual: ("voices", "dt"), ghost: ("ghost", "dt"), civ: ("fleet", "fleet"),
        traffic: ("traffic", "drive"), dynamic: ("dynamic", "drive"), playback: ("trajectory", "drive"),
        stale: ("other", "drive"), bkg: ("foreign", "other"), cone: ("prop", "drive"),
        editor_cam: ("sensor", "drive"), ego_cam: ("sensor", "session"), dt_sensor: ("sensor", "other"),
        signal: ("light", "world"), walker: ("walker", "other"),
    }
    for actor, want in expected.items():
        assert classify(actor, ctx) == want, actor.attributes
    assert classify(world.get_spectator(), ctx) == ("", "")

    listing = list_actors(world, ctx, max_items=5)
    assert listing["total"] == len(expected) and listing["truncated"] is True and len(listing["actors"]) == 5
    assert [a["group"] for a in listing["actors"]][:2] == ["ego", "ego"]      # group order first
    assert listing["groups"]["voices"] == 3 and listing["groups"]["sensor"] == 3
    full = list_actors(world, ctx)["actors"]
    assert [a["group"] for a in full] == sorted([a["group"] for a in full], key=lambda g: [
        "ego", "voices", "ghost", "fleet", "traffic", "dynamic", "trajectory", "foreign", "walker", "prop", "sensor",
        "light", "other"].index(g))
    entry = next(a for a in full if a["id"] == ambulance.id)
    assert entry["name"] == "Ambulance Ford" and entry["role"] == "PATH-M-1" and entry["pos"] == [0.0, 0.0, 0.5]
    assert entry["speed_mps"] == 0.0 and entry["alive"] is True and entry["parent"] is None
    cam_entry = next(a for a in full if a["id"] == ego_cam.id)
    assert cam_entry["parent"] == ego.id


def test_inspect_reads_control_lights_and_bbox():
    world = MockWorld()
    car = spawn(world, "vehicle.tesla.model3", "ego_vehicle_1", x=3.0)
    car.get_light_state = lambda: 512 | 2 | 16
    car.get_speed_limit = lambda: 40.0
    car.bounding_box = types.SimpleNamespace(extent=types.SimpleNamespace(x=2.4, y=1.0, z=0.8))
    car.apply_control(carla.VehicleControl(throttle=0.5, steer=-0.2, brake=0.0, hand_brake=True))
    ctx = OutlinerContext(session_egos={car.id: 1})
    details = inspect(world, car.id, ctx)
    assert details["group"] == "ego" and details["name"] == "Tesla Model3"
    assert details["control"]["throttle"] == 0.5 and details["control"]["hand_brake"] is True
    assert details["lights"] == ["LowBeam", "RightBlinker", "Special1"]
    assert details["speed_limit"] == 40.0 and details["bbox"] == [2.4, 1.0, 0.8]
    assert details["attributes"] == {"role_name": "ego_vehicle_1"} and details["rot"] == [0.0, 0.0, 0.0]
    assert inspect(world, 99999, ctx) is None
    cone = spawn(world, "static.prop.trafficcone01")
    assert "control" not in inspect(world, cone.id, ctx)
    assert decode_lights("x") == [] and decode_lights(0) == []
