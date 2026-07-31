import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import propose_dense_vehicle_tracks as tracks  # noqa: E402


def test_anchor_selection_requires_one_stable_tracker_id():
    tracked = [
        {"instances": [{"track_id": 7, "confidence": 0.9, "bbox_xyxy": [10, 10, 30, 30]}]},
        {"instances": [{"track_id": 7, "confidence": 0.9, "bbox_xyxy": [20, 10, 40, 30]}]},
    ]
    anchors = [
        {"event_id": "e1", "frame_index": 0, "frame_sha256": "a", "timestamp_utc": "2026-07-13T00:00:00.000Z"},
        {"event_id": "e2", "frame_index": 1, "frame_sha256": "b", "timestamp_utc": "2026-07-13T00:00:00.200Z"},
    ]
    consensus = {
        ("ch1", "e1"): {"bbox_xyxy": [10, 10, 30, 30], "frame_sha256": "a", "mask_iou": 0.9},
        ("ch1", "e2"): {"bbox_xyxy": [20, 10, 40, 30], "frame_sha256": "b", "mask_iou": 0.9},
    }

    target, matches, reasons = tracks.select_target_track(
        tracked, anchors, consensus, "ch1"
    )

    assert target == 7
    assert len(matches) == 2
    assert reasons == []


def test_anchor_tracker_switch_rejects_sequence():
    tracked = [
        {"instances": [{"track_id": 7, "confidence": 0.9, "bbox_xyxy": [10, 10, 30, 30]}]},
        {"instances": [{"track_id": 8, "confidence": 0.9, "bbox_xyxy": [20, 10, 40, 30]}]},
    ]
    anchors = [
        {"event_id": "e1", "frame_index": 0, "frame_sha256": "a", "timestamp_utc": "2026-07-13T00:00:00.000Z"},
        {"event_id": "e2", "frame_index": 1, "frame_sha256": "b", "timestamp_utc": "2026-07-13T00:00:00.200Z"},
    ]
    consensus = {
        ("ch1", "e1"): {"bbox_xyxy": [10, 10, 30, 30], "frame_sha256": "a", "mask_iou": 0.9},
        ("ch1", "e2"): {"bbox_xyxy": [20, 10, 40, 30], "frame_sha256": "b", "mask_iou": 0.9},
    }

    target, matches, reasons = tracks.select_target_track(
        tracked, anchors, consensus, "ch1"
    )

    assert target is None
    assert len(matches) == 2
    assert reasons == ["anchor_tracker_identity_conflict"]


def test_anchor_selection_rejects_overlapping_plausible_candidates():
    tracked = [{"instances": [
        {"track_id": 7, "confidence": 0.9, "bbox_xyxy": [10, 10, 30, 30]},
        {"track_id": 8, "confidence": 0.8, "bbox_xyxy": [10, 10, 30, 30]},
    ]}]
    anchors = [{
        "event_id": "e1",
        "frame_index": 0,
        "frame_sha256": "a",
        "timestamp_utc": "2026-07-13T00:00:00.000Z",
    }]
    consensus = {("ch1", "e1"): {
        "bbox_xyxy": [10, 10, 30, 30],
        "frame_sha256": "a",
        "mask_iou": 0.9,
    }}

    target, matches, reasons = tracks.select_target_track(
        tracked, anchors, consensus, "ch1"
    )

    assert target is None
    assert matches == []
    assert reasons == ["anchor_ambiguous:e1", "no_cross_model_anchor_matched"]


def test_two_event_ids_on_one_source_frame_do_not_count_as_two_anchors():
    tracked = [{"instances": [
        {"track_id": 7, "confidence": 0.9, "bbox_xyxy": [10, 10, 30, 30]},
    ]}]
    anchors = [
        {
            "event_id": "e1", "frame_index": 0, "frame_sha256": "a",
            "timestamp_utc": "2026-07-13T00:00:00.000Z",
        },
        {
            "event_id": "e2", "frame_index": 0, "frame_sha256": "a",
            "timestamp_utc": "2026-07-13T00:00:00.000Z",
        },
    ]
    consensus = {
        ("ch1", event_id): {
            "bbox_xyxy": [10, 10, 30, 30], "frame_sha256": "a", "mask_iou": 0.9,
        }
        for event_id in ("e1", "e2")
    }

    target, matches, reasons = tracks.select_target_track(
        tracked, anchors, consensus, "ch1"
    )

    assert target is None
    assert len(matches) == 2
    assert reasons == ["duplicate_anchor_source_frame"]


def test_unmatched_anchor_cannot_be_silently_promoted():
    tracked = [{"instances": [{
        "track_id": 2, "confidence": 0.9, "bbox_xyxy": [80, 80, 100, 100]
    }]}]
    anchors = [{
        "event_id": "e1", "frame_index": 0, "frame_sha256": "a", "timestamp_utc": "2026-07-13T00:00:00.000Z"
    }]
    consensus = {
        ("ch1", "e1"): {
            "bbox_xyxy": [10, 10, 30, 30], "frame_sha256": "a", "mask_iou": 0.9
        }
    }

    target, matches, reasons = tracks.select_target_track(
        tracked, anchors, consensus, "ch1"
    )

    assert target is None
    assert matches == []
    assert reasons == ["anchor_unmatched:e1", "no_cross_model_anchor_matched"]


