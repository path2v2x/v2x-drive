import importlib.util
from pathlib import Path

import numpy as np
import pytest


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "register_orthophoto_to_lidar.py"
)
SPEC = importlib.util.spec_from_file_location("orthophoto_lidar", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_estimate_registration_recovers_similarity_transform():
    points = np.asarray([
        [0, 0], [100, 0], [0, 100], [100, 100], [40, 70], [80, 20],
    ], dtype=np.float32)
    angle = np.radians(2.0)
    matrix = np.asarray([
        [1.01 * np.cos(angle), -1.01 * np.sin(angle), 3.0],
        [1.01 * np.sin(angle), 1.01 * np.cos(angle), -4.0],
    ])
    targets = np.column_stack([points, np.ones(len(points))]) @ matrix.T
    result = tool.estimate_registration(points, targets, 100, 100, 0.1)
    assert result["inlier_count"] == len(points)
    assert np.isclose(result["scale"], 1.01, atol=1e-5)
    assert np.isclose(result["rotation_deg"], 2.0, atol=1e-5)
    assert np.allclose(result["translation_px"], [3.0, -4.0], atol=1e-5)


def test_estimate_registration_rejects_invalid_points():
    try:
        tool.estimate_registration([[0, 0], [1, 1]], [[0, 0], [1, 1]], 2, 2)
    except tool.RegistrationError as error:
        assert "invalid" in str(error)
    else:
        raise AssertionError("underconstrained registration was accepted")


def test_exclusive_writer_refuses_overwrite(tmp_path):
    output = tmp_path / "registration.json"
    tool.write_json_exclusive(output, {"first": True})
    try:
        tool.write_json_exclusive(output, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("registration evidence was overwritten")


def test_non_primary_unstable_threshold_does_not_erase_primary(monkeypatch, tmp_path):
    image = np.zeros((10, 10), dtype=np.uint8)
    monkeypatch.setattr(tool, "verify_checkpoint", lambda: tmp_path / "checkpoint")
    monkeypatch.setattr(
        tool,
        "load_image",
        lambda path, expected_hash, label: (Path(path), image),
    )
    points = np.array([[0, 0], [9, 0], [0, 9], [9, 9]], dtype=np.float32)
    confidence = np.array([0.7, 0.7, 0.7, 0.25], dtype=np.float32)
    monkeypatch.setattr(
        tool, "extract_matches", lambda left, right: (points, points, confidence)
    )
    original = tool.summarize_threshold

    def summarize(*args, **kwargs):
        if args[3] >= 0.3:
            raise tool.RegistrationError("unstable subset")
        return original(*args, **kwargs)

    monkeypatch.setattr(tool, "summarize_threshold", summarize)
    result = tool.register(
        "lidar.png", "a", "ortho.png", "b", [0, 0, 10, 10], 1.0, 0.2
    )
    assert len(result["confidence_threshold_sweep"]) == 1
    assert result["threshold_stability"]["threshold_count"] == 1


def test_primary_unstable_threshold_remains_a_hard_failure(monkeypatch, tmp_path):
    image = np.zeros((10, 10), dtype=np.uint8)
    monkeypatch.setattr(tool, "verify_checkpoint", lambda: tmp_path / "checkpoint")
    monkeypatch.setattr(
        tool,
        "load_image",
        lambda path, expected_hash, label: (Path(path), image),
    )
    points = np.array([[0, 0], [9, 0], [0, 9]], dtype=np.float32)
    confidence = np.ones(3, dtype=np.float32)
    monkeypatch.setattr(
        tool, "extract_matches", lambda left, right: (points, points, confidence)
    )
    monkeypatch.setattr(
        tool,
        "summarize_threshold",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            tool.RegistrationError("primary unstable")
        ),
    )
    with pytest.raises(tool.RegistrationError, match="primary unstable"):
        tool.register(
            "lidar.png", "a", "ortho.png", "b", [0, 0, 10, 10], 1.0, 0.2
        )
