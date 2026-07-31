"""Tests for shared-cluster diagnostic visual calibration."""

import sys
from pathlib import Path

import numpy as np
import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from fit_joint_diagnostic_visual_calibration import (  # noqa: E402
    CAMERA_IDS,
    bounds,
    expand_joint,
    finite_difference_jacobian,
    parameter_names,
)


def baselines():
    return {
        camera_id: np.asarray([index, index + 1, 10 + index, -30, 40 * index, 0, 88], dtype=float)
        for index, camera_id in enumerate(CAMERA_IDS)
    }


def test_joint_model_applies_one_identical_world_translation():
    values = np.zeros(19)
    values[:3] = [1.25, -0.75, 0.4]
    for index in range(4):
        values[3 + 4 * index:7 + 4 * index] = [index, -index, 0.5 * index, 2 * index]
    expanded = expand_joint(values, baselines())
    for index, camera_id in enumerate(CAMERA_IDS):
        assert expanded[camera_id][:3] - baselines()[camera_id][:3] == pytest.approx(values[:3])
        assert expanded[camera_id][3:] - baselines()[camera_id][3:] == pytest.approx(
            values[3 + 4 * index:7 + 4 * index]
        )


def test_joint_model_has_no_independent_camera_translation_parameters():
    names = parameter_names()
    assert len(names) == 19
    assert names[:3] == ("shared_location_x", "shared_location_y", "shared_location_z")
    assert not any(
        name.startswith("ch") and "location" in name
        for name in names
    )
    lower, upper = bounds()
    assert lower.shape == upper.shape == (19,)
    assert np.all(lower < upper)


def test_visual_only_finite_difference_jacobian_recovers_linear_rank():
    matrix = np.arange(95, dtype=float).reshape(5, 19) / 100.0
    matrix[:, :5] += np.eye(5)
    values = np.linspace(-0.2, 0.2, 19)
    lower, upper = np.full(19, -1.0), np.full(19, 1.0)
    jacobian = finite_difference_jacobian(lambda item: matrix @ item, values, lower, upper)
    assert jacobian == pytest.approx(matrix, abs=1e-8)
    assert np.linalg.matrix_rank(jacobian) == 5


def test_expand_joint_rejects_wrong_parameter_count():
    with pytest.raises(ValueError, match="expected 19"):
        expand_joint(np.zeros(18), baselines())
