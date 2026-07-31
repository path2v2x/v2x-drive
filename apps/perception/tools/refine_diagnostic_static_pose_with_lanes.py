#!/usr/bin/env python3
"""Refine only camera translation after a static-geometry orientation fit.

The static signal solution freezes pitch/yaw/roll/FOV. Unreviewed vehicle
contacts may then propose a small translation that improves lane proximity on
whole proposed tracklets, but the result remains acceptance-ineligible. This
optimizer does not claim reviewed lane identity or temporal truth.
"""

import argparse
import json
from pathlib import Path
import sys

import numpy as np
from scipy.optimize import differential_evolution

TOOLS = Path(__file__).resolve().parent
BRIDGE_TOOLS = Path(__file__).resolve().parents[2] / "bridge" / "tools"
for directory in (TOOLS, BRIDGE_TOOLS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from search_diagnostic_lane_pose import (  # noqa: E402
    canonical_hash,
    intersections,
    lane_cloud,
    holdout_not_worse,
    metrics,
    proposal_partition_owners,
    sha256,
)
from fit_diagnostic_visual_calibration import candidate_twin_pose  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-dir", required=True)
    parser.add_argument("--tracklets", required=True)
    parser.add_argument("--geometry", required=True)
    parser.add_argument("--static-search", required=True)
    parser.add_argument("--rank", type=int, required=True)
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
    static_path = Path(args.static_search).resolve()
    cameras_path = Path(args.cameras_json).resolve()
    output_path = Path(args.output).resolve()
    if output_path.exists():
        raise SystemExit("refusing to overwrite diagnostic translation evidence")
    tracklets = json.loads(tracklets_path.read_bytes())
    manifest = json.loads(manifest_path.read_bytes())
    geometry = json.loads(geometry_path.read_bytes())
    static = json.loads(static_path.read_bytes())
    config = json.loads(cameras_path.read_bytes())
    if manifest.get("schema") != "v2x-detection-observation-ledger/v2":
        raise SystemExit("observation ledger manifest schema is unsupported")
    if manifest.get("observations_sha256") != sha256(observations_path):
        raise SystemExit("observation ledger manifest does not bind observations")
    if tracklets.get("source_observations_sha256") != sha256(observations_path):
        raise SystemExit("tracklets do not bind the observation ledger")
    if static.get("schema") != "v2x-signal-hypothesis-search/v1" or static.get("camera") != args.camera:
        raise SystemExit("static signal search is incompatible")
    if static.get("geometry_sha256") != sha256(geometry_path):
        raise SystemExit("static search does not bind map geometry")
    if not 1 <= args.rank <= len(static["results"]):
        raise SystemExit("static candidate rank is outside the search")
    camera_config = next(item for item in config["cameras"] if item["id"] == args.camera)
    if canonical_hash(camera_config) != geometry["cameras"][args.camera]["camera_config_sha256"]:
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
    splits = np.asarray([owner[event_id] for event_id, _, _ in rows])
    fit_mask, holdout_mask = splits == "fit", splits == "holdout"
    if fit_mask.sum() < 12 or holdout_mask.sum() < 4:
        raise SystemExit("deterministic fit/holdout split is too small")
    pixels = np.asarray([row[1] for row in rows], dtype=float)
    sizes = np.asarray([row[2] for row in rows], dtype=float)
    lane_points, lane_widths, tree = lane_cloud(geometry)
    static_result = static["results"][args.rank - 1]
    baseline = np.asarray(static_result["fitted_absolute"], dtype=float)
    bounds = [(-2.0, 2.0), (-2.0, 2.0), (-1.0, 1.0)]
    scales = np.asarray([1.0, 1.0, 0.5])

    def candidate(delta):
        value = baseline.copy()
        value[:3] += delta
        return value

    def objective(delta):
        _world, distance, offroad, valid = intersections(
            candidate(delta), pixels, sizes, lane_points, lane_widths, tree
        )
        selected = fit_mask & valid & np.isfinite(distance)
        if selected.sum() < 0.95 * fit_mask.sum():
            return 1000.0
        values = distance[selected]
        robust = np.mean(np.sqrt(1.0 + values * values) - 1.0)
        return float(
            robust + 1.5 * np.mean(offroad[selected])
            + 0.08 * np.percentile(values, 95)
            + 0.05 * np.sum((delta / scales) ** 2)
        )

    solution = differential_evolution(
        objective, bounds, seed=args.seed, popsize=12, maxiter=100,
        tol=1e-7, polish=True, workers=1, updating="immediate",
    )
    fitted = candidate(solution.x)
    before = intersections(baseline, pixels, sizes, lane_points, lane_widths, tree)
    after = intersections(fitted, pixels, sizes, lane_points, lane_widths, tree)
    static_fit = metrics(before[1], before[2], before[3], fit_mask)
    static_holdout = metrics(before[1], before[2], before[3], holdout_mask)
    refined_fit = metrics(after[1], after[2], after[3], fit_mask)
    refined_holdout = metrics(after[1], after[2], after[3], holdout_mask)
    boundary_hits = [
        axis for axis, value, bound in zip(("x", "y", "z"), solution.x, bounds)
        if min(value - bound[0], bound[1] - value) < 0.05 * (bound[1] - bound[0])
    ]
    improves_holdout = holdout_not_worse(refined_holdout, static_holdout)
    camera_report = geometry["cameras"][args.camera]
    helper = np.asarray([
        *camera_report["tracked_helper_transform"]["location"],
        *camera_report["tracked_helper_transform"]["rotation"],
        camera_report["horizontal_fov_deg"] + camera_report["tracked_helper_delta"]["fov_deg"],
    ], dtype=float)
    report = {
        "schema": "v2x-diagnostic-static-lane-refinement/v1",
        "acceptance_eligible": False,
        "warning": (
            "static orientation frozen; unreviewed vehicle contacts propose "
            "translation from nearest-lane proximity only, without reviewed lane identity"
        ),
        "camera": args.camera,
        "static_candidate_rank": args.rank,
        "static_assignment": static_result["assignment"],
        "source_hashes": {
            "ledger_manifest": sha256(manifest_path),
            "observations": sha256(observations_path),
            "tracklets": sha256(tracklets_path),
            "geometry": sha256(geometry_path),
            "static_search": sha256(static_path),
            "cameras_json": sha256(cameras_path),
            "opendrive": geometry["opendrive_sha256"],
        },
        "split": {
            "fit": int(fit_mask.sum()),
            "holdout": int(holdout_mask.sum()),
            "proposal_tracklets_partitioned_whole": True,
            "unowned_observations_excluded": True,
        },
        "static_absolute": baseline.tolist(),
        "refined_absolute": fitted.tolist(),
        "translation_delta_xyz_m": solution.x.tolist(),
        "candidate_twin_pose": candidate_twin_pose(camera_config, helper, fitted),
        "optimizer": {"success": bool(solution.success), "message": str(solution.message), "objective": float(solution.fun)},
        "boundary_hits": boundary_hits,
        "refinement_recommendation": (
            "retain_for_offline_review"
            if solution.success and not boundary_hits and improves_holdout
            else "reject_or_expand_evidence"
        ),
        "static": {
            "fit": static_fit,
            "holdout": static_holdout,
        },
        "refined": {
            "fit": refined_fit,
            "holdout": refined_holdout,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
