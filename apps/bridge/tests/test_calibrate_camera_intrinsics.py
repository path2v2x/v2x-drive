"""Unit tests for measured physical-camera intrinsics acquisition."""

import importlib.util
from pathlib import Path

import numpy as np
import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "calibrate_camera_intrinsics.py"
SPEC = importlib.util.spec_from_file_location("calibrate_camera_intrinsics", TOOL)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_generates_dimensioned_checkerboard_svg():
    svg = MODULE.checkerboard_svg(9, 6, 25.0)
    assert 'width="270mm"' in svg
    assert 'height="195mm"' in svg
    assert svg.count("fill=\"black\"") == 35


def test_artifact_matches_manifest_schema_and_rejects_weak_evidence():
    hashes = [f"{index:064x}" for index in range(10)]
    artifact = MODULE.build_artifact(
        resolution=(640, 480),
        matrix=np.array([[600, 0, 320], [0, 601, 240], [0, 0, 1]]),
        distortion=np.array([-0.1, 0.01, 0.001, -0.002, 0.0]),
        source_hashes=hashes,
        rms=0.7,
    )
    assert artifact["method"] == "checkerboard"
    assert artifact["image_count"] == 10
    assert artifact["resolution"] == [640, 480]
    assert artifact["distortion"]["p2"] == pytest.approx(-0.002)
    with pytest.raises(MODULE.CalibrationError, match="10 unique"):
        MODULE.build_artifact(
            resolution=(640, 480), matrix=np.eye(3), distortion=np.zeros(5),
            source_hashes=["a" * 64] * 10, rms=0.5,
        )
    with pytest.raises(MODULE.CalibrationError, match="no worse than 2"):
        MODULE.build_artifact(
            resolution=(640, 480), matrix=np.eye(3), distortion=np.zeros(5),
            source_hashes=hashes, rms=2.1,
        )


def test_board_coverage_rejects_center_only_observations():
    centered = [
        np.array([[[250 + x, 180 + y]] for y in range(6) for x in range(9)])
        for _index in range(10)
    ]
    with pytest.raises(MODULE.CalibrationError, match="edge/corner coverage"):
        MODULE.validate_board_coverage(centered, (640, 480))


def test_pose_diversity_rejects_frontal_single_distance(monkeypatch):
    monkeypatch.setattr(MODULE.cv2, "Rodrigues", lambda _rotation: (np.eye(3), None))
    rotations = [np.zeros((3, 1)) for _index in range(10)]
    translations = [np.array([[0.0], [0.0], [2.0]]) for _index in range(10)]
    with pytest.raises(MODULE.CalibrationError, match="tilt spread"):
        MODULE.validate_pose_diversity(rotations, translations)
