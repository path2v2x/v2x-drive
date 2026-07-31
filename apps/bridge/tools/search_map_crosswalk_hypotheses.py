#!/usr/bin/env python3
"""Enumerate map/real crosswalk identities and rank on untouched corners."""

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

from fit_diagnostic_visual_calibration import project  # noqa: E402


def orientations():
    base = [0, 1, 2, 3]
    values = []
    for reverse in (False, True):
        source = list(reversed(base)) if reverse else base
        for offset in range(4):
            values.append(source[offset:] + source[:offset])
    return values


def metric(errors):
    errors = np.asarray(errors, dtype=float)
    return {
        "rmse_px": float(math.sqrt(np.mean(errors ** 2))),
        "median_px": float(np.median(errors)),
        "max_px": float(np.max(errors)),
    }


def fit_hypothesis(world, real, train_mask, baseline, width, height):
    lower = baseline[:6] + np.asarray([-15, -15, -8, -60, -90, -45], dtype=float)
    upper = baseline[:6] + np.asarray([15, 15, 8, 60, 90, 45], dtype=float)
    train_world, train_real = world[train_mask], real[train_mask]

    def expand(values):
        return np.asarray([*values, baseline[6]], dtype=float)

    def visual(values):
        pixels, depth = project(train_world, expand(values), width, height)
        residual = (pixels - train_real).ravel()
        residual[(~np.isfinite(residual)) | np.repeat(depth <= 0.2, 2)] = 10_000
        return residual

    def residual(values):
        scales = np.asarray([3, 3, 3, 15, 20, 10], dtype=float)
        prior = 0.03 * (values - baseline[:6]) / scales
        return np.concatenate((visual(values), prior))

    seeds = []
    for yaw_delta in (-60, -30, 0, 30, 60):
        for pitch_delta in (-20, 0, 20):
            seed = baseline[:6].copy()
            seed[3] = np.clip(seed[3] + pitch_delta, lower[3], upper[3])
            seed[4] = np.clip(seed[4] + yaw_delta, lower[4], upper[4])
            seeds.append(seed)
    solutions = [least_squares(
        residual, seed, bounds=(lower, upper), loss="soft_l1", f_scale=3.0,
        max_nfev=1500,
    ) for seed in seeds]
    solution = min(solutions, key=lambda item: float(np.sum(visual(item.x) ** 2)))
    params = expand(solution.x)
    pixels, depth = project(world, params, width, height)
    errors = np.linalg.norm(pixels - real, axis=1)
    errors[(depth <= 0.2) | ~np.isfinite(errors)] = 10_000
    hits = []
    for index, name in enumerate(("x", "y", "z", "pitch", "yaw", "roll")):
        if abs(solution.x[index] - lower[index]) < 1e-4 or abs(solution.x[index] - upper[index]) < 1e-4:
            hits.append(name)
    return {
        "params": params,
        "pixels": pixels,
        "errors": errors,
        "fit": metric(errors[train_mask]),
        "holdout": metric(errors[~train_mask]),
        "all": metric(errors),
        "optimizer_success": bool(solution.success),
        "boundary_hits": hits,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geometry", required=True)
    parser.add_argument("--observations", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    geometry_path = Path(args.geometry).resolve()
    observation_path = Path(args.observations).resolve()
    geometry_bytes, observation_bytes = geometry_path.read_bytes(), observation_path.read_bytes()
    geometry, observations = json.loads(geometry_bytes), json.loads(observation_bytes)
    if geometry.get("schema") != "v2x-map-calibration-geometry/v1":
        raise SystemExit("unsupported geometry schema")
    if observations.get("schema") != "v2x-crosswalk-hypothesis-observations/v1" or observations.get("acceptance_eligible") is not False:
        raise SystemExit("observations lack the diagnostic contract")
    if observations.get("map_geometry_sha256") != hashlib.sha256(geometry_bytes).hexdigest():
        raise SystemExit("observation geometry hash mismatch")
    camera_id = observations["camera"]
    camera = geometry["cameras"][camera_id]
    real_report = camera["real"]
    image = cv2.imread(real_report["frame"], cv2.IMREAD_COLOR)
    if image is None or hashlib.sha256(Path(real_report["frame"]).read_bytes()).hexdigest() != real_report["frame_sha256"]:
        raise SystemExit("retained real frame binding failed")
    height, width = image.shape[:2]
    real_quads = []
    for item in observations["crosswalks"]:
        points = np.asarray(item["real_vertices"], dtype=float)
        if points.shape != (4, 2) or not np.isfinite(points).all():
            raise SystemExit("every real crosswalk requires four finite vertices")
        real_quads.append((item["id"], points))
    if len(real_quads) != 2:
        raise SystemExit("this bounded search requires exactly two real crosswalks")

    world_index = {
        item["id"]: np.asarray(item["world"][:4], dtype=float)
        for item in geometry["geometry"]["crosswalks"]
    }
    baseline = np.asarray([
        *camera["baseline_transform"]["location"],
        *camera["baseline_transform"]["rotation"],
        camera["horizontal_fov_deg"],
    ], dtype=float)
    train_mask = np.asarray([True, False, True, False] * 2, dtype=bool)
    results = []
    ids = sorted(world_index)
    for left_id, right_id in itertools.permutations(ids, 2):
        for left_order in orientations():
            for right_order in orientations():
                world = np.vstack((
                    world_index[left_id][left_order],
                    world_index[right_id][right_order],
                ))
                real = np.vstack((real_quads[0][1], real_quads[1][1]))
                fitted = fit_hypothesis(
                    world, real, train_mask, baseline, width, height
                )
                score = (
                    fitted["fit"]["rmse_px"]
                    + 1000.0 * len(fitted["boundary_hits"])
                )
                results.append({
                    "score": score,
                    "assignment": [
                        {"real_id": real_quads[0][0], "map_id": left_id, "vertex_order": left_order},
                        {"real_id": real_quads[1][0], "map_id": right_id, "vertex_order": right_order},
                    ],
                    "fitted_absolute": {
                        name: float(value) for name, value in zip(
                            ("location_x", "location_y", "location_z", "pitch_deg", "yaw_deg", "roll_deg", "fov_deg"),
                            fitted["params"],
                        )
                    },
                    "delta_absolute": (
                        fitted["params"] - baseline
                    ).tolist(),
                    "fit": fitted["fit"],
                    "holdout": fitted["holdout"],
                    "all": fitted["all"],
                    "boundary_hits": fitted["boundary_hits"],
                    "optimizer_success": fitted["optimizer_success"],
                    "projected_pixels": fitted["pixels"].tolist(),
                })
    results.sort(key=lambda item: item["score"])
    retained = results[:args.top]
    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit("refusing to overwrite nonempty hypothesis directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(retained[:5], start=1):
        overlay = image.copy()
        real = np.vstack((real_quads[0][1], real_quads[1][1]))
        projected = np.asarray(item["projected_pixels"])
        for observed, candidate in zip(real, projected):
            cv2.circle(overlay, tuple(np.rint(observed).astype(int)), 5, (0, 255, 255), 2)
            cv2.circle(overlay, tuple(np.rint(candidate).astype(int)), 4, (0, 255, 0), 2)
        for offset in (0, 4):
            cv2.polylines(overlay, [np.rint(real[offset:offset+4]).astype(np.int32)], True, (0, 255, 255), 2)
            cv2.polylines(overlay, [np.rint(projected[offset:offset+4]).astype(np.int32)], True, (0, 255, 0), 2)
        name = f"rank-{index:02d}-overlay.jpg"
        cv2.imwrite(str(output_dir / name), overlay)
        item["overlay"] = name
    report = {
        "schema": "v2x-crosswalk-hypothesis-search/v1",
        "acceptance_eligible": False,
        "selection_uses_holdout": False,
        "holdout_warning": (
            "holdout corners are excluded from both optimization and hypothesis ranking; "
            "the sparse diagnostic set still cannot authorize deployment"
        ),
        "camera": camera_id,
        "geometry_sha256": hashlib.sha256(geometry_bytes).hexdigest(),
        "observations_sha256": hashlib.sha256(observation_bytes).hexdigest(),
        "hypotheses_evaluated": len(results),
        "train_vertex_indices_per_crosswalk": [0, 2],
        "holdout_vertex_indices_per_crosswalk": [1, 3],
        "results": retained,
    }
    output_path = output_dir / "crosswalk-hypothesis-search.json"
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
