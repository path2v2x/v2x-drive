"""Per-vehicle V2X perspective: distances, the reception radius and the push loop."""

import asyncio
import time
import types

import carla
import pytest

from digital_twin_bridge.j2735_codec import DecodedFrame
from digital_twin_bridge.scenario_roster import ScenarioRoster
from digital_twin_bridge.v2x_feed import FeedMessage, V2xHub
from digital_twin_bridge.v2x_perspective import (
    DEFAULT_RADIUS_M, MAX_RADIUS_M, MIN_RADIUS_M, build_perspective, clamp_radius, distance_and_bearing,
)
from digital_twin_bridge.v2x_registry import VehicleEntry, VehicleRegistry
from digital_twin_bridge.v2x_viewer import V2xContext, V2xViewerConn
from tests.test_v2x_viewer import XML, StubCodec, sent_of, wait_until


def test_distance_and_relative_bearing():
    # CARLA frame: x forward (east), y to the right (south), yaw clockwise from above
    assert distance_and_bearing([0, 0, 0], 0.0, [100, 0, 0]) == (100.0, 0.0, 0.0)        # ahead
    assert distance_and_bearing([0, 0, 0], 0.0, [0, 100, 0]) == (100.0, 90.0, 90.0)      # on the right
    assert distance_and_bearing([0, 0, 0], 90.0, [0, 100, 0]) == (100.0, 90.0, 0.0)      # facing it
    assert distance_and_bearing([0, 0, 0], 0.0, [-50, 0, 0])[2] in (180.0, -180.0)       # behind
    assert distance_and_bearing([0, 0, 0], 0.0, [0, -30, 0])[2] == -90.0                 # on the left
    assert distance_and_bearing([3, 4, 0], None, [0, 0, 0]) == (5.0, -126.9, None)       # no heading: no relative
    assert distance_and_bearing(None, 0.0, [1, 1, 0]) == (None, None, None)
    assert distance_and_bearing([1, 1, 0], 0.0, [1, 1, 5]) == (0.0, None, None)


def test_radius_clamping():
    assert clamp_radius(None) == DEFAULT_RADIUS_M
    assert clamp_radius("abc") == DEFAULT_RADIUS_M
    assert clamp_radius(float("nan")) == DEFAULT_RADIUS_M
    assert clamp_radius(1) == MIN_RADIUS_M
    assert clamp_radius(1e9) == MAX_RADIUS_M
    assert clamp_radius("250") == 250.0
    assert clamp_radius(None, default=42.0) == 42.0


def entry(key, x, y, yaw=0.0, kind="voices", feed_source="tena", role=None, emergency=False):
    return VehicleEntry(key=key, actor_id=hash(key) % 1000, role=role or key, type_id="vehicle.lincoln.mkz",
                        kind=kind, org="PATH", pos=[x, y, 0.0], yaw=yaw, speed_mps=10.0, emergency=emergency,
                        feed={"source": feed_source, "rate_hz": 10.0, "count": 5, "age_s": 0.1, "last_rx": 1.0})


def test_build_perspective_filters_by_radius_and_never_hears_itself():
    now = 1000.0
    a, b, c = entry("PATH-M-1", 0, 0, emergency=True), entry("PATH-BG-1", 100, 0), entry("VW-M-1", 1000, 0, kind="ghost")
    d = entry("PATH-BG-2", 50, 50, feed_source="none")  # placed but silent: a receiver only
    latest = {"PATH-M-1": (b"\x00\x14\x01", now - 0.2), "PATH-BG-1": (b"\x00\x14\x02", now - 0.1),
              "VW-M-1": (b"\x00\x14\x03", now - 0.3)}
    decoded = {k: DecodedFrame("BSM", {}, {"id": k, "speed_mps": 11.0}, None, raw) for k, (raw, _) in latest.items()}

    snap = build_perspective([a, b, c, d], latest, decoded, radius_m=300.0, now=now, interval_s=0.5)
    assert snap["type"] == "v2x_perspective" and snap["active"] and snap["radius_m"] == 300.0
    assert snap["count"] == 4 and snap["transmitting"] == 3 and snap["interval_ms"] == 500
    by_key = {v["key"]: v for v in snap["vehicles"]}

    # the ambulance sends its own frame and hears only the car 100 m ahead, not the ghost 1 km away
    m1 = by_key["PATH-M-1"]
    assert m1["emergency"] and m1["tx"]["summary"]["id"] == "PATH-M-1" and m1["tx"]["age_s"] == 0.2
    assert m1["tx"]["rate_hz"] == 10.0 and m1["tx"]["source"] == "tena" and m1["tx"]["size"] == 3
    assert [r["key"] for r in m1["rx"]] == ["PATH-BG-1"] and m1["rx_count"] == 1
    heard = m1["rx"][0]
    assert heard["distance_m"] == 100.0 and heard["rel_bearing_deg"] == 0.0 and heard["summary"]["speed_mps"] == 11.0
    assert heard["name"] == "PATH-BG-1" and heard["kind"] == "voices" and heard["age_s"] == 0.1

    # the silent car hears both neighbours, nearest first, but transmits nothing
    bg2 = by_key["PATH-BG-2"]
    assert bg2["tx"] is None
    assert [r["key"] for r in bg2["rx"]] == ["PATH-BG-1", "PATH-M-1"]  # equal distance: by name
    assert bg2["rx"][0]["distance_m"] == bg2["rx"][1]["distance_m"] == 70.7
    # nobody hears the silent car, and the far ghost hears nobody
    assert all("PATH-BG-2" not in [r["key"] for r in v["rx"]] for v in snap["vehicles"])
    assert by_key["VW-M-1"]["rx"] == []

    # a wider radius reaches the ghost; a narrower one isolates everyone
    wide = build_perspective([a, b, c, d], latest, decoded, radius_m=1500.0, now=now)
    assert [r["key"] for r in {v["key"]: v for v in wide["vehicles"]}["PATH-M-1"]["rx"]] == ["PATH-BG-1", "VW-M-1"]
    tight = build_perspective([a, b, c, d], latest, decoded, radius_m=10.0, now=now)
    assert all(v["rx"] == [] for v in tight["vehicles"])


