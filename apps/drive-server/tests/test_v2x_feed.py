"""V2X feed: JSON stream parsing (TCP), UDP routing, hub fan-out and bounded queues."""

import asyncio
import json
import os
import socket
import time

import pytest

from digital_twin_bridge.scenario_roster import ScenarioRoster
from digital_twin_bridge.v2x_feed import (
    FeedMessage,
    JsonStreamSource,
    UdpUperSource,
    V2xHub,
    binary_content_bytes,
    parse_land_vehicle_record,
    parse_v2x_record,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "v2x")
ONE_CAR_XML = (
    '<s><vehicles><vehicle name="PATH-BG-1" bsmid="11111111" organization="PATH" '
    'type="vehicle.lincoln.mkz"/></vehicles></s>'
)


def load_sample() -> bytes:
    with open(os.path.join(FIXTURES, "streamer_sample.raw"), "rb") as f:
        return f.read()


def load_hex(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
        return bytes.fromhex(f.read().strip())


def sample_records():
    return [json.loads(p) for p in load_sample().split(b"\x1e") if p.strip()]


async def wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


def test_binary_content_formats():
    assert binary_content_bytes([{"0": 0}, {"1": 20}, {"2": 49}]) == b"\x00\x14\x31"
    assert binary_content_bytes([0, 20]) == b"\x00\x14"
    assert binary_content_bytes("0014") == b"\x00\x14"
    assert binary_content_bytes(None) == b""


def test_parse_v2x_record_from_the_captured_stream():
    recs = [r for r in sample_records() if r["metadata"]["type_name"] == "VUG::TV2XMsg::V2X"]
    msg = parse_v2x_record(recs[0], 100.0)
    assert msg.key == "PATH-M-1" and msg.sender == "PATH-M-1"
    assert msg.msg_type == "BSM" and len(msg.raw) == 52 and msg.raw[:2] == b"\x00\x14"
    assert msg.uuid and msg.meta["publisher_endpoint"].startswith("127.0.0.1:")
    assert msg.meta["time_of_transmission"].endswith("Z")
    assert parse_v2x_record({"metadata": {}, "attributes": {"binaryContent": []}}, 1.0) is None


def test_parse_land_vehicle_record():
    recs = [r for r in sample_records() if r["metadata"]["type_name"] == "VUG::Entities::LandVehicle"]
    state = parse_land_vehicle_record(recs[0], 5.0)
    assert state.key == "PATH-M-1" and state.org == "PATH"
    assert state.x == pytest.approx(100.33, abs=0.05) and state.y == pytest.approx(9.09, abs=0.05)
    assert state.speed_mps == pytest.approx(0.0)
    assert state.special_lights is False and state.removed is False
    gone = dict(recs[0])
    gone["metadata"] = dict(recs[0]["metadata"], is_removed=True)
    assert parse_land_vehicle_record(gone, 5.0).removed is True


def test_hub_routes_records_and_tracks_senders(tmp_path):
    roster = ScenarioRoster(xml_path="", dt_scenario_config=str(tmp_path / "none"), dt_repo=str(tmp_path))
    hub = V2xHub(roster=roster, stale_s=3.0, queue_max=100)
    now = 1000.0
    for r in sample_records():
        hub.on_record(r, now)
        now += 0.1
    assert hub.total == 6
    assert hub.senders() == ["PATH-M-1"]
    feed = hub.sender_feed("PATH-M-1", now)
    assert feed["source"] == "tena" and feed["count"] == 6 and feed["types"] == {"BSM": 6}
    assert hub.vehicle_state("PATH-M-1").special_lights is False
    assert hub.sender_feed("nobody")["source"] == "none"
    assert hub.sender_feed("PATH-M-1", now + 10.0)["source"] == "stale"
    # the live scenario object populated the roster
    assert roster.get("VW-M-1").org == "VW" and roster.get("PATH-M-1") is not None
    assert roster.status()["scenario"] == "Cohort 2 - ERV"
    # a removed LandVehicle drops out of the state table
    hub.update_vehicle(parse_land_vehicle_record(
        {"metadata": {"is_removed": True}, "attributes": {"identifier": "PATH-M-1"}}, now))
    assert hub.vehicle_state("PATH-M-1") is None


def test_duplicate_tena_messages_are_dropped_by_uuid():
    hub = V2xHub(stale_s=3.0)
    raw = load_hex("20261015_PATHBG1_1.hex")
    assert hub.publish(FeedMessage("PATH-BG-1", "BSM", raw, "tena", 100.0, uuid="u1"))
    assert not hub.publish(FeedMessage("PATH-BG-1", "BSM", raw, "tena", 100.01, uuid="u1"))  # second streamer
    assert hub.publish(FeedMessage("PATH-BG-1", "BSM", raw, "tena", 100.1, uuid="u2"))
    assert hub.publish(FeedMessage("PATH-BG-1", "BSM", raw, "tena", 100.2))               # no uuid: kept
    feed = hub.sender_feed("PATH-BG-1", 100.3)
    assert feed["count"] == 3 and feed["duplicates"] == 1


def test_streamer_keeper_waits_for_a_young_container(monkeypatch):
    from digital_twin_bridge.v2x_feed import StreamerKeeper
    keeper = StreamerKeeper(container="dt-core")
    calls = []

    class Res:
        def __init__(self, stdout="", returncode=0):
            self.stdout, self.returncode = stdout, returncode

    def fake_run(args, timeout=10.0):
        calls.append(args)
        if args[:2] == ["docker", "ps"]:
            return Res("dt-core\ncarla-rr-maps\n")
        if "pgrep" in args:
            return Res("", 1)                     # no streamer
        if "inspect" in args:
            from datetime import datetime, timezone, timedelta
            started = datetime.now(timezone.utc) - timedelta(seconds=fake_run.age)
            return Res(started.strftime("%Y-%m-%dT%H:%M:%S.123456789Z"))
        return Res()

    fake_run.age = 30.0
    monkeypatch.setattr(keeper, "_run_cmd", fake_run)
    result = keeper.check_once()
    assert result["dt_running"] and not result["streamer_running"] and not result["launched"]
    assert keeper.launches == 0 and result["container_age_s"] == 30
    fake_run.age = 600.0
    result = keeper.check_once()
    assert result["launched"] and keeper.launches == 1
    assert any("exec" in a and "-d" in a for a in calls)


async def test_subscription_queue_is_bounded_and_only_gets_wanted_keys():
    hub = V2xHub(stale_s=3.0, queue_max=20)
    sub = hub.subscribe()
    sub.keys.add("PATH-BG-1")
    for i in range(30):
        hub.publish(FeedMessage("PATH-BG-1", "BSM", b"\x00\x14" + bytes([i]), "tena", 1.0 + i * 0.1))
    hub.publish(FeedMessage("OTHER", "BSM", b"\x00\x14\x00", "tena", 5.0))
    assert sub.queue.qsize() == 20 and sub.dropped == 10
    first = sub.queue.get_nowait()
    assert first.key == "PATH-BG-1" and first.seq == 11  # the oldest ten were dropped
    assert hub.wanted_keys() == {"PATH-BG-1"}
    assert hub.status()["senders"] == 2 and hub.status()["subscriptions"] == 1
    hub.unsubscribe(sub)
    assert hub.status()["subscriptions"] == 0


def test_udp_is_suppressed_while_the_json_stream_is_fresh(tmp_path):
    xml = tmp_path / "scn.xml"
    xml.write_text(ONE_CAR_XML)
    roster = ScenarioRoster(xml_path=str(xml))
    hub = V2xHub(roster=roster, stale_s=3.0)
    raw = load_hex("20261015_PATHBG1_1.hex")
    assert hub.publish(FeedMessage("PATH-BG-1", "BSM", raw, "tena", 100.0))
    hub.on_datagram(raw, 100.5)              # same sender over UDP while TENA is fresh -> suppressed
    assert hub.sender_feed("PATH-BG-1", 100.6)["count"] == 1
    hub.on_datagram(raw, 104.0)              # TENA silent for 4 s -> UDP takes over
    feed = hub.sender_feed("PATH-BG-1", 104.1)
    assert feed["count"] == 2 and feed["source"] == "udp"
    hub.on_datagram(raw, 104.01)             # exact repeat within 50 ms -> duplicate
    assert hub.sender_feed("PATH-BG-1", 104.1)["count"] == 2
    hub.on_datagram(load_hex("20261008_UTCBG1_0.hex"), 104.2)  # unknown bsmid -> bsm:<hex>
    assert "bsm:33333331" in hub.senders()
    hub.on_datagram(b"\x00\x13\x00\x00", 104.3)                  # a SPaT has no vehicle key
    assert "udp:SPaT" in hub.senders()


async def test_json_stream_source_accepts_the_streamer_and_parses_chunks():
    loop = asyncio.get_running_loop()
    received = []
    src = JsonStreamSource("127.0.0.1", 0, loop, lambda rec, t: received.append(rec))
    src.start()
    assert src.port > 0 and src.status()["listening"] == "127.0.0.1:%d" % src.port
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", src.port)
        data = load_sample()
        for i in range(0, len(data), 1234):   # records straddle recv() boundaries
            writer.write(data[i:i + 1234])
            await writer.drain()
            await asyncio.sleep(0.01)
        writer.close()
        await wait_until(lambda: len(received) >= 11)
        assert len(received) == 11            # the last record has no trailing separator: flushed at EOF
        types = [r["metadata"]["type_name"] for r in received]
        assert types.count("VUG::TV2XMsg::V2X") == 6 and types.count("VUG::Entities::LandVehicle") == 4
        st = src.status()
        assert st["records"] == 11 and st["parse_errors"] == 0 and st["connections"] == 1
        assert st["bytes"] == len(data)
        # the streamer reconnects after a restart: a second connection is accepted; bad records are counted
        _, writer2 = await asyncio.open_connection("127.0.0.1", src.port)
        writer2.write(b"\x1enot json\n\x1e" + json.dumps({"metadata": {"type_name": "x"}, "attributes": {}}).encode())
        await writer2.drain()
        writer2.close()
        await wait_until(lambda: len(received) >= 12)
        st = src.status()
        assert st["parse_errors"] == 1 and st["connections"] == 2
        assert received[-1]["metadata"]["type_name"] == "x"
    finally:
        src.stop()


async def test_udp_source_delivers_datagrams():
    got = []
    src = UdpUperSource("127.0.0.1", 0, lambda raw, t: got.append(raw))
    await src.start()
    assert src.port > 0
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b"\x00\x14\x31\x40", ("127.0.0.1", src.port))
        s.close()
        await wait_until(lambda: bool(got))
        assert got == [b"\x00\x14\x31\x40"]
        assert src.status()["datagrams"] == 1
    finally:
        src.stop()
