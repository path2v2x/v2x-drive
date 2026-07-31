#!/usr/bin/env python3
"""Rasterize a hash-bound LAS/LAZ subset into a georeferenced intensity image."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path

import cv2
import numpy as np


class RasterError(RuntimeError):
    pass


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_bytes_exclusive(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_exclusive(path, value):
    write_bytes_exclusive(
        path, json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
    )


def raster_shape(bounds, resolution_m):
    xmin, ymin, xmax, ymax = bounds
    if not all(math.isfinite(value) for value in bounds):
        raise RasterError("bounds must be finite")
    if xmax <= xmin or ymax <= ymin:
        raise RasterError("bounds are not ordered")
    if not math.isfinite(resolution_m) or not 0.05 <= resolution_m <= 5.0:
        raise RasterError("resolution must be between 0.05 and 5 meters")
    width = int(math.ceil((xmax - xmin) / resolution_m))
    height = int(math.ceil((ymax - ymin) / resolution_m))
    if width * height > 25_000_000:
        raise RasterError("requested raster is too large")
    return height, width


def accumulate_maximum(grid, counts, x, y, intensity, bounds, resolution_m):
    xmin, ymin, _xmax, _ymax = bounds
    height, width = grid.shape
    columns = np.floor((x - xmin) / resolution_m).astype(np.int64)
    rows = height - 1 - np.floor((y - ymin) / resolution_m).astype(np.int64)
    inside = (
        (columns >= 0) & (columns < width) & (rows >= 0) & (rows < height)
    )
    rows, columns = rows[inside], columns[inside]
    values = np.asarray(intensity[inside], dtype=np.float64)
    np.maximum.at(grid, (rows, columns), values)
    np.add.at(counts, (rows, columns), 1)
    return int(np.count_nonzero(inside))


def render_intensity(grid, counts):
    populated = counts > 0
    if np.count_nonzero(populated) < 10:
        raise RasterError("too few lidar points in requested bounds")
    values = grid[populated]
    low, high = np.quantile(values, [0.01, 0.99])
    if not math.isfinite(low) or not math.isfinite(high) or high <= low:
        raise RasterError("lidar intensity has no usable dynamic range")
    normalized = np.zeros(grid.shape, dtype=np.uint8)
    normalized[populated] = np.clip(
        (grid[populated] - low) * (255.0 / (high - low)), 0.0, 255.0
    ).astype(np.uint8)
    # A 1 m nominal point spacing is sparse at sub-meter raster sizes.  This
    # display-only dilation exposes linear structure without changing the raw
    # counts or values retained in the manifest.
    display = cv2.dilate(normalized, np.ones((3, 3), dtype=np.uint8))
    return display, float(low), float(high)


def rasterize(input_path, expected_sha256, bounds, resolution_m,
              classifications=()):
    try:
        import laspy
    except ImportError as exc:
        raise RasterError("laspy with a LAZ backend is required") from exc
    input_path = Path(input_path).resolve()
    actual_hash = sha256(input_path)
    if actual_hash != expected_sha256:
        raise RasterError("lidar input hash does not match")
    height, width = raster_shape(bounds, resolution_m)
    grid = np.zeros((height, width), dtype=np.float64)
    counts = np.zeros((height, width), dtype=np.uint32)
    accepted = set(classifications)
    selected_points = 0
    with laspy.open(input_path) as source:
        header = source.header
        crs = header.parse_crs()
        for points in source.chunk_iterator(500_000):
            mask = np.ones(len(points), dtype=bool)
            if accepted:
                mask &= np.isin(np.asarray(points.classification), list(accepted))
            selected_points += accumulate_maximum(
                grid, counts,
                np.asarray(points.x)[mask], np.asarray(points.y)[mask],
                np.asarray(points.intensity)[mask], bounds, resolution_m,
            )
    display, low, high = render_intensity(grid, counts)
    return display, {
        "schema": "v2x-georeferenced-lidar-intensity/v1",
        "acceptance_eligible": False,
        "input": {
            "path": str(input_path),
            "sha256": actual_hash,
        },
        "source_crs": None if crs is None else crs.to_wkt(),
        "bounds_xmin_ymin_xmax_ymax_m": list(bounds),
        "resolution_m_per_pixel": resolution_m,
        "width": width,
        "height": height,
        "classifications": sorted(accepted),
        "selected_point_count": selected_points,
        "populated_pixel_count": int(np.count_nonzero(counts)),
        "intensity_display_quantiles": {"p01": low, "p99": high},
        "display_processing": "maximum_intensity_per_cell_then_3x3_dilation",
        "acceptance_failures": [
            "2010_lidar_predates_current_road_paint_and_camera_installation",
            "nominal_point_spacing_is_one_meter",
            "intensity_raster_is_registration_support_not_current_landmark_truth",
        ],
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--bounds", type=float, nargs=4, required=True)
    parser.add_argument("--resolution-m", type=float, default=0.25)
    parser.add_argument("--classification", type=int, action="append", default=[])
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--survey-metadata-url", required=True)
    parser.add_argument("--output-image", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    image, manifest = rasterize(
        args.input, args.input_sha256, args.bounds, args.resolution_m,
        args.classification,
    )
    success, encoded = cv2.imencode(".png", image)
    if not success:
        raise RasterError("failed to encode intensity raster")
    write_bytes_exclusive(args.output_image, encoded.tobytes())
    manifest["source_url"] = args.source_url
    manifest["survey_metadata_url"] = args.survey_metadata_url
    manifest["output_image"] = {
        "path": str(args.output_image.resolve()),
        "sha256": sha256(args.output_image),
    }
    write_json_exclusive(args.output_manifest, manifest)
    print(json.dumps({
        "output_image": str(args.output_image.resolve()),
        "output_manifest": str(args.output_manifest.resolve()),
        "selected_point_count": manifest["selected_point_count"],
        "acceptance_eligible": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
