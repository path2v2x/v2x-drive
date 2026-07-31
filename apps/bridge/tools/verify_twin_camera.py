#!/usr/bin/env python3
"""
Twin camera alignment harness.

Renders each twin camera in CARLA and cross-checks the pose conversion
against the perception calibration ground truth:

1. Saves a JPEG render per channel (blend these 50/50 with a real frame
   grab to eyeball road alignment).
2. For every calibration point (u,v -> True_X, True_Z from
   apps/perception/calibration/chN_calibration_errors.csv), converts the
   true ground point through the SAME chain the twin uses
   (local XZ -> GPS -> CARLA world) and reprojects it through the twin
   camera. Reports the pixel delta vs. the original (u,v) — large,
   systematic deltas mean the heading->yaw convention is wrong.

Run on the Path PC (needs the carla package + a running simulator):

    python tools/verify_twin_camera.py --host 127.0.0.1 --port 2000 \
        --out /tmp/twin-verify

Safe to run alongside the bridge: it only spawns temporary cameras and
destroys them on exit. Avoid running during an active drive session.
"""

import argparse
import csv
import re
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from digital_twin_bridge.geo_utils import gps_to_carla
from v2x_common.geodesy import local_xz_to_geodetic
from digital_twin_bridge.twin_camera_rig import (
    configure_twin_camera_blueprint,
    compute_twin_camera_transform,
    load_cameras_config,
    twin_horizontal_fov_deg,
)

CALIBRATION_DIR = Path(__file__).resolve().parents[3] / "apps" / "perception" / "calibration"
ACCEPTANCE_WIDTH = 1280
ACCEPTANCE_HEIGHT = 960
ACCEPTANCE_MINIMUM_POINTS = 12
ACCEPTANCE_MINIMUM_HELDOUT = 4
ACCEPTANCE_MINIMUM_HORIZONTAL_SPAN = 0.5
ACCEPTANCE_MINIMUM_VERTICAL_SPAN = 0.3
ACCEPTANCE_MAXIMUM_RMSE_PX = 10.0
ACCEPTANCE_MAXIMUM_P95_PX = 16.0
ACCEPTANCE_MAXIMUM_ERROR_PX = 24.0
ACCEPTANCE_MAXIMUM_NEAR_OCCLUSION_FRACTION = 0.10


def xz_to_gps(
    x, z, origin_lat, origin_lon, heading_deg, map_georeference=None
):
    """Perception's local-XZ -> GPS conversion (mirror of xy_to_gps)."""
    projection = map_georeference or (
        f"+proj=tmerc +lat_0={float(origin_lat):.15g} "
        f"+lon_0={float(origin_lon):.15g} +k=1 +x_0=0 +y_0=0 "
        "+datum=WGS84 +units=m +no_defs"
    )
    return local_xz_to_geodetic(
        float(x),
        float(z),
        float(origin_lat),
        float(origin_lon),
        float(heading_deg),
        projection,
    )


