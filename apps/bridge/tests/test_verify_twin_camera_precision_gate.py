import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from verify_twin_camera import heldout_calibration_gate  # noqa: E402


def _points():
    return [
        {"u": 0.0, "v": 0.0, "split": "train"},
        {"u": 100.0, "v": 100.0, "split": "train"},
        {"u": 200.0, "v": 200.0, "split": "train"},
        {"u": 300.0, "v": 300.0, "split": "train"},
        {"u": 400.0, "v": 400.0, "split": "train"},
        {"u": 500.0, "v": 500.0, "split": "train"},
        {"u": 600.0, "v": 600.0, "split": "train"},
        {"u": 700.0, "v": 700.0, "split": "train"},
        {"u": 0.0, "v": 0.0, "split": "holdout"},
        {"u": 1280.0, "v": 0.0, "split": "holdout"},
        {"u": 0.0, "v": 960.0, "split": "holdout"},
        {"u": 1280.0, "v": 960.0, "split": "holdout"},
    ]


def test_precision_gate_rejects_former_diagnostic_error_level():
    gate = heldout_calibration_gate(_points(), [0.0] * 8 + [30.0] * 4, 1280, 960)
    assert not gate["passed"]
    assert {"rmse", "p95", "maximum_error"}.issubset(gate["reasons"])


def test_precision_gate_accepts_errors_under_fixed_thresholds():
    gate = heldout_calibration_gate(_points(), [0.0] * 8 + [5.0, 8.0, 9.0, 10.0], 1280, 960)
    assert gate["passed"]
