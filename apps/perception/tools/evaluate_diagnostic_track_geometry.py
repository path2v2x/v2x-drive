#!/usr/bin/env python3
"""Compare baseline/candidate camera geometry on retained vehicle tracklets.

This evaluator never consumes the observations' derived GPS coordinates.  It
recomputes bbox-bottom rays, intersects them iteratively with the active UE5
road surface, and reports lane/temporal diagnostics.  Unreviewed contact and
identity proposals remain explicitly non-acceptance evidence.
"""

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np

BRIDGE_TOOLS = Path(__file__).resolve().parents[2] / "bridge" / "tools"
if str(BRIDGE_TOOLS) not in sys.path:
    sys.path.insert(0, str(BRIDGE_TOOLS))

from fit_diagnostic_visual_calibration import rotation_matrix  # noqa: E402


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def finite_metric(values):
    values = np.asarray([value for value in values if math.isfinite(value)], dtype=float)
    if not len(values):
        return None
    return {
        "count": int(len(values)),
        "median": float(np.median(values)),
        "p95": float(np.percentile(values, 95)),
        "max": float(np.max(values)),
    }


def absolute_params(camera_report, candidate_search, rank):
    baseline = np.asarray([
        *camera_report["baseline_transform"]["location"],
        *camera_report["baseline_transform"]["rotation"],
        camera_report["horizontal_fov_deg"],
    ], dtype=float)
    result = candidate_search["results"][rank - 1]
    candidate = np.asarray(result["fitted_absolute"], dtype=float)
    if candidate.shape != (7,) or not np.isfinite(candidate).all():
        raise ValueError("candidate search result has no finite absolute model")
    return baseline, candidate, result


def ground_intersection(carla_map, params, pixel, image_size):
    import carla

    width, height = image_size
    fov = float(params[6])
    focal = (width / 2.0) / math.tan(math.radians(fov) / 2.0)
    local_direction = np.asarray([
        1.0,
        (float(pixel[0]) - width / 2.0) / focal,
        -(float(pixel[1]) - height / 2.0) / focal,
    ])
    direction = rotation_matrix(params[3], params[4], params[5]) @ local_direction
    origin = np.asarray(params[:3], dtype=float)
    if direction[2] >= -1e-5:
        return None
    road_z = 6.6
    waypoint = None
    point = None
    converged = False
    for _ in range(8):
        distance = (road_z - origin[2]) / direction[2]
        if not math.isfinite(distance) or not 0.25 <= distance <= 300.0:
            return None
        point = origin + distance * direction
        waypoint = carla_map.get_waypoint(
            carla.Location(x=float(point[0]), y=float(point[1]), z=float(point[2])),
            project_to_road=True,
            lane_type=carla.LaneType.Driving,
        )
        if waypoint is None:
            return None
        next_z = float(waypoint.transform.location.z) + 0.05
        if abs(next_z - road_z) <= 0.005:
            road_z = next_z
            converged = True
            break
        road_z = next_z
    if not converged:
        return None
    distance = (road_z - origin[2]) / direction[2]
    point = origin + distance * direction
    waypoint = carla_map.get_waypoint(
        carla.Location(x=float(point[0]), y=float(point[1]), z=float(point[2])),
        project_to_road=True,
        lane_type=carla.LaneType.Driving,
    )
    if (
        waypoint is None
        or abs(float(waypoint.transform.location.z) + 0.05 - road_z) > 0.005
    ):
        return None
    center = waypoint.transform.location
    lateral_distance = math.hypot(point[0] - center.x, point[1] - center.y)
    lane_half_width = float(waypoint.lane_width) / 2.0
    return {
        "world_xyz": [float(point[0]), float(point[1]), float(road_z)],
        "ray_distance_m": float(distance * np.linalg.norm(direction)),
        "road_id": int(waypoint.road_id),
        "lane_id": int(waypoint.lane_id),
        "lane_width_m": float(waypoint.lane_width),
        "lane_center_distance_m": lateral_distance,
        "offroad": lateral_distance > lane_half_width + 0.75,
    }


