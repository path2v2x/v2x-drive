#!/usr/bin/env python3
"""Rank retained inverse-render candidates by independent SIFT structure."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path

from propose_twin_calibration_annotations import (
    ProposalError,
    coverage,
    detect_mutual_sift_matches,
    read_gray_image,
)


INPUT_SCHEMA = "v2x-diagnostic-inverse-render-search/v1"
OUTPUT_SCHEMA = "v2x-diagnostic-inverse-render-structural-ranking/v1"


class RankingError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def structural_score(diagnostics: dict, matches: list[dict], width: int, height: int) -> dict:
    homography = diagnostics.get("homography")
    inliers = int(homography.get("inliers", 0)) if isinstance(homography, dict) else 0
    mutual = int(diagnostics.get("mutual_matches", 0))
    inlier_points = [
        item["real_pixel"] for item in matches if item.get("homography_inlier") is True
    ]
    spread = coverage(inlier_points, width, height)
    inlier_ratio = 0.0 if mutual == 0 else inliers / mutual
    score = (
        8.0 * inliers
        + 2.0 * mutual
        + 5.0 * int(spread["cells"])
        + 20.0 * float(spread["width_fraction"])
        + 20.0 * float(spread["height_fraction"])
    )
    if inliers < 4 or inlier_ratio < 0.20:
        score *= 0.25
    return {
        "score": float(score),
        "homography_inliers": inliers,
        "mutual_matches": mutual,
        "homography_inlier_ratio": float(inlier_ratio),
        "inlier_coverage": spread,
    }


def rank(report_path: Path, real_frame: Path, ratio: float, ransac_px: float) -> dict:
    report_path = report_path.resolve()
    real_frame = real_frame.resolve()
    try:
        report = json.loads(report_path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise RankingError("inverse-render report is unreadable or invalid") from exc
    if report.get("schema") != INPUT_SCHEMA or report.get("acceptance_eligible") is not False:
        raise RankingError("inverse-render report contract is unsupported")
    expected_real_hash = report.get("fit_target", {}).get("sha256")
    if not real_frame.is_file() or sha256(real_frame) != expected_real_hash:
        raise RankingError("real frame does not match the report fit target")
    real = read_gray_image(real_frame, "real")
    output = []
    report_root = report_path.parent
    for evaluation in report.get("evaluations", []):
        index = evaluation.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise RankingError("inverse-render candidate index is invalid")
        image_path = report_root / "renders" / f"candidate-{index:04d}" / "rgb.png"
        twin = read_gray_image(image_path, "twin")
        try:
            matches, diagnostics = detect_mutual_sift_matches(
                real["image"], twin["image"], ratio=ratio, ransac_px=ransac_px
            )
            score = structural_score(
                diagnostics, matches, real["width"], real["height"]
            )
            error = None
        except ProposalError as exc:
            diagnostics = {"mutual_matches": 0, "homography": None}
            score = structural_score(
                diagnostics, [], real["width"], real["height"]
            )
            error = str(exc)
        output.append(
            {
                "index": index,
                "image": {"path": str(image_path), "sha256": twin["sha256"]},
                "twin_pose": evaluation.get("twin_pose"),
                "paint_objective": evaluation.get("combined_objective"),
                "road_surface_fraction": evaluation.get("road_surface_fraction"),
                "near_occlusion_fraction": evaluation.get("near_occlusion_fraction"),
                "structure": score,
                "matcher_error": error,
            }
        )
    if not output:
        raise RankingError("inverse-render report has no candidates")
    output.sort(
        key=lambda item: (
            -item["structure"]["score"],
            float(item["paint_objective"]),
            item["index"],
        )
    )
    return {
        "schema": OUTPUT_SCHEMA,
        "generated_at_utc": utc_now(),
        "acceptance_eligible": False,
        "camera_id": report.get("camera_id"),
        "source_report": {"path": str(report_path), "sha256": sha256(report_path)},
        "real_frame": {"path": str(real_frame), "sha256": real["sha256"]},
        "matcher": {"method": "mutual_sift_ransac_diagnostic", "ratio": ratio, "ransac_px": ransac_px},
        "ranked": output,
        "limitations": [
            "feature_matches_are_domain_shifted_proposals_not_landmark_truth",
            "ranking_cannot_replace_manual_or_heldout_geometry_gates",
        ],
    }


def write_exclusive(path: Path, value: dict) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--real-frame", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ratio", type=float, default=0.80)
    parser.add_argument("--ransac-px", type=float, default=6.0)
    args = parser.parse_args()
    if not 0.5 <= args.ratio < 1.0 or not 0.5 <= args.ransac_px <= 20.0:
        parser.error("matcher thresholds are invalid")
    result = rank(args.report, args.real_frame, args.ratio, args.ransac_px)
    write_exclusive(args.output, result)
    print(json.dumps({"camera_id": result["camera_id"], "candidates": len(result["ranked"]), "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