def test_retained_track_mask_is_largest_component_inside_bbox():
    mask = np.zeros((100, 100), dtype=np.float32)
    mask[30:70, 30:70] = 1.0
    mask[10:20, 10:20] = 1.0
    mask[75:82, 75:82] = 1.0

    class Data:
        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return np.asarray([mask])

    class Masks:
        data = Data()

    class Box:
        cls = np.asarray([2])
        conf = np.asarray([0.95])
        xyxy = np.asarray([[20.0, 20.0, 80.0, 80.0]])

    class Boxes:
        id = np.asarray([7.0])

        def __len__(self):
            return 1

        def __iter__(self):
            return iter([Box()])

    class Result:
        boxes = Boxes()
        masks = Masks()
        names = {2: "car"}

    instances = tracks.model_instances_from_result(
        Result(), np.zeros((100, 100, 3), dtype=np.uint8)
    )

    assert len(instances) == 1
    clean = instances[0]["mask"]
    assert clean[35, 35]
    assert not clean[12, 12]
    assert not clean[77, 77]

    Result.boxes.id = np.asarray([7.5])
    with pytest.raises(tracks.DenseTrackError, match="non-integral"):
        tracks.model_instances_from_result(
            Result(), np.zeros((100, 100, 3), dtype=np.uint8)
        )

    Result.boxes.id = np.asarray([7.0])

    Box.xyxy = np.asarray([[20.0, 20.0, 80.0, 80.0], [1.0, 1.0, 2.0, 2.0]])
    with pytest.raises(tracks.DenseTrackError, match="bbox.*exactly 4"):
        tracks.model_instances_from_result(
            Result(), np.zeros((100, 100, 3), dtype=np.uint8)
        )
    Box.xyxy = np.asarray([[20.0, 20.0, 80.0, 80.0]])

    mask[30, 30] = 1.1
    with pytest.raises(tracks.DenseTrackError, match=r"masks.*outside \[0, 1\]"):
        tracks.model_instances_from_result(
            Result(), np.zeros((100, 100, 3), dtype=np.uint8)
        )
    mask[30, 30] = 1.0
    for malformed_class in (
        np.asarray([2.5]), np.asarray([np.nan]), np.asarray([2, 3]),
        np.asarray([999]), np.asarray([True]), np.asarray(["2"]),
    ):
        Box.cls = malformed_class
        with pytest.raises(tracks.DenseTrackError, match="class"):
            tracks.model_instances_from_result(
                Result(), np.zeros((100, 100, 3), dtype=np.uint8)
            )
    Box.cls = np.asarray([2])

    for malformed_confidence in (
        np.asarray([np.nan]), np.asarray([0.9, 0.8]), np.asarray([1.1]),
        np.asarray([True]), np.asarray(["0.9"]),
    ):
        Box.conf = malformed_confidence
        with pytest.raises(tracks.DenseTrackError, match="confidence"):
            tracks.model_instances_from_result(
                Result(), np.zeros((100, 100, 3), dtype=np.uint8)
            )
    Box.conf = np.asarray([0.95])

    for malformed_ids in (
        np.asarray([np.nan]), np.asarray([7.0, 8.0]), np.asarray([-1.0]),
        np.asarray([True]), np.asarray(["7"]),
    ):
        Result.boxes.id = malformed_ids
        with pytest.raises(tracks.DenseTrackError, match="tracking ID|tracker ID"):
            tracks.model_instances_from_result(
                Result(), np.zeros((100, 100, 3), dtype=np.uint8)
            )
    Result.boxes.id = np.asarray([7.0])


