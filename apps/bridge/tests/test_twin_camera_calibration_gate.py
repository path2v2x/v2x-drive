"""Independent-landmark acceptance tests for the twin camera verifier."""

import math
import sys
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from verify_twin_camera import (  # noqa: E402
    calibration_dataset_gate,
    calibration_metrics,
    heldout_calibration_gate,
    near_depth_fraction,
)


def point(index, split="train"):
    columns = (0.05, 0.35, 0.65, 0.95)
    rows = (0.05, 0.4, 0.75, 0.95)
    return {
        "u": columns[index % len(columns)] * 2560,
        "v": rows[index % len(rows)] * 1920,
        "x": float(index),
        "z": float(index + 1),
        "split": split,
        "carla_xyz": [float(index), float(index + 1), 0.0],
        "landmark_id": f"landmark-{index}",
        "source_frame_sha256": "a" * 64,
        "provenance": "surveyed",
        "category": "road_edge",
    }


def test_metrics_report_rmse_p95_and_max():
    metrics = calibration_metrics([3.0, 4.0, 12.0, float("inf")])
    assert metrics["count"] == 3
    assert metrics["rmse_px"] == math.sqrt((9 + 16 + 144) / 3)
    assert metrics["p95_px"] == 12.0
    assert metrics["max_px"] == 12.0


def test_near_depth_fraction_detects_camera_inside_geometry():
    near = bytes((0, 0, 0, 255))
    far_normalized = 0.5
    packed = int(far_normalized * 16777215)
    far = bytes(((packed >> 16) & 0xFF, (packed >> 8) & 0xFF, packed & 0xFF, 255))
    assert near_depth_fraction(near + far) == 0.5


def test_dataset_gate_rejects_collinear_points():
    points = [point(index, "train" if index < 8 else "holdout") for index in range(12)]
    for calibration_point in points:
        calibration_point["x"] = 0.0
    result = calibration_dataset_gate(points, 2560, 1920)
    assert result["passed"] is False
    assert "rank_deficient_ground_geometry" in result["reasons"]


def test_dataset_gate_accepts_distributed_frozen_split():
    points = [point(index, "train" if index < 8 else "holdout") for index in range(12)]
    for index, calibration_point in enumerate(points):
        calibration_point["x"] = float(index % 4)
        calibration_point["z"] = float(index // 4) * 5.0 + float(index % 2)
        calibration_point["carla_xyz"] = [
            calibration_point["x"], calibration_point["z"], 0.0
        ]
        calibration_point["carla_xyz"] = [
            calibration_point["x"], calibration_point["z"], 0.0
        ]
    result = calibration_dataset_gate(points, 2560, 1920)
    assert result["passed"] is True


def test_gate_requires_independent_spatially_distributed_landmarks():
    points = [point(index, "train" if index < 8 else "holdout") for index in range(12)]
    result = heldout_calibration_gate(points, [8.0] * 12, 2560, 1920)
    assert result["passed"] is True
    assert result["heldout_landmarks"] == 4


def test_legacy_reused_points_cannot_pass_even_with_zero_error():
    points = [point(index, "legacy") for index in range(20)]
    for calibration_point in points:
        calibration_point.pop("carla_xyz")
        calibration_point["provenance"] = "legacy"
    result = heldout_calibration_gate(points, [0.0] * len(points), 2560, 1920)
    assert result["passed"] is False
    assert "insufficient_heldout_landmarks" in result["reasons"]
    assert "no_finite_heldout_errors" in result["reasons"]


def test_accuracy_thresholds_fail_closed():
    points = [point(index, "train" if index < 8 else "holdout") for index in range(12)]
    errors = [0.0] * 8 + [40.0, 60.0, 100.0, 190.0]
    result = heldout_calibration_gate(points, errors, 2560, 1920)
    assert result["passed"] is False
    assert "rmse" in result["reasons"]
    assert "p95" in result["reasons"]
    assert "maximum_error" in result["reasons"]


def test_nonfinite_heldout_landmark_cannot_count_as_evidence():
    points = [point(index, "train" if index < 8 else "holdout") for index in range(12)]
    errors = [0.0] * 11 + [float("inf")]
    result = heldout_calibration_gate(points, errors, 2560, 1920)
    assert result["passed"] is False
    assert "nonfinite_heldout_error" in result["reasons"]


def test_error_cardinality_mismatch_fails_closed():
    points = [point(index, "train" if index < 8 else "holdout") for index in range(12)]
    result = heldout_calibration_gate(points, [0.0] * 11, 2560, 1920)
    assert result["passed"] is False
    assert result["reasons"] == ["error_cardinality_mismatch"]