def evaluate_model(carla_map, observations, proposals, params):
    resolved = {}
    for event_id, observation in observations.items():
        contact = (observation.get("ground_contact") or {}).get("pixel")
        size = observation.get("native_resolution")
        if (
            not isinstance(contact, list) or len(contact) != 2
            or not isinstance(size, list) or len(size) != 2
        ):
            continue
        value = ground_intersection(carla_map, params, contact, size)
        if value is None:
            continue
        value["media_timestamp_utc"] = observation["media_timestamp_utc"]
        resolved[event_id] = value

    speeds, accelerations = [], []
    all_lane_distances = [value["lane_center_distance_m"] for value in resolved.values()]
    all_offroad = sum(int(value["offroad"]) for value in resolved.values())
    track_lane_distances = []
    track_offroad = 0
    track_observations = 0
    track_reports = []
    for proposal in proposals:
        samples = []
        for event_id in proposal["event_ids"]:
            if event_id in resolved:
                samples.append((parse_time(resolved[event_id]["media_timestamp_utc"]), resolved[event_id]))
        samples.sort(key=lambda item: item[0])
        if not samples:
            continue
        track_observations += len(samples)
        track_speeds = []
        for _timestamp, sample in samples:
            track_lane_distances.append(sample["lane_center_distance_m"])
            track_offroad += int(sample["offroad"])
        for (left_time, left), (right_time, right) in zip(samples, samples[1:]):
            delta = right_time - left_time
            if delta <= 0:
                continue
            left_xy = np.asarray(left["world_xyz"][:2])
            right_xy = np.asarray(right["world_xyz"][:2])
            speed = float(np.linalg.norm(right_xy - left_xy) / delta)
            speeds.append(speed)
            track_speeds.append((right_time, speed))
        track_accel = []
        for (left_time, left_speed), (right_time, right_speed) in zip(track_speeds, track_speeds[1:]):
            delta = right_time - left_time
            if delta > 0:
                acceleration = abs(right_speed - left_speed) / delta
                accelerations.append(acceleration)
                track_accel.append(acceleration)
        track_reports.append({
            "proposal_id": proposal["proposal_id"],
            "resolved_observations": len(samples),
            "speed_mps": finite_metric([value for _, value in track_speeds]),
            "absolute_acceleration_mps2": finite_metric(track_accel),
        })
    total = len(resolved)
    return {
        "resolved_observations": total,
        "unresolved_observations": len(observations) - total,
        "lane_center_distance_m": finite_metric(all_lane_distances),
        "offroad_count": all_offroad,
        "offroad_fraction": None if not total else all_offroad / total,
        "tracklet_observations": track_observations,
        "tracklet_lane_center_distance_m": finite_metric(track_lane_distances),
        "tracklet_offroad_count": track_offroad,
        "tracklet_offroad_fraction": (
            None if not track_observations else track_offroad / track_observations
        ),
        "speed_mps": finite_metric(speeds),
        "implausible_speed_fraction_over_50_mps": (
            None if not speeds else sum(value > 50.0 for value in speeds) / len(speeds)
        ),
        "absolute_acceleration_mps2": finite_metric(accelerations),
        "tracklets": track_reports,
        "observations": resolved,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-dir", required=True)
    parser.add_argument("--tracklets", required=True)
    parser.add_argument("--geometry", required=True)
    parser.add_argument("--candidate-search", required=True)
    parser.add_argument("--rank", type=int, default=1)
    parser.add_argument("--camera", required=True, choices=("ch1", "ch2", "ch3", "ch4"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    args = parser.parse_args()

    ledger_dir = Path(args.ledger_dir).resolve()
    observations_path = ledger_dir / "observations.ndjson"
    manifest_path = ledger_dir / "manifest.json"
    tracklets_path = Path(args.tracklets).resolve()
    geometry_path = Path(args.geometry).resolve()
    search_path = Path(args.candidate_search).resolve()
    output_path = Path(args.output).resolve()
    if output_path.exists():
        raise SystemExit("refusing to overwrite diagnostic track geometry")
    manifest = json.loads(manifest_path.read_bytes())
    tracklets = json.loads(tracklets_path.read_bytes())
    geometry = json.loads(geometry_path.read_bytes())
    search = json.loads(search_path.read_bytes())
    if manifest.get("schema") != "v2x-detection-observation-ledger/v2":
        raise SystemExit("observation ledger manifest schema is unsupported")
    if manifest.get("observations_sha256") != sha256(observations_path):
        raise SystemExit("observation ledger manifest does not bind observations")
    if tracklets.get("schema") != "v2x-tracklet-proposals/v1":
        raise SystemExit("tracklet proposal schema is unsupported")
    if tracklets.get("source_observations_sha256") != sha256(observations_path):
        raise SystemExit("tracklets do not bind the observation ledger")
    if geometry.get("schema") != "v2x-map-calibration-geometry/v1":
        raise SystemExit("map geometry schema is unsupported")
    if search.get("schema") != "v2x-signal-hypothesis-search/v1" or search.get("camera") != args.camera:
        raise SystemExit("candidate signal search is incompatible")
    if search.get("geometry_sha256") != sha256(geometry_path):
        raise SystemExit("candidate signal search does not bind map geometry")
    if not 1 <= args.rank <= len(search["results"]):
        raise SystemExit("candidate rank is outside the search result")

    observations = {}
    with observations_path.open() as handle:
        for line in handle:
            value = json.loads(line)
            if value.get("camera_id") == args.camera and value.get("object_type") in {"car", "truck", "bus"}:
                observations[value["event_id"]] = value
    proposals = [
        item for item in tracklets["proposals"]
        if item.get("camera_id") == args.camera
    ]
    owned_events = set()
    for proposal in proposals:
        for event_id in proposal.get("event_ids", []):
            if event_id in owned_events:
                raise SystemExit(
                    f"event {event_id} is reused across tracklet proposals"
                )
            owned_events.add(event_id)
    camera_report = geometry["cameras"][args.camera]
    baseline, candidate, candidate_result = absolute_params(
        camera_report, search, args.rank
    )

    import carla

    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    carla_map = world.get_map()
    opendrive_hash = hashlib.sha256(carla_map.to_opendrive().encode("utf-8")).hexdigest()
    if opendrive_hash != geometry.get("opendrive_sha256"):
        raise SystemExit("active OpenDRIVE map does not match geometry evidence")

    report = {
        "schema": "v2x-diagnostic-track-geometry/v1",
        "acceptance_eligible": False,
        "warning": "unreviewed bbox contacts/track identities; comparison only",
        "camera": args.camera,
        "candidate_rank": args.rank,
        "candidate_assignment": candidate_result["assignment"],
        "source_hashes": {
            "ledger_manifest": sha256(manifest_path),
            "observations": sha256(observations_path),
            "tracklets": sha256(tracklets_path),
            "geometry": sha256(geometry_path),
            "candidate_search": sha256(search_path),
            "opendrive": opendrive_hash,
        },
        "counts": {
            "camera_vehicle_observations": len(observations),
            "camera_tracklet_proposals": len(proposals),
        },
        "baseline": evaluate_model(carla_map, observations, proposals, baseline),
        "candidate": evaluate_model(carla_map, observations, proposals, candidate),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
