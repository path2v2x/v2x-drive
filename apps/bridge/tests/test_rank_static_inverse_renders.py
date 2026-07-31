import sys
from pathlib import Path

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from rank_static_inverse_renders import RankingError, rank  # noqa: E402


def score(candidate, loss, p95, f1, camera="ch4", annotations="a" * 64):
    return {
        "camera_id": camera,
        "candidate_id": candidate,
        "annotations_sha256": annotations,
        "twin_pose": {},
        "fov_deg": 88.0,
        "_path": f"/{candidate}.json",
        "_sha256": candidate.ljust(64, "0")[:64],
        "_normalized_metrics": {
            "optimization_loss": loss,
            "symmetric_p95_px": p95,
            "tolerance_f1": f1,
        },
    }


def test_rank_promotes_only_joint_metric_improvement():
    baseline = score("baseline", 20.0, 50.0, 0.4)
    good = score("good", 18.0, 45.0, 0.5)
    bad_tail = score("bad-tail", 17.0, 60.0, 0.6)
    report = rank(baseline, [bad_tail, good])
    assert report["promoted_for_visual_review"] == ["good"]
    assert report["ranking"][0] == "good"
    assert report["candidates"][1]["promotion_checks"][
        "p95_does_not_regress"
    ] is False


def test_rank_rejects_mixed_evidence_and_duplicate_ids():
    baseline = score("baseline", 20.0, 50.0, 0.4)
    with pytest.raises(RankingError):
        rank(baseline, [score("candidate", 18.0, 40.0, 0.5, camera="ch3")])
    duplicate = score("baseline", 18.0, 40.0, 0.5)
    with pytest.raises(RankingError):
        rank(baseline, [duplicate])
