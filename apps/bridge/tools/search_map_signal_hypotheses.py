#!/usr/bin/env python3
"""Enumerate fixed UE5 signal-stack identities for one real camera view."""

import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path
import sys

import cv2
import numpy as np
from scipy.optimize import least_squares

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from fit_diagnostic_visual_calibration import (  # noqa: E402
    candidate_twin_pose,
    project,
)


def group_signal_components(objects):
    groups = {}
    for item in objects:
        if item["category"] != "TrafficLight":
            continue
        extent = item["extent"]
        if max(extent) > 0.2:
            continue
        point = item["center_world"]
        key = (round(point[0], 1), round(point[1], 1))
        groups.setdefault(key, []).append(item)
    output = []
    for key, values in groups.items():
        values.sort(key=lambda item: item["center_world"][2], reverse=True)
        if len(values) == 3:
            output.append({
                "id": f"signal-stack-{key[0]:.1f}-{key[1]:.1f}",
                "xy": list(key),
                "components": values,
            })
    return sorted(output, key=lambda item: item["id"])


def metrics(errors):
    return {
        "rmse_px": float(math.sqrt(np.mean(errors ** 2))),
        "median_px": float(np.median(errors)),
        "max_px": float(np.max(errors)),
    }


def fit(world, real, train_mask, baseline, width, height):
    free = np.asarray([3, 4, 5, 6], dtype=int)
    lower = baseline[free] + np.asarray([-40, -90, -30, -30], dtype=float)
    upper = baseline[free] + np.asarray([40, 90, 30, 30], dtype=float)

    def expand(values):
        params = baseline.copy()
        params[free] = values
        return params

    def visual(values):
        pixels, depth = project(world[train_mask], expand(values), width, height)
        residual = (pixels - real[train_mask]).ravel()
        residual[(~np.isfinite(residual)) | np.repeat(depth <= 0.2, 2)] = 10_000
        return residual

    def residual(values):
        prior = 0.03 * (values - baseline[free]) / np.asarray([10, 20, 10, 10])
        return np.concatenate((visual(values), prior))

    seeds = []
    for yaw in (-75, -45, 0, 45, 75):
        for pitch in (-15, 0, 15):
            seed = baseline[free].copy()
            seed[0] = np.clip(seed[0] + pitch, lower[0], upper[0])
            seed[1] = np.clip(seed[1] + yaw, lower[1], upper[1])
            seeds.append(seed)
    solutions = [least_squares(
        residual, seed, bounds=(lower, upper), loss="soft_l1", f_scale=2.0,
        max_nfev=2000,
    ) for seed in seeds]
    solution = min(solutions, key=lambda item: float(np.sum(visual(item.x) ** 2)))
    params = expand(solution.x)
    pixels, depth = project(world, params, width, height)
    errors = np.linalg.norm(pixels - real, axis=1)
    errors[(depth <= 0.2) | ~np.isfinite(errors)] = 10_000
    hits = []
    for index, name in enumerate(("pitch", "yaw", "roll", "fov")):
        if abs(solution.x[index] - lower[index]) < 1e-4 or abs(solution.x[index] - upper[index]) < 1e-4:
            hits.append(name)
    return {
        "params": params,
        "pixels": pixels,
        "fit": metrics(errors[train_mask]),
        "holdout": metrics(errors[~train_mask]),
        "all": metrics(errors),
        "boundary_hits": hits,
        "optimizer_success": bool(solution.success),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geometry", required=True)
    parser.add_argument("--observations", required=True)
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    geometry_path = Path(args.geometry).resolve()
    observation_path = Path(args.observations).resolve()
    cameras_path = Path(args.cameras_json).resolve()
    geometry_bytes, observation_bytes, cameras_bytes = (
        geometry_path.read_bytes(), observation_path.read_bytes(), cameras_path.read_bytes()
    )
    geometry, observations, config = (
        json.loads(geometry_bytes), json.loads(observation_bytes), json.loads(cameras_bytes)
    )
    if geometry.get("schema") != "v2x-map-calibration-geometry/v1":
        raise SystemExit("map geometry schema is unsupported")
    if observations.get("schema") != "v2x-signal-hypothesis-observations/v1" or observations.get("acceptance_eligible") is not False:
        raise SystemExit("observations lack the diagnostic contract")
    if observations.get("map_geometry_sha256") != hashlib.sha256(geometry_bytes).hexdigest():
        raise SystemExit("observation geometry hash mismatch")
    if geometry.get("cameras_file_sha256") != hashlib.sha256(cameras_bytes).hexdigest():
        raise SystemExit("cameras file does not match geometry")
    camera_id = observations["camera"]
    camera_report = geometry["cameras"][camera_id]
    camera_config = next(item for item in config["cameras"] if item["id"] == camera_id)
    width, height = camera_report["real"]["width"], camera_report["real"]["height"]
    real_frame = Path(camera_report["real"]["frame"])
    real_frame_sha256 = hashlib.sha256(real_frame.read_bytes()).hexdigest()
    if (
        real_frame_sha256 != camera_report["real"]["frame_sha256"]
        or observations.get("real_frame_sha256") != real_frame_sha256
        or cv2.imread(str(real_frame), cv2.IMREAD_COLOR) is None
    ):
        raise SystemExit("signal observations do not bind the retained real frame")
    stacks = group_signal_components(geometry["geometry"]["objects"])
    real_stacks = []
    for item in observations["stacks"]:
        pixels = np.asarray(item["real_component_pixels"], dtype=float)
        if (
            pixels.shape != (3, 2)
            or not np.isfinite(pixels).all()
            or np.any(pixels[:, 0] < 0) or np.any(pixels[:, 0] >= width)
            or np.any(pixels[:, 1] < 0) or np.any(pixels[:, 1] >= height)
        ):
            raise SystemExit("each real stack requires red/amber/green pixels")
        real_stacks.append((item["id"], pixels))
    if not 1 <= len(real_stacks) <= len(stacks):
        raise SystemExit("one or more real stacks are required")
    identity_underconstrained = len(real_stacks) < 2

    baseline = np.asarray([
        *camera_report["baseline_transform"]["location"],
        *camera_report["baseline_transform"]["rotation"],
        camera_report["horizontal_fov_deg"],
    ], dtype=float)
    helper_baseline = np.asarray([
        *camera_report["tracked_helper_transform"]["location"],
        *camera_report["tracked_helper_transform"]["rotation"],
        camera_report["horizontal_fov_deg"] + camera_report["tracked_helper_delta"]["fov_deg"],
    ], dtype=float)
    real = np.vstack([item[1] for item in real_stacks])
    # Red and green constrain the fit; amber remains untouched in each stack.
    train_mask = np.tile(np.asarray([True, False, True], dtype=bool), len(real_stacks))
    results = []
    for assignment in itertools.permutations(stacks, len(real_stacks)):
        world = np.asarray([
            item["center_world"]
            for stack in assignment for item in stack["components"]
        ], dtype=float)
        fitted = fit(world, real, train_mask, baseline, width, height)
        score = (
            fitted["fit"]["rmse_px"]
            + 1000 * len(fitted["boundary_hits"])
        )
        results.append({
            "score": score,
            "assignment": [
                {"real_id": real_stack[0], "map_id": map_stack["id"]}
                for real_stack, map_stack in zip(real_stacks, assignment)
            ],
            "fitted_absolute": fitted["params"].tolist(),
            "delta_absolute": (fitted["params"] - baseline).tolist(),
            "candidate_twin_pose": candidate_twin_pose(
                camera_config, helper_baseline, fitted["params"]
            ),
            "fit": fitted["fit"],
            "interpolation_check": fitted["holdout"],
            "holdout": fitted["holdout"],
            "independent_holdout": False,
            "all": fitted["all"],
            "boundary_hits": fitted["boundary_hits"],
            "optimizer_success": fitted["optimizer_success"],
            "identity_underconstrained": identity_underconstrained,
            "projected_pixels": fitted["pixels"].tolist(),
        })
    results.sort(key=lambda item: item["score"])
    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit("refusing to overwrite nonempty signal search directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema": "v2x-signal-hypothesis-search/v1",
        "acceptance_eligible": False,
        "camera": camera_id,
        "geometry_sha256": hashlib.sha256(geometry_bytes).hexdigest(),
        "observations_sha256": hashlib.sha256(observation_bytes).hexdigest(),
        "real_frame_sha256": real_frame_sha256,
        "hypotheses_evaluated": len(results),
        "identity_underconstrained": identity_underconstrained,
        "independent_holdout": False,
        "selection_uses_interpolation_check": False,
        "holdout_warning": (
            "amber lens is withheld from the optimizer but shares each fitted "
            "signal stack; it is an interpolation check, not an independent holdout"
        ),
        "results": results,
    }
    output_path = output_dir / "signal-hypothesis-search.json"
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    search_sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()
    for index, item in enumerate(results[:4], start=1):
        passed = (
            not item["identity_underconstrained"]
            and item["optimizer_success"] and not item["boundary_hits"]
            and item["holdout"]["rmse_px"] <= 5.0
            and item["holdout"]["max_px"] <= 8.0
        )
        candidate_report = {
            "schema": "v2x-diagnostic-map-calibration/v1",
            "acceptance_eligible": False,
            "independent_holdout": False,
            "source_signal_search_sha256": search_sha256,
            "cameras_json_sha256": hashlib.sha256(cameras_bytes).hexdigest(),
            "cameras": {camera_id: {
                "candidate_twin_pose": item["candidate_twin_pose"],
                "candidate_recommendation": (
                    "continue_offline_render_review" if passed
                    else "reject_or_expand_evidence"
                ),
                "source_signal_hypothesis_rank": index,
            }},
        }
        (output_dir / f"rank-{index:02d}-candidate.json").write_text(
            json.dumps(candidate_report, indent=2, sort_keys=True) + "\n"
        )
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
