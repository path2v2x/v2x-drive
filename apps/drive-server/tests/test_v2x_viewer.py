"""Viewer connection: request/reply handlers and the push pump."""

import asyncio
import os
import time

import pytest

from digital_twin_bridge.j2735_codec import Codec, DecodedFrame
from digital_twin_bridge.scenario_roster import ScenarioRoster
from digital_twin_bridge.v2x_feed import FeedMessage, V2xHub
from digital_twin_bridge.v2x_registry import VehicleRegistry
from digital_twin_bridge.v2x_viewer import V2xContext, V2xViewerConn, message_payload

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "v2x")
XML = (
    "<scenario><vehicles>"
    "<vehicle name='PATH-M-1' bsmid='11111110' organization='PATH' type='vehicle.ambulance.ford'/>"
    "<vehicle name='PATH-BG-1' bsmid='11111111' organization='PATH' type='vehicle.lincoln.mkz'/>"
    "</vehicles></scenario>"
)


def load_hex(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
        return bytes.fromhex(f.read().strip())


class StubCodec:
    """Decodes nothing but reports what it was asked, so the pump is testable without pycrate."""

    def __init__(self):
        self.calls = []

    def status(self):
        return {"available": False, "error": "stub"}

    async def decode_async(self, raw):
        return self.decode(raw)

    async def decode_many_async(self, frames):
        self.calls.append(list(frames))
        return [self.decode(r) for r in frames]

    def decode(self, raw):
        return DecodedFrame("BSM", {"messageId": 20}, {"id": raw[-1:].hex()}, None, raw)


def sent_of(ws, mtype):
    return [m for m in ws.get_sent_json() if m.get("type") == mtype]


async def wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


@pytest.fixture
def ctx(tmp_path, mock_world):
    path = tmp_path / "scn.xml"
    path.write_text(XML)
    roster = ScenarioRoster(xml_path=str(path))
    hub = V2xHub(roster=roster, stale_s=3.0, queue_max=50)
    registry = VehicleRegistry(roster, hub)
    registry.refresh(mock_world)
    return V2xContext(hub, registry, StubCodec(), roster, push_interval_s=0.02)


async def test_list_subscribe_unsubscribe(ctx, mock_websocket):
    conn = V2xViewerConn(ctx, mock_websocket)
    reply = await conn.handle({"type": "v2x_list_vehicles"})
    assert reply["type"] == "v2x_vehicle_list" and reply["vehicles"] == []
    assert reply["feed"]["decoder"] == {"available": False, "error": "stub"}
    assert reply["feed"]["roster"]["vehicles"] == 2 and reply["feed"]["json_stream"] == {"enabled": False}
    assert conn.lease_until > time.time()

    ctx.hub.publish(FeedMessage("PATH-M-1", "BSM", b"\x00\x14\x01", "tena", time.time()))
    reply = await conn.handle({"type": "v2x_subscribe", "keys": ["PATH-M-1", "ghost-9"], "detail": "full"})
    assert reply == {"type": "v2x_subscribed", "keys": ["PATH-M-1", "ghost-9"], "detail": "full",
                     "unknown": ["ghost-9"]}
    assert conn.sub.keys == {"PATH-M-1", "ghost-9"} and conn.sub.detail == "full"

    reply = await conn.handle({"type": "v2x_unsubscribe", "keys": ["ghost-9"]})
    assert reply["keys"] == ["PATH-M-1"]
    reply = await conn.handle({"type": "v2x_unsubscribe"})
    assert reply["keys"] == []
    assert await conn.handle({"type": "something_else"}) is None
    conn.close()
    assert ctx.hub.status()["subscriptions"] == 0


async def test_subscribe_caps_the_key_count(ctx, mock_websocket):
    conn = V2xViewerConn(ctx, mock_websocket)
    reply = await conn.handle({"type": "v2x_subscribe", "keys": ["k%d" % i for i in range(100)]})
    assert len(reply["keys"]) == 64
    conn.close()


async def test_decode_request(ctx, mock_websocket):
    conn = V2xViewerConn(ctx, mock_websocket)
    reply = await conn.handle({"type": "v2x_decode", "raw_hex": "001401"})
    assert reply["type"] == "v2x_decoded" and reply["msg_type"] == "BSM" and reply["summary"] == {"id": "01"}
    assert (await conn.handle({"type": "v2x_decode", "raw_hex": "zz"}))["error"] == "raw_hex is not hexadecimal"
    assert (await conn.handle({"type": "v2x_decode", "raw_hex": ""}))["error"] == "empty frame"
    conn.close()


async def test_disabled_feed_replies_with_an_error(mock_websocket):
    conn = V2xViewerConn(None, mock_websocket)
    assert conn.sub is None
    reply = await conn.handle({"type": "v2x_list_vehicles"})
    assert reply["type"] == "error"
    await conn.pump()  # returns at once without a subscription
    conn.close()


async def test_pump_batches_messages_and_pushes_list_changes(ctx, mock_websocket):
    conn = V2xViewerConn(ctx, mock_websocket)
    await conn.handle({"type": "v2x_subscribe", "keys": ["PATH-M-1"], "detail": "full"})
    task = asyncio.create_task(conn.pump())
    now = time.time()
    for i in range(5):
        ctx.hub.publish(FeedMessage("PATH-M-1", "BSM", b"\x00\x14" + bytes([i % 2]), "tena", now + i * 0.01,
                                    uuid="u%d" % i, sender="PATH-M-1", meta={"time_of_transmission": "t%d" % i}))
    ctx.hub.publish(FeedMessage("PATH-BG-1", "BSM", b"\x00\x14\x09", "tena", now))  # not subscribed
    await wait_until(lambda: sum(len(p["batch"]) for p in sent_of(mock_websocket, "v2x_messages")) >= 5)
    pushes = sent_of(mock_websocket, "v2x_messages")
    rows = [r for p in pushes for r in p["batch"]]
    assert len(rows) == 5 and pushes[0]["dropped"] == 0
    assert [r["key"] for r in rows] == ["PATH-M-1"] * 5
    assert [r["uuid"] for r in rows] == ["u0", "u1", "u2", "u3", "u4"]
    assert rows[0]["raw_hex"] == "001400" and rows[0]["size"] == 3 and rows[0]["source"] == "tena"
    assert rows[0]["summary"] == {"id": "00"} and rows[0]["decoded"] == {"messageId": 20} and rows[0]["tx"] == "t0"
    assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)
    # identical frames are decoded once per batch
    assert all(len(set(call)) == len(call) for call in ctx.codec.calls)

    # a registry change is pushed as a new vehicle list while the client watches
    ctx.registry.version += 1
    wanted = ctx.registry.version
    await wait_until(lambda: any(m["version"] == wanted for m in sent_of(mock_websocket, "v2x_vehicle_list")))
    lists = sent_of(mock_websocket, "v2x_vehicle_list")
    assert lists[-1]["version"] == wanted and lists[-1]["vehicles"] == []

    # compact detail: no decoded body
    conn.detail = "compact"
    ctx.hub.publish(FeedMessage("PATH-M-1", "BSM", b"\x00\x14\x07", "tena", time.time()))
    await wait_until(lambda: len(sent_of(mock_websocket, "v2x_messages")) > len(pushes))
    last = sent_of(mock_websocket, "v2x_messages")[-1]
    assert "decoded" not in last["batch"][-1] and last["batch"][-1]["summary"] == {"id": "07"}

    # the pump stops when the socket breaks
    await mock_websocket.close()
    ctx.hub.publish(FeedMessage("PATH-M-1", "BSM", b"\x00\x14\x08", "tena", time.time()))
    await asyncio.wait_for(task, timeout=3.0)
    conn.close()