def _jpeg(value):
    image = np.full((48, 64, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def write_dense_fixture(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, value in enumerate((20, 80, 140)):
        raw = _jpeg(value)
        path = tmp_path / "frames" / f"frame-{index:03d}.jpg"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        frames.append({
            "index": index,
            "path": f"frames/{path.name}",
            "sha256": _sha(raw),
            "byte_count": len(raw),
            "width": 64,
            "height": 48,
            "producer_timestamp_utc": f"2026-07-13T18:09:44.{index * 200:03d}Z",
        })
    source = tmp_path / "source.json"
    source_events = [
        {
            "event_id": "event-1",
            "camera_id": "ch1",
            "object_id": "proposal-1",
            "selected_frame_timestamp_utc": frames[0]["producer_timestamp_utc"],
            "frame": {
                "path": str((tmp_path / frames[0]["path"]).resolve()),
                "sha256": frames[0]["sha256"],
            },
        },
        {
            "event_id": "event-2",
            "camera_id": "ch1",
            "object_id": "proposal-1",
            "selected_frame_timestamp_utc": frames[2]["producer_timestamp_utc"],
            "frame": {
                "path": str((tmp_path / frames[2]["path"]).resolve()),
                "sha256": frames[2]["sha256"],
            },
        },
    ]
    source.write_text(json.dumps({
        "schema": "v2x-detection-event-frame-capture/v2",
        "events": source_events,
    }))
    report = {
        "schema": tracks.DENSE_SCHEMA,
        "camera_id": "ch1",
        "object_id": "proposal-1",
        "frame_count": len(frames),
        "frames": frames,
        "resolution": [64, 48],
        "acceptance_eligible": False,
        "source_event_report": {
            "path": str(source.resolve()),
            "sha256": _sha(source.read_bytes()),
        },
        "source_events": [
            {
                "event_id": event["event_id"],
                "frame_sha256": event["frame"]["sha256"],
                "selected_frame_timestamp_utc": event[
                    "selected_frame_timestamp_utc"
                ],
            }
            for event in source_events
        ],
    }
    path = tmp_path / "capture-report.json"
    path.write_text(json.dumps(report))
    return path, report


def test_dense_report_recomputes_every_frame_binding(tmp_path):
    path, report = write_dense_fixture(tmp_path)

    loaded = tracks.load_dense_report(path)

    assert loaded[2]["object_id"] == "proposal-1"
    assert len(loaded[3]) == 3
    assert [anchor["frame_index"] for anchor in loaded[4]] == [0, 2]

    (tmp_path / report["frames"][1]["path"]).write_bytes(b"tampered")
    with pytest.raises(tracks.DenseTrackError, match="byte identity"):
        tracks.load_dense_report(path)


def test_dense_report_rejects_path_escape(tmp_path):
    path, report = write_dense_fixture(tmp_path)
    report["frames"][0]["path"] = "../outside.jpg"
    path.write_text(json.dumps(report))

    with pytest.raises(tracks.DenseTrackError, match="escapes"):
        tracks.load_dense_report(path)


def test_dense_report_rejects_unrelated_or_drifted_source_denominator(tmp_path):
    path, report = write_dense_fixture(tmp_path)
    source_path = Path(report["source_event_report"]["path"])
    source_path.write_text("{}\n")
    report["source_event_report"]["sha256"] = _sha(source_path.read_bytes())
    path.write_text(json.dumps(report))
    with pytest.raises(tracks.DenseTrackError, match="invalid or unrelated"):
        tracks.load_dense_report(path)

    path, report = write_dense_fixture(tmp_path / "fresh")
    report["source_events"][0]["event_id"] = "fabricated"
    path.write_text(json.dumps(report))
    with pytest.raises(tracks.DenseTrackError, match="drift"):
        tracks.load_dense_report(path)

    path, _report = write_dense_fixture(tmp_path / "denominator")
    with pytest.raises(tracks.DenseTrackError, match="consensus capture denominator"):
        tracks.load_dense_report(path, expected_source_report_sha256="f" * 64)


def write_consensus_fixture(tmp_path):
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({
        "schema": "v2x-detection-event-frame-capture/v2",
        "events": [{"event_id": "event-1"}],
    }))
    reports = []
    models = []
    for side, shift in (("left", 0), ("right", 1)):
        model = tmp_path / f"{side}.pt"
        model.write_bytes(f"{side}-model".encode())
        models.append(model)
        mask = np.zeros((100, 160), dtype=np.uint8)
        mask[35:80, 40 + shift:120 + shift] = 255
        mask_path = tmp_path / f"{side}.png"
        assert cv2.imwrite(str(mask_path), mask)
        report = {
            "schema": "v2x-segmentation-ground-contact-proposals/v1",
            "acceptance_eligible": False,
            "capture_report": {"path": str(capture), "sha256": _sha(capture.read_bytes())},
            "model": {"path": str(model), "sha256": _sha(model.read_bytes())},
            "events": [{
                "event_id": "event-1",
                "camera_id": "ch1",
                "selected_frame_timestamp_utc": "2026-07-13T00:00:00.000Z",
                "frame": {"encoded_jpeg_sha256": "f" * 64, "width": 160, "height": 100},
                "matched_instance": {"bbox_xyxy": [40 + shift, 35, 120 + shift, 80]},
                "ground_contact_proposal": {
                    "pixel": [80 + shift, 79], "covariance_px2": [[4, 0], [0, 4]],
                },
                "mask": {"path": str(mask_path), "sha256": _sha(mask_path.read_bytes())},
            }],
        }
        report_path = tmp_path / f"{side}.json"
        report_path.write_text(json.dumps(report))
        reports.append(report_path)
    consensus = tracks.rebuild_segmentation_consensus(*reports)
    consensus_path = tmp_path / "consensus.json"
    consensus_path.write_text(json.dumps(consensus))
    return consensus_path, reports, models


def write_bound_pipeline_fixture(tmp_path):
    capture, dense = write_dense_fixture(tmp_path / "dense")
    source_path = Path(dense["source_event_report"]["path"])
    reports = []
    models = []
    masks = []
    for side in ("left", "right"):
        model = tmp_path / f"{side}.pt"
        model.write_bytes(f"{side}-model".encode())
        models.append(model)
        events = []
        for event_index, source_event in enumerate(dense["source_events"]):
            mask = np.zeros((48, 64), dtype=np.uint8)
            mask[12:39, 14:50] = 255
            mask_path = tmp_path / f"{side}-{event_index}.png"
            assert cv2.imwrite(str(mask_path), mask)
            masks.append(mask_path)
            events.append({
                "event_id": source_event["event_id"],
                "camera_id": "ch1",
                "selected_frame_timestamp_utc": source_event[
                    "selected_frame_timestamp_utc"
                ],
                "frame": {
                    "encoded_jpeg_sha256": source_event["frame_sha256"],
                    "width": 64,
                    "height": 48,
                },
                "matched_instance": {"bbox_xyxy": [10, 8, 54, 42]},
                "ground_contact_proposal": {
                    "pixel": [31.5, 38.0],
                    "covariance_px2": [[4, 0], [0, 4]],
                },
                "mask": {"path": str(mask_path), "sha256": _sha(mask_path.read_bytes())},
            })
        report = {
            "schema": "v2x-segmentation-ground-contact-proposals/v1",
            "acceptance_eligible": False,
            "capture_report": {
                "path": str(source_path),
                "sha256": _sha(source_path.read_bytes()),
            },
            "model": {"path": str(model), "sha256": _sha(model.read_bytes())},
            "events": events,
        }
        report_path = tmp_path / f"{side}.json"
        report_path.write_text(json.dumps(report))
        reports.append(report_path)
    consensus = tracks.rebuild_segmentation_consensus(*reports)
    consensus_path = tmp_path / "consensus.json"
    consensus_path.write_text(json.dumps(consensus))
    return {
        "capture": capture,
        "dense": dense,
        "source": source_path,
        "frames": [capture.parent / row["path"] for row in dense["frames"]],
        "consensus": consensus_path,
        "reports": reports,
        "models": models,
        "masks": masks,
    }


def test_consensus_requires_both_exact_retained_model_artifacts(tmp_path):
    consensus, reports, models = write_consensus_fixture(tmp_path)
    selected_hash = _sha(models[0].read_bytes())
    loaded = tracks.load_consensus(consensus, selected_hash)
    assert loaded[4] == "left"

    models[1].unlink()
    with pytest.raises(tracks.DenseTrackError, match="model identity"):
        tracks.load_consensus(consensus, selected_hash)

    models[1].write_bytes(b"right-model")
    value = json.loads(reports[1].read_text())
    value["events"][0]["matched_instance"]["bbox_xyxy"] = [1, 1, 2, 2]
    reports[1].write_text(json.dumps(value))
    with pytest.raises(tracks.DenseTrackError, match="report or model identity"):
        tracks.load_consensus(consensus, selected_hash)


def test_dense_consensus_loader_defensively_rejects_nonfinite_source_bbox(
    tmp_path, monkeypatch
):
    consensus, _reports, models = write_consensus_fixture(tmp_path)
    value = json.loads(consensus.read_text())
    value["events"][0]["right"]["matched_instance"]["bbox_xyxy"] = [
        float("nan"), 35, 119, 80
    ]
    consensus.write_text(json.dumps(value))
    monkeypatch.setattr(
        tracks, "rebuild_segmentation_consensus",
        lambda *_paths: json.loads(consensus.read_text()),
    )

    with pytest.raises(tracks.DenseTrackError, match="right bbox is invalid"):
        tracks.load_consensus(consensus, _sha(models[0].read_bytes()))


def _valid_track_instance(contact_x=31.5):
    mask = np.zeros((48, 64), dtype=bool)
    mask[12:32, 14:50] = True
    mask[30:39, 17:24] = True
    mask[30:39, 41:48] = True
    bbox = [10.0, 8.0, 54.0, 42.0]
    proposal = tracks.estimate_contact(mask, bbox)
    proposal["pixel"][0] = contact_x
    return {
        "track_id": 1,
        "label": "car",
        "confidence": 0.9,
        "bbox_xyxy": bbox,
        "mask": mask,
        "ground_contact_proposal": proposal,
        "rejection_reasons": [],
    }


def test_window_with_no_valid_contacts_is_never_review_ready(tmp_path, monkeypatch):
    capture, report = write_dense_fixture(tmp_path / "capture")
    source_hash = report["source_event_report"]["sha256"]
    instance = _valid_track_instance()
    instance["ground_contact_proposal"] = None
    instance["rejection_reasons"] = ["mask_has_no_supported_bottom_edge"]
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    consensus = {
        ("ch1", event["event_id"]): {
            "bbox_xyxy": instance["bbox_xyxy"],
            "frame_sha256": event["frame_sha256"],
            "mask_iou": 0.9,
        }
        for event in report["source_events"]
    }
    staged = tmp_path / "staged"
    staged.mkdir()
    row = tracks.process_window(
        capture, consensus, tmp_path / "model.pt", staged, lambda _path: Model(),
        0.2, 0.7, 1280, "cpu",
        expected_source_report_sha256=source_hash,
        model_sha256="a" * 64,
        consensus_sha256="b" * 64,
    )

    assert row["proposal_status"] == "rejected"
    assert row["summary"]["contact_proposal_count"] == 0
    assert "not_every_input_frame_has_a_valid_contact_and_mask" in row[
        "rejection_reasons"
    ]
    assert "mask_has_no_supported_bottom_edge" in row["rejection_reasons"]


def test_temporal_gate_rejects_343px_contact_flip_with_stationary_box_and_mask():
    states = []
    contacts = ([20.0, 40.0], [20.0, 40.0], [363.0, 40.0], [20.0, 40.0])
    for index, contact in enumerate(contacts):
        states.append({
            "frame_index": index,
            "timestamp_epoch": index * 0.2,
            "contact": np.asarray(contact),
            "bbox_center": np.asarray([32.0, 24.0]),
            "mask_centroid": np.asarray([32.0, 24.0]),
            "row": {"rejection_reasons": []},
        })

    diagnostics, reasons, gate = tracks.evaluate_contact_temporal_consistency(
        states, 2560, 1920
    )

    assert max(row["bbox_center_residual_jump_px"] for row in diagnostics) == 343.0
    assert gate["maximum_residual_jump_px"] == 48.0
    assert "contact_bbox_residual_jump_above_gate" in reasons
    assert "contact_mask_residual_acceleration_above_gate" in reasons


def test_hash_sequence_ids_separate_same_basenames_and_artifacts_never_clobber(tmp_path):
    left_root = tmp_path / "left" / "same"
    right_root = tmp_path / "right" / "same"
    left_root.mkdir(parents=True)
    right_root.mkdir(parents=True)
    left_path, left = write_dense_fixture(left_root)
    right_path, right = write_dense_fixture(right_root)
    left_id = tracks.dense_sequence_identity(
        left_path, left_path.read_bytes(), left, "a" * 64, "b" * 64
    )[0]
    right_id = tracks.dense_sequence_identity(
        right_path, right_path.read_bytes(), right, "a" * 64, "b" * 64
    )[0]
    assert left_path.parent.name == right_path.parent.name == "same"
    assert left_id != right_id

    destination = tmp_path / "exclusive.bin"

    def writer(value):
        try:
            tracks.write_bytes_exclusive(destination, value, "test artifact")
            return "written"
        except tracks.DenseTrackError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(writer, (b"left", b"right")))
    assert results == ["test artifact path collision", "written"]
    assert destination.read_bytes() in {b"left", b"right"}


def test_propose_publishes_one_verified_model_snapshot_and_bound_artifact_tree(
    tmp_path, monkeypatch
):
    capture, dense = write_dense_fixture(tmp_path / "capture")
    model = tmp_path / "model.pt"
    model.write_bytes(b"model-bytes")
    consensus_file = tmp_path / "consensus.json"
    consensus_raw = b'{"diagnostic":true}\n'
    consensus_file.write_bytes(consensus_raw)
    instance = _valid_track_instance()
    consensus_index = {
        ("ch1", event["event_id"]): {
            "bbox_xyxy": instance["bbox_xyxy"],
            "frame_sha256": event["frame_sha256"],
            "mask_iou": 0.9,
        }
        for event in dense["source_events"]
    }
    monkeypatch.setattr(
        tracks,
        "load_consensus",
        lambda _path, _hash: (
            consensus_file.resolve(), consensus_raw,
            {"capture_report_sha256": dense["source_event_report"]["sha256"]},
            consensus_index, "left", {
                "consensus": tracks.pin_source_file(
                    consensus_file, _sha(consensus_raw), "test consensus"
                ),
                "capture_report": tracks.pin_source_file(
                    dense["source_event_report"]["path"],
                    dense["source_event_report"]["sha256"],
                    "test capture report",
                ),
                "inputs": [],
            },
        ),
    )
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    output = tmp_path / "output"
    tracks.propose(
        [capture], consensus_file, model, output,
        model_factory=lambda _path: Model(), image_size=1280,
    )

    report = json.loads((output / "report.json").read_text())
    sequence = report["sequences"][0]
    assert sequence["proposal_status"] == "ready_for_independent_review"
    assert sequence["summary"]["contact_proposal_count"] == 3
    assert sequence["summary"]["temporal_contact_rejection_pair_count"] == 0
    snapshot = output / report["model"]["execution_snapshot"]["path"]
    assert snapshot.read_bytes() == model.read_bytes()
    assert report["consensus"]["path"].startswith("inputs/reports/")
    assert (output / report["consensus"]["path"]).is_file()
    assert sequence["capture_report"]["path"].startswith("inputs/reports/")
    assert sequence["capture_report"]["source_path"] == str(capture.resolve())
    assert len(sequence["input_frames"]) == 3
    for descriptor in sequence["input_frames"]:
        assert (output / descriptor["path"]).is_file()
        assert _sha((output / descriptor["path"]).read_bytes()) == descriptor["sha256"]
    for line in (output / "SHA256SUMS").read_text().splitlines():
        expected, relative = line.split("  ", 1)
        assert _sha((output / relative).read_bytes()) == expected
    assert not (output / tracks.STAGING_OWNER_MARKER).exists()


def test_propose_snapshots_every_bound_input_before_inference(tmp_path, monkeypatch):
    fixture = write_bound_pipeline_fixture(tmp_path)
    instance = _valid_track_instance()
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    output = tmp_path / "output"
    tracks.propose(
        [fixture["capture"]], fixture["consensus"], fixture["models"][0], output,
        model_factory=lambda _path: Model(), image_size=1280,
    )

    report = json.loads((output / "report.json").read_text())
    sequence = report["sequences"][0]
    descriptors = [
        report["consensus"],
        report["consensus"]["capture_report"],
        report["model"]["execution_snapshot"],
        sequence["capture_report"],
        sequence["source_event_report"],
        *sequence["input_frames"],
    ]
    for consensus_input in report["consensus"]["inputs"]:
        descriptors.extend([
            consensus_input["report"], consensus_input["model"],
            *consensus_input["masks"],
        ])
    assert len(report["consensus"]["inputs"]) == 2
    assert len(sequence["input_frames"]) == 3
    assert all(descriptor["path"].startswith("inputs/") for descriptor in descriptors)
    assert all(Path(descriptor["source_path"]).is_absolute() for descriptor in descriptors)
    for descriptor in descriptors:
        snapshot = output / descriptor["path"]
        assert snapshot.is_file()
        assert _sha(snapshot.read_bytes()) == descriptor["sha256"]
    for line in (output / "SHA256SUMS").read_text().splitlines():
        expected, relative = line.split("  ", 1)
        assert _sha((output / relative).read_bytes()) == expected


@pytest.mark.parametrize(
    "source_key,index",
    [
        ("capture", None),
        ("source", None),
        ("frames", 0),
        ("consensus", None),
        ("reports", 0),
        ("reports", 1),
        ("models", 0),
        ("models", 1),
        ("masks", 0),
        ("masks", 2),
    ],
)
def test_mutation_of_any_bound_input_during_inference_fails_and_cleans(
    tmp_path, monkeypatch, source_key, index
):
    fixture = write_bound_pipeline_fixture(tmp_path)
    instance = _valid_track_instance()
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )
    source = fixture[source_key] if index is None else fixture[source_key][index]

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    mutated = False

    def factory(_path):
        nonlocal mutated
        if not mutated:
            source.write_bytes(source.read_bytes() + b"\nmutated")
            mutated = True
        return Model()

    output = tmp_path / "output"
    with pytest.raises(tracks.DenseTrackError, match="changed during dense tracking"):
        tracks.propose(
            [fixture["capture"]], fixture["consensus"], fixture["models"][0],
            output, model_factory=factory, image_size=1280,
        )
    assert not output.exists()
    assert list(tmp_path.glob(f".{output.name}.tmp-*")) == []


