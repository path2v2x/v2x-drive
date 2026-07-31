import sys
from pathlib import Path

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from evaluate_shared_static_features import (  # noqa: E402
    StaticAlignmentError,
    aggregate_metrics,
    validate_features,
    validate_topology_blockers,
)


def metrics(loss, p95, f1):
    return {
        "metrics": {
            "optimization_loss": loss,
            "symmetric_p95_px": p95,
            "tolerance_f1": f1,
        }
    }


def test_aggregate_preserves_worst_feature_tail():
    result = aggregate_metrics([metrics(2.0, 4.0, 0.8), metrics(6.0, 20.0, 0.4)])
    assert result["optimization_loss"] == 4.0
    assert result["symmetric_p95_px"] == 20.0
    assert result["tolerance_f1"] == pytest.approx(0.6)
    assert result["feature_tolerance_f1_min"] == 0.4


def test_features_require_reviewed_unique_regions():
    values = [
        {
            "id": "shared-a",
            "class": "crosswalk_paint",
            "provenance": "codex_visual_review_diagnostic",
            "real_polygon": [[0, 0], [50, 0], [50, 50], [0, 50]],
            "twin_search_polygon": [[0, 0], [50, 0], [50, 50], [0, 50]],
        },
        {
            "id": "shared-b",
            "class": "road_edge",
            "provenance": "codex_visual_review_diagnostic",
            "real_polygon": [[50, 50], [99, 50], [99, 99], [50, 99]],
            "twin_search_polygon": [[50, 50], [99, 50], [99, 99], [50, 99]],
        },
    ]
    assert len(validate_features(values, (100, 100), (100, 100))) == 2
    values[1]["id"] = "shared-a"
    with pytest.raises(StaticAlignmentError):
        validate_features(values, (100, 100), (100, 100))


def test_topology_blockers_cannot_be_vague():
    good = [
        {
            "id": "missing-west-crosswalk",
            "status": "missing_in_ue5_map",
            "description": "real ladder crosswalk has no UE5 counterpart",
        }
    ]
    assert validate_topology_blockers(good) == good
    good[0]["status"] = "ignore"
    with pytest.raises(StaticAlignmentError):
        validate_topology_blockers(good)