def test_vehicles_without_a_position_are_left_out():
    lost = VehicleEntry(key="PATH-BG-3", actor_id=None, role="PATH-BG-3", type_id="", kind="voices", pos=None,
                        in_world=False, feed={"source": "tena", "rate_hz": 10.0, "count": 1})
    snap = build_perspective([lost, entry("PATH-BG-1", 0, 0)], {"PATH-BG-3": (b"\x00", 1.0)}, {}, 300.0, 2.0)
    assert [v["key"] for v in snap["vehicles"]] == ["PATH-BG-1"] and snap["transmitting"] == 0


def spawn(world, role, x, y, yaw=0.0):
    bp = types.SimpleNamespace(id="vehicle.lincoln.mkz", _attributes={"role_name": role})
    return world.try_spawn_actor(bp, carla.Transform(carla.Location(x=x, y=y, z=0.5), carla.Rotation(yaw=yaw)))


@pytest.fixture
def live_ctx(tmp_path, mock_world):
    path = tmp_path / "scn.xml"
    path.write_text(XML)
    roster = ScenarioRoster(xml_path=str(path))
    hub = V2xHub(roster=roster, stale_s=3.0, queue_max=50)
    registry = VehicleRegistry(roster, hub)
    spawn(mock_world, "PATH-M-1", 0.0, 0.0)
    spawn(mock_world, "PATH-BG-1", 100.0, 0.0, yaw=180.0)
    now = time.time()
    hub.publish(FeedMessage(key="PATH-M-1", msg_type="BSM", raw=b"\x00\x14\x01", source="tena", t_rx=now, sender="PATH-M-1"))
    hub.publish(FeedMessage(key="PATH-BG-1", msg_type="BSM", raw=b"\x00\x14\x02", source="tena", t_rx=now, sender="PATH-BG-1"))
    registry.refresh(mock_world)
    return V2xContext(hub, registry, StubCodec(), roster, push_interval_s=0.02)


async def test_connection_subscribes_pushes_and_unsubscribes(live_ctx, mock_websocket):
    conn = V2xViewerConn(live_ctx, mock_websocket)
    reply = await conn.handle({"type": "v2x_perspective_subscribe", "radius_m": 150, "interval_ms": 200})
    assert reply["type"] == "v2x_perspective" and reply["active"] and reply["radius_m"] == 150.0
    assert reply["interval_ms"] == 200 and reply["count"] == 2
    by_key = {v["key"]: v for v in reply["vehicles"]}
    assert by_key["PATH-M-1"]["tx"]["summary"] == {"id": "01"}  # the stub decodes the last byte
    heard = by_key["PATH-M-1"]["rx"]
    assert len(heard) == 1 and heard[0]["key"] == "PATH-BG-1" and heard[0]["distance_m"] == 100.0
    # each car faces the other (yaw 0 at the origin, yaw 180 at x=100): both hear the other straight ahead
    assert heard[0]["rel_bearing_deg"] == 0.0 and by_key["PATH-BG-1"]["rx"][0]["rel_bearing_deg"] == 0.0
    assert by_key["PATH-BG-1"]["rx"][0]["bearing_deg"] in (180.0, -180.0)

    # the pump keeps pushing snapshots at the interval
    task = asyncio.create_task(conn.pump())
    try:
        await wait_until(lambda: len(sent_of(mock_websocket, "v2x_perspective")) >= 2, timeout=3.0)
        pushed = sent_of(mock_websocket, "v2x_perspective")
        assert len(pushed) >= 2 and all(p["count"] == 2 for p in pushed)

        # a tighter radius takes effect on the next snapshot
        reply = await conn.handle({"type": "v2x_perspective_set", "radius_m": 50})
        assert reply["radius_m"] == 50.0 and all(v["rx"] == [] for v in reply["vehicles"])
        out_of_range = {v["key"]: v for v in reply["vehicles"]}
        assert out_of_range["PATH-M-1"]["tx"] is not None  # still sending, just hears nobody

        # unsubscribing stops the pushes
        reply = await conn.handle({"type": "v2x_perspective_unsubscribe"})
        assert reply == {"type": "v2x_perspective", "active": False, "vehicles": [], "count": 0, "radius_m": None}
        before = len(sent_of(mock_websocket, "v2x_perspective"))
        await asyncio.sleep(0.5)
        assert len(sent_of(mock_websocket, "v2x_perspective")) == before
    finally:
        conn.close()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_radius_defaults_and_bad_values(live_ctx, mock_websocket):
    conn = V2xViewerConn(live_ctx, mock_websocket)
    reply = await conn.handle({"type": "v2x_perspective_subscribe"})
    assert reply["radius_m"] == DEFAULT_RADIUS_M
    reply = await conn.handle({"type": "v2x_perspective_set", "radius_m": "huge"})
    assert reply["radius_m"] == DEFAULT_RADIUS_M  # unchanged on garbage
    reply = await conn.handle({"type": "v2x_perspective_set", "radius_m": 20000})
    assert reply["radius_m"] == MAX_RADIUS_M
    conn.close()