def test_same_content_source_replacement_during_inference_fails_and_cleans(
    tmp_path, monkeypatch
):
    fixture = write_bound_pipeline_fixture(tmp_path)
    instance = _valid_track_instance()
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )
    source = fixture["capture"]

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    def factory(_path):
        replacement = source.with_suffix(".replacement")
        replacement.write_bytes(source.read_bytes())
        replacement.replace(source)
        return Model()

    output = tmp_path / "output"
    with pytest.raises(tracks.DenseTrackError, match="changed during dense tracking"):
        tracks.propose(
            [fixture["capture"]], fixture["consensus"], fixture["models"][0],
            output, model_factory=factory, image_size=1280,
        )
    assert not output.exists()
    assert list(tmp_path.glob(f".{output.name}.tmp-*")) == []


def test_staged_input_snapshot_mutation_during_inference_fails_and_cleans(
    tmp_path, monkeypatch
):
    fixture = write_bound_pipeline_fixture(tmp_path)
    instance = _valid_track_instance()
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    def factory(model_snapshot):
        staged = Path(model_snapshot).parents[2]
        report_snapshot = next((staged / "inputs" / "reports").glob("*.json"))
        report_snapshot.write_bytes(report_snapshot.read_bytes() + b"\nmutated")
        return Model()

    output = tmp_path / "output"
    with pytest.raises(tracks.DenseTrackError, match="input snapshot changed"):
        tracks.propose(
            [fixture["capture"]], fixture["consensus"], fixture["models"][0],
            output, model_factory=factory, image_size=1280,
        )
    assert not output.exists()
    assert list(tmp_path.glob(f".{output.name}.tmp-*")) == []


