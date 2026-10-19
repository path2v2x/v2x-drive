"""Browser sessions under VOICES scenario slots: claim, keep, adopt, refuse."""

import types

import carla
import pytest

from digital_twin_bridge.drive_server import DriveSession, handle_message
from digital_twin_bridge.voices_slots import SlotClaimError, SlotKeeper, SlotService

START, END = "2026-03-22T17:00:00Z", "2026-03-22T17:30:00Z"


def make_session(mock_world, fake_v2x_api, slots):
    return DriveSession(world=mock_world, carla_map=mock_world.get_map(),
                        api_fetcher=fake_v2x_api.get_detections_range, slots=slots)


def role_of(actor):
    return actor.attributes.get("role_name", "")


@pytest.fixture
def slots():
    return SlotService(keeper=SlotKeeper(path=""), config_roles=("PATH-BG-5",), default_slot="PATH-BG-5")


@pytest.mark.asyncio
async def test_slot_ego_is_kept_between_sessions_and_adopted(mock_world, fake_v2x_api, slots):
    s1 = make_session(mock_world, fake_v2x_api, slots)
    ready = await s1.start(START, END, voices_role="PATH-BG-5")
    assert ready["type"] == "session_ready" and ready["voices_role"] == "PATH-BG-5" and ready["ego_role"] == "PATH-BG-5"
    car = s1.vehicle
    assert role_of(car) == "PATH-BG-5"
    assert slots.holder("PATH-BG-5").by == "session" and slots.holder("PATH-BG-5").actor_id == car.id
    assert {s["name"]: s["state"] for s in slots.states(mock_world)}["PATH-BG-5"] == "session"

    s1.end()
    assert not car.is_destroyed and car.control_history[-1].hand_brake  # parked, not destroyed
    assert slots.holder("PATH-BG-5") is None and slots.keeper.record("PATH-BG-5")["actor_id"] == car.id
    assert {s["name"]: s["state"] for s in slots.states(mock_world)}["PATH-BG-5"] == "kept"

    s2 = make_session(mock_world, fake_v2x_api, slots)
    ready = await s2.start(START, END, voices_role="PATH-BG-5")
    assert ready["vehicle_id"] == car.id and s2.vehicle is car and not car.control_history[-1].hand_brake
    # remembered while driven too, so a drive-server restart can adopt it
    assert slots.keeper.record("PATH-BG-5")["actor_id"] == car.id and slots.holder("PATH-BG-5").by == "session"

    # a third session cannot take the slot while the second drives
    s3 = make_session(mock_world, fake_v2x_api, slots)
    with pytest.raises(SlotClaimError) as e:
        await s3.start(START, END, voices_role="PATH-BG-5")
    assert e.value.code == "slot_busy" and s3.vehicle is None and not s3.is_active

    # release destroys the kept car
    s2.end(release=True)
    assert car.is_destroyed and slots.keeper.record("PATH-BG-5") is None and slots.holder("PATH-BG-5") is None


@pytest.mark.asyncio
async def test_a_slot_carried_by_a_foreign_car_is_refused(mock_world, fake_v2x_api, slots):
    bp = types.SimpleNamespace(id="vehicle.lincoln.mkz", _attributes={"role_name": "PATH-BG-5"})
    foreign = mock_world.try_spawn_actor(bp, carla.Transform(carla.Location(x=5.0, y=0.0, z=0.5), carla.Rotation(yaw=0.0)))
    session = make_session(mock_world, fake_v2x_api, slots)
    reply = await handle_message(session, {"type": "start_session", "start": START, "end": END, "voices_role": "PATH-BG-5"})
    assert reply["type"] == "error" and reply["code"] == "slot_taken" and not session.is_active
    assert not foreign.is_destroyed
    status = await handle_message(session, {"type": "list_voices_slots"})
    assert status["type"] == "voices_slots" and status["default"] == "PATH-BG-5" and status["available"]
    assert status["slots"] == [{"name": "PATH-BG-5", "vehicle_type": "", "bsm_id": "", "emergency": False,
                                "state": "taken", "holder": "foreign", "actor_id": foreign.id}]

    # without a slot the ego is a plain drive car
    reply = await handle_message(session, {"type": "start_session", "start": START, "end": END, "voices_role": ""})
    assert reply["type"] == "session_ready" and reply["voices_role"] == ""
    assert role_of(session.vehicle).startswith("ego_vehicle")
    ego = session.vehicle
    session.end()
    assert ego.is_destroyed  # a plain ego is not kept


@pytest.mark.asyncio
async def test_sessions_without_a_slot_service_keep_the_legacy_behaviour(mock_world, fake_v2x_api):
    session = DriveSession(world=mock_world, carla_map=mock_world.get_map(),
                           api_fetcher=fake_v2x_api.get_detections_range)
    ready = await session.start(START, END)
    assert ready["voices_role"] == "" and role_of(session.vehicle).startswith("ego_vehicle")
    status = await handle_message(session, {"type": "list_voices_slots"})
    assert status == {"type": "voices_slots", "slots": [], "local_org": "", "default": "", "available": False,
                      "scenario": ""}
    session.end()


@pytest.mark.asyncio
async def test_a_slot_car_driven_at_a_restart_is_adopted_by_the_next_server(mock_world, fake_v2x_api, tmp_path):
    path = str(tmp_path / "voices_slots.json")
    before = SlotService(keeper=SlotKeeper(path=path), config_roles=("PATH-BG-5",), default_slot="PATH-BG-5")
    s1 = make_session(mock_world, fake_v2x_api, before)
    await s1.start(START, END, voices_role="PATH-BG-5")
    car = s1.vehicle
    rec = before.keeper.record("PATH-BG-5")
    assert rec["actor_id"] == car.id and rec["by"] == "session" and rec["type_id"] == getattr(car, "type_id", "")

    # the drive server restarts while the car is driven: the holders are gone, the file is not
    after = SlotService(keeper=SlotKeeper(path=path), config_roles=("PATH-BG-5",), default_slot="PATH-BG-5")
    assert {s["name"]: s["state"] for s in after.states(mock_world)}["PATH-BG-5"] == "kept"
    s2 = make_session(mock_world, fake_v2x_api, after)
    ready = await s2.start(START, END, voices_role="PATH-BG-5", spawn={"x": 123.0, "y": -45.0, "yaw": 30.0})
    assert ready["vehicle_id"] == car.id and s2.vehicle is car
    # an adopted car starts at the requested pose (switching over from the editor camera)
    tf = car.get_transform()
    assert (tf.location.x, tf.location.y, tf.rotation.yaw) == (123.0, -45.0, 30.0)
    s2.end()
    assert not car.is_destroyed and after.keeper.record("PATH-BG-5")["by"] == "session"
