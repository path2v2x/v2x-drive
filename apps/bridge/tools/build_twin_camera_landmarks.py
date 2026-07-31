#!/usr/bin/env python3
"""Resolve matched real/twin pixels into independent CARLA world landmarks.

Input CSV columns are
``Landmark_ID,Twin_U,Twin_V,u_pixel,v_pixel,Split,Provenance,Category``.
Twin coordinates are at the acceptance render size (1280x960); real pixels are
at native camera resolution.  A temporary UE5 depth camera at the exact shared
rig pose resolves each selected rendered landmark directly into CARLA XYZ,
avoiding the circular legacy local-XZ/heading conversion.
"""

import argparse
import csv
import hashlib
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digital_twin_bridge.twin_camera_rig import (
    compute_twin_camera_transform,
    configure_twin_camera_blueprint,
    load_cameras_config,
)


def encoded_depth_meters(raw_data, width, u, v):
    """Decode CARLA's 24-bit logarithm-free depth value at one pixel."""
    u = int(round(float(u)))
    v = int(round(float(v)))
    if not (0 <= u < width and v >= 0):
        raise ValueError("depth pixel outside image")
    offset = (v * width + u) * 4
    if offset + 3 >= len(raw_data):
        raise ValueError("depth pixel outside image")
    blue, green, red = raw_data[offset], raw_data[offset + 1], raw_data[offset + 2]
    normalized = (red + green * 256.0 + blue * 65536.0) / 16777215.0
    return 1000.0 * normalized


def depth_pixel_to_world(transform, u, v, depth_m, fov_deg, width, height):
    """Back-project one CARLA depth pixel into a world-space Location."""
    import carla

    focal = (width / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    local = carla.Location(
        x=float(depth_m),
        y=(float(u) - width / 2.0) * float(depth_m) / focal,
        z=-(float(v) - height / 2.0) * float(depth_m) / focal,
    )
    return transform.transform(local)


def wait_for_frame(world, frames, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if frames:
            return frames[-1]
        if world.get_settings().synchronous_mode:
            try:
                world.tick(2.0)
            except RuntimeError:
                pass
        time.sleep(0.05)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("annotations", help="matched-pixel input CSV")
    parser.add_argument("output", help="global-landmark output CSV")
    parser.add_argument("--camera", required=True, choices=("ch1", "ch2", "ch3", "ch4"))
    parser.add_argument("--real-frame", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=960)
    parser.add_argument("--cameras-json", default=None)
    args = parser.parse_args()

    with open(args.annotations, newline="") as handle:
        annotations = list(csv.DictReader(handle))
    if len(annotations) < 12:
        raise SystemExit("refusing fewer than 12 matched landmarks")
    if sum(row.get("Split", "").strip().lower() == "holdout" for row in annotations) < 4:
        raise SystemExit("refusing fewer than four frozen holdout landmarks")
    approved = {"surveyed", "manual_verified_static"}
    if any(row.get("Provenance", "").strip().lower() not in approved for row in annotations):
        raise SystemExit("refusing annotations without approved manual/survey provenance")

    source_bytes = Path(args.real_frame).read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    import carla

    config = load_cameras_config(args.cameras_json)
    camera = next(item for item in config["cameras"] if item["id"] == args.camera)
    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    transform = compute_twin_camera_transform(world.get_map(), config["site"], camera)
    fov = math.degrees(
        2.0 * math.atan((camera["intrinsics"]["width"] / 2.0) / camera["intrinsics"]["fx"])
    ) + float((camera.get("twin_pose") or {}).get("fov_offset_deg", 0.0))

    blueprint = world.get_blueprint_library().find("sensor.camera.depth")
    configure_twin_camera_blueprint(blueprint, camera, args.width, args.height)
    frames = []
    actor = world.spawn_actor(blueprint, transform)
    try:
        actor.listen(frames.append)
        image = wait_for_frame(world, frames)
        if image is None:
            raise RuntimeError("no depth frame received")
        rows = []
        for annotation in annotations:
            twin_u = float(annotation["Twin_U"])
            twin_v = float(annotation["Twin_V"])
            depth = encoded_depth_meters(image.raw_data, image.width, twin_u, twin_v)
            if not (0.25 <= depth <= 250.0):
                raise RuntimeError(
                    f"{annotation['Landmark_ID']}: implausible depth {depth:.3f}m"
                )
            location = depth_pixel_to_world(
                transform, twin_u, twin_v, depth, fov, image.width, image.height
            )
            rows.append({
                "Landmark_ID": annotation["Landmark_ID"],
                "u_pixel": annotation["u_pixel"],
                "v_pixel": annotation["v_pixel"],
                "Split": annotation["Split"].strip().lower(),
                "Provenance": annotation["Provenance"].strip().lower(),
                "Category": annotation.get("Category", "").strip().lower(),
                "CARLA_X": f"{location.x:.6f}",
                "CARLA_Y": f"{location.y:.6f}",
                "CARLA_Z": f"{location.z:.6f}",
                "Twin_U": f"{twin_u:.3f}",
                "Twin_V": f"{twin_v:.3f}",
                "Depth_M": f"{depth:.6f}",
                "Source_Frame_SHA256": source_sha256,
            })
    finally:
        try:
            actor.stop()
        finally:
            actor.destroy()

    fields = list(rows[0])
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} globally anchored landmarks to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