def test_staged_output_mutation_after_initial_verification_fails_and_cleans(
    tmp_path, monkeypatch
):
    fixture = write_bound_pipeline_fixture(tmp_path)
    instance = _valid_track_instance()
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    original_revalidate = tracks.revalidate_source_files
    revalidation_count = 0

    def tamper_before_final_artifact_verification(bindings):
        nonlocal revalidation_count
        revalidation_count += 1
        if revalidation_count == 2:
            stage = next(tmp_path.glob(".output.tmp-*"))
            artifact = next((stage / "masks").rglob("*.png"))
            artifact.write_bytes(artifact.read_bytes() + b"tampered")
        return original_revalidate(bindings)

    monkeypatch.setattr(
        tracks, "revalidate_source_files",
        tamper_before_final_artifact_verification,
    )
    output = tmp_path / "output"
    with pytest.raises(tracks.DenseTrackError, match="artifact hash binding failed"):
        tracks.propose(
            [fixture["capture"]], fixture["consensus"], fixture["models"][0],
            output, model_factory=lambda _path: Model(), image_size=1280,
        )
    assert not output.exists()
    assert list(tmp_path.glob(f".{output.name}.tmp-*")) == []


def test_staged_tamper_during_final_fsync_cannot_publish(tmp_path, monkeypatch):
    fixture = write_bound_pipeline_fixture(tmp_path)
    instance = _valid_track_instance()
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    original_fsync = tracks.fsync_directory_tree

    def tamper_after_fsync(stage):
        original_fsync(stage)
        artifact = next((Path(stage) / "masks").rglob("*.png"))
        artifact.write_bytes(artifact.read_bytes() + b"tampered")

    monkeypatch.setattr(tracks, "fsync_directory_tree", tamper_after_fsync)
    output = tmp_path / "output"
    with pytest.raises(tracks.DenseTrackError, match="artifact hash binding failed"):
        tracks.propose(
            [fixture["capture"]], fixture["consensus"], fixture["models"][0],
            output, model_factory=lambda _path: Model(), image_size=1280,
        )
    assert not output.exists()
    assert list(tmp_path.glob(f".{output.name}.tmp-*")) == []


