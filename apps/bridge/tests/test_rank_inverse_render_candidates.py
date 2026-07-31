import importlib.util
from pathlib import Path
import sys


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "rank_inverse_render_candidates.py"
SPEC = importlib.util.spec_from_file_location("rank_inverse_render_candidates", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC.loader.exec_module(MODULE)


def test_structural_score_rewards_inliers_and_spatial_coverage():
    weak = MODULE.structural_score(
        {"mutual_matches": 3, "homography": None}, [], 1280, 960
    )
    matches = [
        {"real_pixel": [10, 10], "homography_inlier": True},
        {"real_pixel": [1270, 10], "homography_inlier": True},
        {"real_pixel": [10, 950], "homography_inlier": True},
        {"real_pixel": [1270, 950], "homography_inlier": True},
        {"real_pixel": [640, 480], "homography_inlier": True},
    ]
    strong = MODULE.structural_score(
        {"mutual_matches": 6, "homography": {"inliers": 5}},
        matches,
        1280,
        960,
    )
    assert strong["score"] > weak["score"]
    assert strong["inlier_coverage"]["cells"] >= 4


def test_structural_score_penalizes_low_inlier_ratio():
    matches = [
        {"real_pixel": [100 + index, 100], "homography_inlier": index < 4}
        for index in range(30)
    ]
    result = MODULE.structural_score(
        {"mutual_matches": 30, "homography": {"inliers": 4}},
        matches,
        1280,
        960,
    )
    assert result["homography_inlier_ratio"] < 0.2
    assert result["score"] < 50
