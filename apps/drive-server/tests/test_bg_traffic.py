"""Background-traffic control: the configured commands run and their outcome is reported."""

import calendar

import pytest

from digital_twin_bridge.bg_traffic import BackgroundTraffic, parse_rfc3339, unavailable_status
from digital_twin_bridge.drive_server import DriveSession, handle_message

T0 = calendar.timegm((2026, 10, 19, 8, 0, 0))


def make(tmp_path, **overrides):
    marker = tmp_path / "running"
    kwargs = dict(
        start_cmd="touch %s && echo started" % marker,
        stop_cmd="rm -f %s" % marker,
        probe_cmd="test -e %s && echo true 2026-10-19T08:00:00Z" % marker,
        tail_cmd="echo '[08:00:10] sim 10 s  SUMO cars 3, mirrored 4'",
        requires=str(tmp_path),
        label="test traffic",
    )
    kwargs.update(overrides)
    return BackgroundTraffic(**kwargs)


def test_rfc3339_times():
    assert parse_rfc3339("2026-10-19T08:00:00Z") == T0
    assert abs(parse_rfc3339("2026-10-19T08:00:00.558245105Z") - (T0 + 0.558245)) < 1e-3
    assert parse_rfc3339("2026-10-19T01:00:00-07:00") == T0
    assert parse_rfc3339("yesterday") is None and parse_rfc3339("") is None


@pytest.mark.asyncio
async def test_unavailable_without_a_start_command_or_the_script(tmp_path):
    none = BackgroundTraffic("")
    assert not none.available and (await none.status())["state"] == "unavailable"
    missing = make(tmp_path, requires=str(tmp_path / "nope"))
    assert not missing.available
    reply = await missing.start()
    assert reply["state"] == "unavailable" and "not configured" in reply["error"]


@pytest.mark.asyncio
async def test_start_and_stop_run_the_commands_and_report(tmp_path):
    clock = {"t": T0 + 90.0}
    bg = make(tmp_path, now_fn=lambda: clock["t"])
    assert await bg.status() == {
        "type": "bg_traffic", "available": True, "label": "test traffic", "state": "stopped", "running": False,
        "since": None, "uptime_s": None, "last_start": None, "last_stop": None,
        "last_line": "[08:00:10] sim 10 s  SUMO cars 3, mirrored 4"}
    st = await bg.start()
    assert st["state"] == "running" and st["running"]
    assert st["last_start"]["ok"] and st["last_start"]["code"] == 0 and st["last_start"]["output"] == "started"
    assert st["since"] == T0 and st["uptime_s"] == 90.0
    assert st["last_line"] == "[08:00:10] sim 10 s  SUMO cars 3, mirrored 4"
    again = await bg.start()
    assert again["error"] == "already running" and again["state"] == "running"
    st = await bg.stop()
    assert st["state"] == "stopped" and not st["running"] and st["last_stop"]["ok"] and st["since"] is None
    assert st["last_line"].startswith("[08:00:10]")  # the last line seen stays readable
    assert (await bg.stop())["error"] == "not running"


@pytest.mark.asyncio
async def test_a_failing_start_is_reported_and_leaves_it_stopped(tmp_path):
    bg = make(tmp_path, start_cmd="echo nope; exit 3")
    st = await bg.start()
    assert st["state"] == "stopped" and not st["running"]
    assert st["last_start"]["ok"] is False and st["last_start"]["code"] == 3 and st["last_start"]["output"] == "nope"


@pytest.mark.asyncio
async def test_starting_and_stopping_are_shown_while_the_script_catches_up(tmp_path):
    clock = {"t": 1000.0}
    marker = tmp_path / "running"
    # the start command returns before the probe sees anything (a detached container coming up)
    bg = make(tmp_path, now_fn=lambda: clock["t"], start_cmd="echo launched", start_grace_s=20.0, stop_grace_s=10.0)
    st = await bg.start()
    assert st["state"] == "starting" and not st["running"]
    marker.write_text("")
    assert (await bg.status())["state"] == "running"
    # the stop command returns at once too; the script needs a moment to park its cars and exit
    bg.stop_cmd = "true"
    st = await bg.stop()
    assert st["state"] == "stopping" and st["running"]
    clock["t"] += 11.0
    assert (await bg.status())["state"] == "running"  # it never went away: back to the truth
    marker.unlink()
    assert (await bg.status())["state"] == "stopped"


@pytest.mark.asyncio
async def test_the_session_handlers_pass_through(mock_world, fake_v2x_api, tmp_path):
    bg = make(tmp_path)
    session = DriveSession(world=mock_world, carla_map=mock_world.get_map(),
                           api_fetcher=fake_v2x_api.get_detections_range, bg_traffic=bg)
    assert (await handle_message(session, {"type": "bg_traffic_status"}))["state"] == "stopped"
    assert (await handle_message(session, {"type": "bg_traffic_start"}))["state"] == "running"
    assert (await handle_message(session, {"type": "bg_traffic_stop"}))["state"] == "stopped"
    bare = DriveSession(world=mock_world, carla_map=mock_world.get_map(), api_fetcher=fake_v2x_api.get_detections_range)
    assert await handle_message(bare, {"type": "bg_traffic_status"}) == unavailable_status()
