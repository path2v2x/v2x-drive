#!/usr/bin/env python3
"""Search bounded camera-pose hypotheses from unreviewed vehicle lane priors.

This is deliberately diagnostic. It independently recomputes bbox-bottom rays,
intersects them with the exported UE5 road surface, and fits only small pose/FOV
deltas against lane-center proximity. Whole proposed tracklets are assigned to
one deterministic fit/holdout partition to avoid sample leakage. No derived GPS
coordinate is consumed and no result can authorize deployment.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
from scipy.optimize import differential_evolution
from scipy.spatial import cKDTree

BRIDGE_TOOLS = Path(__file__).resolve().parents[2] / "bridge" / "tools"
BRIDGE_DIR = Path(__file__).resolve().parents[2] / "bridge"
for directory in (BRIDGE_TOOLS, BRIDGE_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from fit_diagnostic_visual_calibration import rotation_matrix  # noqa: E402
from digital_twin_bridge.twin_camera_rig import normalize_angle_degrees  # noqa: E402


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def partition_for(value):
    return "holdout" if int(hashlib.sha256(value.encode()).hexdigest()[:2], 16) < 64 else "fit"


def proposal_partition_owners(proposals, camera_id):
    owner = {}
    for proposal in proposals:
        if proposal.get("camera_id") != camera_id:
            continue
        split = partition_for(proposal["proposal_id"])
        for event_id in proposal["event_ids"]:
            if event_id in owner:
                raise ValueError(
                    f"event {event_id} is reused across tracklet proposals"
                )
            owner[event_id] = split
    return owner


def lane_cloud(geometry):
    points, widths = [], []
    for lane in geometry["geometry"]["lanes"]:
        width = float(lane["lane_width_m"])
        for point in lane["center_world"]:
            points.append([float(value) for value in point])
            widths.append(width)
    points = np.asarray(points, dtype=float)
    if len(points) < 3 or not np.isfinite(points).all():
        raise ValueError("lane geometry is empty or invalid")
    return points, np.asarray(widths, dtype=float), cKDTree(points[:, :2])


def rays_for(params, pixels, sizes):
    focal = (sizes[:, 0] / 2.0) / np.tan(np.radians(params[6]) / 2.0)
    local = np.column_stack((
        np.ones(len(pixels)),
        (pixels[:, 0] - sizes[:, 0] / 2.0) / focal,
        -(pixels[:, 1] - sizes[:, 1] / 2.0) / focal,
    ))
    return (rotation_matrix(params[3], params[4], params[5]) @ local.T).T


def intersections(params, pixels, sizes, lane_points, lane_widths, tree):
    rays = rays_for(params, pixels, sizes)
    origin = np.asarray(params[:3], dtype=float)
    z = np.full(len(pixels), float(np.median(lane_points[:, 2])))
    valid = rays[:, 2] < -1e-5
    converged = np.zeros(len(pixels), dtype=bool)
    world = np.full((len(pixels), 3), np.nan)
    indices = np.zeros(len(pixels), dtype=int)
    for _ in range(6):
        distance = np.divide(
            z - origin[2], rays[:, 2],
            out=np.full(len(pixels), np.nan), where=valid,
        )
        valid &= np.isfinite(distance) & (distance >= 0.25) & (distance <= 300.0)
        world[valid] = origin + distance[valid, None] * rays[valid]
        _distance, nearest = tree.query(world[valid, :2])
        indices[valid] = nearest
        next_z = z.copy()
        next_z[valid] = lane_points[indices[valid], 2] + 0.05
        converged[valid] |= np.abs(next_z[valid] - z[valid]) <= 0.005
        z = next_z
        if valid.any() and np.all(converged[valid]):
            break
    valid &= converged
    distance = np.divide(
        z - origin[2], rays[:, 2],
        out=np.full(len(pixels), np.nan), where=valid,
    )
    valid &= np.isfinite(distance) & (distance >= 0.25) & (distance <= 300.0)
    world[valid] = origin + distance[valid, None] * rays[valid]
    _distance, nearest = tree.query(world[valid, :2])
    indices[valid] = nearest
    xy_distance = np.full(len(pixels), math.inf)
    xy_distance[valid] = np.linalg.norm(
        world[valid, :2] - lane_points[indices[valid], :2], axis=1
    )
    offroad = xy_distance > (lane_widths[indices] / 2.0 + 0.75)
    return world, xy_distance, offroad, valid


def metrics(distances, offroad, valid, mask):
    selected = mask & valid & np.isfinite(distances)
    values = distances[selected]
    return {
        "count": int(mask.sum()),
        "resolved": int(selected.sum()),
        "median_lane_center_m": None if not len(values) else float(np.median(values)),
        "p95_lane_center_m": None if not len(values) else float(np.percentile(values, 95)),
        "max_lane_center_m": None if not len(values) else float(np.max(values)),
        "offroad_fraction": None if not len(values) else float(np.mean(offroad[selected])),
    }


def holdout_not_worse(candidate, baseline):
    keys = ("median_lane_center_m", "p95_lane_center_m", "offroad_fraction")
    return (
        candidate["resolved"] == candidate["count"]
        and all(
            candidate[key] is not None
            and baseline[key] is not None
            and candidate[key] <= baseline[key]
            for key in keys
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-dir", required=True)
    parser.add_argument("--tracklets", required=True)
    parser.add_argument("--geometry", required=True)
    parser.add_argument("--cameras-json", required=True)
    parser.add_argument("--camera", required=True, choices=("ch1", "ch2", "ch3", "ch4"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--seed", type=int, default=20260711)
    args = parser.parse_args()

    ledger = Path(args.ledger_dir).resolve()
    observations_path = ledger / "observations.ndjson"
    manifest_path = ledger / "manifest.json"
    tracklets_path = Path(args.tracklets).resolve()
    geometry_path = Path(args.geometry).resolve()
    cameras_path = Path(args.cameras_json).resolve()
    output_path = Path(args.output).resolve()
    if output_path.exists():
        raise SystemExit("refusing to overwrite diagnostic lane-pose evidence")
    tracklets = json.loads(tracklets_path.read_bytes())
    manifest = json.loads(manifest_path.read_bytes())
    geometry = json.loads(geometry_path.read_bytes())
    cameras_config = json.loads(cameras_path.read_bytes())
    if manifest.get("schema") != "v2x-detection-observation-ledger/v2":
        raise SystemExit("observation ledger manifest schema is unsupported")
    if manifest.get("observations_sha256") != sha256(observations_path):
        raise SystemExit("observation ledger manifest does not bind observations")
    if tracklets.get("source_observations_sha256") != sha256(observations_path):
        raise SystemExit("tracklets do not bind the observation ledger")
    if geometry.get("schema") != "v2x-map-calibration-geometry/v1":
        raise SystemExit("map geometry schema is unsupported")
    camera_config = next(item for item in cameras_config["cameras"] if item["id"] == args.camera)
    if canonical_hash(camera_config) != geometry["cameras"][args.camera].get("camera_config_sha256"):
        raise SystemExit("map geometry does not bind the selected camera config")

    try:
        owner = proposal_partition_owners(
            tracklets.get("proposals", []), args.camera
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    rows = []
    with observations_path.open() as handle:
        for line in handle:
            value = json.loads(line)
            if value.get("camera_id") != args.camera or value.get("object_type") not in {"car", "truck", "bus"}:
                continue
            if value.get("event_id") not in owner:
                continue
            contact = (value.get("ground_contact") or {}).get("pixel")
            size = value.get("native_resolution")
            if isinstance(contact, list) and len(contact) == 2 and isinstance(size, list) and len(size) == 2:
                rows.append((value["event_id"], contact, size))
    if len(rows) < 20:
        raise SystemExit("insufficient camera observations")

    splits = np.asarray([owner[event_id] for event_id, _, _ in rows])
    fit_mask, holdout_mask = splits == "fit", splits == "holdout"
    if fit_mask.sum() < 12 or holdout_mask.sum() < 4:
        raise SystemExit("deterministic fit/holdout split is too small")
    pixels = np.asarray([row[1] for row in rows], dtype=float)
    sizes = np.asarray([row[2] for row in rows], dtype=float)
    lane_points, lane_widths, tree = lane_cloud(geometry)
    camera_report = geometry["cameras"][args.camera]
    transform = camera_report["baseline_transform"]
    baseline = np.asarray([
        *transform["location"], *transform["rotation"],
        camera_report["horizontal_fov_deg"],
    ], dtype=float)
    delta_bounds = [(-10.0, 10.0), (-10.0, 10.0), (-10.0, 10.0), (-15.0, 15.0)]
    scales = np.asarray([5.0, 5.0, 5.0, 8.0])

    def candidate(delta):
        result = baseline.copy()
        result[3:6] += delta[:3]
        result[4] = normalize_angle_degrees(result[4])
        result[6] += delta[3]
        return result

    def objective(delta):
        _world, distance, offroad, valid = intersections(
            candidate(delta), pixels, sizes, lane_points, lane_widths, tree
        )
        selected = fit_mask & valid & np.isfinite(distance)
        if selected.sum() < 0.95 * fit_mask.sum():
            return 1000.0
        values = distance[selected]
        robust = np.mean(np.sqrt(1.0 + values * values) - 1.0)
        tail = float(np.percentile(values, 95))
        return float(robust + 1.5 * np.mean(offroad[selected]) + 0.08 * tail + 0.03 * np.sum((delta / scales) ** 2))

    solution = differential_evolution(
        objective, delta_bounds, seed=args.seed, popsize=12, maxiter=100,
        tol=1e-5, polish=True, workers=1, updating="immediate",
    )
    fitted = candidate(solution.x)
    base_values = intersections(baseline, pixels, sizes, lane_points, lane_widths, tree)
    fit_values = intersections(fitted, pixels, sizes, lane_points, lane_widths, tree)
    pose = dict(camera_config.get("twin_pose") or {})
    pose["pitch_offset_deg"] = float(pose.get("pitch_offset_deg", 0.0) + solution.x[0])
    pose["yaw_offset_deg"] = float(pose.get("yaw_offset_deg", 0.0) + solution.x[1])
    pose["roll_offset_deg"] = float(pose.get("roll_offset_deg", 0.0) + solution.x[2])
    pose["fov_offset_deg"] = float(pose.get("fov_offset_deg", 0.0) + solution.x[3])
    boundary_hits = [
        name for name, value, bounds in zip(("pitch", "yaw", "roll", "fov"), solution.x, delta_bounds)
        if min(value - bounds[0], bounds[1] - value) < 0.05 * (bounds[1] - bounds[0])
    ]
    baseline_fit = metrics(base_values[1], base_values[2], base_values[3], fit_mask)
    baseline_holdout = metrics(base_values[1], base_values[2], base_values[3], holdout_mask)
    candidate_fit = metrics(fit_values[1], fit_values[2], fit_values[3], fit_mask)
    candidate_holdout = metrics(fit_values[1], fit_values[2], fit_values[3], holdout_mask)
    improves_holdout = holdout_not_worse(candidate_holdout, baseline_holdout)
    report = {
        "schema": "v2x-diagnostic-lane-pose-search/v1",
        "acceptance_eligible": False,
        "warning": (
            "unreviewed bbox contacts/track identities and nearest-lane assignment; "
            "diagnostic only, with no reviewed lane identity or temporal truth"
        ),
        "camera": args.camera,
        "source_hashes": {
            "ledger_manifest": sha256(manifest_path),
            "observations": sha256(observations_path),
            "tracklets": sha256(tracklets_path),
            "geometry": sha256(geometry_path),
            "cameras_json": sha256(cameras_path),
            "opendrive": geometry["opendrive_sha256"],
        },
        "split": {
            "fit": int(fit_mask.sum()),
            "holdout": int(holdout_mask.sum()),
            "proposal_tracklets_partitioned_whole": True,
            "unowned_observations_excluded": True,
        },
        "baseline_absolute": baseline.tolist(),
        "fitted_absolute": fitted.tolist(),
        "delta_pitch_yaw_roll_fov": solution.x.tolist(),
        "candidate_twin_pose": pose,
        "optimizer": {"success": bool(solution.success), "message": str(solution.message), "objective": float(solution.fun)},
        "boundary_hits": boundary_hits,
        "diagnostic_recommendation": (
            "retain_as_secondary_factor"
            if solution.success and not boundary_hits and improves_holdout
            else "reject_or_expand_evidence"
        ),
        "baseline": {
            "fit": baseline_fit,
            "holdout": baseline_holdout,
        },
        "candidate": {
            "fit": candidate_fit,
            "holdout": candidate_holdout,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
