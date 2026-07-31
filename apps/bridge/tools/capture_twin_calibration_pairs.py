#!/usr/bin/env python3
"""Capture observational real/twin calibration source pairs for ch1-ch4."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

import websockets

PERCEPTION_TOOLS = Path(__file__).resolve().parents[2] / "perception" / "tools"
if str(PERCEPTION_TOOLS) not in sys.path:
    sys.path.insert(0, str(PERCEPTION_TOOLS))
from verify_live_feeds import JpegFrameParser  # noqa: E402

CAMERAS = ("ch1", "ch2", "ch3", "ch4")


class CaptureError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def normalized_base(value, schemes):
    parts = urlsplit(value)
    if (
        parts.scheme not in schemes
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise CaptureError("base URL is invalid or contains credentials/query data")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def fetch_json(url, timeout=15.0):
    with urlopen(Request(url, headers={
        "accept": "application/json",
        "cache-control": "no-cache",
        "user-agent": "v2x-calibration-capture/1",
    }), timeout=timeout) as response:
        return json.load(response)


def read_real_frame(url, timeout=15.0):
    parser = JpegFrameParser()
    total = 0
    with urlopen(Request(url, headers={
        "accept": "multipart/x-mixed-replace",
        "cache-control": "no-cache",
        "user-agent": "v2x-calibration-capture/1",
    }), timeout=timeout) as response:
        if response.headers.get_content_type() != "multipart/x-mixed-replace":
            raise CaptureError("physical stream is not MJPEG multipart")
        while total < 16 * 1024 * 1024:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            frames = parser.feed(chunk)
            if frames:
                return frames[-1]
    raise CaptureError("physical stream ended before one complete JPEG")


def validate_twin_metadata(metadata, camera_id, jpeg):
    """Validate that one server packet identifies and hashes the returned frame."""
    if metadata.get("camera_id") != camera_id:
        raise CaptureError(f"{camera_id} twin metadata identifies the wrong camera")
    if metadata.get("mode") != "live":
        raise CaptureError(f"{camera_id} twin is not in LIVE mode")
    for field in ("frame_count", "carla_frame"):
        value = metadata.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CaptureError(f"{camera_id} twin {field} is invalid")
    sensor_timestamp = metadata.get("sensor_timestamp")
    if (
        isinstance(sensor_timestamp, bool)
        or not isinstance(sensor_timestamp, (int, float))
        or not math.isfinite(float(sensor_timestamp))
        or float(sensor_timestamp) < 0.0
    ):
        raise CaptureError(f"{camera_id} twin sensor_timestamp is invalid")
    digest = hashlib.sha256(jpeg).hexdigest()
    if metadata.get("jpeg_sha256") != digest:
        raise CaptureError(f"{camera_id} twin JPEG hash mismatch")
    return digest


async def read_twin_frame(ws_base, camera_id, timeout=15.0):
    url = f"{ws_base}/twin?cam={camera_id}"
    hello = metadata = None
    async with websockets.connect(url, open_timeout=timeout, close_timeout=5) as socket:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise CaptureError(f"{camera_id} twin frame deadline expired")
            raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
            if isinstance(raw, bytes):
                if metadata is None:
                    continue
                validate_twin_metadata(metadata, camera_id, raw)
                return raw, hello, metadata
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if payload.get("type") == "twin_hello":
                hello = payload
            elif payload.get("type") == "twin_frame":
                metadata = payload


def camera_health(payload, camera_id):
    camera = (payload.get("cameras") or {}).get(camera_id)
    if not isinstance(camera, dict):
        raise CaptureError(f"{camera_id} health is missing")
    if (
        camera.get("fresh") is not True
        or camera.get("state") != "streaming"
        or camera.get("media_time_trusted") is not True
        or camera.get("media_clock_status") != "matched"
    ):
        raise CaptureError(f"{camera_id} physical stream is not trusted and fresh")
    return {
        key: camera.get(key)
        for key in (
            "source_updated_at",
            "frame_count",
            "decode_latency_ms",
            "media_clock_status",
            "media_time_trusted",
            "fresh",
            "state",
        )
    }


async def capture(args):
    real_base = normalized_base(args.real_base_url, {"http", "https"})
    ws_base = (
        None
        if args.real_only
        else normalized_base(args.ws_base_url, {"ws", "wss"})
    )
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    cameras_bytes = None
    cameras_sha256 = None
    camera_hashes = {}
    if not args.real_only:
        if not args.cameras_json:
            raise CaptureError("--cameras-json is required for twin capture")
        cameras_bytes = Path(args.cameras_json).read_bytes()
        cameras_sha256 = hashlib.sha256(cameras_bytes).hexdigest()
        try:
            cameras_payload = json.loads(cameras_bytes)
            cameras = cameras_payload["cameras"]
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaptureError("cameras JSON is invalid") from exc
        for camera_id in CAMERAS:
            try:
                camera = next(item for item in cameras if item.get("id") == camera_id)
            except StopIteration as exc:
                raise CaptureError(f"cameras JSON lacks {camera_id}") from exc
            canonical = json.dumps(
                camera, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
            camera_hashes[camera_id] = hashlib.sha256(canonical).hexdigest()
    manifest = {
        "schema": "v2x-observational-calibration-pairs/v1",
        "captured_at_utc": utc_now(),
        "real_base_origin": real_base,
        "twin_ws_origin": ws_base,
        "cameras_file_sha256": cameras_sha256,
        "mutation_log": [],
        "cameras": {},
    }
    for camera_id in CAMERAS:
        health_before = fetch_json(f"{real_base}/health")
        real = await asyncio.to_thread(
            read_real_frame, f"{real_base}/streams/{camera_id}.mjpg"
        )
        real_captured_at = utc_now()
        pair_started = time.monotonic()
        twin_result = (
            None
            if args.real_only
            else await read_twin_frame(ws_base, camera_id)
        )
        pair_gap_seconds = time.monotonic() - pair_started
        if twin_result is not None and pair_gap_seconds > args.maximum_pair_gap:
            raise CaptureError(
                f"{camera_id} real/twin capture gap {pair_gap_seconds:.3f}s exceeds "
                f"{args.maximum_pair_gap:.3f}s"
            )
        health_after = fetch_json(f"{real_base}/health")
        before = camera_health(health_before, camera_id)
        after = camera_health(health_after, camera_id)
        if int(after["frame_count"]) < int(before["frame_count"]):
            raise CaptureError(f"{camera_id} physical frame count regressed")
        real_path = output / f"{camera_id}-real.jpg"
        real_path.write_bytes(real)
        camera_manifest = {
            "real": {
                "file": real_path.name,
                "sha256": hashlib.sha256(real).hexdigest(),
                "captured_at_utc": real_captured_at,
                "twin_capture_gap_seconds": (
                    None if twin_result is None else pair_gap_seconds
                ),
                "trusted_health_before": before,
                "trusted_health_after": after,
            },
            "twin": None,
        }
        if twin_result is not None:
            twin, hello, twin_metadata = twin_result
            twin_path = output / f"{camera_id}-twin.jpg"
            twin_path.write_bytes(twin)
            camera_manifest["twin"] = {
                "file": twin_path.name,
                "sha256": hashlib.sha256(twin).hexdigest(),
                "frame_metadata": twin_metadata,
                "camera_model": (hello or {}).get("camera_model"),
                "camera_config_sha256": camera_hashes[camera_id],
            }
        manifest["cameras"][camera_id] = camera_manifest
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(manifest_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--ws-base-url", default="ws://127.0.0.1:8765")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--cameras-json",
        help="exact config used by the active twin render; required unless --real-only",
    )
    parser.add_argument(
        "--real-only",
        action="store_true",
        help="capture trusted physical frames without requiring twin protocol metadata",
    )
    parser.add_argument(
        "--maximum-pair-gap",
        type=float,
        default=15.0,
        help="maximum elapsed seconds from physical-frame completion to twin frame",
    )
    args = parser.parse_args()
    if (
        not math.isfinite(args.maximum_pair_gap)
        or not 0.0 < args.maximum_pair_gap <= 60.0
    ):
        parser.error("--maximum-pair-gap must be greater than 0 and at most 60")
    asyncio.run(capture(args))


if __name__ == "__main__":
    main()
