#!/usr/bin/env python3
"""Bind legacy calibration pixels to current full-resolution camera frames.

This is a provenance and diagnostic tool, not a physical calibration solver.  It
verifies every input by content hash and Git object, extracts immutable review
patches, and measures whether each legacy pixel is still supported by a simple
road-paint/edge observation in the current image.  The report is deliberately
acceptance-ineligible until landmark identities and surveyed global coordinates
exist independently of the legacy calibration calculation.
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit, urlunsplit

import cv2
import numpy as np


CSV_COLUMNS = {
    "Point_ID",
    "u_pixel",
    "v_pixel",
    "True_X_m",
    "True_Z_m",
    "Pred_X_m",
    "Pred_Z_m",
    "Error_X_m",
    "Error_Z_m",
    "Total_Error_m",
}


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


def git_output(repository, *args):
    return subprocess.check_output(
        ["git", "-C", str(repository), *args], text=True
    ).strip()


def bind_repository_input(repository, path):
    repository = Path(repository).resolve()
    path = Path(path).resolve()
    try:
        relative = path.relative_to(repository).as_posix()
    except ValueError as exc:
        raise ValueError(f"{path} is outside repository {repository}") from exc
    try:
        tracked = git_output(repository, "ls-files", "--error-unmatch", "--", relative)
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"{relative} is not tracked by {repository}") from exc
    if tracked != relative:
        raise ValueError(f"unexpected tracked path response for {relative}")
    head_blob = git_output(repository, "rev-parse", f"HEAD:{relative}")
    working_blob = git_output(repository, "hash-object", "--", relative)
    dirty = git_output(repository, "status", "--porcelain=v1", "--", relative)
    if dirty or head_blob != working_blob:
        raise ValueError(f"{relative} differs from repository HEAD")
    return {
        "relative_path": relative,
        "sha256": sha256(path),
        "git_blob_at_head": head_blob,
        "clean_at_head": True,
    }


def load_frame_manifest(path):
    path = Path(path).resolve()
    manifest = json.loads(path.read_text())
    if manifest.get("schema") != "v2x-diagnostic-fullres-static-frames/v1":
        raise ValueError("unsupported frame manifest schema")
    frames = manifest.get("frames")
    if set(frames or {}) != {"ch1", "ch2", "ch3", "ch4"}:
        raise ValueError("frame manifest must contain exactly ch1 through ch4")
    resolved = {}
    for camera_id, entry in sorted(frames.items()):
        frame_path = (path.parent / entry["file"]).resolve()
        try:
            frame_path.relative_to(path.parent)
        except ValueError as exc:
            raise ValueError(f"{camera_id} frame escapes manifest directory") from exc
        data = np.frombuffer(frame_path.read_bytes(), dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"{camera_id} is not a decodable image")
        height, width = image.shape[:2]
        if [width, height] != [int(entry["width"]), int(entry["height"])]:
            raise ValueError(f"{camera_id} dimensions disagree with manifest")
        digest = sha256(frame_path)
        if digest != entry["sha256"]:
            raise ValueError(f"{camera_id} hash disagrees with manifest")
        resolved[camera_id] = {
            "path": frame_path,
            "image": image,
            "manifest_entry": entry,
        }
    return manifest, resolved


def parse_calibration_csv(path, width, height):
    with Path(path).open(newline="") as stream:
        reader = csv.DictReader(stream)
        if set(reader.fieldnames or ()) != CSV_COLUMNS:
            raise ValueError(f"{path}: unsupported calibration CSV columns")
        rows = list(reader)
    if len(rows) < 3:
        raise ValueError(f"{path}: fewer than three calibration points")
    points = []
    for expected_id, row in enumerate(rows, start=1):
        if int(row["Point_ID"]) != expected_id:
            raise ValueError(f"{path}: Point_ID sequence is not contiguous")
        values = {key: float(row[key]) for key in CSV_COLUMNS - {"Point_ID"}}
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError(f"{path}: non-finite calibration value")
        u, v = values["u_pixel"], values["v_pixel"]
        if not (0 <= u < width and 0 <= v < height):
            raise ValueError(f"{path}: point {expected_id} lies outside its frame")
        points.append({
            "point_id": expected_id,
            "u_pixel": u,
            "v_pixel": v,
            "legacy_local_x_m": values["True_X_m"],
            "legacy_local_z_m": values["True_Z_m"],
        })
    return points


def nearest_mask_measurement(mask, center_x, center_y):
    coordinates = np.column_stack(np.nonzero(mask))
    if not len(coordinates):
        return None
    deltas = coordinates - np.asarray([center_y, center_x])
    squared = np.sum(deltas.astype(float) ** 2, axis=1)
    index = int(np.argmin(squared))
    nearest_y, nearest_x = coordinates[index]
    return {
        "distance_px": float(math.sqrt(squared[index])),
        "vector_dx_dy_px": [
            int(nearest_x) - int(center_x), int(nearest_y) - int(center_y)
        ],
    }


def sanitize_remote_url(url):
    """Remove embedded credentials while retaining repository provenance."""
    parsed = urlsplit(url)
    if parsed.username is None and parsed.password is None:
        return url
    hostname = parsed.hostname or ""
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, parsed.query, parsed.fragment))


def analyze_landmark(image, u, v, radius=80):
    """Return deliberately simple, inspectable road-paint support metrics."""
    height, width = image.shape[:2]
    center_x, center_y = int(round(u)), int(round(v))
    left, right = max(0, center_x - radius), min(width, center_x + radius + 1)
    top, bottom = max(0, center_y - radius), min(height, center_y + radius + 1)
    crop = image[top:bottom, left:right]
    local_x, local_y = center_x - left, center_y - top
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    yellow = (hue >= 8) & (hue <= 42) & (saturation >= 70) & (value >= 70)
    white = (saturation <= 65) & (value >= 155)
    paint = yellow | white
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 60, 160) > 0
    paint_measurement = nearest_mask_measurement(paint, local_x, local_y)
    yellow_measurement = nearest_mask_measurement(yellow, local_x, local_y)
    white_measurement = nearest_mask_measurement(white, local_x, local_y)
    edge_measurement = nearest_mask_measurement(edges, local_x, local_y)
    paint_distance = (
        paint_measurement["distance_px"] if paint_measurement is not None else None
    )
    edge_distance = (
        edge_measurement["distance_px"] if edge_measurement is not None else None
    )
    # A road-marking centre may be several pixels from either painted boundary.
    supported = bool(
        paint_distance is not None
        and paint_distance <= 8.0
        and edge_distance is not None
        and edge_distance <= 20.0
    )
    return {
        "crop_bounds_ltrb": [left, top, right, bottom],
        "paint_distance_px": paint_distance,
        "paint_nearest_vector_dx_dy_px": (
            paint_measurement["vector_dx_dy_px"]
            if paint_measurement is not None else None
        ),
        "yellow_distance_px": (
            yellow_measurement["distance_px"]
            if yellow_measurement is not None else None
        ),
        "yellow_nearest_vector_dx_dy_px": (
            yellow_measurement["vector_dx_dy_px"]
            if yellow_measurement is not None else None
        ),
        "white_distance_px": (
            white_measurement["distance_px"]
            if white_measurement is not None else None
        ),
        "white_nearest_vector_dx_dy_px": (
            white_measurement["vector_dx_dy_px"]
            if white_measurement is not None else None
        ),
        "edge_distance_px": edge_distance,
        "edge_nearest_vector_dx_dy_px": (
            edge_measurement["vector_dx_dy_px"]
            if edge_measurement is not None else None
        ),
        "paint_fraction": float(np.mean(paint)),
        "edge_fraction": float(np.mean(edges)),
        "heuristic_supported": supported,
        "heuristic": {
            "paint_hsv": {
                "yellow": "H[8,42], S>=70, V>=70",
                "white": "S<=65, V>=155",
            },
            "max_paint_distance_px": 8.0,
            "max_edge_distance_px": 20.0,
        },
    }


def render_patch(image, point, analysis, camera_id, patch_size=241):
    half = patch_size // 2
    x, y = int(round(point["u_pixel"])), int(round(point["v_pixel"]))
    padded = cv2.copyMakeBorder(
        image, half, half, half, half, cv2.BORDER_CONSTANT, value=(30, 30, 30)
    )
    patch = padded[y:y + patch_size, x:x + patch_size].copy()
    color = (40, 220, 40) if analysis["heuristic_supported"] else (30, 30, 240)
    cv2.drawMarker(
        patch, (half, half), color, cv2.MARKER_CROSS, 26, 2, cv2.LINE_AA
    )
    paint_text = (
        "none" if analysis["paint_distance_px"] is None
        else f"{analysis['paint_distance_px']:.1f}"
    )
    label = f"{camera_id} p{point['point_id']} paint={paint_text}px"
    cv2.putText(
        patch, label, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.52,
        (0, 0, 0), 3, cv2.LINE_AA,
    )
    cv2.putText(
        patch, label, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.52,
        (255, 255, 255), 1, cv2.LINE_AA,
    )
    return patch


def write_image_exclusive(path, image):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(path)
    success, encoded = cv2.imencode(path.suffix, image)
    if not success:
        raise ValueError(f"failed to encode {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(encoded.tobytes())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def make_contact_sheet(patches, columns=4):
    if not patches:
        raise ValueError("no landmark patches")
    height, width = patches[0].shape[:2]
    rows = math.ceil(len(patches) / columns)
    sheet = np.full((rows * height, columns * width, 3), 24, dtype=np.uint8)
    for index, patch in enumerate(patches):
        row, column = divmod(index, columns)
        sheet[row * height:(row + 1) * height, column * width:(column + 1) * width] = patch
    return sheet


def build_report(repository, frame_manifest_path, csv_paths, output_directory):
    repository = Path(repository).resolve()
    frame_manifest_path = Path(frame_manifest_path).resolve()
    output_directory = Path(output_directory).resolve()
    manifest, frames = load_frame_manifest(frame_manifest_path)
    by_camera = {}
    for csv_path in csv_paths:
        csv_path = Path(csv_path).resolve()
        camera_id = csv_path.name.removesuffix("_calibration_errors.csv")
        if camera_id not in frames or camera_id in by_camera:
            raise ValueError(f"unexpected or duplicate calibration CSV {csv_path.name}")
        by_camera[camera_id] = csv_path
    if set(by_camera) != set(frames):
        raise ValueError("exactly one calibration CSV is required for each camera")

    repository_status = git_output(repository, "status", "--porcelain=v1")
    patches = []
    camera_reports = {}
    for camera_id in sorted(frames):
        frame = frames[camera_id]
        image = frame["image"]
        height, width = image.shape[:2]
        points = parse_calibration_csv(by_camera[camera_id], width, height)
        reports = []
        for point in points:
            analysis = analyze_landmark(image, point["u_pixel"], point["v_pixel"])
            patch = render_patch(image, point, analysis, camera_id)
            patch_name = f"{camera_id}-point-{point['point_id']:02d}.png"
            patch_path = output_directory / "patches" / patch_name
            write_image_exclusive(patch_path, patch)
            patches.append(patch)
            reports.append({
                **point,
                **analysis,
                "review_patch": str(patch_path),
                "review_patch_sha256": sha256(patch_path),
            })
        camera_reports[camera_id] = {
            "frame": {
                "path": str(frame["path"]),
                "sha256": sha256(frame["path"]),
                "width": width,
                "height": height,
                "receipt_time_utc": frame["manifest_entry"]["receipt_time_utc"],
                "stream": frame["manifest_entry"]["stream"],
            },
            "legacy_csv": bind_repository_input(repository, by_camera[camera_id]),
            "landmarks": reports,
            "heuristic_supported_count": sum(
                point["heuristic_supported"] for point in reports
            ),
            "point_count": len(reports),
        }

    sheet_path = output_directory / "legacy-landmark-review-sheet.png"
    write_image_exclusive(sheet_path, make_contact_sheet(patches))
    total = sum(camera["point_count"] for camera in camera_reports.values())
    supported = sum(
        camera["heuristic_supported_count"] for camera in camera_reports.values()
    )
    return {
        "schema": "v2x-legacy-landmark-frame-rebind/v1",
        "generated_at": utc_now(),
        "acceptance_eligible": False,
        "diagnostic_rebind": {
            "all_points_heuristically_supported": supported == total,
            "supported_count": supported,
            "point_count": total,
        },
        "legacy_repository": {
            "path": str(repository),
            "commit": git_output(repository, "rev-parse", "HEAD"),
            "remote_origin": sanitize_remote_url(
                git_output(repository, "remote", "get-url", "origin")
            ),
            "working_tree_clean": not bool(repository_status),
        },
        "frame_manifest": {
            "path": str(frame_manifest_path),
            "sha256": sha256(frame_manifest_path),
            "schema": manifest["schema"],
            "acceptance_eligible": bool(manifest.get("acceptance_eligible", False)),
        },
        "cameras": camera_reports,
        "review_sheet": {
            "path": str(sheet_path),
            "sha256": sha256(sheet_path),
        },
        "acceptance_failures": [
            "legacy_pixels_have_no_source_frame_hash",
            "landmark_physical_identities_are_not_recorded",
            "legacy_coordinates_are_camera_local_not_surveyed_global_truth",
            "road_paint_support_is_a_color_edge_heuristic_not_manual_identity_proof",
            "single_current_frame_per_camera_does_not_prove_temporal_stability",
            "no_independent_held_out_correspondences",
            "no_measured_intrinsics_artifact",
        ],
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--frame-manifest", type=Path, required=True)
    parser.add_argument("--calibration-csv", type=Path, action="append", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    output = args.output.resolve()
    output_directory = args.output_directory.resolve()
    if output.parent != output_directory:
        raise ValueError("--output must be directly inside --output-directory")
    report = build_report(
        args.repository,
        args.frame_manifest,
        args.calibration_csv,
        output_directory,
    )
    write_json_exclusive(output, report)
    print(json.dumps({
        "output": str(output),
        "supported": report["diagnostic_rebind"]["supported_count"],
        "total": report["diagnostic_rebind"]["point_count"],
        "acceptance_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
