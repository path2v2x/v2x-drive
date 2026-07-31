#!/usr/bin/env python3
"""Back-project diagnostic real-image geometry onto the active UE5 road surface.

This tool is intentionally incapable of producing acceptance evidence.  It
binds a retained frame, model-proposed image annotations, a diagnostic camera
candidate, and the active OpenDRIVE map before resolving rays.  The result can
be used to quantify a possible source-map correction, but not to deploy one.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys

import numpy as np

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from evaluate_diagnostic_track_geometry import (  # noqa: E402
    ground_intersection,
    rotation_matrix,
)

APPROVED_HOST = "127.0.0.1"
APPROVED_PORT = 2000
APPROVED_MAP_NAME = "Carla/Maps/Richmond_Field_Station_Richmond_CA"
APPROVED_OPENDRIVE_SHA256 = (
    "0737f3d9f9f344c06b2c63fe669afa8a15f814568ee9c16046795338f56f5ee1"
)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json_exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def candidate_params(search, rank):
    if not 1 <= rank <= len(search.get("results", [])):
        raise ValueError("candidate rank is outside the search result")
    result = search["results"][rank - 1]
    if result.get("optimizer_success") is not True:
        raise ValueError("candidate optimizer did not succeed")
    if result.get("boundary_hits"):
        raise ValueError("candidate reached an optimization boundary")
    if result.get("identity_underconstrained") is not False:
        raise ValueError("candidate signal identity is underconstrained")
    values = np.asarray(result.get("fitted_absolute"), dtype=float)
    if values.shape != (7,) or not np.isfinite(values).all():
        raise ValueError("candidate search result has no finite absolute model")
    if not 30.0 <= values[6] <= 150.0:
        raise ValueError("candidate horizontal FOV is outside the safety bound")
    return values


def signed_polygon_area(vertices):
    vertices = np.asarray(vertices, dtype=float)
    return 0.5 * float(
        np.sum(
            vertices[:, 0] * np.roll(vertices[:, 1], -1)
            - vertices[:, 1] * np.roll(vertices[:, 0], -1)
        )
    )


def _orientation(a, b, c):
    ab = np.asarray(b) - a
    ac = np.asarray(c) - a
    return float(ab[0] * ac[1] - ab[1] * ac[0])


def segments_cross(a, b, c, d):
    values = (
        _orientation(a, b, c),
        _orientation(a, b, d),
        _orientation(c, d, a),
        _orientation(c, d, b),
    )
    return values[0] * values[1] < 0.0 and values[2] * values[3] < 0.0


def validate_polygon(vertices, item_id):
    vertices = np.asarray(vertices, dtype=float)
    if len({tuple(value) for value in vertices.tolist()}) != len(vertices):
        raise ValueError(f"{item_id}: polygon contains duplicate vertices")
    count = len(vertices)
    for left in range(count):
        for right in range(left + 1, count):
            if right in {left, (left + 1) % count} or left == (right + 1) % count:
                continue
            if segments_cross(
                vertices[left],
                vertices[(left + 1) % count],
                vertices[right],
                vertices[(right + 1) % count],
            ):
                raise ValueError(f"{item_id}: polygon is self-intersecting")
    if abs(signed_polygon_area(vertices)) < 1.0:
        raise ValueError(f"{item_id}: polygon is degenerate")


def validate_inputs(
    annotations,
    signal_observations,
    search,
    geometry,
    camera,
    frame_hash,
    frame_size,
):
    if annotations.get("schema") != "v2x-crosswalk-hypothesis-observations/v1":
        raise ValueError("crosswalk annotation schema is unsupported")
    if search.get("schema") != "v2x-signal-hypothesis-search/v1":
        raise ValueError("candidate search schema is unsupported")
    if signal_observations.get("schema") != "v2x-signal-hypothesis-observations/v1":
        raise ValueError("signal observation schema is unsupported")
    if geometry.get("schema") != "v2x-map-calibration-geometry/v1":
        raise ValueError("map geometry schema is unsupported")
    if any(
        value.get("camera") != camera
        for value in (annotations, signal_observations, search)
    ):
        raise ValueError("camera binding failed")
    if any(value.get("acceptance_eligible") is not False for value in (
        annotations,
        signal_observations,
        search,
        geometry,
    )):
        raise ValueError("all diagnostic inputs must explicitly reject acceptance")
    camera_geometry = (geometry.get("cameras") or {}).get(camera)
    if not camera_geometry:
        raise ValueError("camera is absent from map geometry")
    real = camera_geometry.get("real") or {}
    checks = {
        "annotation_frame": annotations.get("real_frame_sha256") == frame_hash,
        "search_frame": search.get("real_frame_sha256") == frame_hash,
        "signal_observation_frame": (
            signal_observations.get("real_frame_sha256") == frame_hash
        ),
        "geometry_frame": real.get("frame_sha256") == frame_hash,
        "frame_width": int(real.get("width", -1)) == frame_size[0],
        "frame_height": int(real.get("height", -1)) == frame_size[1],
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"real-frame binding failed: {failed}")
    crosswalks = annotations.get("crosswalks")
    if not isinstance(crosswalks, list) or not crosswalks:
        raise ValueError("crosswalk annotations are empty")
    ids = set()
    for item in crosswalks:
        if not isinstance(item.get("id"), str) or item["id"] in ids:
            raise ValueError("crosswalk IDs must be unique strings")
        ids.add(item["id"])
        vertices = np.asarray(item.get("real_vertices"), dtype=float)
        if vertices.ndim != 2 or vertices.shape[0] < 3 or vertices.shape[1] != 2:
            raise ValueError(f"{item['id']}: polygon must have at least three 2D vertices")
        if not np.isfinite(vertices).all():
            raise ValueError(f"{item['id']}: polygon contains non-finite coordinates")
        if (
            (vertices[:, 0] < 0).any()
            or (vertices[:, 0] >= frame_size[0]).any()
            or (vertices[:, 1] < 0).any()
            or (vertices[:, 1] >= frame_size[1]).any()
        ):
            raise ValueError(f"{item['id']}: polygon falls outside the bound frame")
        validate_polygon(vertices, item["id"])
    return checks


def carla_to_opendrive(world_xyz):
    """Convert CARLA's left-handed world XY to OpenDRIVE's right-handed XY."""
    return [float(world_xyz[0]), float(-world_xyz[1])]


def backproject(carla_map, annotations, params, frame_size):
    polygons = []
    for item in annotations["crosswalks"]:
        resolved = []
        for index, pixel in enumerate(item["real_vertices"]):
            value = ground_intersection(carla_map, params, pixel, frame_size)
            if value is None:
                raise ValueError(f"{item['id']}: vertex {index} did not resolve")
            value["pixel"] = [float(pixel[0]), float(pixel[1])]
            value["opendrive_xy"] = carla_to_opendrive(value["world_xyz"])
            resolved.append(value)
        polygons.append({"id": item["id"], "vertices": resolved})
    return polygons


def ray_intersection_at_elevation(params, pixel, frame_size, elevation):
    width, height = frame_size
    focal = (width / 2.0) / math.tan(math.radians(float(params[6])) / 2.0)
    local_direction = np.asarray([
        1.0,
        (float(pixel[0]) - width / 2.0) / focal,
        -(float(pixel[1]) - height / 2.0) / focal,
    ])
    direction = rotation_matrix(params[3], params[4], params[5]) @ local_direction
    if direction[2] >= -1e-5:
        raise ValueError("pose hypothesis ray does not intersect the road elevation")
    distance = (float(elevation) - float(params[2])) / direction[2]
    if not math.isfinite(distance) or not 0.25 <= distance <= 300.0:
        raise ValueError("pose hypothesis ray intersection is outside safety bounds")
    return np.asarray(params[:3], dtype=float) + distance * direction


def attach_candidate_spread(selected, params_hypotheses, frame_size):
    """Attach per-vertex spread across viable diagnostic pose hypotheses."""
    for polygon in selected:
        for vertex in polygon["vertices"]:
            points = np.asarray([
                ray_intersection_at_elevation(
                    params,
                    vertex["pixel"],
                    frame_size,
                    vertex["world_xyz"][2],
                )
                for params in params_hypotheses
            ])
            center = np.median(points, axis=0)
            distances = np.linalg.norm(points - center, axis=1)
            vertex["pixel_uncertainty_px"] = None
            vertex["pose_hypothesis_spread_m"] = {
                "candidate_count": int(len(points)),
                "median": float(np.median(distances)),
                "max": float(np.max(distances)),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", required=True)
    parser.add_argument("--signal-observations", required=True)
    parser.add_argument("--candidate-search", required=True)
    parser.add_argument("--geometry", required=True)
    parser.add_argument("--real-frame", required=True)
    parser.add_argument("--camera", required=True, choices=("ch1", "ch2", "ch3", "ch4"))
    parser.add_argument("--rank", type=int, default=1)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 10):
        raise SystemExit("CARLA diagnostics require the intended Python 3.10 client")

    annotation_path = Path(args.annotations).resolve()
    signal_observation_path = Path(args.signal_observations).resolve()
    search_path = Path(args.candidate_search).resolve()
    geometry_path = Path(args.geometry).resolve()
    frame_path = Path(args.real_frame).resolve()
    output_path = Path(args.output).resolve()
    if output_path.exists():
        raise SystemExit("refusing to overwrite diagnostic geometry evidence")

    annotations = json.loads(annotation_path.read_bytes())
    signal_observations = json.loads(signal_observation_path.read_bytes())
    search = json.loads(search_path.read_bytes())
    geometry = json.loads(geometry_path.read_bytes())
    geometry_hash = sha256(geometry_path)
    if annotations.get("map_geometry_sha256") != geometry_hash:
        raise SystemExit("annotations do not bind the map geometry")
    if search.get("geometry_sha256") != geometry_hash:
        raise SystemExit("candidate search does not bind the map geometry")
    if search.get("observations_sha256") != sha256(signal_observation_path):
        raise SystemExit("candidate search does not bind the signal observations")
    if signal_observations.get("map_geometry_sha256") != geometry_hash:
        raise SystemExit("signal observations do not bind the map geometry")

    from PIL import Image

    with Image.open(frame_path) as image:
        image.verify()
    with Image.open(frame_path) as image:
        frame_size = image.size
    frame_hash = sha256(frame_path)
    try:
        binding_checks = validate_inputs(
            annotations,
            signal_observations,
            search,
            geometry,
            args.camera,
            frame_hash,
            frame_size,
        )
        params = candidate_params(search, args.rank)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    import carla

    client = carla.Client(APPROVED_HOST, APPROVED_PORT)
    client.set_timeout(20.0)
    world = client.get_world()
    carla_map = world.get_map()
    opendrive_hash = hashlib.sha256(carla_map.to_opendrive().encode()).hexdigest()
    if carla_map.name != APPROVED_MAP_NAME:
        raise SystemExit("active map is not the approved UE5 Richmond map")
    if opendrive_hash != APPROVED_OPENDRIVE_SHA256:
        raise SystemExit("active map is not the approved deployed OpenDRIVE revision")
    if opendrive_hash != geometry.get("opendrive_sha256"):
        raise SystemExit("active OpenDRIVE map does not match bound geometry")
    try:
        polygons = backproject(carla_map, annotations, params, frame_size)
        viable_hypotheses = []
        for rank in range(1, len(search.get("results", [])) + 1):
            try:
                hypothesis = candidate_params(search, rank)
            except ValueError:
                continue
            viable_hypotheses.append(hypothesis)
        if not viable_hypotheses:
            raise ValueError("no viable pose hypotheses are available")
        attach_candidate_spread(polygons, viable_hypotheses, frame_size)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    report = {
        "schema": "v2x-diagnostic-real-geometry-backprojection/v1",
        "created_at": utc_now(),
        "acceptance_eligible": False,
        "warning": (
            "model-proposed crosswalk pixels and signal-derived camera candidate; "
            "no surveyed or independent held-out correspondences"
        ),
        "camera": args.camera,
        "candidate_rank": args.rank,
        "candidate_absolute": params.tolist(),
        "active_map": {"name": carla_map.name, "opendrive_sha256": opendrive_hash},
        "frame": {
            "path": str(frame_path),
            "sha256": frame_hash,
            "width": frame_size[0],
            "height": frame_size[1],
        },
        "binding_checks": binding_checks,
        "annotation_frame_binding": (
            "direct: annotation, signal observation, candidate search, and map "
            "geometry camera record bind the exact retained frame hash"
        ),
        "source_hashes": {
            "annotations": sha256(annotation_path),
            "signal_observations": sha256(signal_observation_path),
            "candidate_search": sha256(search_path),
            "geometry": geometry_hash,
        },
        "limitations": [
            "crosswalk vertices are model visual proposals, not reviewed labels",
            "camera candidate was fitted from signal proposals in the same frame",
            "reported interpolation holdout is not independent",
            "road elevation is sampled from the deployed map being evaluated",
            "pose spread freezes each selected vertex's sampled road elevation",
            "OpenDRIVE XY uses the explicit CARLA (x, -y) convention",
        ],
        "polygons": polygons,
    }
    try:
        write_json_exclusive(output_path, report)
    except FileExistsError as error:
        raise SystemExit("refusing to overwrite diagnostic geometry evidence") from error
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
