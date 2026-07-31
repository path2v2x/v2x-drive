#!/usr/bin/env python3
"""Project hash-bound CARLA map geometry onto a registered orthophoto grid.

This tool isolates map/georeference error from camera-pose error.  CARLA's
left-handed ``(x, y)`` is converted to OpenDRIVE ``(east, north)=(x, -y)``,
then carried through the declared map projection and the diagnostic
LiDAR-to-orthophoto registration.  The result is review evidence only.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from v2x_common.geodesy import TransverseMercator  # noqa: E402


class OverlayError(RuntimeError):
    pass


UTM10_WGS84 = TransverseMercator(
    latitude_of_origin_deg=0.0,
    central_meridian_deg=-123.0,
    scale_factor=0.9996,
    false_easting_m=500_000.0,
    false_northing_m=0.0,
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_bound_json(path, expected_hash, label):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OverlayError(f"{label} is unreadable") from exc
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise OverlayError(f"{label} hash does not match")
    try:
        return path, json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OverlayError(f"{label} is invalid JSON") from exc


def read_bound_image(path, expected_hash):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OverlayError("orthophoto is unreadable") from exc
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise OverlayError("orthophoto hash does not match")
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise OverlayError("orthophoto is not a decodable image")
    return path, image


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


def carla_xy_to_orthophoto_pixel(
    x, y, map_projection, bounds, resolution_m, registration_matrix
):
    latitude, longitude = map_projection.inverse(float(x), -float(y))
    easting, northing = UTM10_WGS84.forward(latitude, longitude)
    xmin, _ymin, _xmax, ymax = bounds
    lidar_pixel = np.array(
        [(easting - xmin) / resolution_m, (ymax - northing) / resolution_m, 1.0]
    )
    pixel = np.asarray(registration_matrix, dtype=float) @ lidar_pixel
    return pixel.tolist(), [easting, northing], [latitude, longitude]


def transform_polyline(points, projector):
    return [projector(point[0], point[1])[0] for point in points]


def draw_polyline(image, pixels, color, thickness, closed=False):
    if len(pixels) < 2:
        return
    values = np.rint(np.asarray(pixels, dtype=float)).astype(np.int32)
    cv2.polylines(image, [values], closed, color, thickness, cv2.LINE_AA)


def overlay(
    geometry_path,
    geometry_hash,
    config_path,
    config_hash,
    registration_path,
    registration_hash,
    orthophoto_path,
    orthophoto_hash,
):
    geometry_path, report = read_bound_json(
        geometry_path, geometry_hash, "map geometry"
    )
    config_path, config = read_bound_json(config_path, config_hash, "camera config")
    registration_path, registration = read_bound_json(
        registration_path, registration_hash, "registration"
    )
    orthophoto_path, image = read_bound_image(orthophoto_path, orthophoto_hash)
    if report.get("schema") != "v2x-map-calibration-geometry/v1":
        raise OverlayError("map geometry schema is unsupported")
    config_matches_geometry = report.get("cameras_file_sha256") == config_hash
    if registration.get("schema") != "v2x-orthophoto-to-lidar-registration/v1":
        raise OverlayError("registration schema is unsupported")
    if registration.get("diagnostic_registration_passed") is not True:
        raise OverlayError("diagnostic registration did not pass its fixed gates")
    registered_ortho = registration["inputs"]["orthophoto_raster"]
    if registered_ortho.get("sha256") != orthophoto_hash:
        raise OverlayError("registration is not bound to the orthophoto")

    grid = registration["grid"]
    if image.shape[:2] != (grid["height"], grid["width"]):
        raise OverlayError("orthophoto dimensions do not match the registered grid")
    map_projection = TransverseMercator.from_proj_string(
        config["site"]["map_georeference"]
    )
    bounds = grid["bounds_xmin_ymin_xmax_ymax_m"]
    resolution_m = float(grid["resolution_m_per_pixel"])
    matrix = registration["primary"][
        "matrix_lidar_pixel_to_orthophoto_pixel"
    ]

    def projector(x, y):
        return carla_xy_to_orthophoto_pixel(
            x, y, map_projection, bounds, resolution_m, matrix
        )

    layer = image.copy()
    transformed = {"crosswalks": [], "lanes": [], "objects": []}
    for item in report["geometry"]["lanes"]:
        lane = {"id": item["id"]}
        for key in ("center_world", "left_world", "right_world"):
            if key not in item:
                continue
            pixels = transform_polyline(item[key], projector)
            lane[key.replace("_world", "_pixels")] = pixels
            draw_polyline(layer, pixels, (0, 255, 255), 1)
        transformed["lanes"].append(lane)
    for item in report["geometry"]["crosswalks"]:
        pixels = transform_polyline(item["world"], projector)
        transformed["crosswalks"].append({"id": item["id"], "pixels": pixels})
        draw_polyline(layer, pixels, (0, 0, 255), 2, closed=True)
    for item in report["geometry"]["objects"]:
        world = item.get("world") or item.get("location")
        if not world:
            continue
        pixel, utm, _geodetic = projector(world[0], world[1])
        transformed["objects"].append(
            {"id": item["id"], "pixel": pixel, "utm_easting_northing_m": utm}
        )
        cv2.circle(layer, tuple(np.rint(pixel).astype(int)), 2, (255, 0, 255), -1)

    site = config["site"]
    site_local = map_projection.forward(float(site["lat"]), float(site["lon"]))
    site_carla_xy = [site_local[0], -site_local[1]]
    site_pixel, site_utm, _ = projector(*site_carla_xy)
    cv2.drawMarker(
        layer,
        tuple(np.rint(site_pixel).astype(int)),
        (0, 255, 0),
        cv2.MARKER_CROSS,
        13,
        2,
    )
    alpha = cv2.addWeighted(image, 0.62, layer, 0.78, 0.0)
    height, width = image.shape[:2]
    visible_crosswalks = sum(
        any(0 <= x < width and 0 <= y < height for x, y in item["pixels"])
        for item in transformed["crosswalks"]
    )
    return alpha, {
        "schema": "v2x-map-geometry-orthophoto-overlay/v1",
        "acceptance_eligible": False,
        "diagnostic_only": True,
        "inputs": {
            "map_geometry": {"path": str(geometry_path), "sha256": geometry_hash},
            "camera_config": {"path": str(config_path), "sha256": config_hash},
            "registration": {
                "path": str(registration_path),
                "sha256": registration_hash,
            },
            "orthophoto": {"path": str(orthophoto_path), "sha256": orthophoto_hash},
        },
        "coordinate_chain": (
            "CARLA (x,y) -> map (east,north)=(x,-y) -> WGS84 -> UTM10 -> "
            "LiDAR grid pixel -> diagnostic registered orthophoto pixel"
        ),
        "camera_config_binding": {
            "geometry_report_sha256": report.get("cameras_file_sha256"),
            "projection_config_sha256": config_hash,
            "same_file": config_matches_geometry,
            "use": (
                "the newer hash-bound config supplies the explicit map georeference; "
                "camera poses are not consumed"
            ),
        },
        "site_anchor": {
            "carla_xy_m": site_carla_xy,
            "utm_easting_northing_m": site_utm,
            "orthophoto_pixel": site_pixel,
        },
        "counts": {
            "crosswalks": len(transformed["crosswalks"]),
            "visible_crosswalks": visible_crosswalks,
            "lanes": len(transformed["lanes"]),
            "objects": len(transformed["objects"]),
        },
        "transformed_geometry": transformed,
        "acceptance_failures": [
            "geometry_and_projection_configs_are_different_immutable_revisions"
            if not config_matches_geometry
            else "geometry_and_projection_config_match_does_not_prove_alignment",
            "orthophoto_registration_is_diagnostic_and_has_1m_class_residual",
            "aerial_visual_review_does_not_replace_surveyed_landmark_labels",
            "this_evidence_diagnoses_map_alignment_not_camera_intrinsics_or_pose",
        ],
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("geometry", "config", "registration", "orthophoto"):
        parser.add_argument(f"--{name}", type=Path, required=True)
        parser.add_argument(f"--{name}-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    image, report = overlay(
        args.geometry,
        args.geometry_sha256,
        args.config,
        args.config_sha256,
        args.registration,
        args.registration_sha256,
        args.orthophoto,
        args.orthophoto_sha256,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    image_path = args.output_dir / "map-geometry-orthophoto-overlay.png"
    report_path = args.output_dir / "map-geometry-orthophoto-overlay.json"
    if image_path.exists() or report_path.exists():
        raise OverlayError("output already exists")
    if not cv2.imwrite(str(image_path), image):
        raise OverlayError("failed to write overlay image")
    report["output_image"] = {
        "path": str(image_path.resolve()),
        "sha256": sha256(image_path),
    }
    write_json_exclusive(report_path, report)
    print(json.dumps({
        "report": str(report_path.resolve()),
        "overlay": str(image_path.resolve()),
        "visible_crosswalks": report["counts"]["visible_crosswalks"],
        "acceptance_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
