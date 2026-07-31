#!/usr/bin/env python3
"""Score reviewed shared real/UE5 paint features and retain topology blockers."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import cv2
import numpy as np
from PIL import Image

from evaluate_static_inverse_render import (
    RENDER_SCHEMA,
    StaticAlignmentError,
    contour_edges,
    extract_paint_mask,
    polygon_mask,
    robust_edge_metrics,
    sha256_file,
    validate_bound_image,
    validate_polygons,
    validate_thresholds,
)


ANNOTATION_SCHEMA = "v2x-shared-static-feature-annotations/v1"
OUTPUT_SCHEMA = "v2x-static-inverse-render-score/v1"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def validate_features(values, real_size, twin_size):
    if not isinstance(values, list) or len(values) < 2:
        raise StaticAlignmentError("at least two shared static features are required")
    output = []
    seen = set()
    for value in values:
        feature_id = value.get("id") if isinstance(value, dict) else None
        if (
            not isinstance(feature_id, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", feature_id) is None
            or feature_id in seen
        ):
            raise StaticAlignmentError("shared feature ID is invalid or duplicated")
        seen.add(feature_id)
        feature_class = value.get("class")
        if feature_class not in {"crosswalk_paint", "lane_paint", "road_edge"}:
            raise StaticAlignmentError("shared feature class is unsupported")
        if value.get("provenance") != "codex_visual_review_diagnostic":
            raise StaticAlignmentError("shared feature provenance is invalid")
        real = validate_polygons(
            [value.get("real_polygon")], *real_size, f"{feature_id} real"
        )[0].astype(np.int32)
        twin = validate_polygons(
            [value.get("twin_search_polygon")],
            *twin_size,
            f"{feature_id} twin",
        )[0].astype(np.int32)
        output.append(
            {
                "id": feature_id,
                "class": feature_class,
                "real_polygon": real,
                "twin_polygon": twin,
            }
        )
    return output


def validate_topology_blockers(values):
    if not isinstance(values, list):
        raise StaticAlignmentError("topology blocker list is missing")
    output = []
    for value in values:
        if (
            not isinstance(value, dict)
            or value.get("status") != "missing_in_ue5_map"
            or not isinstance(value.get("id"), str)
            or not value["id"]
            or not isinstance(value.get("description"), str)
            or not value["description"].strip()
        ):
            raise StaticAlignmentError("topology blocker is malformed")
        output.append(dict(value))
    return output


def aggregate_metrics(feature_metrics):
    losses = np.asarray(
        [value["metrics"]["optimization_loss"] for value in feature_metrics],
        dtype=float,
    )
    p95 = np.asarray(
        [value["metrics"]["symmetric_p95_px"] for value in feature_metrics],
        dtype=float,
    )
    f1 = np.asarray(
        [value["metrics"]["tolerance_f1"] for value in feature_metrics],
        dtype=float,
    )
    return {
        "optimization_loss": float(np.mean(losses)),
        "symmetric_p95_px": float(np.max(p95)),
        "tolerance_f1": float(np.mean(f1)),
        "feature_loss_mean": float(np.mean(losses)),
        "feature_loss_max": float(np.max(losses)),
        "feature_p95_mean_px": float(np.mean(p95)),
        "feature_p95_max_px": float(np.max(p95)),
        "feature_tolerance_f1_mean": float(np.mean(f1)),
        "feature_tolerance_f1_min": float(np.min(f1)),
    }


def evaluate(annotations, render_path, output):
    if (
        annotations.get("schema") != ANNOTATION_SCHEMA
        or annotations.get("acceptance_eligible") is not False
    ):
        raise StaticAlignmentError("annotations lack the diagnostic contract")
    camera_id = annotations.get("camera_id")
    if camera_id not in {"ch1", "ch2", "ch3", "ch4"}:
        raise StaticAlignmentError("annotation camera ID is invalid")
    real_path, real_rgb = validate_bound_image(
        annotations.get("real_frame"), "real"
    )
    real_height, real_width = real_rgb.shape[:2]
    real_thresholds = validate_thresholds(
        annotations.get("real_paint_thresholds"), "real"
    )
    twin_thresholds = validate_thresholds(
        annotations.get("twin_paint_thresholds"), "twin"
    )
    blockers = validate_topology_blockers(
        annotations.get("required_topology_blockers")
    )

    render_path = Path(render_path).resolve()
    render_bytes = render_path.read_bytes()
    render = json.loads(render_bytes)
    if (
        render.get("schema") != RENDER_SCHEMA
        or render.get("acceptance_eligible") is not False
        or render.get("camera_id") != camera_id
    ):
        raise StaticAlignmentError("render metadata does not match annotations")
    rgb_binding = (render.get("files") or {}).get("rgb.png") or {}
    twin_path = render_path.parent / str(rgb_binding.get("path") or "")
    if not twin_path.is_file() or sha256_file(twin_path) != rgb_binding.get("sha256"):
        raise StaticAlignmentError("render RGB hash binding is invalid")
    with Image.open(twin_path) as image:
        twin_rgb = np.asarray(image.convert("RGB"))
    twin_height, twin_width = twin_rgb.shape[:2]
    if [twin_width, twin_height] != render.get("resolution"):
        raise StaticAlignmentError("render RGB dimensions are invalid")
    real_resized = cv2.resize(
        real_rgb, (twin_width, twin_height), interpolation=cv2.INTER_AREA
    )
    if (real_width, real_height) != (twin_width, twin_height):
        raise StaticAlignmentError(
            "shared feature annotations currently require matched resolution"
        )
    features = validate_features(
        annotations.get("features"),
        (real_width, real_height),
        (twin_width, twin_height),
    )

    output = Path(output).resolve()
    if output.exists():
        raise StaticAlignmentError("refusing to overwrite shared-feature output")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent)
    )
    try:
        real_overlay_edges = np.zeros((twin_height, twin_width), dtype=np.uint8)
        twin_overlay_edges = np.zeros_like(real_overlay_edges)
        feature_metrics = []
        files = {}
        for feature in features:
            real_region = polygon_mask(
                (twin_width, twin_height), [feature["real_polygon"]], []
            )
            twin_region = polygon_mask(
                (twin_width, twin_height), [feature["twin_polygon"]], []
            )
            real_mask = extract_paint_mask(
                real_resized, real_thresholds, real_region
            )
            twin_mask = extract_paint_mask(
                twin_rgb, twin_thresholds, twin_region
            )
            real_edges = contour_edges(real_mask)
            twin_edges = contour_edges(twin_mask)
            metrics = robust_edge_metrics(real_edges, twin_edges)
            real_overlay_edges = cv2.bitwise_or(real_overlay_edges, real_edges)
            twin_overlay_edges = cv2.bitwise_or(twin_overlay_edges, twin_edges)
            real_name = f"{feature['id']}-real-mask.png"
            twin_name = f"{feature['id']}-twin-mask.png"
            Image.fromarray(real_mask).save(temporary / real_name)
            Image.fromarray(twin_mask).save(temporary / twin_name)
            files[real_name] = {
                "path": real_name,
                "sha256": sha256_file(temporary / real_name),
            }
            files[twin_name] = {
                "path": twin_name,
                "sha256": sha256_file(temporary / twin_name),
            }
            feature_metrics.append(
                {
                    "id": feature["id"],
                    "class": feature["class"],
                    "metrics": metrics,
                }
            )
        blend = (
            0.5 * real_resized.astype(np.float32)
            + 0.5 * twin_rgb.astype(np.float32)
        ).astype(np.uint8)
        overlay = blend.copy()
        overlay[real_overlay_edges > 0] = [255, 48, 48]
        overlay[twin_overlay_edges > 0] = [48, 255, 48]
        overlap = (real_overlay_edges > 0) & (twin_overlay_edges > 0)
        overlay[overlap] = [255, 255, 0]
        overlay_name = "shared-feature-overlay.png"
        Image.fromarray(overlay).save(temporary / overlay_name)
        files[overlay_name] = {
            "path": overlay_name,
            "sha256": sha256_file(temporary / overlay_name),
        }
        report = {
            "schema": OUTPUT_SCHEMA,
            "acceptance_eligible": False,
            "created_at_utc": utc_now(),
            "camera_id": camera_id,
            "candidate_id": render.get("candidate_id"),
            "twin_pose": render.get("twin_pose"),
            "fov_deg": render.get("fov_deg"),
            "buffer_statistics": render.get("buffer_statistics"),
            "annotations_sha256": annotations["_sha256"],
            "render_sha256": hashlib.sha256(render_bytes).hexdigest(),
            "real_frame_sha256": sha256_file(real_path),
            "metrics": aggregate_metrics(feature_metrics),
            "features": feature_metrics,
            "required_topology_blockers": blockers,
            "topology_gate_passed": len(blockers) == 0,
            "files": files,
            "limitations": [
                "shared_features_are_codex_reviewed_diagnostic_regions",
                "topology_blockers_are_not_removed_from_acceptance",
                "shared_feature_fit_does_not_prove_metric_world_scale",
            ],
        }
        (temporary / "score.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output / "score.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("annotations")
    parser.add_argument("render_json")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    annotation_path = Path(args.annotations).resolve()
    raw = annotation_path.read_bytes()
    annotations = json.loads(raw)
    annotations["_sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        result = evaluate(annotations, args.render_json, args.output_dir)
    except (OSError, ValueError, StaticAlignmentError) as exc:
        raise SystemExit(str(exc)) from exc
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
