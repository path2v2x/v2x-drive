import importlib.util
import json
from pathlib import Path
import sys

import cv2
import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location(
    "segmentation_contact_consensus", TOOLS / "build_segmentation_contact_consensus.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def proposal(tmp_path, name, model_hash, pixel, shift=0):
    mask = np.zeros((100, 160), dtype=np.uint8)
    mask[35:80, 40 + shift:120 + shift] = 255
    mask_path = tmp_path / f"{name}.png"
    assert cv2.imwrite(str(mask_path), mask)
    event = {
        "event_id": "event-1",
        "camera_id": "ch4",
        "selected_frame_timestamp_utc": "2026-07-12T00:00:00.000Z",
        "frame": {
            "encoded_jpeg_sha256": "f" * 64,
            "width": 160,
            "height": 100,
        },
        "matched_instance": {"bbox_xyxy": [40 + shift, 35, 120 + shift, 80]},
        "ground_contact_proposal": {
            "pixel": pixel,
            "covariance_px2": [[4, 0], [0, 4]],
        },
        "mask": {"path": str(mask_path), "sha256": module.sha256(mask_path)},
    }
    capture_path = tmp_path / "capture.json"
    if not capture_path.exists():
        capture_path.write_text(json.dumps({
            "schema": "v2x-detection-event-frame-capture/v2",
            "events": [{"event_id": "event-1"}],
        }))
    report = {
        "schema": module.INPUT_SCHEMA,
        "acceptance_eligible": False,
        "capture_report": {
            "path": str(capture_path),
            "sha256": module.sha256(capture_path),
        },
        "model": {"sha256": model_hash},
        "events": [event],
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(report))
    return path


def test_consensus_accepts_matching_masks_and_contacts(tmp_path):
    left = proposal(tmp_path, "left", "a" * 64, [80, 79])
    right = proposal(tmp_path, "right", "b" * 64, [81, 78.5], shift=1)
    result = module.build(left, right)
    event = result["events"][0]
    assert result["summary"]["consensus_count"] == 1
    assert event["bbox_iou"] > 0.9
    assert event["mask_iou"] > 0.9
    assert event["consensus"]["pixel"] == [80.5, 78.75]
    assert event["contact_disagreement_px"] == {"x": 1.0, "y": 0.5}
    assert result["acceptance_eligible"] is False


def test_consensus_rejects_contact_or_mask_disagreement(tmp_path):
    left = proposal(tmp_path, "left", "a" * 64, [60, 79])
    right = proposal(tmp_path, "right", "b" * 64, [110, 79], shift=30)
    result = module.build(left, right)
    reasons = result["events"][0]["rejection_reasons"]
    assert "mask_iou_below_gate" in reasons
    assert "contact_disagreement_above_gate" in reasons
    assert result["summary"]["consensus_count"] == 0


def test_consensus_rejects_same_model_or_missing_proposal(tmp_path):
    left = proposal(tmp_path, "left", "a" * 64, [80, 79])
    right = proposal(tmp_path, "right", "a" * 64, [80, 79])
    with pytest.raises(module.ConsensusError, match="distinct"):
        module.build(left, right)

    value = json.loads(right.read_text())
    value["model"]["sha256"] = "b" * 64
    value["events"][0]["ground_contact_proposal"] = None
    right.write_text(json.dumps(value))
    result = module.build(left, right)
    assert result["events"][0]["rejection_reasons"] == [
        "right_model_has_no_contact_proposal"
    ]


def test_consensus_uses_independent_per_axis_scaling(tmp_path):
    left = proposal(tmp_path, "left", "a" * 64, [80, 60])
    right = proposal(tmp_path, "right", "b" * 64, [81.9, 61.5], shift=1)
    result = module.build(left, right)
    event = result["events"][0]
    # At 160x100 the native x/y gates are 2.0 and 1.666..., respectively.
    assert event["maximum_contact_disagreement_px"]["x"] == pytest.approx(2.0)
    assert event["maximum_contact_disagreement_px"]["y"] == pytest.approx(5 / 3)
    assert event["consensus"] is not None

    value = json.loads(right.read_text())
    value["events"][0]["ground_contact_proposal"]["pixel"] = [81.9, 61.8]
    right.write_text(json.dumps(value))
    rejected = module.build(left, right)["events"][0]
    assert "contact_disagreement_above_gate" in rejected["rejection_reasons"]


def test_consensus_rejects_wrong_mask_dimensions_or_covariance(tmp_path):
    left = proposal(tmp_path, "left", "a" * 64, [80, 79])
    right = proposal(tmp_path, "right", "b" * 64, [81, 78.5], shift=1)

    for path in (left, right):
        value = json.loads(path.read_text())
        value["events"][0]["frame"]["height"] = 101
        path.write_text(json.dumps(value))
    with pytest.raises(module.ConsensusError, match="dimensions"):
        module.build(left, right)

    left = proposal(tmp_path, "left2", "a" * 64, [80, 79])
    right = proposal(tmp_path, "right2", "b" * 64, [81, 78.5], shift=1)
    value = json.loads(right.read_text())
    value["events"][0]["ground_contact_proposal"]["covariance_px2"] = [[1, 2], [0, -1]]
    right.write_text(json.dumps(value))
    with pytest.raises(module.ConsensusError, match="covariance"):
        module.build(left, right)


@pytest.mark.parametrize(
    "bbox,error",
    [
        ([float("nan"), 35, 119, 80], "invalid"),
        ([40, float("inf"), 120, 80], "invalid"),
        ([40, 35, 120], "invalid"),
        ([True, 35, 120, 80], "invalid"),
        ([40, 35, 40, 80], "outside"),
        ([-1, 35, 120, 80], "outside"),
        ([40, 35, 161, 80], "outside"),
    ],
)
def test_consensus_rejects_malformed_or_out_of_frame_bboxes(tmp_path, bbox, error):
    left = proposal(tmp_path, "left", "a" * 64, [80, 79])
    right = proposal(tmp_path, "right", "b" * 64, [81, 78.5], shift=1)
    value = json.loads(right.read_text())
    value["events"][0]["matched_instance"]["bbox_xyxy"] = bbox
    right.write_text(json.dumps(value))

    with pytest.raises(module.ConsensusError, match=error):
        module.build(left, right)


@pytest.mark.parametrize("bad_iou", [float("nan"), float("inf"), -0.1, 1.1])
def test_consensus_rejects_nonfinite_or_out_of_range_iou(
    tmp_path, monkeypatch, bad_iou
):
    left = proposal(tmp_path, "left", "a" * 64, [80, 79])
    right = proposal(tmp_path, "right", "b" * 64, [81, 78.5], shift=1)
    monkeypatch.setattr(module, "bbox_iou", lambda *_args: bad_iou)

    with pytest.raises(module.ConsensusError, match="IoU"):
        module.build(left, right)


@pytest.mark.parametrize(
    "pixel,error",
    [
        ([-1, 79], "outside the frame"),
        ([160, 79], "outside the frame"),
        ([20, 79], "outside its bbox"),
    ],
)
def test_consensus_rejects_out_of_frame_or_bbox_contact_pixels(
    tmp_path, pixel, error
):
    left = proposal(tmp_path, "left", "a" * 64, pixel)
    right = proposal(tmp_path, "right", "b" * 64, pixel)

    with pytest.raises(module.ConsensusError, match=error):
        module.build(left, right)


@pytest.mark.parametrize("bad_iou", [float("nan"), float("inf"), -0.1, 1.1])
def test_consensus_rejects_nonfinite_or_out_of_range_mask_iou(
    tmp_path, monkeypatch, bad_iou
):
    left = proposal(tmp_path, "left", "a" * 64, [80, 79])
    right = proposal(tmp_path, "right", "b" * 64, [81, 78.5], shift=1)
    monkeypatch.setattr(module, "mask_iou", lambda *_args: bad_iou)

    with pytest.raises(module.ConsensusError, match="mask IoU"):
        module.build(left, right)


def test_consensus_rejects_silently_shrunk_capture_denominator(tmp_path):
    left = proposal(tmp_path, "left", "a" * 64, [80, 79])
    right = proposal(tmp_path, "right", "b" * 64, [81, 78.5], shift=1)
    capture_path = tmp_path / "capture.json"
    capture = json.loads(capture_path.read_text())
    capture["events"].append({"event_id": "event-2"})
    capture_path.write_text(json.dumps(capture))
    new_hash = module.sha256(capture_path)
    for path in (left, right):
        value = json.loads(path.read_text())
        value["capture_report"]["sha256"] = new_hash
        path.write_text(json.dumps(value))
    with pytest.raises(module.ConsensusError, match="full capture denominator"):
        module.build(left, right)
