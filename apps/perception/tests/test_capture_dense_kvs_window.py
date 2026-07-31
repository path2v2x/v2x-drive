from datetime import datetime, timezone
import base64
import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from capture_dense_kvs_window import DenseCaptureError, capture  # noqa: E402


def jpeg(value):
    image = np.full((48, 64, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def source_report(tmp_path, schema="v2x-detection-event-frame-capture/v2"):
    frames = []
    for index in range(2):
        raw = jpeg(40 + index)
        path = tmp_path / f"source-{index}.jpg"
        path.write_bytes(raw)
        frames.append((path, hashlib.sha256(raw).hexdigest()))
    report = {
        "schema": schema,
        "events": [
            {
                "event_id": f"event-{index}",
                "object_id": "car-1",
                "camera_id": "ch4",
                "selected_frame_timestamp_utc": f"2026-07-11T03:32:2{index}.000Z",
                "frame": {"path": str(path), "sha256": digest},
            }
            for index, (path, digest) in enumerate(frames)
        ],
    }
    path = tmp_path / "source-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


class FakeArchived:
    def __init__(self):
        self.calls = []

    def get_images(self, **kwargs):
        self.calls.append(kwargs)
        offset = 2 if kwargs.get("NextToken") else 0
        return {
            "Images": [
                {
                    "TimeStamp": datetime(
                        2026,
                        7,
                        11,
                        3,
                        32,
                        19 + offset + index,
                        tzinfo=timezone.utc,
                    ),
                    "ImageContent": (
                        base64.b64encode(jpeg(60 + offset + index)).decode()
                        if index == 0
                        else jpeg(60 + offset + index)
                    ),
                }
                for index in range(2)
            ],
            **({"NextToken": "page2"} if not kwargs.get("NextToken") else {}),
        }


class FakeKvs:
    def get_data_endpoint(self, **kwargs):
        assert kwargs["APIName"] == "GET_IMAGES"
        return {"DataEndpoint": "https://signed.invalid"}


class FakeSession:
    def __init__(self):
        self.archived = FakeArchived()

    def factory(self, **kwargs):
        assert kwargs == {"profile_name": "path", "region_name": "us-west-2"}
        return self

    def client(self, name, **kwargs):
        if name == "kinesisvideo":
            return FakeKvs()
        assert name == "kinesis-video-archived-media"
        assert kwargs == {"endpoint_url": "https://signed.invalid"}
        return self.archived


def test_capture_binds_dense_frames_without_endpoint(tmp_path):
    session = FakeSession()
    output = tmp_path / "dense"
    report_path = capture(
        source_report(tmp_path),
        "ch4",
        "car-1",
        output,
        "path",
        session_factory=session.factory,
    )
    report = json.loads(report_path.read_text())
    assert report["schema"] == "v2x-dense-kvs-window/v1"
    assert report["frame_count"] == 4
    assert report["resolution"] == [64, 48]
    assert report["safety"]["signed_endpoints_persisted"] is False
    assert "signed.invalid" not in report_path.read_text()
    assert len(session.archived.calls) == 2
    assert session.archived.calls[0]["SamplingInterval"] == 200
    assert session.archived.calls[1]["NextToken"] == "page2"
    assert "page2" not in report_path.read_text()
    for frame in report["frames"]:
        raw = (output / frame["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == frame["sha256"]


def test_capture_accepts_retained_v1_report(tmp_path):
    output = tmp_path / "dense"
    report_path = capture(
        source_report(tmp_path, "v2x-detection-event-frame-capture/v1"),
        "ch4",
        "car-1",
        output,
        "path",
        session_factory=FakeSession().factory,
    )
    assert json.loads(report_path.read_text())["frame_count"] == 4


def test_capture_rejects_tampered_source_frame(tmp_path):
    report_path = source_report(tmp_path)
    report = json.loads(report_path.read_text())
    Path(report["events"][0]["frame"]["path"]).write_bytes(b"tampered")
    with pytest.raises(DenseCaptureError, match="hash does not match"):
        capture(
            report_path,
            "ch4",
            "car-1",
            tmp_path / "dense",
            "path",
            session_factory=FakeSession().factory,
        )


def test_capture_rejects_window_over_api_bound(tmp_path):
    with pytest.raises(DenseCaptureError, match="100-image"):
        capture(
            source_report(tmp_path),
            "ch4",
            "car-1",
            tmp_path / "dense",
            "path",
            padding_seconds=10.0,
            sampling_ms=200,
            session_factory=FakeSession().factory,
        )
