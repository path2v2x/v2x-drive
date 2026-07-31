#!/usr/bin/env python3
"""
Fit per-channel twin_pose overrides against the calibration ground truth.

The perception model's calibrated pitch/yaw fit ITS pinhole model to the
real (distorted) camera, so residual pose error remains when mirroring the
pose into CARLA. This tool optimises small offsets (yaw, pitch, height)
per channel by minimising the reprojection error of the surveyed
calibration points through the twin camera, using the live map for
terrain heights. Prints a cameras.json `twin_pose` block per channel.

Run on the Path PC:
    ~/V2XCarla/carla-venv-310/bin/python tools/fit_twin_camera_poses.py
"""

import argparse
from copy import deepcopy
import hashlib
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digital_twin_bridge.geo_utils import gps_to_carla
from digital_twin_bridge.twin_camera_rig import (
    camera_with_twin_pose,
    compute_twin_camera_transform,
    load_cameras_config,
    twin_horizontal_fov_deg,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_twin_camera import (
    calibration_dataset_gate,
    calibration_world_location,
    load_calibration_points,
    project_world_point,
    xz_to_gps,
)


def nelder_mead(f, x0, step=1.0, max_iter=300, tol=1e-3, bounds=None):
    """Minimal Nelder-Mead for a handful of parameters (no scipy needed)."""
    n = len(x0)
    simplex = [list(x0)]
    for i in range(n):
        p = list(x0)
        p[i] += step if isinstance(step, (int, float)) else step[i]
        simplex.append(p)
    def bounded(point):
        if bounds is None:
            return point
        return [max(low, min(high, value)) for value, (low, high) in zip(point, bounds)]

    simplex = [bounded(point) for point in simplex]
    scores = [f(p) for p in simplex]

    for _ in range(max_iter):
        order = sorted(range(n + 1), key=lambda i: scores[i])
        simplex = [simplex[i] for i in order]
        scores = [scores[i] for i in order]
        if abs(scores[-1] - scores[0]) < tol:
            break
        centroid = [sum(p[i] for p in simplex[:-1]) / n for i in range(n)]
        worst = simplex[-1]
        reflect = bounded([centroid[i] + (centroid[i] - worst[i]) for i in range(n)])
        r_score = f(reflect)
        if r_score < scores[0]:
            expand = bounded([centroid[i] + 2 * (centroid[i] - worst[i]) for i in range(n)])
            e_score = f(expand)
            if e_score < r_score:
                simplex[-1], scores[-1] = expand, e_score
            else:
                simplex[-1], scores[-1] = reflect, r_score
        elif r_score < scores[-2]:
            simplex[-1], scores[-1] = reflect, r_score
        else:
            contract = bounded([centroid[i] + 0.5 * (worst[i] - centroid[i]) for i in range(n)])
            c_score = f(contract)
            if c_score < scores[-1]:
                simplex[-1], scores[-1] = contract, c_score
            else:
                best = simplex[0]
                for i in range(1, n + 1):
                    simplex[i] = bounded([best[j] + 0.5 * (simplex[i][j] - best[j]) for j in range(n)])
                    scores[i] = f(simplex[i])
    order = sorted(range(n + 1), key=lambda i: scores[i])
    return simplex[order[0]], scores[order[0]]


POSE_PARAMETER_NAMES = (
    "yaw_offset_deg", "pitch_offset_deg", "height_offset_m",
    "roll_offset_deg", "forward_offset_m", "right_offset_m",
    "fov_offset_deg",
)


def absolute_pose_bounds(optimize_translation=False):
    """Return zero-referenced bounds that cannot drift across repeated runs."""
    translation = (-3.0, 3.0) if optimize_translation else (0.0, 0.0)
    return [
        (-15.0, 15.0),
        (-15.0, 15.0),
        translation,
        (-8.0, 8.0),
        translation,
        translation,
        (-20.0, 20.0),
    ]


def bounded_initial_pose(camera, bounds, start_from_config=False):
    pose = camera.get("twin_pose") or {}
    raw = [
        float(pose.get(name, 0.0)) if start_from_config else 0.0
        for name in POSE_PARAMETER_NAMES
    ]
    return [max(low, min(high, value)) for value, (low, high) in zip(raw, bounds)]


def candidate_config_is_eligible(report):
    cameras = report.get("cameras") or {}
    return bool(cameras) and all(
        value.get("dataset_gate", {}).get("passed") is True
        and value.get("heldout_gate", {}).get("passed") is True
        and value.get("acceptance_eligible") is True
        for value in cameras.values()
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=960)
    parser.add_argument("--cameras-json", default=None)
    parser.add_argument("--calibration-dir", default=None)
    parser.add_argument("--camera", choices=("ch1", "ch2", "ch3", "ch4"))
    parser.add_argument("--output", default=None, help="write detailed JSON fit report")
    parser.add_argument(
        "--candidate-config",
        default=None,
        help="write a complete non-deployed cameras.json candidate",
    )
    parser.add_argument(
        "--allow-diagnostic-legacy",
        action="store_true",
        help="print an explicitly non-deployable fit for sparse legacy rows",
    )
    parser.add_argument(
        "--optimize-translation",
        action="store_true",
        help="allow height/forward/right search only with independently surveyed position data",
    )
    parser.add_argument(
        "--start-from-config",
        action="store_true",
        help=(
            "seed from the configured pose, clamped to absolute zero-referenced "
            "bounds; the default seed is the physical zero-offset model"
        ),
    )
    args = parser.parse_args()

    import carla

    config = load_cameras_config(args.cameras_json)
    if config is None:
        print("ERROR: cameras config not found", file=sys.stderr)
        return 1

    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    carla_map = client.get_world().get_map()
    print(f"Connected. Map: {carla_map.name}")

    site = config["site"]
    suggestions = {}
    config_path = Path(args.cameras_json).resolve() if args.cameras_json else None
    report = {
        "schema": "v2x-diagnostic-legacy-twin-pose-fit/v1",
        "acceptance_eligible": False,
        "warning": "legacy local-XZ evidence cannot authorize deployment",
        "map": carla_map.name,
        "cameras_json_sha256": (
            hashlib.sha256(config_path.read_bytes()).hexdigest()
            if config_path is not None else None
        ),
        "cameras": {},
    }
    for camera in config["cameras"]:
        camera_id = camera["id"]
        if args.camera and camera_id != args.camera:
            continue
        loaded_points = load_calibration_points(camera_id, args.calibration_dir)
        dataset_gate = calibration_dataset_gate(
            loaded_points,
            camera["intrinsics"]["width"],
            camera["intrinsics"]["height"],
        )
        if not dataset_gate["passed"] and not args.allow_diagnostic_legacy:
            print(
                f"{camera_id}: refusing fit: "
                f"{', '.join(dataset_gate['reasons'])}"
            )
            continue
        points = [
            point for point in loaded_points if point.get("split") != "holdout"
        ]
        if not any(point.get("split") == "train" for point in loaded_points):
            print(
                f"{camera_id}: no explicit train/holdout split; fitting legacy "
                "points for diagnostics only"
            )
        if len(points) < 3:
            print(f"{camera_id}: only {len(points)} calibration points, skipping")
            continue

        real_w, real_h = camera["intrinsics"]["width"], camera["intrinsics"]["height"]
        scale_u, scale_v = args.width / real_w, args.height / real_h

        # Resolve each true point's CARLA location once (z from the map).
        world_points = []
        for point in points:
            world_points.append((
                calibration_world_location(carla_map, site, camera, point),
                point,
            ))

        names = POSE_PARAMETER_NAMES
        bounds = absolute_pose_bounds(args.optimize_translation)
        x0 = bounded_initial_pose(camera, bounds, args.start_from_config)

        def objective(params, _camera=camera, _world_points=world_points,
                      _scale=(scale_u, scale_v)):
            candidate = camera_with_twin_pose(_camera, dict(zip(names, params)))
            transform = compute_twin_camera_transform(carla_map, site, candidate)
            fov = twin_horizontal_fov_deg(candidate)
            total = 0.0
            for world_loc, point in _world_points:
                projected = project_world_point(transform, world_loc, fov, args.width, args.height)
                if projected is None:
                    total += 5000.0
                    continue
                pu, pv, _depth = projected
                residual = math.hypot(
                    pu - point["u"] * _scale[0], pv - point["v"] * _scale[1]
                )
                # Huber loss limits one bad landmark without hiding it from the
                # untouched held-out acceptance metrics.
                delta = 75.0
                total += residual if residual <= delta else delta + 0.25 * (residual - delta)
            return total / len(_world_points)

        # Deterministic bounded coarse search over dominant orientation/FOV
        # axes, then refine the full seven-dimensional pose from the top seeds.
        seeds = []
        for yaw_delta in (-5.0, -2.5, 0.0, 2.5, 5.0):
            for pitch_delta in (-5.0, -2.5, 0.0, 2.5, 5.0):
                for fov_delta in (-4.0, -2.0, 0.0, 2.0, 4.0):
                    seed = list(x0)
                    seed[0] = x0[0] + yaw_delta
                    seed[1] = x0[1] + pitch_delta
                    seed[6] = x0[6] + fov_delta
                    seeds.append((objective(seed), seed))
        seeds.sort(key=lambda item: item[0])
        before = objective(x0)
        candidates = []
        for _seed_score, seed in seeds[:8]:
            candidates.append(nelder_mead(
                objective,
                seed,
                step=[0.5, 0.5, 0.15, 0.25, 0.2, 0.2, 0.25],
                bounds=bounds,
                max_iter=800,
            ))
        best, score = min(candidates, key=lambda item: item[1])
        fitted = dict(zip(names, best))
        print(
            f"{camera_id}: err {before:.0f}px -> {score:.0f}px "
            f"(yaw {fitted['yaw_offset_deg']:+.2f} deg, "
            f"pitch {fitted['pitch_offset_deg']:+.2f} deg, "
            f"dz {fitted['height_offset_m']:+.2f} m)"
        )
        suggestion = {name: round(value, 3) for name, value in fitted.items()}
        suggestions[camera_id] = suggestion
        report["cameras"][camera_id] = {
            "twin_pose": suggestion,
            "training_objective_before": before,
            "training_objective_after": score,
            "dataset_gate": dataset_gate,
            "acceptance_eligible": False,
            "seed": "config_clamped_to_absolute_bounds" if args.start_from_config else "zero",
            "absolute_bounds": dict(zip(names, bounds)),
            "candidate_only": True,
        }

    print("\ntwin_pose blocks for config/cameras.json:")
    print(json.dumps(suggestions, indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        print(f"detailed candidate report: {args.output}")
    if args.candidate_config:
        if not candidate_config_is_eligible(report):
            print(
                "refusing candidate config: one or more camera datasets failed "
                "the independent evidence gate",
                file=sys.stderr,
            )
            return 2
        candidate_config = deepcopy(config)
        for candidate_camera in candidate_config["cameras"]:
            if candidate_camera["id"] in suggestions:
                candidate_camera["twin_pose"] = suggestions[candidate_camera["id"]]
        Path(args.candidate_config).write_text(
            json.dumps(candidate_config, indent=2) + "\n"
        )
        print(f"non-deployed candidate config: {args.candidate_config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
