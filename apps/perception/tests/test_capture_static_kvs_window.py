from datetime import datetime, timedelta, timezone
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
from capture_static_kvs_window import (  # noqa: E402
    StaticCaptureError,
    atomic_publish_directory,
    build_request_segments,
    capture,
    verify_staged_frames,
)


START = "2026-07-11T03:32:20.000Z"
END = "2026-07-11T03:32:21.000Z"
FIVE_SECOND_END = "2026-07-11T03:32:25.000Z"


def jpeg(value, width=64, height=48):
    image = np.full((height, width, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


class FakeArchived:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses
        self.image_index = 0

    def get_images(self, **kwargs):
        self.calls.append(kwargs)
        if self.responses is not None:
            return self.responses[len(self.calls) - 1]
        start = kwargs["StartTimestamp"]
        end = kwargs["EndTimestamp"]
        sampling = timedelta(milliseconds=kwargs["SamplingInterval"])
        timestamp = start
        images = []
        while timestamp <= end:
            content = jpeg(30 + self.image_index % 200)
            images.append({
                "TimeStamp": timestamp,
                "ImageContent": (
                    base64.b64encode(content).decode()
                    if self.image_index == 0
                    else content
                ),
            })
            self.image_index += 1
            timestamp += sampling
        assert len(images) <= 25
        return {"Images": images}


class FakeKvs:
    def __init__(self, stream_name="v2x-backend-cam-ch4"):
        self.stream_name = stream_name
        self.calls = []

    def describe_stream(self, **kwargs):
        self.calls.append(("describe_stream", kwargs))
        return {
            "StreamInfo": {
                "StreamName": self.stream_name,
                "StreamARN": "arn:aws:kinesisvideo:us-west-2:123:stream/example/1",
                "Status": "ACTIVE",
                "Version": "version-1",
                "CreationTime": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "DataRetentionInHours": 720,
            }
        }

    def get_data_endpoint(self, **kwargs):
        self.calls.append(("get_data_endpoint", kwargs))
        assert kwargs["APIName"] == "GET_IMAGES"
        return {"DataEndpoint": "https://signed.invalid"}


class FakeSession:
    def __init__(self, responses=None, stream_name="v2x-backend-cam-ch4"):
        self.archived = FakeArchived(responses)
        self.kvs = FakeKvs(stream_name)

    def factory(self, **kwargs):
        assert kwargs == {"profile_name": "path", "region_name": "us-west-2"}
        return self

    def client(self, name, **kwargs):
        if name == "kinesisvideo":
            assert kwargs == {}
            return self.kvs
        assert name == "kinesis-video-archived-media"
        assert kwargs == {"endpoint_url": "https://signed.invalid"}
        return self.archived


def test_capture_is_detection_independent_and_hash_bound(tmp_path):
    session = FakeSession()
    output = tmp_path / "static"
    report_path = capture(
        "ch4",
        START,
        END,
        output,
        "path",
        session_factory=session.factory,
    )
    report_text = report_path.read_text()
    report = json.loads(report_text)
    assert report["schema"] == "v2x-static-kvs-window-proposal/v1"
    assert report["acceptance_eligible"] is False
    assert report["camera_id"] == "ch4"
    assert report["request"]["bounded_expected_image_count"] == 6
    assert report["request"]["start_utc"] == START
    assert report["request"]["end_utc"] == END
    assert report["request"]["maximum_total_images"] == 100
    assert report["request"]["max_results_per_call"] == 25
    segmentation = report["request"]["segmentation"]
    assert segmentation["strategy"] == "one_boundary_sample_overlap/v1"
    assert segmentation["segment_count"] == 1
    assert segmentation["response_item_count_sum"] == 6
    assert segmentation["unique_retained_frame_count"] == 6
    assert segmentation["segments"] == [{
        "boundary_overlap_sample_count": 0,
        "decoded_in_global_candidate_count": 6,
        "discarded_duplicate_count": 0,
        "discarded_error_count": 0,
        "discarded_outside_global_window_count": 0,
        "end_utc": END,
        "expected_image_count": 6,
        "global_end_sample_index": 5,
        "global_start_sample_index": 0,
        "index": 0,
        "response_item_count": 6,
        "retained_unique_frame_count": 6,
        "start_utc": START,
    }]
    assert report["stream"]["retention_hours"] == 720
    assert report["retention"]["data_retention_hours"] == 720
    assert report["frame_count"] == 6
    assert report["resolution"] == [64, 48]
    assert report["response_call_count"] == 1
    assert report["response_pagination_token_count"] == 0
    assert report["safety"]["detection_or_object_id_dependency"] is False
    assert report["safety"]["atomic_no_overwrite_publication"] is True
    assert report["frame_change_diagnostics"]["does_not_prove_vehicle_free"] is True
    assert "signed.invalid" not in report_text
    assert len(session.archived.calls) == 1
    assert session.archived.calls[0]["MaxResults"] == 25
    assert session.archived.calls[0]["SamplingInterval"] == 200
    assert "NextToken" not in session.archived.calls[0]
    for frame in report["frames"]:
        raw = (output / frame["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == frame["sha256"]
        assert len(raw) == frame["byte_count"]


def test_five_second_26_image_window_uses_two_tokenless_segments(tmp_path):
    session = FakeSession()
    report_path = capture(
        "ch4",
        START,
        FIVE_SECOND_END,
        tmp_path / "static",
        "path",
        session_factory=session.factory,
    )
    report = json.loads(report_path.read_text())
    assert report["frame_count"] == 26
    assert report["request"]["bounded_expected_image_count"] == 26
    assert report["response_call_count"] == 2
    assert report["response_pagination_token_count"] == 0
    assert [
        segment["expected_image_count"]
        for segment in report["request"]["segmentation"]["segments"]
    ] == [25, 2]
    assert [
        segment["retained_unique_frame_count"]
        for segment in report["request"]["segmentation"]["segments"]
    ] == [25, 1]
    assert session.archived.calls[0]["StartTimestamp"] == datetime(
        2026, 7, 11, 3, 32, 20, tzinfo=timezone.utc
    )
    assert session.archived.calls[0]["EndTimestamp"] == datetime(
        2026, 7, 11, 3, 32, 24, 800000, tzinfo=timezone.utc
    )
    assert session.archived.calls[1]["StartTimestamp"] == datetime(
        2026, 7, 11, 3, 32, 24, 800000, tzinfo=timezone.utc
    )
    assert session.archived.calls[1]["EndTimestamp"] == datetime(
        2026, 7, 11, 3, 32, 25, tzinfo=timezone.utc
    )
    assert all(call["MaxResults"] == 25 for call in session.archived.calls)
    assert all("NextToken" not in call for call in session.archived.calls)
    assert all(
        call["StartTimestamp"] < call["EndTimestamp"]
        for call in session.archived.calls
    )


def test_five_second_window_accepts_nearest_media_and_deduplicates_overlap(
    tmp_path,
):
    base = datetime(2026, 7, 11, 3, 32, 20, tzinfo=timezone.utc)

    def image(index):
        return {
            "TimeStamp": base + timedelta(milliseconds=index * 200),
            "ImageContent": jpeg(20 + index),
        }

    responses = [
        {"Images": [image(index) for index in range(11)]},
        {"Images": [image(index) for index in range(10, 26)]},
    ]
    session = FakeSession(responses)
    report_path = capture(
        "ch4",
        START,
        FIVE_SECOND_END,
        tmp_path / "static",
        "path",
        session_factory=session.factory,
    )
    report = json.loads(report_path.read_text())
    segment_rows = report["request"]["segmentation"]["segments"]
    assert [row["response_item_count"] for row in segment_rows] == [11, 16]
    assert [row["decoded_in_global_candidate_count"] for row in segment_rows] == [
        11,
        16,
    ]
    assert [row["retained_unique_frame_count"] for row in segment_rows] == [
        11,
        15,
    ]
    assert session.archived.calls[1]["StartTimestamp"] == base + timedelta(
        seconds=4.8
    )
    assert responses[1]["Images"][0]["TimeStamp"] == base + timedelta(seconds=2)
    assert report["discarded_duplicate_count"] == 1
    assert report["discarded_duplicates"][0]["reasons"] == [
        "duplicate_producer_timestamp",
        "duplicate_jpeg_sha256",
    ]
    assert report["discarded_outside_global_window_count"] == 0
    assert report["frame_count"] == 26
    assert report["frames"][0]["producer_timestamp_utc"] == START
    assert report["frames"][-1]["producer_timestamp_utc"] == FIVE_SECOND_END
    assert report["maximum_interframe_gap_ms"] == 200.0
    assert report["coverage"]["requested_count_complete"] is True
    assert report["coverage"]["requested_window_coverage_established"] is False


def test_capture_filters_and_records_media_outside_global_window(tmp_path):
    base = datetime(2026, 7, 11, 3, 32, 20, tzinfo=timezone.utc)
    images = [{
        "TimeStamp": base - timedelta(milliseconds=200),
        "ImageContent": jpeg(10),
    }]
    images.extend({
        "TimeStamp": base + timedelta(milliseconds=index * 200),
        "ImageContent": jpeg(20 + index),
    } for index in range(6))
    report_path = capture(
        "ch4",
        START,
        END,
        tmp_path / "static",
        "path",
        session_factory=FakeSession([{"Images": images}]).factory,
    )
    report = json.loads(report_path.read_text())
    assert report["frame_count"] == 6
    assert report["discarded_outside_global_window_count"] == 1
    discarded = report["discarded_outside_global_window"][0]
    assert discarded["producer_timestamp_utc"] == "2026-07-11T03:32:19.800Z"
    assert discarded["call_index"] == 0
    assert len(discarded["sha256"]) == 64
    assert discarded["byte_count"] > 0
    assert report["coverage"]["requested_count_complete"] is False
    assert "out_of_requested_window_media_were_discarded" in report[
        "acceptance_failures"
    ]


def test_full_100_image_capability_uses_five_boundary_overlap_segments():
    start = datetime(2026, 7, 11, 3, 32, 20, tzinfo=timezone.utc)
    end = start + timedelta(seconds=19.8)
    segments = build_request_segments(start, end, 200, 100)
    assert len(segments) == 5
    assert [segment["expected_image_count"] for segment in segments] == [
        25,
        25,
        25,
        25,
        4,
    ]
    assert segments[0]["start"] == start
    assert segments[-1]["end"] == end
    for left, right in zip(segments, segments[1:]):
        assert right["start"] == left["end"]


@pytest.mark.parametrize("expected_count", [2, 3, 24, 25, 26, 49, 50, 51, 99, 100])
def test_segment_boundaries_never_create_degenerate_api_windows(expected_count):
    start = datetime(2026, 7, 11, 3, 32, 20, tzinfo=timezone.utc)
    sampling = timedelta(milliseconds=200)
    end = start + sampling * (expected_count - 1)
    segments = build_request_segments(start, end, 200, expected_count)
    assert (
        sum(segment["expected_image_count"] for segment in segments)
        - (len(segments) - 1)
    ) == expected_count
    assert segments[0]["start"] == start
    assert segments[-1]["end"] == end
    assert all(
        2 <= segment["expected_image_count"] <= 25
        and segment["start"] < segment["end"]
        for segment in segments
    )
    for left, right in zip(segments, segments[1:]):
        assert right["start"] == left["end"]


@pytest.mark.parametrize(
    ("start", "end", "sampling_ms", "message"),
    [
        ("2026-07-11T03:32:20Z", END, 200, "millisecond precision"),
        (START, "2026-07-11T03:32:20.999Z", 200, "between 1 and 20"),
        (START, "2026-07-11T03:32:41.000Z", 1000, "between 1 and 20"),
        (START, "2026-07-11T03:32:40.000Z", 200, "100-image"),
        (START, END, 199, "sampling interval"),
        (START, END, 1000, "at least three expected samples"),
    ],
)
def test_capture_rejects_invalid_time_and_image_bounds(
    tmp_path, start, end, sampling_ms, message
):
    with pytest.raises(StaticCaptureError, match=message):
        capture(
            "ch4",
            start,
            end,
            tmp_path / "static",
            "path",
            sampling_ms=sampling_ms,
            session_factory=FakeSession().factory,
        )


def test_capture_discards_documented_gaps_with_sanitized_evidence(tmp_path):
    base = datetime(2026, 7, 11, 3, 32, 20, tzinfo=timezone.utc)
    timestamps = [base + timedelta(milliseconds=index * 200) for index in range(6)]
    responses = [{"Images": [
        {"TimeStamp": timestamps[0], "ImageContent": jpeg(10)},
        {
            "TimeStamp": timestamps[1],
            "ImageContent": b"must-not-persist",
            "Error": "NO_MEDIA",
        },
        {"TimeStamp": timestamps[2], "ImageContent": jpeg(11)},
        {"Error": "MEDIA_ERROR", "ImageContent": b"must-not-persist"},
        {"TimeStamp": timestamps[4], "ImageContent": jpeg(12)},
        {"TimeStamp": timestamps[5], "Error": "NO_MEDIA"},
    ]}]
    output = tmp_path / "static"
    report_path = capture(
        "ch4",
        START,
        END,
        output,
        "path",
        session_factory=FakeSession(responses).factory,
    )
    report_text = report_path.read_text()
    report = json.loads(report_text)
    assert report["frame_count"] == 3
    assert report["unique_jpeg_sha256_count"] == 3
    assert report["discarded_error_count"] == 3
    assert report["discarded_errors_by_code"] == {
        "MEDIA_ERROR": 1,
        "NO_MEDIA": 2,
    }
    assert report["discarded_errors"] == [
        {
            "code": "NO_MEDIA",
            "outside_requested_window": False,
            "producer_timestamp_utc": "2026-07-11T03:32:20.200Z",
            "segment_index": 0,
        },
        {
            "code": "MEDIA_ERROR",
            "outside_requested_window": None,
            "producer_timestamp_utc": None,
            "segment_index": 0,
        },
        {
            "code": "NO_MEDIA",
            "outside_requested_window": False,
            "producer_timestamp_utc": "2026-07-11T03:32:21.000Z",
            "segment_index": 0,
        },
    ]
    assert report["coverage"] == {
        "reason": (
            "GetImages samples and temporal differences are proposal-only; "
            "they do not establish continuous window coverage"
        ),
        "requested_count_complete": False,
        "requested_window_coverage_established": False,
    }
    assert report["request"]["segmentation"][
        "returned_count_matches_bounded_expectation"
    ] is False
    assert "kvs_documented_image_gaps_were_discarded" in report[
        "acceptance_failures"
    ]
    assert "requested_sampling_count_is_incomplete" in report[
        "acceptance_failures"
    ]
    assert "must-not-persist" not in report_text


@pytest.mark.parametrize("valid_count", [0, 2])
def test_capture_rejects_all_or_too_many_documented_gaps(tmp_path, valid_count):
    images = []
    base = datetime(2026, 7, 11, 3, 32, 20, tzinfo=timezone.utc)
    for index in range(6):
        timestamp = base + timedelta(milliseconds=index * 200)
        if index < valid_count:
            images.append({"TimeStamp": timestamp, "ImageContent": jpeg(20 + index)})
        else:
            images.append({"TimeStamp": timestamp, "Error": "NO_MEDIA"})
    output = tmp_path / "static"
    with pytest.raises(StaticCaptureError, match="fewer than three usable"):
        capture(
            "ch4",
            START,
            END,
            output,
            "path",
            session_factory=FakeSession([{"Images": images}]).factory,
        )
    assert not output.exists()
    assert not list(tmp_path.glob(".static.tmp-*"))


def test_capture_rejects_unknown_image_error_code(tmp_path):
    responses = [{"Images": [{
        "TimeStamp": datetime(2026, 7, 11, 3, 32, 20, tzinfo=timezone.utc),
        "Error": "UNEXPECTED_INTERNAL_CODE",
    }]}]
    with pytest.raises(StaticCaptureError, match="unknown image error code"):
        capture(
            "ch4",
            START,
            END,
            tmp_path / "static",
            "path",
            session_factory=FakeSession(responses).factory,
        )


def test_capture_rejects_invalid_jpeg_as_tampered_input(tmp_path):
    responses = [{
        "Images": [
            {
                "TimeStamp": datetime(
                    2026, 7, 11, 3, 32, 20, index * 200000, tzinfo=timezone.utc
                ),
                "ImageContent": b"not-a-jpeg",
            }
            for index in range(3)
        ]
    }]
    with pytest.raises(StaticCaptureError, match="not a complete JPEG"):
        capture(
            "ch4",
            START,
            END,
            tmp_path / "static",
            "path",
            session_factory=FakeSession(responses).factory,
        )


def test_staged_verification_rejects_post_write_tamper(tmp_path):
    frames = tmp_path / "frames"
    frames.mkdir()
    raw = jpeg(20)
    path = frames / "frame-000.jpg"
    path.write_bytes(raw)
    row = {
        "path": "frames/frame-000.jpg",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_count": len(raw),
        "width": 64,
        "height": 48,
    }
    path.write_bytes(jpeg(21))
    with pytest.raises(StaticCaptureError, match="hash binding"):
        verify_staged_frames(tmp_path, [row])


def test_capture_refuses_existing_output_without_api_calls(tmp_path):
    output = tmp_path / "static"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("owned", encoding="utf-8")
    session = FakeSession()
    with pytest.raises(StaticCaptureError, match="already exists"):
        capture(
            "ch4",
            START,
            END,
            output,
            "path",
            session_factory=session.factory,
        )
    assert marker.read_text() == "owned"
    assert session.kvs.calls == []
    assert session.archived.calls == []


def test_atomic_publication_refuses_a_racing_destination(tmp_path):
    source = tmp_path / "staged"
    destination = tmp_path / "static"
    source.mkdir()
    destination.mkdir()
    (source / "new.txt").write_text("new", encoding="utf-8")
    (destination / "owned.txt").write_text("owned", encoding="utf-8")
    with pytest.raises(StaticCaptureError, match="already exists"):
        atomic_publish_directory(source, destination)
    assert (source / "new.txt").read_text() == "new"
    assert (destination / "owned.txt").read_text() == "owned"


def test_capture_rejects_any_pagination_token_in_bounded_segment(tmp_path):
    responses = [{"Images": [], "NextToken": "must-not-be-followed"}]
    session = FakeSession(responses)
    with pytest.raises(StaticCaptureError, match="unexpected pagination token"):
        capture(
            "ch4",
            START,
            END,
            tmp_path / "static",
            "path",
            session_factory=session.factory,
        )
    assert len(session.archived.calls) == 1
    assert "NextToken" not in session.archived.calls[0]


def test_capture_rejects_more_than_one_bounded_segment_can_return(tmp_path):
    base = datetime(2026, 7, 11, 3, 32, 20, tzinfo=timezone.utc)
    responses = [
        {
            "Images": [
                {
                    "TimeStamp": base,
                    "ImageContent": jpeg(10),
                }
            ] * 26,
        },
    ]
    with pytest.raises(StaticCaptureError, match="more than 25 rows"):
        capture(
            "ch4",
            START,
            END,
            tmp_path / "static",
            "path",
            session_factory=FakeSession(responses).factory,
        )