def test_staged_tamper_inside_atomic_publish_fails_removes_output_and_cleans(
    tmp_path, monkeypatch
):
    fixture = write_bound_pipeline_fixture(tmp_path)
    instance = _valid_track_instance()
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    original_publish = tracks.atomic_publish_directory
    tampered = False

    def tamper_immediately_before_rename(staged, output):
        nonlocal tampered
        artifact = next((Path(staged) / "masks").rglob("*.png"))
        artifact.write_bytes(artifact.read_bytes() + b"post-verification-tamper")
        tampered = True
        return original_publish(staged, output)

    monkeypatch.setattr(
        tracks, "atomic_publish_directory", tamper_immediately_before_rename
    )
    output = tmp_path / "output"
    with pytest.raises(tracks.DenseTrackError, match="changed during publication"):
        tracks.propose(
            [fixture["capture"]], fixture["consensus"], fixture["models"][0],
            output, model_factory=lambda _path: Model(), image_size=1280,
        )
    assert tampered is True
    assert not output.exists()
    assert list(tmp_path.glob(f".{output.name}.tmp-*")) == []


def test_parent_fsync_failure_quarantines_unverified_publication(tmp_path, monkeypatch):
    fixture = write_bound_pipeline_fixture(tmp_path)
    instance = _valid_track_instance()
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    output = tmp_path / "output"
    original_fsync = tracks.fsync_directory

    def fail_published_parent(directory):
        if Path(directory) == tmp_path and output.exists():
            raise OSError("injected parent fsync failure")
        return original_fsync(directory)

    monkeypatch.setattr(tracks, "fsync_directory", fail_published_parent)
    with pytest.raises(
        tracks.DenseTrackError, match="publication durability verification failed"
    ):
        tracks.propose(
            [fixture["capture"]], fixture["consensus"], fixture["models"][0],
            output, model_factory=lambda _path: Model(), image_size=1280,
        )
    assert not output.exists()
    quarantines = list(tmp_path.glob(f".{output.name}.staging-quarantine-*"))
    assert len(quarantines) == 1
    assert quarantines[0].stat().st_mode & 0o077 == 0
    assert list(tmp_path.glob(f".{output.name}.tmp-*")) == []


