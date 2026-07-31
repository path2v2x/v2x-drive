import hashlib
import json
import math
from pathlib import Path
import sys

import cv2
import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from estimate_vanishing_intrinsics import (  # noqa: E402
    VanishingCalibrationError,
    evaluate_pair,
    fit_vanishing_point,
    focal_from_orthogonal_vanishing_points,
    main,
    normalized_line,
)


def physical_lines(vanishing_point, anchors, split_frames):
    values = []
    vanishing = np.asarray(vanishing_point, dtype=float)
    for index, (anchor, split, frame_id) in enumerate(zip(anchors, *split_frames)):
        anchor = np.asarray(anchor, dtype=float)
        toward = anchor + 0.25 * (vanishing - anchor)
        values.append({
            "id": f"line-{frame_id}-{index}",
            "physical_line_id": f"paint-{frame_id}-{index}",
            "frame_id": frame_id,
            "endpoints": [anchor.tolist(), toward.tolist()],
            "uncertainty_px": 1.0,
            "split": split,
        })
    return values


def synthetic_pair():
    width, height = 1280, 960
    centre = np.asarray([640.0, 480.0])
    focal = 800.0
    left = np.asarray([100.0, 200.0])
    left_delta = left - centre
    right_x = 1640.0
    right_y = centre[1] + (
        -focal ** 2 - left_delta[0] * (right_x - centre[0])
    ) / left_delta[1]
    right = np.asarray([right_x, right_y])
    splits = (["fit"] * 3 + ["holdout"] * 2, ["fit-a"] * 3 + ["hold-a"] * 2)
    left_lines = physical_lines(
        left,
        [(300, 300), (450, 350), (600, 310), (250, 420), (500, 440)],
        splits,
    )
    splits = (["fit"] * 3 + ["holdout"] * 2, ["fit-b"] * 3 + ["hold-b"] * 2)
    right_lines = physical_lines(
        right,
        [(800, 300), (900, 400), (1000, 500), (760, 600), (880, 700)],
        splits,
    )
    return left_lines, right_lines, centre, width, height, focal


def test_focal_from_orthogonal_vanishing_points_recovers_solution():
    left_lines, right_lines, centre, width, _height, expected = synthetic_pair()
    left = fit_vanishing_point(left_lines[:3])["pixel"]
    right = fit_vanishing_point(right_lines[:3])["pixel"]
    result = focal_from_orthogonal_vanishing_points(left, right, centre, width)
    assert result["focal_px"] == pytest.approx(expected, abs=1e-6)


def test_evaluate_pair_requires_disjoint_frames_and_passes_stable_geometry():
    left, right, centre, width, height, expected = synthetic_pair()
    result = evaluate_pair(left, right, centre, width, height)
    assert result["passed"] is True
    assert result["candidate"]["focal_px"] == pytest.approx(expected, abs=1e-6)
    assert result["holdout_line_metrics"]["max_px"] < 1e-8
    assert result["leave_one_line_out"]["relative_focal_spread"] < 1e-8


def test_evaluate_pair_rejects_reused_fit_and_holdout_frames():
    left, right, centre, width, height, _expected = synthetic_pair()
    left[-1]["frame_id"] = "fit-a"
    result = evaluate_pair(left, right, centre, width, height)
    assert result["passed"] is False
    assert "fit_and_holdout_reuse_frames" in result["reasons"]


def test_nonpositive_focal_and_degenerate_lines_fail_closed():
    with pytest.raises(VanishingCalibrationError, match="non-positive"):
        focal_from_orthogonal_vanishing_points(
            [100.0, 100.0], [200.0, 200.0], [0.0, 0.0], 1280
        )
    with pytest.raises(VanishingCalibrationError, match="too short"):
        normalized_line([[1.0, 1.0], [2.0, 2.0]])


def test_principal_point_sensitivity_is_reported():
    left, right, centre, width, height, _expected = synthetic_pair()
    result = evaluate_pair(left, right, centre, width, height)
    sensitivity = result["principal_point_sensitivity"]
    assert len(sensitivity["solutions"]) == 9
    assert math.isfinite(sensitivity["relative_focal_spread"])


def test_near_collinear_fragments_cannot_inflate_line_count():
    left, right, centre, width, height, _expected = synthetic_pair()
    duplicate = dict(left[0])
    duplicate["id"] = "fragment-with-a-new-id"
    duplicate["physical_line_id"] = "fragment-with-a-new-physical-id"
    duplicate["endpoints"] = [[320.0, 310.0], [270.0, 285.0]]
    with pytest.raises(VanishingCalibrationError, match="near-collinear"):
        evaluate_pair(left + [duplicate], right, centre, width, height)


def test_cli_binds_distinct_frames_and_writes_initialization(tmp_path):
    left, right, centre, width, height, expected = synthetic_pair()
    frame_ids = sorted({item["frame_id"] for item in left + right})
    frames = []
    for index, frame_id in enumerate(frame_ids):
        image = np.full((height, width, 3), index, dtype=np.uint8)
        path = tmp_path / f"{frame_id}.png"
        assert cv2.imwrite(str(path), image)
        frames.append({
            "id": frame_id,
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "resolution": [width, height],
        })
    observations = {
        "schema": "v2x-vanishing-intrinsics-observations/v1",
        "acceptance_eligible": False,
        "frames": frames,
        "cameras": {
            "ch4": {
                "resolution": [width, height],
                "principal_point": centre.tolist(),
                "orthogonal_pair": ["road-a", "road-b"],
                "line_families": {"road-a": left, "road-b": right},
            }
        },
    }
    source = tmp_path / "observations.json"
    source.write_text(json.dumps(observations))
    output = tmp_path / "report.json"
    assert main([str(source), "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["candidate_recommendation"] == "initialization_only"
    assert report["cameras"]["ch4"]["candidate"]["focal_px"] == pytest.approx(
        expected, abs=1e-6
    )