def project_world_point(carla_transform, world_location, fov_deg, width, height):
    """Project a CARLA world point through a pinhole camera at `carla_transform`.

    Returns (u, v, depth) or None when the point is behind the camera.
    """
    import numpy as np

    # World -> camera (UE axes): rows of the inverse transform matrix
    inv = np.array(carla_transform.get_inverse_matrix())
    p_world = np.array([world_location.x, world_location.y, world_location.z, 1.0])
    p_cam_ue = inv @ p_world  # UE camera frame: x forward, y right, z up

    # UE -> standard camera frame: x right, y down, z forward
    x_cam, y_cam, z_cam = p_cam_ue[1], -p_cam_ue[2], p_cam_ue[0]
    if z_cam <= 0.1:
        return None

    focal = (width / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    u = width / 2.0 + focal * (x_cam / z_cam)
    v = height / 2.0 + focal * (y_cam / z_cam)
    return u, v, z_cam


def load_calibration_points(camera_id, calibration_dir=None):
    directory = Path(calibration_dir) if calibration_dir else CALIBRATION_DIR
    global_path = directory / f"{camera_id}_global_landmarks.csv"
    # Global candidate evidence is opt-in through --calibration-dir.  Merely
    # placing an exploratory CSV beside production calibration must never
    # silently change the acceptance dataset.
    path = (
        global_path
        if calibration_dir and global_path.exists()
        else directory / f"{camera_id}_calibration_errors.csv"
    )
    if not path.exists():
        return []
    points = []
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                point = {
                    "u": float(row["u_pixel"]),
                    "v": float(row["v_pixel"]),
                    "split": str(row.get("Split") or row.get("split") or "legacy").strip().lower(),
                    "landmark_id": str(row.get("Landmark_ID") or row.get("landmark_id") or "").strip(),
                    "source_frame_sha256": str(row.get("Source_Frame_SHA256") or "").strip(),
                    "provenance": str(row.get("Provenance") or "").strip().lower(),
                    "category": str(row.get("Category") or "").strip().lower(),
                }
                if row.get("True_X_m") and row.get("True_Z_m"):
                    point["x"] = float(row["True_X_m"])
                    point["z"] = float(row["True_Z_m"])
                if row.get("CARLA_X") and row.get("CARLA_Y") and row.get("CARLA_Z"):
                    point["carla_xyz"] = [
                        float(row["CARLA_X"]),
                        float(row["CARLA_Y"]),
                        float(row["CARLA_Z"]),
                    ]
                elif row.get("Latitude") and row.get("Longitude"):
                    point["gps"] = [float(row["Latitude"]), float(row["Longitude"])]
                points.append(point)
            except (KeyError, ValueError):
                continue
    return points


def calibration_world_location(carla_map, site, camera, point):
    """Resolve an independent global landmark, with legacy XZ as diagnostic only."""
    import carla

    if point.get("carla_xyz") is not None:
        x, y, z = point["carla_xyz"]
        return carla.Location(x=float(x), y=float(y), z=float(z))
    if point.get("gps") is not None:
        lat, lon = point["gps"]
        return gps_to_carla(carla_map, float(lat), float(lon))
    lat, lon = xz_to_gps(
        point["x"],
        point["z"],
        site["lat"],
        site["lon"],
        camera["heading_deg"],
        site.get("map_georeference"),
    )
    return gps_to_carla(carla_map, lat, lon)


def calibration_metrics(errors):
    finite = [float(error) for error in errors if math.isfinite(float(error))]
    if not finite:
        return None
    ordered = sorted(finite)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "count": len(ordered),
        "mean_px": sum(ordered) / len(ordered),
        "rmse_px": math.sqrt(sum(error * error for error in ordered) / len(ordered)),
        "p95_px": ordered[p95_index],
        "max_px": ordered[-1],
    }