def test_cleanup_swap_preserves_and_restores_foreign_output(tmp_path, monkeypatch):
    fixture = write_bound_pipeline_fixture(tmp_path)
    instance = _valid_track_instance()
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    output = tmp_path / "output"
    detached_owned = tmp_path / "detached-owned-failed-publication"
    original_publish = tracks.atomic_publish_directory
    original_quarantine_rename = tracks.rename_noreplace_at
    cleanup_swap_done = False

    def publish_tampered(staged, destination):
        artifact = next((Path(staged) / "masks").rglob("*.png"))
        artifact.write_bytes(artifact.read_bytes() + b"force-cleanup")
        return original_publish(staged, destination)

    def swap_before_quarantine(directory_fd, source, destination):
        nonlocal cleanup_swap_done
        if source == output.name and destination.startswith(f".{output.name}.failed-"):
            output.rename(detached_owned)
            output.mkdir()
            (output / "foreign.txt").write_text("foreign-owner\n")
            cleanup_swap_done = True
        return original_quarantine_rename(directory_fd, source, destination)

    monkeypatch.setattr(tracks, "atomic_publish_directory", publish_tampered)
    monkeypatch.setattr(tracks, "rename_noreplace_at", swap_before_quarantine)
    with pytest.raises(tracks.DenseTrackError, match="changed during publication"):
        tracks.propose(
            [fixture["capture"]], fixture["consensus"], fixture["models"][0],
            output, model_factory=lambda _path: Model(), image_size=1280,
        )
    assert cleanup_swap_done is True
    assert (output / "foreign.txt").read_text() == "foreign-owner\n"
    assert not detached_owned.exists()
    assert list(tmp_path.glob(f".{output.name}.failed-*")) == []
    assert len(list(tmp_path.glob(f".{output.name}.staging-quarantine-*"))) == 1
    assert list(tmp_path.glob(f".{output.name}.tmp-*")) == []


def test_staging_context_preserves_foreign_replacement_and_quarantines_owned_stage(
    tmp_path,
):
    output = tmp_path / "output"
    owned_aside = tmp_path / "owned-stage-moved-aside"
    staged_path = None

    with pytest.raises(tracks.DenseTrackError, match="injected body failure"):
        with tracks.invocation_staging_directory(output) as (staged, _marker):
            staged_path = staged
            staged.rename(owned_aside)
            staged.mkdir()
            (staged / "foreign.txt").write_text("foreign-owner\n")
            raise tracks.DenseTrackError("injected body failure")

    assert staged_path is not None
    assert (staged_path / "foreign.txt").read_text() == "foreign-owner\n"
    assert not owned_aside.exists()
    quarantines = list(tmp_path.glob(".*.staging-quarantine-*"))
    assert len(quarantines) == 1
    owner = json.loads((quarantines[0] / tracks.STAGING_OWNER_MARKER).read_text())
    assert owner["staging_directory"] == str(staged_path.resolve())


def test_successful_publish_preserves_foreign_recreated_stage_and_verified_output(
    tmp_path, monkeypatch
):
    fixture = write_bound_pipeline_fixture(tmp_path)
    instance = _valid_track_instance()
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    original_publish = tracks.publish_staged_tree
    recreated_stage = None
    published_identity = None

    def publish_then_recreate_foreign_stage(staged, output):
        nonlocal recreated_stage, published_identity
        original_publish(staged, output)
        published = Path(output).stat()
        published_identity = (published.st_dev, published.st_ino)
        recreated_stage = Path(staged)
        recreated_stage.mkdir()
        (recreated_stage / "foreign.txt").write_text("foreign-owner\n")

    monkeypatch.setattr(
        tracks, "publish_staged_tree", publish_then_recreate_foreign_stage
    )
    output = tmp_path / "output"
    result = tracks.propose(
        [fixture["capture"]], fixture["consensus"], fixture["models"][0],
        output, model_factory=lambda _path: Model(), image_size=1280,
    )

    current = output.stat()
    assert result == output
    assert (current.st_dev, current.st_ino) == published_identity
    assert json.loads((output / "report.json").read_text())["acceptance_eligible"] is False
    assert recreated_stage is not None
    assert (recreated_stage / "foreign.txt").read_text() == "foreign-owner\n"
    assert list(tmp_path.glob(f".{output.name}.staging-quarantine-*")) == []


def test_return_boundary_replacement_fails_preserves_foreign_and_quarantines_owned(
    tmp_path, monkeypatch
):
    fixture = write_bound_pipeline_fixture(tmp_path)
    instance = _valid_track_instance()
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    original_publish = tracks.publish_staged_tree
    owned_moved = tmp_path / "owned-published-tree-moved-before-return"

    def publish_then_replace_output(staged, output):
        original_publish(staged, output)
        Path(output).rename(owned_moved)
        Path(output).mkdir()
        (Path(output) / "foreign.txt").write_text("foreign-owner\n")

    monkeypatch.setattr(tracks, "publish_staged_tree", publish_then_replace_output)
    output = tmp_path / "output"
    with pytest.raises(
        tracks.DenseTrackError, match="publication root was replaced"
    ):
        tracks.propose(
            [fixture["capture"]], fixture["consensus"], fixture["models"][0],
            output, model_factory=lambda _path: Model(), image_size=1280,
        )

    assert (output / "foreign.txt").read_text() == "foreign-owner\n"
    assert not owned_moved.exists()
    quarantines = list(tmp_path.glob(f".{output.name}.staging-quarantine-*"))
    assert len(quarantines) == 1
    report = json.loads((quarantines[0] / "report.json").read_text())
    assert report["acceptance_eligible"] is False
    assert list(tmp_path.glob(f".{output.name}.tmp-*")) == []


