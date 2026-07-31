#!/usr/bin/env python3
"""Collect a diagnostic world-space road-line cloud from temporary UE5 sensors.

The output is deliberately not acceptance evidence.  It is a reproducible,
hash-bound proposal source for visual self-calibration: semantic road-line
pixels are paired with depth at the exact active twin pose and back-projected
into CARLA world coordinates.  Both temporary sensors are destroyed in a
``finally`` block and this tool never writes camera configuration.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import numpy as np
from PIL import Image
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digital_twin_bridge.twin_camera_rig import (  # noqa: E402
    compute_twin_camera_transform,
    configure_twin_camera_blueprint,
    load_cameras_config,
)


ROAD_LINE_TAG = 6


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


async def query_server_status(url):
    async with websockets.connect(url, open_timeout=5.0, close_timeout=2.0) as socket:
        await socket.send(json.dumps({"type": "server_status"}))
        while True:
            message = await asyncio.wait_for(socket.recv(), timeout=5.0)
            if isinstance(message, str):
                payload = json.loads(message)
                if payload.get("type") == "server_status":
                    return payload


def verify_zero_active_sessions(url):
    status = asyncio.run(query_server_status(url))
    if status.get("active_sessions") != 0:
        raise RuntimeError(
            f"refusing temporary sensors with active_sessions={status.get('active_sessions')}"
        )
    return status


def wait_for_matched_images(depth_frames, semantic_frames, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if depth_frames and semantic_frames:
            depth_by_frame = {int(image.frame): image for image in depth_frames[-8:]}
            semantic_by_frame = {int(image.frame): image for image in semantic_frames[-8:]}
            common = set(depth_by_frame) & set(semantic_by_frame)
            if common:
                frame = max(common)
                return depth_by_frame[frame], semantic_by_frame[frame]
        time.sleep(0.05)
    raise RuntimeError("temporary cameras produced no frame-matched depth/semantic pair")


def decode_depth(raw_data, width, height):
    bgra = np.frombuffer(raw_data, dtype=np.uint8).reshape(height, width, 4)
    normalized = (
        bgra[:, :, 2].astype(np.float64)
        + 256.0 * bgra[:, :, 1].astype(np.float64)
        + 65536.0 * bgra[:, :, 0].astype(np.float64)
    ) / 16777215.0
    return normalized * 1000.0


def semantic_tags(raw_data, width, height):
    return np.frombuffer(raw_data, dtype=np.uint8).reshape(height, width, 4)[:, :, 2]


def rgb_road_marking_mask(path, width, height):
    """Conservative white/yellow proposal mask for custom maps with bad tags."""
    image = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
    if image.shape[:2] != (height, width):
        raise RuntimeError(
            f"twin frame is {image.shape[1]}x{image.shape[0]}, expected {width}x{height}"
        )
    red, green, blue = (image[:, :, index] for index in range(3))
    white = (
        (red >= 205)
        & (green >= 205)
        & (blue >= 205)
        & ((image.max(axis=2) - image.min(axis=2)) <= 35)
    )
    yellow = (
        (red >= 115)
        & (green >= 90)
        & (blue <= 125)
        & (red.astype(np.int16) - blue.astype(np.int16) >= 35)
        & (green.astype(np.int16) - blue.astype(np.int16) >= 20)
    )
    return white | yellow


def backproject(transform, u, v, depth, fov_deg, width, height):
    focal = (width / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    local = np.column_stack((
        depth,
        (u - width / 2.0) * depth / focal,
        -(v - height / 2.0) * depth / focal,
        np.ones_like(depth),
    ))
    matrix = np.asarray(transform.get_matrix(), dtype=np.float64)
    return (matrix @ local.T).T[:, :3]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", required=True, choices=("ch1", "ch2", "ch3", "ch4"))
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--pair-manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=960)
    parser.add_argument("--stride", type=int, default=2)
    parser.add_argument("--maximum-depth-m", type=float, default=150.0)
    parser.add_argument("--drive-ws-url", default="ws://127.0.0.1:8765")
    parser.add_argument(
        "--authorized-zero-session",
        action="store_true",
        help="required acknowledgement for temporary CARLA sensor mutation",
    )
    args = parser.parse_args()

    if not args.authorized_zero_session:
        parser.error("--authorized-zero-session is required")
    if not 1 <= args.stride <= 16:
        parser.error("--stride must be in [1, 16]")
    output = Path(args.output).resolve()
    if output.exists():
        raise SystemExit("refusing to overwrite existing road-line cloud")
    pair_path = Path(args.pair_manifest).resolve()
    pair_bytes = pair_path.read_bytes()
    pair_manifest = json.loads(pair_bytes)
    if pair_manifest.get("schema") != "v2x-observational-calibration-pairs/v1":
        raise SystemExit("pair manifest has an unsupported schema")
    twin = pair_manifest.get("cameras", {}).get(args.camera, {}).get("twin", {})
    if not twin.get("sha256") or not twin.get("camera_config_sha256"):
        raise SystemExit("pair manifest does not bind the selected twin frame/config")
    twin_frame_path = pair_path.parent / twin.get("file", "")
    twin_frame_bytes = twin_frame_path.read_bytes()
    if hashlib.sha256(twin_frame_bytes).hexdigest() != twin["sha256"]:
        raise SystemExit("retained twin frame hash does not match pair manifest")

    import carla

    config_path = Path(args.cameras_json).resolve()
    config_bytes = config_path.read_bytes()
    if hashlib.sha256(config_bytes).hexdigest() != pair_manifest.get("cameras_file_sha256"):
        raise SystemExit("cameras JSON file hash does not match the pair manifest")
    config = load_cameras_config(str(config_path))
    canonical_config = json.dumps(
        config, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    active_config_hash = (twin.get("camera_model") or {}).get(
        "cameras_config_sha256"
    )
    if hashlib.sha256(canonical_config).hexdigest() != active_config_hash:
        raise SystemExit("cameras JSON does not match the active twin frame metadata")
    camera = next(item for item in config["cameras"] if item["id"] == args.camera)
    canonical_camera = json.dumps(
        camera, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    if hashlib.sha256(canonical_camera).hexdigest() != twin["camera_config_sha256"]:
        raise SystemExit("selected camera config does not match active twin metadata")
    verify_zero_active_sessions(args.drive_ws_url)
    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    transform = compute_twin_camera_transform(world.get_map(), config["site"], camera)
    fov = math.degrees(2.0 * math.atan(
        (camera["intrinsics"]["width"] / 2.0) / camera["intrinsics"]["fx"]
    )) + float((camera.get("twin_pose") or {}).get("fov_offset_deg", 0.0))

    library = world.get_blueprint_library()
    depth_bp = library.find("sensor.camera.depth")
    semantic_bp = library.find("sensor.camera.semantic_segmentation")
    configure_twin_camera_blueprint(depth_bp, camera, args.width, args.height)
    configure_twin_camera_blueprint(semantic_bp, camera, args.width, args.height)
    depth_frames, semantic_frames = [], []
    actors = []
    cleanup_errors = []
    operation_error = None
    try:
        depth_actor = world.spawn_actor(depth_bp, transform)
        actors.append(depth_actor)
        semantic_actor = world.spawn_actor(semantic_bp, transform)
        actors.append(semantic_actor)
        depth_actor.listen(depth_frames.append)
        semantic_actor.listen(semantic_frames.append)
        depth_image, semantic_image = wait_for_matched_images(
            depth_frames, semantic_frames
        )

        tags = semantic_tags(semantic_image.raw_data, args.width, args.height)
        depth = decode_depth(depth_image.raw_data, args.width, args.height)
        yy, xx = np.mgrid[0:args.height:args.stride, 0:args.width:args.stride]
        sampled_tags = tags[::args.stride, ::args.stride]
        sampled_depth = depth[::args.stride, ::args.stride]
        padded_depth = np.pad(depth, 1, mode="edge")
        windows = np.lib.stride_tricks.sliding_window_view(
            padded_depth, (3, 3)
        )
        depth_range = windows.max(axis=(-2, -1)) - windows.min(axis=(-2, -1))
        sampled_depth_range = depth_range[::args.stride, ::args.stride]
        semantic_keep = (
            (sampled_tags == ROAD_LINE_TAG)
            & np.isfinite(sampled_depth)
            & (sampled_depth >= 0.25)
            & (sampled_depth <= args.maximum_depth_m)
            & (sampled_depth_range <= 0.5)
        )
        proposal_source = "carla_semantic_road_line"
        if int(np.count_nonzero(semantic_keep)) < 100:
            # The custom Richmond assets currently emit tag 11 for the entire
            # frame.  Preserve that failure as evidence and fall back to a
            # conservative, hash-bound RGB marking proposal without pretending
            # it is semantic or acceptance-grade ground truth.
            rgb_mask = rgb_road_marking_mask(
                twin_frame_path, args.width, args.height
            )[::args.stride, ::args.stride]
            keep = (
                rgb_mask
                & np.isfinite(sampled_depth)
                & (sampled_depth >= 0.25)
                & (sampled_depth <= args.maximum_depth_m)
                & (sampled_depth_range <= 0.5)
            )
            proposal_source = "rgb_threshold_custom_map_semantic_fallback"
        else:
            keep = semantic_keep
        u = xx[keep].astype(np.float64)
        v = yy[keep].astype(np.float64)
        depth_m = sampled_depth[keep].astype(np.float64)
        if len(u) < 100:
            unique, counts = np.unique(tags, return_counts=True)
            histogram = dict(zip((int(x) for x in unique), (int(x) for x in counts)))
            raise RuntimeError(
                f"only {len(u)} road-line pixels; semantic histogram={histogram}"
            )
        world_xyz = backproject(
            transform, u, v, depth_m, fov, args.width, args.height
        )
        grid_keep = (
            np.isfinite(sampled_depth)
            & (sampled_depth >= 0.25)
            & (sampled_depth <= args.maximum_depth_m)
            & (sampled_depth_range <= 0.5)
        )
        grid_u = xx[grid_keep].astype(np.float64)
        grid_v = yy[grid_keep].astype(np.float64)
        grid_depth_m = sampled_depth[grid_keep].astype(np.float64)
        grid_world_xyz = backproject(
            transform,
            grid_u,
            grid_v,
            grid_depth_m,
            fov,
            args.width,
            args.height,
        )
    except BaseException as exc:
        operation_error = exc
    finally:
        for actor in reversed(actors):
            try:
                actor.stop()
            except Exception:
                cleanup_errors.append(f"stop:{actor.id}")
            try:
                destroyed = actor.destroy()
                if destroyed is False or actor.is_alive:
                    cleanup_errors.append(f"destroy:{actor.id}")
            except Exception:
                cleanup_errors.append(f"destroy:{actor.id}")
    if cleanup_errors:
        raise RuntimeError(
            f"temporary sensor cleanup failed: {cleanup_errors}"
        ) from operation_error
    if operation_error is not None:
        raise operation_error
    verify_zero_active_sessions(args.drive_ws_url)

    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema": "v2x-diagnostic-roadline-cloud/v1",
        "acceptance_eligible": False,
        "camera": args.camera,
        "created_at_utc": utc_now(),
        "pair_manifest": str(pair_path),
        "pair_manifest_sha256": hashlib.sha256(pair_bytes).hexdigest(),
        "twin_frame_sha256": twin["sha256"],
        "camera_config_sha256": twin["camera_config_sha256"],
        "cameras_json_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "carla_map": world.get_map().name,
        "image_width": args.width,
        "image_height": args.height,
        "horizontal_fov_deg": fov,
        "stride": args.stride,
        "road_line_tag": ROAD_LINE_TAG,
        "proposal_source": proposal_source,
        "semantic_road_line_pixel_count": int(np.count_nonzero(tags == ROAD_LINE_TAG)),
        "semantic_unique_tags": [int(value) for value in np.unique(tags)],
        "point_count": int(len(world_xyz)),
        "depth_grid_point_count": int(len(grid_world_xyz)),
        "matched_carla_frame": int(depth_image.frame),
        "depth_discontinuity_limit_m": 0.5,
        "baseline_transform": {
            "location": [transform.location.x, transform.location.y, transform.location.z],
            "rotation": [
                transform.rotation.pitch,
                transform.rotation.yaw,
                transform.rotation.roll,
            ],
        },
        "warning": "proposal-only runtime semantic/depth evidence; not independent acceptance truth",
    }
    np.savez_compressed(
        output,
        world_xyz=world_xyz,
        twin_uv=np.column_stack((u, v)),
        depth_m=depth_m,
        grid_world_xyz=grid_world_xyz,
        grid_uv=np.column_stack((grid_u, grid_v)),
        grid_depth_m=grid_depth_m,
        metadata=np.asarray(json.dumps(metadata, sort_keys=True)),
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
