#!/usr/bin/env python3
"""Propose spatially distributed real/twin feature matches for manual review.

This tool is deliberately incapable of producing acceptance annotations.  It
emits matcher proposals without train/holdout splits, semantic identities, or
world truth.  A human/survey workflow must independently verify and rewrite
accepted landmarks using the strict schema consumed by
``build_twin_calibration_manifest.py``.
"""

import argparse
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError


CAMERAS = {"ch1", "ch2", "ch3", "ch4"}
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_DECODED_DIMENSION = 8192
MAX_DECODED_PIXELS = 40_000_000


class ProposalError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def read_gray_image(path, label):
    source = Path(path)
    if not source.is_file():
        raise ProposalError(f"{label} frame is not a regular file")
    size = source.stat().st_size
    if not 1 <= size <= MAX_IMAGE_BYTES:
        raise ProposalError(f"{label} frame size is invalid")
    payload = source.read_bytes()
    if len(payload) != size:
        raise ProposalError(f"{label} frame size is invalid")
    try:
        with Image.open(BytesIO(payload)) as header:
            header_width, header_height = (int(value) for value in header.size)
    except (OSError, UnidentifiedImageError) as exc:
        raise ProposalError(f"{label} frame is not a usable image") from exc
    if (
        min(header_width, header_height) < 64
        or max(header_width, header_height) > MAX_DECODED_DIMENSION
        or header_width * header_height > MAX_DECODED_PIXELS
    ):
        raise ProposalError(f"{label} decoded dimensions are invalid")
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if (
        image is None
        or image.ndim != 2
        or tuple(reversed(image.shape)) != (header_width, header_height)
    ):
        raise ProposalError(f"{label} frame is not a usable image")
    height, width = (int(value) for value in image.shape)
    return {
        "path": str(source.resolve()),
        "bytes": payload,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "width": width,
        "height": height,
        "image": image,
    }


def _ratio_matches(left, right, ratio):
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    accepted = {}
    for neighbors in matcher.knnMatch(left, right, k=2):
        if len(neighbors) != 2:
            continue
        best, second = neighbors
        if second.distance <= 0.0 or best.distance >= ratio * second.distance:
            continue
        accepted[int(best.queryIdx)] = {
            "train_index": int(best.trainIdx),
            "distance": float(best.distance),
            "ratio": float(best.distance / second.distance),
        }
    return accepted


def detect_mutual_sift_matches(real, twin, ratio=0.72, ransac_px=4.0):
    if not 0.5 <= float(ratio) < 1.0:
        raise ProposalError("SIFT ratio must be in [0.5, 1.0)")
    if not 0.5 <= float(ransac_px) <= 20.0:
        raise ProposalError("RANSAC threshold must be in [0.5, 20] px")
    if not hasattr(cv2, "SIFT_create"):
        raise ProposalError("OpenCV SIFT support is unavailable")
    sift = cv2.SIFT_create(nfeatures=5000, contrastThreshold=0.02)
    real_keys, real_desc = sift.detectAndCompute(real, None)
    twin_keys, twin_desc = sift.detectAndCompute(twin, None)
    if real_desc is None or twin_desc is None:
        raise ProposalError("SIFT found no usable descriptors in one frame")
    forward = _ratio_matches(real_desc, twin_desc, float(ratio))
    reverse = _ratio_matches(twin_desc, real_desc, float(ratio))
    matches = []
    for real_index, candidate in forward.items():
        twin_index = candidate["train_index"]
        reciprocal = reverse.get(twin_index)
        if reciprocal is None or reciprocal["train_index"] != real_index:
            continue
        real_point = real_keys[real_index]
        twin_point = twin_keys[twin_index]
        matches.append({
            "real_pixel": [float(value) for value in real_point.pt],
            "twin_pixel": [float(value) for value in twin_point.pt],
            "descriptor_distance": candidate["distance"],
            "ratio": max(candidate["ratio"], reciprocal["ratio"]),
            "real_response": float(real_point.response),
            "twin_response": float(twin_point.response),
            "homography_inlier": None,
        })
    homography = None
    if len(matches) >= 4:
        source = np.float32([item["real_pixel"] for item in matches]).reshape(-1, 1, 2)
        target = np.float32([item["twin_pixel"] for item in matches]).reshape(-1, 1, 2)
        matrix, mask = cv2.findHomography(
            source, target, cv2.RANSAC, float(ransac_px)
        )
        if matrix is not None and mask is not None and np.isfinite(matrix).all():
            flags = mask.reshape(-1).astype(bool).tolist()
            for item, inlier in zip(matches, flags):
                item["homography_inlier"] = bool(inlier)
            homography = {
                "matrix": [[float(value) for value in row] for row in matrix],
                "inliers": int(sum(flags)),
                "total": len(flags),
                "warning": (
                    "Homography is diagnostic only; the scene is non-planar and "
                    "this flag cannot certify a correspondence."
                ),
            }
    return matches, {
        "real_keypoints": len(real_keys),
        "twin_keypoints": len(twin_keys),
        "forward_ratio_matches": len(forward),
        "reverse_ratio_matches": len(reverse),
        "mutual_matches": len(matches),
        "homography": homography,
    }


