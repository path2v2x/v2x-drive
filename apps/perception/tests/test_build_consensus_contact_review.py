import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from build_consensus_contact_review import (  # noqa: E402
    ContactReviewError,
    build,
)


def report(tmp_path):
    image = np.zeros((120, 160, 3), dtype=np.uint8)
    image[35:90, 50:110] = 180
    frame = tmp_path / "frame.jpg"
    assert cv2.imwrite(str(frame), image)
    frame_hash = hashlib.sha256(frame.read_bytes()).hexdigest()
    value = {
        "schema": "v2x-selected-frame-redetection-consensus/v1",
        "acceptance_eligible": False,
        "events": [{
            "event_id": "event-1",
            "camera_id": "ch4",
            "selected_frame_timestamp_utc": "2026-07-12T00:00:00.000Z",
            "frame": {
                "path": str(frame),
                "encoded_jpeg_sha256": frame_hash,
                "width": 160,
                "height": 120,
            },
            "left_detection": {"bbox_xyxy": [50.0, 35.0, 108.0, 88.0]},
            "right_detection": {"bbox_xyxy": [52.0, 36.0, 110.0, 90.0]},
            "consensus": {
                "bbox_xyxy": [51.0, 35.5, 109.0, 89.0],
                "bottom_center_pixel": [80.0, 89.0],
                "bottom_center_uncertainty_px": [2.0, 2.5],
            },
        }],
    }
    source = tmp_path / "consensus.json"
    source.write_text(json.dumps(value))
    return source, frame


def test_build_writes_hash_bound_raw_and_annotated_crops(tmp_path):
    source, frame = report(tmp_path)
    output = tmp_path / "review"
    report_path = build(source, output, 0.25)
    value = json.loads(report_path.read_text())
    assert value["schema"] == "v2x-consensus-contact-review-sheet/v1"
    assert value["acceptance_eligible"] is False
    crop = value["crops"][0]
    assert crop["frame"]["sha256"] == hashlib.sha256(frame.read_bytes()).hexdigest()
    assert crop["wheel_road_contact_reviewed"] is False
    for key in ("raw_crop", "annotated_crop"):
        path = output / crop[key]["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == crop[key]["sha256"]
        assert cv2.imread(str(path)) is not None


def test_build_rejects_frame_hash_drift_and_existing_output(tmp_path):
    source, frame = report(tmp_path)
    frame.write_bytes(b"changed")
    with pytest.raises(ContactReviewError, match="hash"):
        build(source, tmp_path / "review")

    source, _frame = report(tmp_path)
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(ContactReviewError, match="already exists"):
        build(source, output)


def test_build_rejects_out_of_frame_boxes(tmp_path):
    source, _frame = report(tmp_path)
    value = json.loads(source.read_text())
    value["events"][0]["consensus"]["bbox_xyxy"] = [-1.0, 2.0, 40.0, 50.0]
    source.write_text(json.dumps(value))
    with pytest.raises(ContactReviewError, match="outside"):
        build(source, tmp_path / "review")