async def test_pump_reports_dropped_messages(ctx, mock_websocket):
    conn = V2xViewerConn(ctx, mock_websocket)
    await conn.handle({"type": "v2x_subscribe", "keys": ["PATH-M-1"]})
    now = time.time()
    for i in range(80):   # queue_max is 50
        ctx.hub.publish(FeedMessage("PATH-M-1", "BSM", b"\x00\x14" + bytes([i]), "tena", now + i * 0.001))
    task = asyncio.create_task(conn.pump())
    await wait_until(lambda: bool(mock_websocket.sent))
    first = mock_websocket.get_sent_json()[0]
    assert first["type"] == "v2x_messages" and first["dropped"] == 30 and len(first["batch"]) == 50
    conn.close()
    await mock_websocket.close()
    ctx.hub.publish(FeedMessage("PATH-M-1", "BSM", b"\x00\x14\x01", "tena", time.time()))
    await asyncio.wait_for(task, timeout=3.0)


def test_message_payload_without_a_decoder():
    msg = FeedMessage("bsm:11111110", "BSM", load_hex("20261015_PATHM1_1.hex"), "udp", 12.3456, seq=4)
    row = message_payload(msg, None, "full")
    assert row["key"] == "bsm:11111110" and row["seq"] == 4 and row["t_rx"] == 12.346
    assert row["summary"] is None and row["error"] is None and "decoded" not in row and row["size"] == 52
    unavailable = DecodedFrame("BSM", None, None, "decoder unavailable", msg.raw)
    row = message_payload(msg, unavailable, "full")
    assert row["error"] == "decoder unavailable" and row["decoded"] is None


def test_real_codec_degrades_without_the_package(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("j2735_202409"):
            raise ImportError("no j2735 here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    codec = Codec()
    assert codec.available is False and "ImportError" in codec.error
    frame = codec.decode(load_hex("20261015_PATHM1_1.hex"))
    assert frame.msg_type == "BSM" and frame.error == "decoder unavailable" and frame.decoded is None
    assert codec.status()["available"] is False
    codec.shutdown()
