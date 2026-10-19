#!/usr/bin/env python3
"""Smoke test for the V2X driving models against a live drive server.

Spawns two civilians ahead of a short-lived ego session and an ambulance
behind them, then watches the fleet for a while over the drive WebSocket:
state transitions, the closest any two fleet vehicles get, whether the
civilians yielded and whether the ambulance got past them.

    python scripts/v2x_fleet_smoke.py --url ws://127.0.0.1:8765 --duration 120

Leaves nothing behind: the fleet is despawned at the end (also on Ctrl+C).
"""

import argparse
import asyncio
import json
import math
import time
from datetime import datetime, timedelta, timezone

import websockets


async def request(ws, msg, expect=None, timeout=20.0):
    """Send one request and wait for its reply, collecting pushes on the side."""
    await ws.send(json.dumps(msg))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
        if isinstance(raw, (bytes, bytearray)):
            continue
        reply = json.loads(raw)
        if reply.get("type") == "v2x_vehicle_event":
            EVENTS.append(reply)
            continue
        if reply.get("type") in ("telemetry", "xosc_event"):
            continue
        if expect is None or reply.get("type") in (expect, "error"):
            return reply
    raise TimeoutError("no reply to %s" % msg.get("type"))


EVENTS = []


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8765")
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--civilians", type=int, default=2)
    ap.add_argument("--erv", default="ambulance")
    ap.add_argument("--ahead", type=float, default=90.0, help="first civilian this far ahead of the ego")
    ap.add_argument("--erv-behind", type=float, default=110.0, help="ERV this far behind the first civilian")
    ap.add_argument("--keep", action="store_true", help="leave the fleet in the world at the end")
    args = ap.parse_args()

    async with websockets.connect(args.url, max_size=16 * 1024 * 1024) as ws:
        status = await request(ws, {"type": "server_status"}, "server_status")
        print("server:", status)
        now = datetime.now(timezone.utc)
        ready = await request(ws, {"type": "start_session", "start": (now - timedelta(hours=1)).isoformat(),
                                   "end": now.isoformat(), "vehicle": "vehicle.lincoln.mkz"}, "session_ready", timeout=90)
        if ready.get("type") != "session_ready":
            print("could not start a session:", ready)
            return 2
        print("ego session", ready.get("vehicle_id"), "role", ready.get("ego_role"))
        spawned = []

        async def spawn_with_retries(msg, key, distance):
            """Short RFS roads: back off the distance when the lane ends or the spot is taken."""
            for d in (distance, distance * 0.6, distance * 0.35, 25.0):
                msg["spawn"][key] = round(d, 1)
                r = await request(ws, msg, "v2x_vehicle_spawned")
                if r.get("type") == "v2x_vehicle_spawned":
                    return r
                print("  spawn at %.0f m failed: %s" % (d, r.get("message")))
            return r

        try:
            first = None
            for i in range(args.civilians):
                r = await spawn_with_retries({"type": "spawn_v2x_vehicle", "kind": "civilian",
                                              "spawn": {"ahead_of": "ego", "distance_m": 0}}, "distance_m", args.ahead + 25.0 * i)
                print("spawn civilian:", r.get("vehicle", r))
                if r.get("type") != "v2x_vehicle_spawned":
                    break
                spawned.append(r["vehicle"]["actor_id"])
                first = first or r["vehicle"]["actor_id"]
            if first is not None:
                r = await spawn_with_retries({"type": "spawn_v2x_vehicle", "kind": "erv", "profile": args.erv,
                                              "spawn": {"behind": first, "distance_m": 0}}, "distance_m", args.erv_behind)
                print("spawn ERV:", r.get("vehicle", r))
                if r.get("type") == "v2x_vehicle_spawned":
                    spawned.append(r["vehicle"]["actor_id"])
        finally:
            await request(ws, {"type": "end_session"}, "session_ended")
            print("ego session ended (the fleet is server-owned and stays)")

        t0 = time.monotonic()
        min_dist = math.inf
        timeline = {}
        last_listing = None
        try:
            while time.monotonic() - t0 < args.duration:
                listing = await request(ws, {"type": "list_v2x_vehicles"}, "v2x_vehicles")
                last_listing = listing
                vs = listing.get("vehicles", [])
                positions = [v["pos"] for v in vs if v.get("pos")]
                for i in range(len(positions)):
                    for j in range(i + 1, len(positions)):
                        d = math.hypot(positions[i][0] - positions[j][0], positions[i][1] - positions[j][1])
                        min_dist = min(min_dist, d)
                line = " | ".join("%s %s %.0f km/h%s" % (v["name"], v["state"], (v.get("speed_mps") or 0) * 3.6,
                                                        " " + v["reason"] if v.get("reason") else "") for v in vs)
                print("[%5.1f s] %s" % (time.monotonic() - t0, line))
                for v in vs:
                    if (v.get("speed_mps") or 0) < 0.5 and v.get("debug"):
                        dbg = dict(v["debug"])
                        print("          %s pos=%s dbg=%s" % (v["name"], v.get("pos"), json.dumps(dbg, default=str)))
                for v in vs:
                    timeline.setdefault(v["name"], [])
                    if not timeline[v["name"]] or timeline[v["name"]][-1] != v["state"]:
                        timeline[v["name"]].append(v["state"])
                if vs and all(v["kind"] == "civilian" or v["state"] == "ARRIVED" for v in vs) and any(v["kind"] == "erv" for v in vs):
                    print("the ERV arrived")
                    break
                await asyncio.sleep(2.0)
        finally:
            if not args.keep:
                r = await request(ws, {"type": "despawn_v2x_vehicles"}, "v2x_vehicles_despawned")
                print("despawned:", r)

        print("\n== summary ==")
        for name, states in timeline.items():
            print("%-14s %s" % (name, " -> ".join(states)))
        print("closest two fleet vehicles: %.1f m" % min_dist if min_dist < math.inf else "no distance samples")
        print("events: %d" % len(EVENTS))
        for e in EVENTS[-20:]:
            print("  %s %s -> %s (%s)" % (e.get("name"), e.get("from"), e.get("to"), e.get("reason")))
        if last_listing:
            print("tracks:", last_listing.get("tracks"))
            print("stats:", last_listing.get("stats"))
        yielded = any(any(s in ("STOPPED_HOLD", "SLOW_IN_LANE", "MOVE_OVER", "PULL_OVER") for s in states)
                      for name, states in timeline.items() if name.startswith("drive_civ"))
        print("civilians yielded:", yielded)
        return 0 if yielded else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