def calibration_dataset_gate(points, width, height, *, minimum_total=12,
                             minimum_train=8, minimum_holdout=4,
                             minimum_horizontal_span=0.5,
                             minimum_vertical_span=0.3):
    """Reject sparse, circular, or geometrically degenerate fit inputs."""
    train = [point for point in points if point.get("split") == "train"]
    holdout = [point for point in points if point.get("split") == "holdout"]
    reasons = []
    if len(points) < minimum_total:
        reasons.append("insufficient_total_landmarks")
    if len(train) < minimum_train:
        reasons.append("insufficient_train_landmarks")
    if len(holdout) < minimum_holdout:
        reasons.append("insufficient_heldout_landmarks")
    if any(point.get("carla_xyz") is None and point.get("gps") is None for point in points):
        reasons.append("non_global_landmarks")
    approved_provenance = {"surveyed"}
    if any(point.get("provenance") not in approved_provenance for point in points):
        reasons.append("unverified_landmark_provenance")
    landmark_ids = [point.get("landmark_id") for point in points]
    if any(not landmark_id for landmark_id in landmark_ids) or len(set(landmark_ids)) != len(points):
        reasons.append("missing_or_duplicate_landmark_ids")
    frame_hashes = {point.get("source_frame_sha256") for point in points}
    if (
        len(frame_hashes) != 1
        or not all(re.fullmatch(r"[0-9a-f]{64}", value or "") for value in frame_hashes)
    ):
        reasons.append("invalid_source_frame_provenance")

    def span(rows, key, size):
        values = [point[key] for point in rows]
        return (max(values) - min(values)) / float(size) if values else 0.0

    train_h = span(train, "u", width)
    train_v = span(train, "v", height)
    holdout_h = span(holdout, "u", width)
    holdout_v = span(holdout, "v", height)
    if train_h < minimum_horizontal_span or train_v < minimum_vertical_span:
        reasons.append("train_image_coverage")
    if holdout_h < minimum_horizontal_span or holdout_v < minimum_vertical_span:
        reasons.append("heldout_image_coverage")

    ground = []
    for point in train:
        if point.get("carla_xyz") is not None:
            ground.append((point["carla_xyz"][0], point["carla_xyz"][1]))
        else:
            ground.append((point.get("x", 0.0), point.get("z", 0.0)))
    non_collinear = False
    for i in range(len(ground)):
        for j in range(i + 1, len(ground)):
            for k in range(j + 1, len(ground)):
                ax, az = ground[i]
                bx, bz = ground[j]
                cx, cz = ground[k]
                area2 = (bx - ax) * (cz - az) - (bz - az) * (cx - ax)
                if abs(area2) > 0.25:
                    non_collinear = True
                    break
            if non_collinear:
                break
        if non_collinear:
            break
    if not non_collinear:
        reasons.append("rank_deficient_ground_geometry")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "total": len(points),
        "train": len(train),
        "holdout": len(holdout),
        "train_horizontal_span": train_h,
        "train_vertical_span": train_v,
        "holdout_horizontal_span": holdout_h,
        "holdout_vertical_span": holdout_v,
    }


def heldout_calibration_gate(points, errors, width, height, *,
                             minimum_total_points=12,
                             minimum_heldout_points=4,
                             minimum_horizontal_span=0.5,
                             minimum_vertical_span=0.3,
                             maximum_rmse_px=ACCEPTANCE_MAXIMUM_RMSE_PX,
                             maximum_p95_px=ACCEPTANCE_MAXIMUM_P95_PX,
                             maximum_error_px=ACCEPTANCE_MAXIMUM_ERROR_PX):
    """Predeclared independent-landmark acceptance gate at render resolution."""
    if len(errors) != len(points):
        return {
            "passed": False,
            "reasons": ["error_cardinality_mismatch"],
            "total_landmarks": len(points),
            "heldout_landmarks": 0,
            "heldout_horizontal_span": 0.0,
            "heldout_vertical_span": 0.0,
            "metrics": None,
        }
    heldout = [
        (point, error)
        for point, error in zip(points, errors)
        if point.get("split") == "holdout"
    ]
    metrics = calibration_metrics(error for _point, error in heldout)
    horizontal_span = 0.0
    vertical_span = 0.0
    if heldout:
        horizontal = [point["u"] for point, _error in heldout]
        vertical = [point["v"] for point, _error in heldout]
        horizontal_span = (max(horizontal) - min(horizontal)) / float(width)
        vertical_span = (max(vertical) - min(vertical)) / float(height)
    reasons = []
    if len(points) < minimum_total_points:
        reasons.append("insufficient_total_landmarks")
    if len(heldout) < minimum_heldout_points:
        reasons.append("insufficient_heldout_landmarks")
    if horizontal_span < minimum_horizontal_span:
        reasons.append("heldout_horizontal_coverage")
    if vertical_span < minimum_vertical_span:
        reasons.append("heldout_vertical_coverage")
    if metrics is None:
        reasons.append("no_finite_heldout_errors")
    else:
        if metrics["count"] != len(heldout):
            reasons.append("nonfinite_heldout_error")
        if metrics["rmse_px"] > maximum_rmse_px:
            reasons.append("rmse")
        if metrics["p95_px"] > maximum_p95_px:
            reasons.append("p95")
        if metrics["max_px"] > maximum_error_px:
            reasons.append("maximum_error")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "total_landmarks": len(points),
        "heldout_landmarks": len(heldout),
        "heldout_horizontal_span": horizontal_span,
        "heldout_vertical_span": vertical_span,
        "metrics": metrics,
    }


