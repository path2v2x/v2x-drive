"""J2735 codec: golden frames recorded from the VOICES network (TDCS recordings)."""

import os

import pytest

from digital_twin_bridge import j2735_codec
from digital_twin_bridge.j2735_codec import Codec, peek_bsm_id, peek_message_id, summarize_bsm

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "v2x")


def load_hex(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
        return bytes.fromhex(f.read().strip())


def test_peek_message_id_reads_the_15_bit_id():
    assert peek_message_id(load_hex("20261015_PATHM1_1.hex")) == 20
    assert peek_message_id(load_hex("20261008_UTCBG1_0.hex")) == 20
    assert peek_message_id(b"\x00") is None


def test_peek_bsm_id_matches_the_scenario_bsmid():
    assert peek_bsm_id(load_hex("20261015_PATHM1_1.hex")) == "11111110"
    assert peek_bsm_id(load_hex("20261015_PATHBG1_1.hex")) == "11111111"
    assert peek_bsm_id(load_hex("20261008_UTCBG1_0.hex")) == "33333331"
    assert peek_bsm_id(b"\x00\x14\x05\x00") is None  # too short


def test_summarize_bsm_scales_core_data_and_flags_origin_relative_positions():
    decoded = {"messageId": 20, "value": {"coreData": {
        "id": "11111110", "msgCnt": 0, "secMark": 46000, "lat": 7912, "long": 4081, "elev": 62,
        "speed": 163, "heading": 16151, "brakes": {"wheelBrakes": "80"}, "size": {"length": 2, "width": 6},
        "transmission": "forwardGears",
    }, "partII": [
        {"partII-Id": 2, "partII-Value": {"classDetails": {"role": "ambulance"}, "classification": 65}},
        {"partII-Id": 1, "partII-Value": {"vehicleAlerts": {"sirenUse": "inUse", "lightsUse": "inUse", "multi": "unavailable"}}},
    ]}}
    s = summarize_bsm(decoded)
    assert s["id"] == "11111110"
    assert s["speed_mps"] == pytest.approx(3.26)
    assert s["heading_deg"] == pytest.approx(201.8875)
    assert s["elev_m"] == pytest.approx(6.2)
    assert s["lat"] == pytest.approx(0.0007912) and s["coords"] == "origin-relative"
    assert s["brakes"] is False  # only the "unavailable" bit is set
    assert s["lights"]["siren"] == "inUse" and s["lights"]["role"] == "ambulance"
    assert s["emergency"] is True


def test_summarize_bsm_wgs84_and_unavailable_values():
    decoded = {"messageId": 20, "value": {"coreData": {
        "id": "33333331", "lat": 379182333, "long": -1223336432, "elev": -4096, "speed": 8191, "heading": 28800,
        "brakes": {"wheelBrakes": "00"},
    }}}
    s = summarize_bsm(decoded)
    assert s["coords"] == "wgs84" and s["lat"] == pytest.approx(37.9182333)
    assert s["elev_m"] is None and s["speed_mps"] is None and s["heading_deg"] is None
    assert s["lights"] is None


j2735 = pytest.importorskip("j2735_202409", reason="J2735 decoder package not installed")


def test_decode_golden_frames_with_the_real_decoder():
    codec = Codec()
    assert codec.available
    amb = codec.decode(load_hex("20261015_PATHM1_1.hex"))
    assert amb.error is None and amb.msg_type == "BSM"
    assert amb.summary["id"] == "11111110"
    assert amb.summary["lights"]["siren"] == "inUse" and amb.summary["lights"]["lights"] == "inUse"
    assert amb.summary["lights"]["role"] == "ambulance"
    bg = codec.decode(load_hex("20261015_PATHBG1_1.hex"))
    assert bg.summary["lights"]["siren"] == "unavailable" and bg.summary["emergency"] is False
    old = codec.decode(load_hex("20261008_PATHBG1_0.hex"))  # 40-byte frame, no Part II
    assert old.error is None and old.summary["lights"] is None
    utc = codec.decode(load_hex("20261008_UTCBG1_0.hex"))
    assert utc.summary["coords"] == "wgs84"
    # peek must agree with the full decode
    assert peek_bsm_id(load_hex("20261015_PATHM1_1.hex")) == amb.summary["id"].lower()


def test_decode_errors_are_reported_not_raised():
    codec = Codec()
    bad = codec.decode(load_hex("20261015_PATHM1_1.hex")[:20])
    assert bad.decoded is None and bad.error
    assert codec.decode(b"\x00\x14").error


def test_decode_cache_returns_the_same_object():
    codec = Codec(cache_size=16)
    raw = load_hex("20261015_PATHM1_1.hex")
    first = codec.decode(raw)
    assert codec.decode(raw) is first


async def test_decode_async_runs_off_the_loop():
    codec = Codec()
    result = await codec.decode_async(load_hex("20261015_PATHM1_1.hex"))
    assert result.msg_type == "BSM" and result.summary["id"] == "11111110"
    many = await codec.decode_many_async([load_hex("20261015_PATHM1_1.hex"), load_hex("20261015_PATHBG1_1.hex")])
    assert [m.summary["id"] for m in many] == ["11111110", "11111111"]
