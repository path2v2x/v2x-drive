"""Capture a per-stage latency baseline for the live perception pipeline.

Samples the perception /health endpoint and the detections read API for a
bounded window, then reports p50/p95/max for each measurable stage:

  decode   media_timestamp_utc -> decode_received_at (event to decoded frame;
           includes camera->KVS->HLS transport plus NVDEC decode)
  ingest   decode_received_at -> ingested_at (upload batching + API + DynamoDB)
  end_to_end  media_timestamp_utc -> ingested_at

The twin adds a further poll interval (<= 5 s) on top of end_to_end; that
constant is reported, not measured. Only schema-v2 records with
media_time_trusted are counted — untrusted clocks would corrupt the baseline.

Usage:
  latency_baseline.py [--duration 600] [--interval 10]
      [--health-url http://127.0.0.1:8090/health]
      [--api-base https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com]
      [--output baseline.json]

Historical mode computes the same baseline retroactively from persisted
detections via /detections/range (no live feeds or health required):
  latency_baseline.py --historical \
      --start 2026-07-13T17:00:00Z --end 2026-07-14T14:15:00Z
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TWIN_POLL_INTERVAL_SECONDS = 5.0


def parse_utc(ts: str) -> float:
    """Parse an ISO-8601 UTC timestamp to an epoch float."""
    return (
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
        .astimezone(timezone.utc)
        .timestamp()
    )


def percentile(values, fraction):
    """Nearest-rank percentile; values need not be sorted."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def record_lags(record):
    """Extract (decode_lag, ingest_lag, end_to_end) seconds from one
    detection record, or None if the record lacks a trusted schema-v2 clock.
    """
    if record.get("timestamp_schema_version") != 2:
        return None
    if record.get("media_time_trusted") is not True:
        return None
    try:
        event = parse_utc(record["media_timestamp_utc"])
        decoded = float(record["decode_received_at_epoch"])
        ingested = float(record["ingested_at_epoch"])
    except (KeyError, TypeError, ValueError):
        return None
    decode_lag = decoded - event
    ingest_lag = ingested - decoded
    if decode_lag < 0 or ingest_lag < -1:
        # A negative decode lag means clock disagreement; drop the record
        # rather than let it pull the percentiles down. Ingest gets 1 s of
        # slack because ingested_at is whole-second.
        return None
    return decode_lag, ingest_lag, ingested - event


def summarize(lag_rows, health_decode_ms, sample_count):
    """Build the baseline summary from per-record lag tuples and per-sample
    health decode_latency_ms values."""
    stages = {}
    for name, idx in (("decode", 0), ("ingest", 1), ("end_to_end", 2)):
        values = [row[idx] for row in lag_rows]
        stages[name] = {
            "count": len(values),
            "p50_seconds": percentile(values, 0.50),
            "p95_seconds": percentile(values, 0.95),
            "max_seconds": max(values) if values else None,
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "samples": sample_count,
        "records_used": len(lag_rows),
        "stages": stages,
        "health_decode_latency_ms": {
            "count": len(health_decode_ms),
            "p50": percentile(health_decode_ms, 0.50),
            "p95": percentile(health_decode_ms, 0.95),
            "max": max(health_decode_ms) if health_decode_ms else None,
        },
        "twin_poll_interval_seconds": TWIN_POLL_INTERVAL_SECONDS,
        "note": (
            "twin visibility adds up to twin_poll_interval_seconds on top of "
            "end_to_end"
        ),
    }


def fetch_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def iter_range_pages(api_base, start, end, page_limit=100, max_pages=500,
                     fetch=fetch_json):
    """Yield item pages from /detections/range, following the `next`
    cursor until exhaustion or max_pages."""
    next_token = None
    for _ in range(max_pages):
        url = (
            f"{api_base}/detections/range?start={urllib.parse.quote(start)}"
            f"&end={urllib.parse.quote(end)}&limit={page_limit}"
        )
        if next_token:
            url += f"&next={urllib.parse.quote(next_token)}"
        data = fetch(url)
        yield data.get("items", [])
        next_token = data.get("next")
        if not next_token:
            return
    print(f"stopped at max_pages={max_pages}; results truncated", file=sys.stderr)


def run_historical(args):
    seen_events = set()
    lag_rows = []
    pages = 0
    for items in iter_range_pages(
        args.api_base, args.start, args.end,
        page_limit=args.page_limit, max_pages=args.max_pages,
    ):
        pages += 1
        for record in items:
            event_id = record.get("event_id")
            if not event_id or event_id in seen_events:
                continue
            seen_events.add(event_id)
            lags = record_lags(record)
            if lags is not None:
                lag_rows.append(lags)
    summary = summarize(lag_rows, [], pages)
    summary["mode"] = "historical"
    summary["window"] = {"start": args.start, "end": args.end}
    return summary, lag_rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument(
        "--health-url", default="http://127.0.0.1:8090/health"
    )
    parser.add_argument(
        "--api-base",
        default="https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com",
    )
    parser.add_argument("--output", default=None)
    parser.add_argument("--historical", action="store_true")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=500)
    args = parser.parse_args()

    if args.historical:
        if not args.start or not args.end:
            parser.error("--historical requires --start and --end")
        summary, lag_rows = run_historical(args)
        output = json.dumps(summary, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(output + "\n")
        print(output)
        if not lag_rows:
            print("no usable detection records in window", file=sys.stderr)
            return 1
        return 0

    seen_events = set()
    lag_rows = []
    health_decode_ms = []
    samples = 0
    deadline = time.monotonic() + args.duration

    while time.monotonic() < deadline:
        samples += 1
        try:
            health = fetch_json(args.health_url, timeout=5)
            for camera in health.get("cameras", {}).values():
                latency = camera.get("decode_latency_ms")
                if isinstance(latency, (int, float)) and camera.get("fresh"):
                    health_decode_ms.append(float(latency))
        except Exception as exc:  # noqa: BLE001 - sampling must survive blips
            print(f"health sample failed: {type(exc).__name__}", file=sys.stderr)

        try:
            data = fetch_json(
                f"{args.api_base}/detections/recent?limit=50", timeout=10
            )
            for record in data.get("items", []):
                event_id = record.get("event_id")
                if not event_id or event_id in seen_events:
                    continue
                seen_events.add(event_id)
                lags = record_lags(record)
                if lags is not None:
                    lag_rows.append(lags)
        except Exception as exc:  # noqa: BLE001
            print(
                f"detections sample failed: {type(exc).__name__}",
                file=sys.stderr,
            )

        time.sleep(args.interval)

    summary = summarize(lag_rows, health_decode_ms, samples)
    output = json.dumps(summary, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output + "\n")
    print(output)
    if not lag_rows:
        print("no usable detection records captured", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
