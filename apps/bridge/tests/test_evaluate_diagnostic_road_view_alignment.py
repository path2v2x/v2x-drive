import hashlib
import json

import pytest
from PIL import Image

from tools.evaluate_diagnostic_road_view_alignment import AlignmentError, evaluate


def frame(tmp_path, name, size=(640, 480)):
    path = tmp_path / name
    Image.new("RGB", size, "black").save(path)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": list(size),
    }


def features(offset=0, provenance="codex_visual_review_diagnostic"):
    return [
        {
            "id": "left_edge",
            "provenance": provenance,
            "uncertainty_px": 1.0,
            "polyline": [[20 + offset, 450], [160 + offset, 250], [300 + offset, 80]],
        },
        {
            "id": "right_edge",
            "provenance": provenance,
            "uncertainty_px": 1.0,
            "polyline": [[230 + offset, 450], [315 + offset, 250], [340 + offset, 80]],
        },
    ]


def annotations(tmp_path):
    return {
        "schema": "v2x-diagnostic-road-view-annotations/v1",
        "acceptance_eligible": False,
        "camera": "ch2",
        "real_frame": frame(tmp_path, "real.png"),
        "real_features": features(),
        "vanishing_edge_ids": ["left_edge", "right_edge"],
        "candidates": [
            {"id": "worse", "frame": frame(tmp_path, "worse.png"), "features": features(20)},
            {"id": "better", "frame": frame(tmp_path, "better.png"), "features": features(2)},
        ],
        "_sha256": "a" * 64,
    }


def test_ranks_candidates_by_symmetric_trace_distance(tmp_path):
    report = evaluate(annotations(tmp_path))
    assert report["acceptance_eligible"] is False
    assert report["ranking"] == ["better", "worse"]
    assert report["candidates"][0]["mean_trace_distance_px"] < 3.0
    assert report["real_vanishing_point"]["valid"] is True


def test_rejects_frame_hash_mismatch(tmp_path):
    value = annotations(tmp_path)
    value["real_frame"]["sha256"] = "0" * 64
    with pytest.raises(AlignmentError, match="hash mismatch"):
        evaluate(value)


def test_rejects_unreviewed_provenance(tmp_path):
    value = annotations(tmp_path)
    value["real_features"] = features(provenance="automatic_match")
    with pytest.raises(AlignmentError, match="provenance"):
        evaluate(value)


def test_rejects_mismatched_semantic_trace_ids(tmp_path):
    value = annotations(tmp_path)
    value["candidates"][0]["features"][0]["id"] = "curb"
    with pytest.raises(AlignmentError, match="semantic trace IDs"):
        evaluate(value)
