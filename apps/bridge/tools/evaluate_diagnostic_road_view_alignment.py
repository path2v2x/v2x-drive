#!/usr/bin/env python3
"""Compare reviewed real/twin road traces without claiming calibration truth.

The input binds every image by SHA-256 and supplies semantic polylines traced
in the real frame and each candidate render.  The evaluator reports symmetric
polyline distances and the intersection of two designated road-edge traces.
It is intentionally diagnostic-only: image-to-image agreement cannot prove
the physical camera pose when the UE5 map geometry or occluders are wrong.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np
from PIL import Image


SCHEMA = "v2x-diagnostic-road-view-annotations/v1"
OUTPUT_SCHEMA = "v2x-diagnostic-road-view-alignment/v1"


class AlignmentError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_frame(value, label):
    if not isinstance(value, dict):
        raise AlignmentError(f"{label} frame is missing")
    path = Path(value.get("path", "")).resolve()
    if not path.is_file():
        raise AlignmentError(f"{label} frame is not a regular file")
    digest = sha256(path)
    if value.get("sha256") != digest:
        raise AlignmentError(f"{label} frame hash mismatch")
    with Image.open(path) as image:
        width, height = (int(item) for item in image.size)
    if [width, height] != value.get("size"):
        raise AlignmentError(f"{label} frame dimensions mismatch")
    if width < 64 or height < 64 or width > 8192 or height > 8192:
        raise AlignmentError(f"{label} frame dimensions are invalid")
    return {"path": str(path), "sha256": digest, "size": [width, height]}


def validate_features(values, size, label):
    if not isinstance(values, list) or len(values) < 2:
        raise AlignmentError(f"{label} needs at least two semantic traces")
    width, height = size
    output = {}
    for feature in values:
        if not isinstance(feature, dict):
            raise AlignmentError(f"{label} trace is malformed")
        feature_id = feature.get("id")
        if not isinstance(feature_id, str) or not feature_id.strip() or feature_id in output:
            raise AlignmentError(f"{label} trace ID is invalid or duplicated")
        if feature.get("provenance") not in {
            "codex_visual_review_diagnostic",
            "named_human_review",
        }:
            raise AlignmentError(f"{label}:{feature_id} provenance is unsupported")
        try:
            uncertainty = float(feature["uncertainty_px"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AlignmentError(
                f"{label}:{feature_id} annotation uncertainty is missing"
            ) from exc
        if not math.isfinite(uncertainty) or not 0.25 <= uncertainty <= 25.0:
            raise AlignmentError(
                f"{label}:{feature_id} annotation uncertainty is invalid"
            )
        reviewer = str(feature.get("reviewer") or "").strip()
        if feature["provenance"] == "named_human_review" and not reviewer:
            raise AlignmentError(f"{label}:{feature_id} named reviewer is missing")
        try:
            points = np.asarray(feature["polyline"], dtype=float)
        except (KeyError, TypeError, ValueError) as exc:
            raise AlignmentError(f"{label}:{feature_id} polyline is malformed") from exc
        if (
            points.ndim != 2
            or points.shape[1] != 2
            or len(points) < 2
            or not np.isfinite(points).all()
            or np.any(points[:, 0] < 0)
            or np.any(points[:, 0] >= width)
            or np.any(points[:, 1] < 0)
            or np.any(points[:, 1] >= height)
        ):
            raise AlignmentError(f"{label}:{feature_id} polyline is invalid")
        if float(np.sum(np.linalg.norm(np.diff(points, axis=0), axis=1))) < 20.0:
            raise AlignmentError(f"{label}:{feature_id} trace is too short")
        output[feature_id] = {
            "points": points,
            "provenance": feature["provenance"],
            "reviewer": reviewer or None,
            "uncertainty_px": uncertainty,
            "description": str(feature.get("description") or "").strip(),
        }
    return output


def resample_polyline(points, spacing_px=1.0):
    samples = []
    for left, right in zip(points[:-1], points[1:]):
        length = float(np.linalg.norm(right - left))
        count = max(2, int(math.ceil(length / spacing_px)) + 1)
        segment = np.linspace(left, right, count, endpoint=True)
        if samples:
            segment = segment[1:]
        samples.extend(segment)
    return np.asarray(samples, dtype=float)


def nearest_distances(source, target):
    values = []
    for start in range(0, len(source), 1024):
        delta = source[start:start + 1024, None, :] - target[None, :, :]
        values.extend(np.sqrt(np.min(np.sum(delta * delta, axis=2), axis=1)))
    return np.asarray(values, dtype=float)


def trace_metrics(real, candidate):
    left = resample_polyline(real)
    right = resample_polyline(candidate)
    distances = np.concatenate((nearest_distances(left, right), nearest_distances(right, left)))
    return {
        "symmetric_mean_px": float(np.mean(distances)),
        "symmetric_rmse_px": float(math.sqrt(np.mean(distances**2))),
        "symmetric_p95_px": float(np.quantile(distances, 0.95)),
        "symmetric_max_px": float(np.max(distances)),
        "real_samples": int(len(left)),
        "candidate_samples": int(len(right)),
    }


def fitted_line(points):
    homogeneous = np.column_stack((points, np.ones(len(points))))
    _u, _s, vectors = np.linalg.svd(homogeneous)
    line = vectors[-1]
    norm = float(np.linalg.norm(line[:2]))
    if norm <= 1e-12:
        raise AlignmentError("road trace does not define a line")
    return line / norm


def vanishing_point(features, edge_ids):
    if not isinstance(edge_ids, list) or len(edge_ids) != 2 or edge_ids[0] == edge_ids[1]:
        raise AlignmentError("vanishing_edge_ids must contain two distinct trace IDs")
    try:
        lines = [fitted_line(features[feature_id]["points"]) for feature_id in edge_ids]
    except KeyError as exc:
        raise AlignmentError("vanishing edge trace is missing") from exc
    sine = abs(float(np.linalg.det(np.asarray([line[:2] for line in lines]))))
    if sine < 0.01:
        return {
            "valid": False,
            "reason": "edge_intersection_ill_conditioned",
            "line_normal_sine": sine,
        }
    intersection = np.cross(lines[0], lines[1])
    if abs(float(intersection[2])) < 1e-9:
        return {
            "valid": False,
            "reason": "edge_intersection_at_infinity",
            "line_normal_sine": sine,
        }
    point = intersection[:2] / intersection[2]
    return {
        "valid": bool(np.isfinite(point).all()),
        "pixel": [float(value) for value in point],
        "line_normal_sine": sine,
    }


def evaluate(annotations):
    if annotations.get("schema") != SCHEMA or annotations.get("acceptance_eligible") is not False:
        raise AlignmentError("annotations do not have the diagnostic contract")
    camera = annotations.get("camera")
    if camera not in {"ch1", "ch2", "ch3", "ch4"}:
        raise AlignmentError("camera is invalid")
    real_frame = validate_frame(annotations.get("real_frame"), "real")
    real = validate_features(annotations.get("real_features"), real_frame["size"], "real")
    candidates = annotations.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise AlignmentError("candidate list is empty")
    edge_ids = annotations.get("vanishing_edge_ids")
    real_vp = vanishing_point(real, edge_ids)
    results = []
    seen = set()
    for candidate in candidates:
        candidate_id = candidate.get("id") if isinstance(candidate, dict) else None
        if not isinstance(candidate_id, str) or not candidate_id.strip() or candidate_id in seen:
            raise AlignmentError("candidate ID is invalid or duplicated")
        seen.add(candidate_id)
        frame = validate_frame(candidate.get("frame"), f"candidate:{candidate_id}")
        if frame["size"] != real_frame["size"]:
            raise AlignmentError(f"candidate:{candidate_id} dimensions differ from real")
        features = validate_features(
            candidate.get("features"), frame["size"], f"candidate:{candidate_id}"
        )
        if set(features) != set(real):
            raise AlignmentError(f"candidate:{candidate_id} semantic trace IDs differ")
        metrics = {
            feature_id: trace_metrics(
                real[feature_id]["points"], features[feature_id]["points"]
            )
            for feature_id in sorted(real)
        }
        all_mean = np.asarray([value["symmetric_mean_px"] for value in metrics.values()])
        candidate_vp = vanishing_point(features, edge_ids)
        vp_error = None
        if real_vp.get("valid") and candidate_vp.get("valid"):
            vp_error = float(
                np.linalg.norm(
                    np.asarray(real_vp["pixel"])
                    - np.asarray(candidate_vp["pixel"])
                )
            )
        # Vanishing-point error is reported separately: distant intersections
        # are numerically sensitive and must not dominate trace agreement.
        results.append({
            "id": candidate_id,
            "frame": frame,
            "features": metrics,
            "mean_trace_distance_px": float(np.mean(all_mean)),
            "vanishing_point": candidate_vp,
            "vanishing_point_error_px": vp_error,
        })
    results.sort(key=lambda item: (item["mean_trace_distance_px"], item["id"]))
    return {
        "schema": OUTPUT_SCHEMA,
        "created_at_utc": utc_now(),
        "acceptance_eligible": False,
        "camera": camera,
        "annotations_sha256": annotations["_sha256"],
        "real_frame": real_frame,
        "real_vanishing_point": real_vp,
        "ranking": [item["id"] for item in results],
        "candidates": results,
        "limitations": [
            "traces_are_visual_diagnostic_annotations_not_surveyed_world_truth",
            "same_frame_candidate_ranking_is_not_an_independent_holdout",
            "agreement_can_be_limited_by_or_overfit_to_ue5_map_geometry",
            "occluder_alignment_is_not_scored",
            "physical_intrinsics_and_distortion_remain_unmeasured",
        ],
    }


def write_exclusive(path, report):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise AlignmentError("refusing to overwrite output") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("annotations")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        payload = Path(args.annotations).read_bytes()
        annotations = json.loads(payload)
        annotations["_sha256"] = hashlib.sha256(payload).hexdigest()
        report = evaluate(annotations)
        write_exclusive(args.output, report)
    except (OSError, json.JSONDecodeError, AlignmentError) as exc:
        parser.error(str(exc))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