@pytest.mark.parametrize("recreate_active_stage", [False, True])
def test_swap_after_return_verify_before_output_stat_never_returns_foreign(
    tmp_path, monkeypatch, recreate_active_stage
):
    fixture = write_bound_pipeline_fixture(tmp_path)
    instance = _valid_track_instance()
    monkeypatch.setattr(
        tracks, "model_instances_from_result", lambda _result, _image: [instance]
    )

    class Model:
        def track(self, *_args, **_kwargs):
            return [object()]

    output = tmp_path / "output"
    owned_moved = tmp_path / "owned-moved-after-return-verification"
    original_publish = tracks.publish_staged_tree
    original_verify = tracks.verify_published_tree
    staged_path = None
    verify_count = 0

    def capture_staged_path(staged, destination):
        nonlocal staged_path
        staged_path = Path(staged)
        return original_publish(staged, destination)

    def swap_after_third_verify(root, contract):
        nonlocal verify_count
        result = original_verify(root, contract)
        verify_count += 1
        if verify_count == 3:
            Path(root).rename(owned_moved)
            Path(root).mkdir()
            (Path(root) / "foreign-output.txt").write_text("foreign-output\n")
            if recreate_active_stage:
                assert staged_path is not None
                staged_path.mkdir()
                (staged_path / "foreign-stage.txt").write_text("foreign-stage\n")
        return result

    monkeypatch.setattr(tracks, "publish_staged_tree", capture_staged_path)
    monkeypatch.setattr(tracks, "verify_published_tree", swap_after_third_verify)
    with pytest.raises(
        tracks.DenseTrackError, match="changed after return verification"
    ):
        tracks.propose(
            [fixture["capture"]], fixture["consensus"], fixture["models"][0],
            output, model_factory=lambda _path: Model(), image_size=1280,
        )

    assert verify_count == 3
    assert (output / "foreign-output.txt").read_text() == "foreign-output\n"
    assert not owned_moved.exists()
    quarantines = list(tmp_path.glob(f".{output.name}.staging-quarantine-*"))
    assert len(quarantines) == 1
    assert json.loads((quarantines[0] / "report.json").read_text())[
        "acceptance_eligible"
    ] is False
    if recreate_active_stage:
        assert staged_path is not None
        assert (staged_path / "foreign-stage.txt").read_text() == "foreign-stage\n"
    else:
        assert staged_path is not None
        assert not staged_path.exists()


@pytest.mark.parametrize("termination_signal", [signal.SIGTERM, signal.SIGINT])
def test_subprocess_interrupt_removes_only_its_owned_staging_directory(
    tmp_path, termination_signal
):
    model = tmp_path / "model.pt"
    model.write_bytes(b"model")
    consensus = tmp_path / "consensus.json"
    consensus.write_text("{}\n")
    output = tmp_path / "interrupted-output"
    sentinel = tmp_path / f".{output.name}.tmp-concurrent-sentinel"
    sentinel.mkdir()
    sentinel_file = sentinel / "foreign-owner.txt"
    sentinel_file.write_text("must remain unchanged\n")
    child_source = r'''
import signal
import sys
import time
from pathlib import Path

tools, model_text, consensus_text, output_text = sys.argv[1:]
sys.path.insert(0, tools)
import propose_dense_vehicle_tracks as tracks

model = Path(model_text)
consensus = Path(consensus_text)
output = Path(output_text)
previous_handlers = {
    handled: signal.getsignal(handled)
    for handled in tracks.HANDLED_TERMINATION_SIGNALS
}
previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())

tracks.load_consensus = lambda _path, _hash: (
    consensus.resolve(),
    consensus.read_bytes(),
    {"capture_report_sha256": "a" * 64},
    {},
    "left",
    {
        "consensus": tracks.pin_source_file(
            consensus, tracks.sha256_file(consensus), "test consensus"
        ),
        "capture_report": tracks.pin_source_file(
            consensus, tracks.sha256_file(consensus), "test capture"
        ),
        "inputs": [],
    },
)

def stall_after_owner_marker(*_args, **_kwargs):
    while True:
        time.sleep(0.05)

tracks.copy_file_exclusive = stall_after_owner_marker
try:
    tracks.propose(
        ["unused"], consensus, model, output,
        model_factory=lambda _path: object(),
    )
except tracks.DenseTrackInterrupted:
    handlers_restored = all(
        signal.getsignal(handled) == previous
        for handled, previous in previous_handlers.items()
    )
    mask_restored = signal.pthread_sigmask(signal.SIG_BLOCK, set()) == previous_mask
    raise SystemExit(23 if handlers_restored and mask_restored else 24)
raise SystemExit(25)
'''
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_source,
            str(TOOLS),
            str(model),
            str(consensus),
            str(output),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    owner_marker = None
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            candidates = list(
                tmp_path.glob(
                    f".{output.name}.tmp-*/{tracks.STAGING_OWNER_MARKER}"
                )
            )
            if candidates:
                assert len(candidates) == 1
                owner_marker = candidates[0]
                break
            if process.poll() is not None:
                break
            time.sleep(0.02)
        assert owner_marker is not None
        owner = json.loads(owner_marker.read_text())
        owned_stage = Path(owner["staging_directory"])
        assert owned_stage == owner_marker.parent.resolve()
        assert owner["final_output_directory"] == str(output.resolve())
        assert owner["pid"] == process.pid
        assert len(owner["nonce"]) == 32
        process.send_signal(termination_signal)
        stdout, stderr = process.communicate(timeout=10.0)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10.0)

    assert process.returncode == 23, (stdout, stderr)
    assert not output.exists()
    assert not owned_stage.exists()
    assert sentinel.is_dir()
    assert sentinel_file.read_text() == "must remain unchanged\n"