def _normalized_distance(left, right, width, height):
    return math.hypot(
        (left[0] - right[0]) / float(width),
        (left[1] - right[1]) / float(height),
    )


def select_spatially_distributed(
    matches,
    real_size,
    twin_size,
    maximum=48,
    grid_columns=6,
    grid_rows=4,
    minimum_separation_fraction=0.025,
):
    maximum = int(maximum)
    grid_columns, grid_rows = int(grid_columns), int(grid_rows)
    minimum_separation_fraction = float(minimum_separation_fraction)
    if not 1 <= maximum <= 500:
        raise ProposalError("maximum proposals must be between 1 and 500")
    if not 1 <= grid_columns <= 20 or not 1 <= grid_rows <= 20:
        raise ProposalError("proposal grid dimensions are invalid")
    if not 0.0 <= minimum_separation_fraction <= 0.25:
        raise ProposalError("minimum separation fraction is invalid")
    real_width, real_height = real_size
    twin_width, twin_height = twin_size
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (real_width, real_height, twin_width, twin_height)
    ):
        raise ProposalError("proposal frame dimensions are invalid")
    buckets = {}
    for item in matches:
        try:
            real_u, real_v = (float(value) for value in item["real_pixel"])
            twin_u, twin_v = (float(value) for value in item["twin_pixel"])
            numeric = (
                real_u, real_v, twin_u, twin_v,
                float(item["ratio"]), float(item["descriptor_distance"]),
                float(item["real_response"]), float(item["twin_response"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProposalError("matcher proposal is malformed") from exc
        if not all(math.isfinite(value) for value in numeric):
            raise ProposalError("matcher proposal contains non-finite data")
        if not (0.0 <= real_u < real_width and 0.0 <= real_v < real_height):
            raise ProposalError("matcher proposal lies outside the real frame")
        if not (0.0 <= twin_u < twin_width and 0.0 <= twin_v < twin_height):
            raise ProposalError("matcher proposal lies outside the twin frame")
        column = min(grid_columns - 1, int(real_u / real_width * grid_columns))
        row = min(grid_rows - 1, int(real_v / real_height * grid_rows))
        buckets.setdefault((row, column), []).append(item)
    for items in buckets.values():
        items.sort(key=lambda item: (
            item["ratio"],
            -(item["real_response"] + item["twin_response"]),
            item["descriptor_distance"],
            item["real_pixel"],
            item["twin_pixel"],
        ))
    selected = []
    while len(selected) < maximum and any(buckets.values()):
        progressed = False
        for cell in sorted(buckets):
            candidates = buckets[cell]
            while candidates:
                item = candidates.pop(0)
                separated = all(
                    _normalized_distance(
                        item["real_pixel"], other["real_pixel"],
                        real_width, real_height,
                    ) >= minimum_separation_fraction
                    and _normalized_distance(
                        item["twin_pixel"], other["twin_pixel"],
                        twin_width, twin_height,
                    ) >= minimum_separation_fraction
                    for other in selected
                )
                if separated:
                    selected.append(item)
                    progressed = True
                    break
            if len(selected) >= maximum:
                break
        if not progressed:
            break
    return selected


def coverage(points, width, height, grid_columns=6, grid_rows=4):
    if not points:
        return {"width_fraction": 0.0, "height_fraction": 0.0, "cells": 0}
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    cells = {
        (
            min(grid_columns - 1, int(x / width * grid_columns)),
            min(grid_rows - 1, int(y / height * grid_rows)),
        )
        for x, y in points
    }
    return {
        "width_fraction": float((max(xs) - min(xs)) / width),
        "height_fraction": float((max(ys) - min(ys)) / height),
        "cells": len(cells),
    }


def build_report(camera_id, real, twin, matches, diagnostics, args):
    if camera_id not in CAMERAS:
        raise ProposalError("camera ID is invalid")
    selected = select_spatially_distributed(
        matches,
        (real["width"], real["height"]),
        (twin["width"], twin["height"]),
        maximum=args.maximum,
        grid_columns=args.grid_columns,
        grid_rows=args.grid_rows,
        minimum_separation_fraction=args.minimum_separation_fraction,
    )
    proposals = []
    for index, item in enumerate(selected, 1):
        proposals.append({
            "id": f"proposal-{index:03d}",
            "provenance": "matcher_proposal_only",
            "acceptance_eligible": False,
            "real_pixel": [round(value, 3) for value in item["real_pixel"]],
            "twin_pixel": [round(value, 3) for value in item["twin_pixel"]],
            "descriptor_distance": round(item["descriptor_distance"], 6),
            "ratio": round(item["ratio"], 6),
            "homography_inlier_diagnostic": item["homography_inlier"],
            "required_manual_fields": [
                "unique_semantic_description",
                "independent_global_identity",
                "manually_verified_unique",
                "frozen_train_or_holdout_split",
            ],
        })
    real_coverage = coverage(
        [item["real_pixel"] for item in selected], real["width"], real["height"],
        int(args.grid_columns), int(args.grid_rows),
    )
    twin_coverage = coverage(
        [item["twin_pixel"] for item in selected], twin["width"], twin["height"],
        int(args.grid_columns), int(args.grid_rows),
    )
    warnings = [
        "Matcher output cannot certify landmark identity or held-out truth.",
        "Repeated lane, curb, tree, and crosswalk features require independent manual verification.",
        "Do not rename matcher_proposal_only to an accepted provenance label.",
        "Do not assign train/holdout splits until unique world identities are frozen.",
    ]
    if len(proposals) < 12:
        warnings.append("Fewer than 12 proposals; acquire a better source pair.")
    if real_coverage["width_fraction"] < 0.5 or real_coverage["height_fraction"] < 0.3:
        warnings.append("Real-frame proposals do not meet diagnostic spatial coverage.")
    if real["sha256"] == twin["sha256"]:
        raise ProposalError("real and twin frames are byte-identical")
    return {
        "schema": "v2x-calibration-annotation-proposals/v1",
        "created_at_utc": utc_now(),
        "camera_id": camera_id,
        "acceptance_eligible": False,
        "provenance": "matcher_proposal_only",
        "frames": {
            "real": {
                key: real[key] for key in ("path", "sha256", "width", "height")
            },
            "twin": {
                key: twin[key] for key in ("path", "sha256", "width", "height")
            },
        },
        "algorithm": {
            "feature": "opencv_sift",
            "matching": "mutual_lowe_ratio",
            "ratio": float(args.ratio),
            "ransac_px": float(args.ransac_px),
            "grid": [int(args.grid_columns), int(args.grid_rows)],
            "minimum_separation_fraction": float(args.minimum_separation_fraction),
        },
        "diagnostics": {
            **diagnostics,
            "selected_proposals": len(proposals),
            "real_coverage": real_coverage,
            "twin_coverage": twin_coverage,
        },
        "proposals": proposals,
        "warnings": warnings,
        "conversion_policy": {
            "automatic_conversion_allowed": False,
            "accepted_point_provenance": "manually_verified_unique",
            "accepted_polyline_provenance": "manually_traced_geometry",
            "requires_surveyed_or_depth_revalidated_world_truth": True,
            "requires_untouched_holdouts": True,
        },
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", required=True, choices=sorted(CAMERAS))
    parser.add_argument("--real-frame", required=True)
    parser.add_argument("--twin-frame", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--maximum", type=int, default=48)
    parser.add_argument("--ratio", type=float, default=0.72)
    parser.add_argument("--ransac-px", type=float, default=4.0)
    parser.add_argument("--grid-columns", type=int, default=6)
    parser.add_argument("--grid-rows", type=int, default=4)
    parser.add_argument("--minimum-separation-fraction", type=float, default=0.025)
    return parser.parse_args(argv)


def write_report_exclusive(path, report):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ProposalError("output already exists") from exc
    return output


def main(argv=None):
    args = parse_args(argv)
    try:
        output = Path(args.output)
        real = read_gray_image(args.real_frame, "real")
        twin = read_gray_image(args.twin_frame, "twin")
        matches, diagnostics = detect_mutual_sift_matches(
            real["image"], twin["image"], args.ratio, args.ransac_px
        )
        report = build_report(args.camera, real, twin, matches, diagnostics, args)
        write_report_exclusive(output, report)
        print(output)
        return 0
    except (OSError, ValueError, ProposalError) as exc:
        print(f"proposal generation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
