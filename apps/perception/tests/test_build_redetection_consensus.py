import copy
import importlib.util
from pathlib import Path

import pytest


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "build_redetection_consensus.py"
)
SPEC = importlib.util.spec_from_file_location("redetection_consensus", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def report(model_hash, bbox=(100, 120, 180, 220), boundary=False):
    return {
        "schema": "v2x-selected-frame-redetection/v1",
        "acceptance_eligible": False,
        "capture_report": {"sha256": "c" * 64},
        "model": {"sha256": model_hash},
        "events": [{
            "event_id": "event-1",
            "camera_id": "ch2",
            "selected_frame_timestamp_utc": "2026-07-12T01:00:00Z",
            "frame": {
                "encoded_jpeg_sha256": "f" * 64,
                "width": 640,
                "height": 480,
            },
            "detections": [{
                "label": "car",
                "confidence": 0.8,
                "bbox_xyxy": list(bbox),
                "touches_frame_boundary": boundary,
            }],
            "event_match_proposal": {"detection_index": 0},
        }],
    }


def test_builds_unweighted_consensus_and_uncertainty():
    result = tool.build_consensus(
        report("a" * 64, (100, 120, 180, 220)),
        report("b" * 64, (102, 118, 184, 224)),
        minimum_iou=0.6,
    )
    item = result["events"][0]
    assert item["consensus"]["bbox_xyxy"] == [101.0, 119.0, 182.0, 222.0]
    assert item["consensus"]["coordinate_uncertainty_px"] == [2.0, 2.0, 2.0, 2.0]
    assert result["summary"]["consensus_count"] == 1


def test_rejects_boundary_box_even_when_models_agree():
    result = tool.build_consensus(
        report("a" * 64, boundary=True), report("b" * 64), minimum_iou=0.6
    )
    assert result["events"][0]["consensus"] is None
    assert "left_box_touches_frame_boundary" in result["events"][0]["rejection_reasons"]


def test_rejects_low_iou():
    result = tool.build_consensus(
        report("a" * 64),
        report("b" * 64, (300, 320, 350, 380)),
        minimum_iou=0.6,
    )
    assert result["events"][0]["consensus"] is None
    assert "box_iou_below_consensus_gate" in result["events"][0]["rejection_reasons"]


def test_rejects_near_edge_box_and_contact_disagreement():
    edge = tool.build_consensus(
        report("a" * 64, (1, 120, 80, 220)),
        report("b" * 64, (2, 120, 82, 220)),
    )
    assert "left_box_lacks_full_visibility_margin" in edge["events"][0]["rejection_reasons"]
    contact = tool.build_consensus(
        report("a" * 64, (100, 120, 300, 300)),
        report("b" * 64, (130, 120, 330, 340)),
        minimum_iou=0.6,
    )
    assert "contact_disagreement_above_gate" in contact["events"][0]["rejection_reasons"]


def test_rejects_same_model_or_frame_drift():
    with pytest.raises(tool.ConsensusError, match="distinct"):
        tool.build_consensus(report("a" * 64), report("a" * 64))
    right = copy.deepcopy(report("b" * 64))
    right["events"][0]["frame"]["encoded_jpeg_sha256"] = "0" * 64
    with pytest.raises(tool.ConsensusError, match="frame binding"):
        tool.build_consensus(report("a" * 64), right)