def wait_for_frame(world, queue, timeout=5.0):
    """Wait for one frame; tick manually only if nothing else is ticking."""
    deadline = time.time() + timeout
    settings = world.get_settings()
    while time.time() < deadline:
        if not queue:
            if settings.synchronous_mode:
                try:
                    world.tick(2.0)
                except RuntimeError:
                    pass  # another process (the bridge) owns the tick
            time.sleep(0.1)
            continue
        return queue.pop()
    return None


def near_depth_fraction(raw_data, maximum_depth_m=3.0):
    """Fraction of pixels occupied by geometry implausibly near the lens."""
    if len(raw_data) % 4:
        raise ValueError("CARLA depth buffer must be BGRA")
    near = 0
    total = len(raw_data) // 4
    threshold = float(maximum_depth_m) / 1000.0
    for offset in range(0, len(raw_data), 4):
        blue, green, red = raw_data[offset], raw_data[offset + 1], raw_data[offset + 2]
        normalized = (red + green * 256.0 + blue * 65536.0) / 16777215.0
        if normalized < threshold:
            near += 1
    return near / total if total else 1.0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--out", default="/tmp/twin-verify")
    parser.add_argument("--width", type=int, default=ACCEPTANCE_WIDTH)
    parser.add_argument("--height", type=int, default=ACCEPTANCE_HEIGHT)
    parser.add_argument("--cameras-json", default=None)
    parser.add_argument("--calibration-dir", default=None)
    parser.add_argument("--camera", choices=("ch1", "ch2", "ch3", "ch4"))
    args = parser.parse_args()

    if (args.width, args.height) != (ACCEPTANCE_WIDTH, ACCEPTANCE_HEIGHT):
        print(
            "ERROR: acceptance evidence must be rendered at "
            f"{ACCEPTANCE_WIDTH}x{ACCEPTANCE_HEIGHT}",
            file=sys.stderr,
        )
        return 2

    import carla

    config = load_cameras_config(args.cameras_json)
    if config is None:
        print("ERROR: cameras config not found", file=sys.stderr)
        return 1

    os.makedirs(args.out, exist_ok=True)

    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    carla_map = world.get_map()
    print(f"Connected. Map: {carla_map.name}")

    site = config["site"]
    bp_lib = world.get_blueprint_library()

    exit_code = 0
    for camera in config["cameras"]:
        camera_id = camera["id"]
        if args.camera and camera_id != args.camera:
            continue
        fov = twin_horizontal_fov_deg(camera)
        transform = compute_twin_camera_transform(carla_map, site, camera)
        print(f"\n=== {camera_id} ===")
        print(
            f"pose: loc=({transform.location.x:.2f}, {transform.location.y:.2f}, "
            f"{transform.location.z:.2f}) yaw={transform.rotation.yaw:.2f} "
            f"pitch={transform.rotation.pitch:.2f} fov={fov:.2f}"
        )

        # 1. Render a frame
        camera_bp = bp_lib.find("sensor.camera.rgb")
        configure_twin_camera_blueprint(
            camera_bp, camera, args.width, args.height
        )
        frames = []
        actor = world.spawn_actor(camera_bp, transform)
        actor.listen(frames.append)
        image = wait_for_frame(world, frames)
        actor.stop()
        actor.destroy()
        if image is not None:
            out_path = os.path.join(args.out, f"twin_{camera_id}.jpg")
            from digital_twin_bridge.frame_encoder import encode_jpeg

            with open(out_path, "wb") as f:
                f.write(encode_jpeg(image, quality=90))
            print(f"render: {out_path}")
        else:
            print("render: NO FRAME (is the simulator ticking?)")
            exit_code = 1

        # A numerically good projection is still unusable if the fitted camera
        # sits inside a pole, signal head, tree, or building mesh.
        depth_bp = bp_lib.find("sensor.camera.depth")
        configure_twin_camera_blueprint(depth_bp, camera, args.width, args.height)
        depth_frames = []
        depth_actor = world.spawn_actor(depth_bp, transform)
        depth_actor.listen(depth_frames.append)
        depth_image = wait_for_frame(world, depth_frames)
        depth_actor.stop()
        depth_actor.destroy()
        if depth_image is None:
            print("occlusion gate: FAIL (no depth frame)")
            exit_code = 1
        else:
            near_fraction = near_depth_fraction(depth_image.raw_data)
            if near_fraction > ACCEPTANCE_MAXIMUM_NEAR_OCCLUSION_FRACTION:
                print(
                    "occlusion gate: FAIL "
                    f"near_geometry_fraction={near_fraction:.3f}"
                )
                exit_code = 1
            else:
                print(
                    "occlusion gate: PASS "
                    f"near_geometry_fraction={near_fraction:.3f}"
                )

        # 2. Reproject calibration ground truth through the twin camera
        points = load_calibration_points(camera_id, args.calibration_dir)
        if not points:
            print("calibration: FAIL (no CSV points found)")
            exit_code = 1
            continue

        dataset_gate = calibration_dataset_gate(
            points,
            camera["intrinsics"]["width"],
            camera["intrinsics"]["height"],
        )
        if not dataset_gate["passed"]:
            print(
                "calibration dataset: FAIL "
                f"({', '.join(dataset_gate['reasons'])})"
            )
            exit_code = 1

        # The calibration CSV pixels are in the REAL camera resolution.
        real_w = camera["intrinsics"]["width"]
        real_h = camera["intrinsics"]["height"]
        scale_u = args.width / real_w
        scale_v = args.height / real_h

        errors = []
        for point in points:
            world_loc = calibration_world_location(carla_map, site, camera, point)
            projected = project_world_point(transform, world_loc, fov, args.width, args.height)
            if projected is None:
                print(f"  point ({point['u']:.0f},{point['v']:.0f}): behind camera!")
                errors.append(float("inf"))
                continue
            pu, pv, depth = projected
            du = pu - point["u"] * scale_u
            dv = pv - point["v"] * scale_v
            err = math.hypot(du, dv)
            errors.append(err)
            print(
                f"  point ({point['u']:.0f},{point['v']:.0f}) -> "
                f"({pu:.0f},{pv:.0f}) delta=({du:+.0f},{dv:+.0f})px "
                f"err={err:.0f}px depth={depth:.1f}m"
            )

        diagnostic = calibration_metrics(errors)
        if diagnostic:
            print(
                "diagnostic all-point error: "
                f"mean={diagnostic['mean_px']:.0f}px "
                f"rmse={diagnostic['rmse_px']:.0f}px "
                f"p95={diagnostic['p95_px']:.0f}px "
                f"max={diagnostic['max_px']:.0f}px over {diagnostic['count']} points"
            )
        gate = heldout_calibration_gate(
            points,
            errors,
            real_w,
            real_h,
            minimum_total_points=ACCEPTANCE_MINIMUM_POINTS,
            minimum_heldout_points=ACCEPTANCE_MINIMUM_HELDOUT,
            minimum_horizontal_span=ACCEPTANCE_MINIMUM_HORIZONTAL_SPAN,
            minimum_vertical_span=ACCEPTANCE_MINIMUM_VERTICAL_SPAN,
            maximum_rmse_px=ACCEPTANCE_MAXIMUM_RMSE_PX,
            maximum_p95_px=ACCEPTANCE_MAXIMUM_P95_PX,
            maximum_error_px=ACCEPTANCE_MAXIMUM_ERROR_PX,
        )
        if gate["passed"]:
            metrics = gate["metrics"]
            print(
                "diagnostic held-out geometry check: PASS "
                f"rmse={metrics['rmse_px']:.0f}px "
                f"p95={metrics['p95_px']:.0f}px max={metrics['max_px']:.0f}px"
            )
            print(
                "production calibration gate: FAIL "
                "(legacy verifier does not bind measured intrinsics artifacts/source images)"
            )
            exit_code = 1
        else:
            print(
                "diagnostic held-out geometry check: FAIL "
                f"({', '.join(gate['reasons'])})"
            )
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
