import hashlib
import importlib.util
import json
from pathlib import Path

import cv2
import numpy as np
import pytest


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "build_temporal_static_targets.py"
)
SPEC = importlib.util.spec_from_file_location("temporal_static_targets", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def jpeg(path, value, width=64, height=48):
    image = np.full((height, width, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    raw = encoded.tobytes()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def event_report(root, name, timestamp, value, camera="ch4", width=64, height=48):
    directory = root / name
    directory.mkdir(parents=True)
    frame_path = directory / "frame.jpg"
    digest = jpeg(frame_path, value, width, height)
    report = {
        "schema": "v2x-detection-event-frame-capture/v1",
        "acceptance_eligible": False,
        "events": [
            {
                "event_id": f"{name}-event",
                "camera_id": camera,
                "selected_frame_timestamp_utc": timestamp,
                "frame": {
                    "path": str(frame_path),
                    "sha256": digest,
                    "width": width,
                    "height": height,
                },
            }
        ],
    }
    path = directory / "capture-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def dense_report(root, name, timestamp, value):
    directory = root / name
    frames = directory / "frames"
    frames.mkdir(parents=True)
    frame_path = frames / "frame-000.jpg"
    digest = jpeg(frame_path, value)
    report = {
        "schema": "v2x-dense-kvs-window/v1",
        "acceptance_eligible": False,
        "camera_id": "ch4",
        "frame_count": 1,
        "resolution": [64, 48],
        "frames": [
            {
                "index": 0,
                "path": "frames/frame-000.jpg",
                "sha256": digest,
                "width": 64,
                "height": 48,
                "producer_timestamp_utc": timestamp,
            }
        ],
    }
    path = directory / "capture-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def three_windows(tmp_path):
    return [
        event_report(tmp_path, "early", "2026-07-10T01:00:00.000Z", 20),
        event_report(tmp_path, "middle", "2026-07-10T02:00:00.000Z", 40),
        dense_report(tmp_path, "late", "2026-07-10T03:00:00.000Z", 60),
    ]


def test_builds_hash_bound_whole_window_split_and_composites(tmp_path):
    reports = three_windows(tmp_path)
    source_before = [path.read_bytes() for path in reports]
    output = tmp_path / "output"
    manifest_path = tool.build_targets(
        reports, "ch4", output, seed="test-seed", minimum_valid_samples=1
    )
    manifest = json.loads(manifest_path.read_text())

    assert manifest["schema"] == tool.OUTPUT_SCHEMA
    assert manifest["acceptance_eligible"] is False
    assert manifest["resolution"] == [64, 48]
    assert {window["split"] for window in manifest["windows"]} == {
        "fit",
        "dev",
        "holdout",
    }
    assert manifest["split_strategy"]["whole_capture_window_atomic"] is True
    assert set(manifest["composites"]) == {"fit", "dev", "holdout"}
    for split, value in manifest["composites"].items():
        assert value["frame_count"] == 1
        for artifact in value["artifacts"].values():
            path = output / artifact["path"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
            assert cv2.imread(str(path), cv2.IMREAD_UNCHANGED).shape[:2] == (48, 64)
    assert [path.read_bytes() for path in reports] == source_before


def test_split_is_deterministic_and_explicit(tmp_path):
    reports = three_windows(tmp_path)
    left = json.loads(tool.build_targets(
        reports, "ch4", tmp_path / "a", minimum_valid_samples=1
    ).read_text())
    right = json.loads(tool.build_targets(
        reports, "ch4", tmp_path / "b", minimum_valid_samples=1
    ).read_text())
    left_assignment = {
        value["window_id"]: value["split"] for value in left["windows"]
    }
    right_assignment = {
        value["window_id"]: value["split"] for value in right["windows"]
    }
    assert left_assignment == right_assignment
    assert left["split_strategy"] == right["split_strategy"]


def test_rejects_tampering_dimension_drift_and_overwrite(tmp_path):
    reports = three_windows(tmp_path)
    first = json.loads(reports[0].read_text())
    Path(first["events"][0]["frame"]["path"]).write_bytes(b"tampered")
    with pytest.raises(tool.StaticTargetError, match="sha256"):
        tool.build_targets(reports, "ch4", tmp_path / "tampered")

    reports = [
        event_report(tmp_path, "x1", "2026-07-10T01:00:00.000Z", 1),
        event_report(tmp_path, "x2", "2026-07-10T02:00:00.000Z", 2),
        event_report(
            tmp_path,
            "x3",
            "2026-07-10T03:00:00.000Z",
            3,
            width=80,
        ),
    ]
    with pytest.raises(tool.StaticTargetError, match="identical dimensions"):
        tool.build_targets(reports, "ch4", tmp_path / "dimensions")

    valid = three_windows(tmp_path / "valid")
    output = tmp_path / "exists"
    tool.build_targets(valid, "ch4", output, minimum_valid_samples=1)
    with pytest.raises(tool.StaticTargetError, match="refusing overwrite"):
        tool.build_targets(valid, "ch4", output, minimum_valid_samples=1)


def test_requires_three_windows_unless_no_split_is_explicit(tmp_path):
    report = event_report(tmp_path, "only", "2026-07-10T01:00:00.000Z", 30)
    with pytest.raises(tool.StaticTargetError, match="at least three"):
        tool.build_targets([report], "ch4", tmp_path / "strict")

    manifest = json.loads(
        tool.build_targets(
            [report],
            "ch4",
            tmp_path / "proposal",
            minimum_valid_samples=1,
            proposal_only_no_split=True,
        ).read_text()
    )
    assert manifest["split_strategy"]["mode"] == "explicit_no_split_proposal_only"
    assert list(manifest["composites"]) == ["proposal"]
    assert manifest["windows"][0]["split"] == "proposal"
    assert manifest["acceptance_eligible"] is False


def test_default_rejects_single_sample_stability_and_window_id_is_path_independent(tmp_path):
    report = event_report(tmp_path, "single", "2026-07-10T01:00:00.000Z", 30)
    with pytest.raises(tool.StaticTargetError, match="fewer frames"):
        tool.build_targets(
            [report], "ch4", tmp_path / "single-output",
            proposal_only_no_split=True,
        )

    copied_root = tmp_path / "copied"
    copied_root.mkdir()
    copied_frame = copied_root / "frame.jpg"
    original = json.loads(report.read_text())
    copied_frame.write_bytes(Path(original["events"][0]["frame"]["path"]).read_bytes())
    original["events"][0]["frame"]["path"] = str(copied_frame)
    copied_report = copied_root / "capture-report.json"
    copied_report.write_text(json.dumps(original, sort_keys=True))

    # Make the report bytes identical after expressing both frame paths as the
    # same relative name; identity must be content-bound, never directory-bound.
    original["events"][0]["frame"]["path"] = "frame.jpg"
    report.write_text(json.dumps(original, sort_keys=True))
    copied_report.write_text(json.dumps(original, sort_keys=True))
    assert tool.load_window(report, "ch4")["window_id"] == tool.load_window(
        copied_report, "ch4"
    )["window_id"]


def test_requires_requested_single_camera_and_reported_dimensions(tmp_path):
    wrong_camera = event_report(
        tmp_path, "wrong", "2026-07-10T01:00:00.000Z", 30, camera="ch3"
    )
    with pytest.raises(tool.StaticTargetError, match="no ch4 frames"):
        tool.build_targets(
            [wrong_camera],
            "ch4",
            tmp_path / "wrong-output",
            proposal_only_no_split=True,
        )
    report = event_report(tmp_path, "bad-size", "2026-07-10T02:00:00.000Z", 40)
    value = json.loads(report.read_text())
    value["events"][0]["frame"]["width"] = 65
    report.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(tool.StaticTargetError, match="decoded dimensions"):
        tool.build_targets(
            [report],
            "ch4",
            tmp_path / "bad-size-output",
            proposal_only_no_split=True,
        )


def test_temporal_median_mad_and_stability_mask(tmp_path):
    directory = tmp_path / "sequence"
    directory.mkdir()
    events = []
    for index, value in enumerate((0, 20, 100)):
        frame_path = directory / f"frame-{index}.jpg"
        digest = jpeg(frame_path, value)
        events.append(
            {
                "event_id": f"event-{index}",
                "camera_id": "ch4",
                "selected_frame_timestamp_utc": (
                    f"2026-07-10T01:00:0{index}.000Z"
                ),
                "frame": {
                    "path": str(frame_path),
                    "sha256": digest,
                    "width": 64,
                    "height": 48,
                },
            }
        )
    report = directory / "capture-report.json"
    report.write_text(
        json.dumps(
            {
                "schema": "v2x-detection-event-frame-capture/v1",
                "acceptance_eligible": False,
                "events": events,
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "sequence-output"
    manifest = json.loads(
        tool.build_targets(
            [report],
            "ch4",
            output,
            stability_mad_threshold=10,
            proposal_only_no_split=True,
        ).read_text()
    )
    median = cv2.imread(
        str(output / manifest["composites"]["proposal"]["artifacts"]["median_rgb"]["path"])
    )
    mad = cv2.imread(
        str(output / manifest["composites"]["proposal"]["artifacts"]["mad_rgb"]["path"])
    )
    mask = cv2.imread(
        str(output / manifest["composites"]["proposal"]["artifacts"]["stability_mask"]["path"]),
        cv2.IMREAD_GRAYSCALE,
    )
    assert np.all(median == 20)
    assert np.all(mad == 20)
    assert np.all(mask == 0)
    assert manifest["composites"]["proposal"]["stable_pixel_fraction"] == 0.0


def test_deduplicates_one_frame_referenced_by_multiple_events(tmp_path):
    report = event_report(tmp_path, "shared", "2026-07-10T01:00:00.000Z", 30)
    value = json.loads(report.read_text())
    value["events"][0]["bbox_xyxy"] = [2, 2, 12, 12]
    duplicate = dict(value["events"][0])
    duplicate["event_id"] = "shared-second-detection"
    duplicate["bbox_xyxy"] = [40, 30, 50, 40]
    value["events"].append(duplicate)
    report.write_text(json.dumps(value))

    output = tmp_path / "shared-output"
    manifest = json.loads(
        tool.build_targets(
            [report], "ch4", output, minimum_valid_samples=1,
            proposal_only_no_split=True
        ).read_text()
    )
    assert manifest["composites"]["proposal"]["frame_count"] == 1
    frame = manifest["windows"][0]["frames"][0]
    assert frame["source_identities"] == [
        "shared-event",
        "shared-second-detection",
    ]
    assert len(frame["dynamic_exclusion_boxes"]) == 2
    assert all(
        value["provenance"] == "persisted_detection_bbox_proposal_only"
        and value["acceptance_eligible"] is False
        for value in frame["dynamic_exclusion_boxes"]
    )
    assert frame["dynamic_exclusion_boxes"][0][
        "expanded_clamped_bbox_xyxy"
    ][0] == 0
    artifacts = manifest["composites"]["proposal"]["artifacts"]
    canonical = cv2.imread(str(output / artifacts["median_rgb"]["path"]))
    raw = cv2.imread(str(output / artifacts["raw_median_rgb_diagnostic"]["path"]))
    validity = cv2.imread(
        str(output / artifacts["validity_mask"]["path"]), cv2.IMREAD_GRAYSCALE
    )
    assert np.all(canonical[7, 7] == 0)
    assert np.all(canonical[35, 45] == 0)
    assert np.all(raw[7, 7] == 30)
    assert validity[7, 7] == 0
    assert validity[35, 45] == 0
    assert validity[20, 25] == 255
    assert "dynamic_exclusion_masks_are_detection_proposals_not_truth" in manifest[
        "acceptance_failures"
    ]
    assert manifest["dynamic_exclusion"][
        "duplicate_frame_boxes_combined_by_union"
    ] is True


def test_accepts_detection_independent_static_window_schema(tmp_path):
    report = dense_report(
        tmp_path, "static", "2026-07-10T01:00:00.000Z", 30
    )
    value = json.loads(report.read_text())
    value["schema"] = "v2x-static-kvs-window-proposal/v1"
    report.write_text(json.dumps(value))
    manifest = json.loads(
        tool.build_targets(
            [report],
            "ch4",
            tmp_path / "static-output",
            minimum_valid_samples=1,
            proposal_only_no_split=True,
        ).read_text()
    )
    assert manifest["windows"][0]["capture_report"]["schema"] == value["schema"]


def test_insufficient_unmasked_coverage_cannot_be_stable(tmp_path):
    directory = tmp_path / "coverage"
    directory.mkdir()
    events = []
    for index, value in enumerate((20, 40, 60)):
        frame_path = directory / f"frame-{index}.jpg"
        digest = jpeg(frame_path, value)
        event = {
            "event_id": f"coverage-{index}",
            "camera_id": "ch4",
            "selected_frame_timestamp_utc": f"2026-07-10T01:00:0{index}.000Z",
            "frame": {
                "path": str(frame_path),
                "sha256": digest,
                "width": 64,
                "height": 48,
            },
        }
        if index < 2:
            event["bbox_xyxy"] = [10, 10, 30, 30]
        events.append(event)
    report = directory / "capture-report.json"
    report.write_text(
        json.dumps(
            {
                "schema": "v2x-detection-event-frame-capture/v1",
                "acceptance_eligible": False,
                "events": events,
            }
        )
    )
    output = tmp_path / "coverage-output"
    manifest = json.loads(
        tool.build_targets(
            [report],
            "ch4",
            output,
            dynamic_bbox_expansion_fraction=0,
            minimum_valid_samples=2,
            minimum_valid_fraction=0.5,
            proposal_only_no_split=True,
        ).read_text()
    )
    composite = manifest["composites"]["proposal"]
    artifacts = composite["artifacts"]
    count = cv2.imread(
        str(output / artifacts["valid_sample_count"]["path"]),
        cv2.IMREAD_UNCHANGED,
    )
    validity = cv2.imread(
        str(output / artifacts["validity_mask"]["path"]), cv2.IMREAD_GRAYSCALE
    )
    stability = cv2.imread(
        str(output / artifacts["stability_mask"]["path"]), cv2.IMREAD_GRAYSCALE
    )
    canonical = cv2.imread(str(output / artifacts["median_rgb"]["path"]))
    raw = cv2.imread(str(output / artifacts["raw_median_rgb_diagnostic"]["path"]))
    assert count.dtype == np.uint16
    assert count[20, 20] == 1
    assert count[5, 5] == 3
    assert validity[20, 20] == 0
    assert stability[20, 20] == 0
    assert validity[5, 5] == 255
    assert np.all(canonical[20, 20] == 0)
    assert np.all(raw[20, 20] == 40)
    assert composite["required_valid_samples_per_pixel"] == 2
    assert composite["valid_pixel_fraction"] < 1.0
    assert manifest["safety"]["masked_pixels_can_be_marked_stable"] is False
