import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import threading

import cv2
import jsonschema
import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
PERCEPTION = Path(__file__).resolve().parents[1]
BRIDGE = Path(__file__).resolve().parents[2] / "bridge"
BRIDGE_TESTS = BRIDGE / "tests"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PERCEPTION))
sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, str(BRIDGE_TESTS))

import attach_reviewed_localizations as attachment_module  # noqa: E402
from attach_reviewed_localizations import (  # noqa: E402
    AttachmentError,
    _appearance_embedding,
    attach,
)
from digital_twin_bridge.reviewed_localization import (  # noqa: E402
    canonical_json_bytes,
    canonical_object_sha256,
    seal_authenticated_artifact,
    sha256_bytes,
)
from process_video import persist_vehicle_inference_evidence  # noqa: E402
from reviewed_calibration_fixture import (  # noqa: E402
    build_reviewed_static_calibration,
)


AUTHORITY_KEY_ID = "reviewer-a"
AUTHORITY_KEY = hashlib.sha256(b"review-authority-test-key").digest()
SURVEY_KEY_ID = "survey-a"
SURVEY_KEY = hashlib.sha256(b"survey-authority-test-key").digest()
MODEL_HASH = "c" * 64
CONFIG_HASH = "d" * 64


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return {
        "path": str(path),
        "sha256": sha256_bytes(path.read_bytes()),
        "schema": value.get("schema"),
    }


def save_trajectory(inputs):
    inputs["trajectory"] = seal_authenticated_artifact(
        inputs["trajectory"], AUTHORITY_KEY_ID, AUTHORITY_KEY
    )
    write_json(inputs["trajectory_path"], inputs["trajectory"])


def rewrite_artifact(inputs, source_key, mutate):
    descriptor = inputs["trajectory"]["source"][source_key]
    path = Path(descriptor["path"])
    value = json.loads(path.read_text())
    mutate(value)
    key_id, key = (
        (SURVEY_KEY_ID, SURVEY_KEY)
        if source_key == "independent_reference"
        else (AUTHORITY_KEY_ID, AUTHORITY_KEY)
    )
    value = seal_authenticated_artifact(value, key_id, key)
    inputs["trajectory"]["source"][source_key] = write_json(path, value)
    save_trajectory(inputs)


def measured_camera(tmp_path, camera_id):
    hashes = []
    source_paths = []
    for index in range(12):
        image = np.zeros((960, 1280, 3), dtype=np.uint8)
        image[:, :, 0] = index * 17
        stripe_start = index * 80
        image[:, stripe_start:stripe_start + 64, 1] = 255
        source_path = tmp_path / f"{camera_id}-intrinsics-source-{index}.png"
        assert cv2.imwrite(str(source_path), image)
        source_paths.append(str(source_path))
        hashes.append(sha256_bytes(source_path.read_bytes()))
    matrix = [[1000.0, 0, 640.0], [0, 1000.0, 480.0], [0, 0, 1]]
    distortion = {key: 0.0 for key in ("k1", "k2", "p1", "p2", "k3")}
    normalized = {
        "method": "checkerboard",
        "image_count": 12,
        "source_images_sha256": hashes,
        "rms_reprojection_error_px": 0.5,
        "resolution": [1280, 960],
        "camera_matrix": matrix,
        "distortion": distortion,
    }
    artifact_path = tmp_path / f"{camera_id}-intrinsics.json"
    report_path = tmp_path / f"{camera_id}-intrinsics-report.json"
    write_json(artifact_path, normalized)
    write_json(report_path, {
        "schema": "v2x-checkerboard-calibration-report/v1",
        "accepted": [
            {"path": source_paths[index], "sha256": value,
             "rmse_px": 0.4, "max_error_px": 0.8}
            for index, value in enumerate(hashes[:10])
        ],
        "holdouts": [
            {"path": source_paths[index + 10], "sha256": value,
             "rmse_px": 0.5, "max_error_px": 1.0}
            for index, value in enumerate(hashes[10:])
        ],
        "holdout_metrics": {"rmse_px": 0.6, "max_error_px": 1.2},
    })
    return {
        "id": camera_id,
        "pitch_deg": 0.0,
        "yaw_deg": 0.0,
        "heading_deg": 90.0,
        "roll_deg": 0.0,
        "twin_pose": {},
        "intrinsics": {
            "width": 1280,
            "height": 960,
            "fx": 1000.0,
            "fy": 1000.0,
            "cx": 640.0,
            "cy": 480.0,
        },
        "intrinsics_calibration": {
            **normalized,
            "artifact_path": str(artifact_path),
            "artifact_sha256": sha256_bytes(artifact_path.read_bytes()),
            "report_path": str(report_path),
            "report_sha256": sha256_bytes(report_path.read_bytes()),
            "source_image_paths": source_paths,
        },
    }


def build_inputs(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    authority_path = tmp_path / "authority.json"
    write_json(authority_path, {
        "schema": "v2x-review-authority-keys/v1",
        "keys": {
            AUTHORITY_KEY_ID: {
                "key_hex": AUTHORITY_KEY.hex(),
                "roles": [
                    "reviewed_contract", "contact_consensus", "factor_graph",
                    "trajectory_identity", "appearance_model",
                    "blueprint_catalog", "static_calibration",
                ],
            },
            SURVEY_KEY_ID: {
                "key_hex": SURVEY_KEY.hex(),
                "roles": ["independent_reference"],
            },
        },
    })

    cameras = {
        "cameras": [
            measured_camera(tmp_path, camera_id)
            for camera_id in ("ch1", "ch2", "ch3", "ch4")
        ]
    }
    cameras_path = tmp_path / "cameras.json"
    write_json(cameras_path, cameras)
    cameras_hash = sha256_bytes(cameras_path.read_bytes())
    camera_hashes = {
        camera["id"]: canonical_object_sha256(camera)
        for camera in cameras["cameras"]
    }

    opendrive_path = tmp_path / "map.xodr"
    opendrive_path.write_text("<OpenDRIVE><road id='1'/></OpenDRIVE>\n")
    opendrive_hash = sha256_bytes(opendrive_path.read_bytes())
    map_name = "Carla/Maps/Richmond_Field_Station_Richmond_CA"
    static_path = build_reviewed_static_calibration(
        tmp_path / "static-evidence",
        cameras_path,
        cameras["cameras"],
        map_name,
        opendrive_hash,
        AUTHORITY_KEY_ID,
        AUTHORITY_KEY,
    )
    static_descriptor = {
        "path": str(static_path),
        "sha256": sha256_bytes(static_path.read_bytes()),
        "schema": "v2x-static-camera-survey-manifest/v1",
    }
    appearance_model = seal_authenticated_artifact({
        "schema": "v2x-pinned-vehicle-appearance-model/v1",
        "algorithm": "masked-bgr-histogram-l2/v1",
        "bins": [8, 8, 8],
        "crop_source": "producer_instance_mask",
    }, AUTHORITY_KEY_ID, AUTHORITY_KEY)
    appearance_descriptor = write_json(
        tmp_path / "appearance-model.json", appearance_model
    )

    detections = []
    samples = []
    embeddings = []
    for index, camera_id in enumerate(("ch1", "ch2")):
        timestamp = f"2026-07-13T21:00:0{index}.000Z"
        detection = {
            "event_id": f"event-{index}",
            "object_id": "global_car_reviewed",
            "object_type": "car",
            "timestamp_utc": timestamp,
            "media_timestamp_utc": timestamp,
            "timestamp_schema_version": 2,
            "media_time_trusted": True,
            "media_clock_status": "matched",
            "media_clock": {
                "schema_version": 1,
                "source": "hls_ext_x_program_date_time",
                "matching_method": "exact_same_session_pts",
                "evidence_method": "exact_same_session_pts",
                "session_id": f"session-{camera_id}",
                "position_milliseconds": float(index * 1000),
                "source_pts": index * 1000,
                "source_time_base_numerator": 1,
                "source_time_base_denominator": 1000,
            },
            "device_id": f"cam-001-{camera_id}",
            "track_id": 42,
            "confidence_score": 0.91,
            "gps_location": {"latitude": 37.0, "longitude": -122.0},
            "bbox": {"x1": 450.0, "y1": 400.0, "x2": 750.0, "y2": 721.0},
            "camera_data": {"bifocal_metadata": {"frame": 10 + index}},
            "raw_observation": {
                "native_resolution": [1280, 960],
                "fingerprints": {
                    "cameras_json_sha256": cameras_hash,
                    "camera_config_sha256": camera_hashes[camera_id],
                    "detector_model_sha256": MODEL_HASH,
                    "detector_config_sha256": CONFIG_HASH,
                },
                "optimizer_contract": {
                    "gps_location_is_derived_baseline": True,
                    "acceptance_eligible": False,
                },
            },
        }
        yy, xx = np.indices((960, 1280))
        frame = np.stack(
            (
                (xx + index * 11) % 256,
                (yy * 2 + index * 17) % 256,
                ((xx // 2 + yy // 3) + index * 23) % 256,
            ),
            axis=2,
        ).astype(np.uint8)
        mask = np.zeros((960, 1280), dtype=np.uint8)
        cv2.fillConvexPoly(
            mask,
            np.asarray([[500, 400], [700, 400], [740, 720], [460, 720]]),
            1,
        )
        detection["_review_instance_mask"] = mask.astype(bool)
        detection["_segmentation_output_index"] = index
        evidence = persist_vehicle_inference_evidence(
            detection, frame, tmp_path / "review-evidence"
        )
        assert evidence["acceptance_eligible"] is True
        manifest = json.loads(Path(evidence["manifest_path"]).read_text())
        assert manifest["session_id"] == f"session-{camera_id}"
        assert manifest["detector_output"]["session_id"] == (
            manifest["session_id"]
        )
        detections.append(detection)
        contact = {
            "method": "reviewed_vehicle_footprint_midpoint",
            "left_ground_pixel": [500.0, 700.0],
            "right_ground_pixel": [700.0, 700.0],
            "footprint_midpoint_pixel": [600.0, 700.0],
            "covariance_px2": [[4.0, 1.0], [1.0, 9.0]],
        }
        placement = {
            "coordinate_frame": "carla_world",
            "position_semantics": "ue5_actor_center",
            "position_m": {"x": 10.0 + index, "y": 20.0, "z": 1.25},
            "covariance_m2": [
                [0.25, 0.0, 0.0],
                [0.0, 0.36, 0.0],
                [0.0, 0.0, 0.04],
            ],
            "uncertainty_m": 0.75,
            "heading_deg": 30.0,
            "dimensions_m": {"length": 4.7, "width": 1.9, "height": 1.6},
            "blueprint_family": "passenger_car",
        }
        samples.append({
            "event_id": detection["event_id"],
            "camera_id": camera_id,
            "sample_index": index,
            "frame": {
                "path": manifest["frame"]["path"],
                "sha256": manifest["frame"]["sha256"],
            },
            "mask": {
                "path": manifest["instance_mask"]["path"],
                "sha256": manifest["instance_mask"]["sha256"],
            },
            "native_resolution": [1280, 960],
            "frame_number": 10 + index,
            "contact": contact,
            "timing": {
                "method": "exact_same_session_pts",
                "trusted": True,
                "session_id": f"session-{camera_id}",
                "pts_seconds": float(index),
                "media_timestamp_utc": timestamp,
                "timestamp_error_ms": 0.0,
            },
            "placement": placement,
        })
        embeddings.append(
            _appearance_embedding(frame, mask, detection["bbox"], appearance_model)
        )

    detections_path = tmp_path / "detections.ndjson"
    detections_body = b"".join(canonical_json_bytes(item) for item in detections)
    detections_path.write_bytes(detections_body)

    consensus = seal_authenticated_artifact({
        "schema": "v2x-reviewed-footprint-consensus/v1",
        "acceptance_eligible": True,
        "reviewer_ids": [AUTHORITY_KEY_ID, "reviewer-b"],
        "events": [
            {
                "event_id": sample["event_id"],
                "accepted": True,
                "ambiguity": False,
                "camera_id": sample["camera_id"],
                "frame_sha256": sample["frame"]["sha256"],
                "mask_sha256": sample["mask"]["sha256"],
                "contact": sample["contact"],
            }
            for sample in samples
        ],
    }, AUTHORITY_KEY_ID, AUTHORITY_KEY)
    consensus_descriptor = write_json(tmp_path / "consensus.json", consensus)
    factor = seal_authenticated_artifact({
        "schema": "v2x-reviewed-detection-factor-graph/v1",
        "acceptance_eligible": True,
        "gate_passed": True,
        "optimizer_contract": {"diagnostic_until_independent_truth": False},
        "events": [
            {
                "event_id": sample["event_id"],
                "accepted": True,
                "ambiguity": False,
                "global_track_id": "global_car_reviewed",
                "trajectory_id": "trajectory-1",
                "camera_id": sample["camera_id"],
                "sample_index": sample["sample_index"],
                "placement": sample["placement"],
            }
            for sample in samples
        ],
    }, AUTHORITY_KEY_ID, AUTHORITY_KEY)
    factor_descriptor = write_json(tmp_path / "factor.json", factor)
    pair_similarity = float(np.dot(embeddings[0][0], embeddings[1][0]))
    identity = seal_authenticated_artifact({
        "schema": "v2x-reviewed-trajectory-identity/v1",
        "acceptance_eligible": True,
        "status": "unambiguous",
        "ambiguity_count": 0,
        "global_track_id": "global_car_reviewed",
        "trajectory_id": "trajectory-1",
        "camera_ids": ["ch1", "ch2"],
        "appearance_model_sha256": appearance_descriptor["sha256"],
        "minimum_appearance_similarity": 0.60,
        "events": [
            {
                "event_id": sample["event_id"],
                "accepted": True,
                "ambiguity": False,
                "global_track_id": "global_car_reviewed",
                "trajectory_id": "trajectory-1",
                "camera_id": sample["camera_id"],
                "sample_index": sample["sample_index"],
                "frame_sha256": sample["frame"]["sha256"],
                "mask_sha256": sample["mask"]["sha256"],
                "crop_bbox": embeddings[sample["sample_index"]][2],
                "embedding_sha256": embeddings[sample["sample_index"]][1],
            }
            for sample in samples
        ],
        "pairs": [{
            "previous_event_id": "event-0",
            "event_id": "event-1",
            "previous_camera_id": "ch1",
            "camera_id": "ch2",
            "accepted": True,
            "ambiguity": False,
            "global_track_id": "global_car_reviewed",
            "trajectory_id": "trajectory-1",
            "appearance_similarity": pair_similarity,
            "transit_seconds": 1.0,
            "distance_m": 1.0,
            "trajectory_covariance_m2": [
                [0.3, 0.0, 0.0],
                [0.0, 0.3, 0.0],
                [0.0, 0.0, 0.1],
            ],
        }],
    }, AUTHORITY_KEY_ID, AUTHORITY_KEY)
    identity_descriptor = write_json(tmp_path / "identity.json", identity)
    measurements = seal_authenticated_artifact({
        "schema": "v2x-independent-rtk-measurements/v1",
        "measurements": [
            {
                "measurement_id": f"measurement-{sample['sample_index']}",
                "event_id": sample["event_id"],
                "camera_id": sample["camera_id"],
                "media_timestamp_utc": sample["timing"]["media_timestamp_utc"],
                "map_name": map_name,
                "opendrive_sha256": opendrive_hash,
                "method": "independent_rtk_fix",
                "source_device_id": "rtk-rover-01",
                "capture_run_id": "survey-run-20260713",
                "position_m": {
                    **sample["placement"]["position_m"],
                    "x": sample["placement"]["position_m"]["x"] + 0.1,
                },
                "covariance_m2": [
                    [0.04, 0.0, 0.0],
                    [0.0, 0.04, 0.0],
                    [0.0, 0.0, 0.01],
                ],
                "uncertainty_m": 0.25,
            }
            for sample in samples
        ],
    }, SURVEY_KEY_ID, SURVEY_KEY)
    measurements_descriptor = write_json(
        tmp_path / "measurements.json", measurements
    )
    reference = seal_authenticated_artifact({
        "schema": "v2x-independent-vehicle-reference/v1",
        "acceptance_eligible": True,
        "source": measurements_descriptor,
        "events": [
            {
                "event_id": sample["event_id"],
                "accepted": True,
                "ambiguity": False,
                "global_track_id": "global_car_reviewed",
                "trajectory_id": "trajectory-1",
                "camera_id": sample["camera_id"],
                "sample_index": sample["sample_index"],
                "measurement_id": f"measurement-{sample['sample_index']}",
            }
            for sample in samples
        ],
    }, SURVEY_KEY_ID, SURVEY_KEY)
    reference_descriptor = write_json(tmp_path / "reference.json", reference)
    catalog = seal_authenticated_artifact({
        "schema": "v2x-reviewed-ue5-blueprint-catalog/v1",
        "acceptance_eligible": True,
        "families": {
            "passenger_car": [
                {"id": "vehicle.audi.tt", "dimensions_m": {"length": 4.7, "width": 1.9, "height": 1.6}},
                {"id": "vehicle.tesla.model3", "dimensions_m": {"length": 4.7, "width": 1.9, "height": 1.6}},
            ],
            "truck": [
                {"id": "vehicle.ford.truck", "dimensions_m": {"length": 6.5, "width": 2.4, "height": 2.8}},
            ],
            "bus": [
                {"id": "vehicle.ford.truck", "dimensions_m": {"length": 6.5, "width": 2.4, "height": 2.8}},
            ],
        },
    }, AUTHORITY_KEY_ID, AUTHORITY_KEY)
    catalog_descriptor = write_json(tmp_path / "blueprints.json", catalog)

    trajectory = {
        "schema": "v2x-reviewed-vehicle-trajectory/v1",
        "authority_key_id": AUTHORITY_KEY_ID,
        "acceptance_eligible": True,
        "global_track_id": "global_car_reviewed",
        "trajectory_id": "trajectory-1",
        "reviewer": {"kind": "human", "id": AUTHORITY_KEY_ID},
        "blueprint_dimension_tolerance_m": 0.25,
        "source": {
            "detections_ndjson_sha256": sha256_bytes(detections_body),
            "consensus": consensus_descriptor,
            "factor_graph": factor_descriptor,
            "identity": identity_descriptor,
            "independent_reference": reference_descriptor,
            "blueprint_catalog": catalog_descriptor,
            "appearance_model": appearance_descriptor,
            "static_calibration": static_descriptor,
            "cameras_json": {
                "path": str(cameras_path),
                "sha256": cameras_hash,
            },
            "opendrive": {
                "path": str(opendrive_path),
                "sha256": opendrive_hash,
                "map_name": map_name,
            },
        },
        "samples": samples,
    }
    trajectory_path = tmp_path / "trajectory.json"
    inputs = {
        "authority_path": authority_path,
        "detections_path": detections_path,
        "trajectory_path": trajectory_path,
        "trajectory": trajectory,
    }
    save_trajectory(inputs)
    return inputs


def run_attach(inputs, output_name="attached.ndjson"):
    return attach(
        inputs["detections_path"],
        inputs["trajectory_path"],
        inputs["trajectory_path"].parent / output_name,
        inputs["authority_path"],
    )


def test_attaches_semantically_bound_two_camera_trajectory(tmp_path):
    inputs = build_inputs(tmp_path)
    schema_root = Path(__file__).resolve().parents[3] / "config" / "schemas"
    trajectory_schema = json.loads(
        (schema_root / "reviewed-vehicle-trajectory-v1.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(trajectory_schema).validate(inputs["trajectory"])
    short_trajectory = copy.deepcopy(inputs["trajectory"])
    short_trajectory["samples"][0]["native_resolution"] = [1280]
    assert not jsonschema.Draft202012Validator(trajectory_schema).is_valid(
        short_trajectory
    )
    output_path, manifest_path = run_attach(inputs)
    rows = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["gps_location"] == {"latitude": 37.0, "longitude": -122.0}
    contract = rows[0]["reviewed_localization"]
    localization_schema = json.loads(
        (schema_root / "reviewed-vehicle-localization-v1.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(localization_schema).validate(contract)
    short_contract = copy.deepcopy(contract)
    short_contract["source"]["frame"]["native_resolution"] = [1280]
    assert not jsonschema.Draft202012Validator(localization_schema).is_valid(
        short_contract
    )
    assert contract["authority"]["key_id"] == AUTHORITY_KEY_ID
    assert contract["source"]["frame"]["source_kind"] == "persisted_native_frame_and_instance_mask"
    assert contract["contact"]["footprint_midpoint_pixel"] == [600.0, 700.0]
    assert contract["contact"]["footprint_midpoint_pixel"] != [600.0, 560.5]
    assert rows[1]["reviewed_localization"]["identity"]["transition"]["speed_mps"] == 1.0
    assert (
        rows[1]["reviewed_localization"]["identity"]["transition"]
        ["acceleration_mps2"]
        is None
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["counts"] == {"detections": 2, "reviewed_localizations": 2}
    assert manifest["deployment_eligible"] is False


def test_atomic_bundle_preserves_existing_destination_sentinel(tmp_path):
    inputs = build_inputs(tmp_path)
    bundle = tmp_path / "attached.ndjson"
    bundle.mkdir()
    sentinel = bundle / "owned-by-another-writer"
    sentinel.write_text("preserve-me")

    with pytest.raises(AttachmentError, match="already exists"):
        run_attach(inputs)

    assert sentinel.read_text() == "preserve-me"
    assert sorted(path.name for path in bundle.iterdir()) == [sentinel.name]
    assert not list(tmp_path.glob(".attached.ndjson.tmp-*"))


def test_atomic_bundle_failure_leaves_no_half_pair(tmp_path, monkeypatch):
    inputs = build_inputs(tmp_path)
    bundle = tmp_path / "attached.ndjson"

    def fail_publish(*_args):
        raise AttachmentError("injected publish failure")

    monkeypatch.setattr(
        attachment_module, "_rename_directory_noreplace", fail_publish
    )
    with pytest.raises(AttachmentError, match="injected publish failure"):
        run_attach(inputs)

    assert not bundle.exists()
    assert not list(tmp_path.glob(".attached.ndjson.tmp-*"))


def test_atomic_bundle_allows_exactly_one_concurrent_publisher(
    tmp_path, monkeypatch
):
    inputs = build_inputs(tmp_path)
    original_publish = attachment_module._rename_directory_noreplace
    publication_barrier = threading.Barrier(2)

    def synchronized_publish(*args):
        publication_barrier.wait(timeout=5.0)
        return original_publish(*args)

    monkeypatch.setattr(
        attachment_module, "_rename_directory_noreplace", synchronized_publish
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_attach, inputs) for _ in range(2)]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(("success", future.result()))
        except AttachmentError as exc:
            outcomes.append(("rejected", str(exc)))

    assert [kind for kind, _value in outcomes].count("success") == 1
    assert [kind for kind, _value in outcomes].count("rejected") == 1
    bundle = tmp_path / "attached.ndjson"
    assert sorted(path.name for path in bundle.iterdir()) == [
        "detections.ndjson", "manifest.json",
    ]
    assert not list(tmp_path.glob(".attached.ndjson.tmp-*"))


def test_rejects_diagnostic_factor_graph_even_with_accepted_ids(tmp_path):
    inputs = build_inputs(tmp_path)
    rewrite_artifact(inputs, "factor_graph", lambda value: (
        value.update(acceptance_eligible=False),
        value["optimizer_contract"].update(diagnostic_until_independent_truth=True),
    ))
    with pytest.raises(AttachmentError, match="diagnostic factor-graph"):
        run_attach(inputs)


def test_rejects_source_detection_or_mask_tampering(tmp_path):
    inputs = build_inputs(tmp_path / "detections")
    rows = [json.loads(line) for line in inputs["detections_path"].read_text().splitlines()]
    rows[0]["gps_location"]["latitude"] = 38.0
    inputs["detections_path"].write_bytes(b"".join(canonical_json_bytes(row) for row in rows))
    with pytest.raises(AttachmentError, match="detection source hash"):
        run_attach(inputs)


def test_producer_rejects_uniform_frame_and_missing_native_instance_mask(tmp_path):
    inputs = build_inputs(tmp_path)
    record = json.loads(inputs["detections_path"].read_text().splitlines()[0])
    mask = cv2.imread(
        inputs["trajectory"]["samples"][0]["mask"]["path"],
        cv2.IMREAD_GRAYSCALE,
    )
    record["_review_instance_mask"] = mask > 0
    record["_segmentation_output_index"] = 0
    evidence = persist_vehicle_inference_evidence(
        record,
        np.full((960, 1280, 3), 40, dtype=np.uint8),
        tmp_path / "uniform-evidence",
    )
    assert evidence == {
        "schema": "v2x-persisted-inference-evidence/v1",
        "acceptance_eligible": False,
        "reason": "uniform_or_nonvehicle_segmentation_substitute",
    }

    record = json.loads(inputs["detections_path"].read_text().splitlines()[0])
    record["_segmentation_output_index"] = 0
    evidence = persist_vehicle_inference_evidence(
        record,
        np.indices((960, 1280))[1].astype(np.uint8)[:, :, None].repeat(3, 2),
        tmp_path / "missing-mask-evidence",
    )
    assert evidence["acceptance_eligible"] is False
    assert evidence["reason"] == "incomplete_exact_inference_evidence"


def test_rejects_circular_camera_sourced_reference_and_one_camera_identity(tmp_path):
    inputs = build_inputs(tmp_path / "circular-reference")
    reference_descriptor = inputs["trajectory"]["source"]["independent_reference"]
    reference_path = Path(reference_descriptor["path"])
    reference = json.loads(reference_path.read_text())
    measurements_path = Path(reference["source"]["path"])
    measurements = json.loads(measurements_path.read_text())
    measurements["measurements"][0]["source_device_id"] = "cam-001-ch1"
    measurements = seal_authenticated_artifact(
        measurements, SURVEY_KEY_ID, SURVEY_KEY
    )
    reference["source"] = write_json(measurements_path, measurements)
    reference = seal_authenticated_artifact(reference, SURVEY_KEY_ID, SURVEY_KEY)
    inputs["trajectory"]["source"]["independent_reference"] = write_json(
        reference_path, reference
    )
    save_trajectory(inputs)
    with pytest.raises(AttachmentError, match="authenticated raw measurement"):
        run_attach(inputs)

    inputs = build_inputs(tmp_path / "one-camera")
    rewrite_artifact(
        inputs,
        "identity",
        lambda value: value.update(camera_ids=["ch1"]),
    )
    with pytest.raises(AttachmentError, match="camera set is missing"):
        run_attach(inputs)

    inputs = build_inputs(tmp_path / "mask")
    Path(inputs["trajectory"]["samples"][0]["mask"]["path"]).write_bytes(b"spoofed")
    with pytest.raises(AttachmentError, match="producer instance mask hash"):
        run_attach(inputs)


def test_rejects_caller_selected_artifact_schema(tmp_path):
    inputs = build_inputs(tmp_path)
    rewrite_artifact(inputs, "consensus", lambda value: value.update(schema="attacker-accepted/v9"))
    with pytest.raises(AttachmentError, match="schema is not allowlisted"):
        run_attach(inputs)


@pytest.mark.parametrize(
    ("source_key", "mutate", "reason"),
    [
        (
            "consensus",
            lambda value: value["events"][0]["contact"].update(footprint_midpoint_pixel=[601.0, 700.0]),
            "exact frame/mask/sample",
        ),
        (
            "factor_graph",
            lambda value: value["events"][0]["placement"]["position_m"].update(x=99.0),
            "exact placement sample",
        ),
        (
            "independent_reference",
            lambda value: value["events"][0].update(sample_index=99),
            "not linked to the exact sample",
        ),
    ],
)
def test_rejects_semantically_unlinked_artifact_events(tmp_path, source_key, mutate, reason):
    inputs = build_inputs(tmp_path)
    rewrite_artifact(inputs, source_key, mutate)
    with pytest.raises(AttachmentError, match=reason):
        run_attach(inputs)


def test_rejects_identity_appearance_or_pair_ambiguity(tmp_path):
    inputs = build_inputs(tmp_path / "appearance")
    rewrite_artifact(
        inputs,
        "identity",
        lambda value: value["pairs"][0].update(appearance_similarity=0.59),
    )
    with pytest.raises(AttachmentError, match="appearance/transit/dynamics"):
        run_attach(inputs)

    inputs = build_inputs(tmp_path / "ambiguity")
    rewrite_artifact(
        inputs,
        "identity",
        lambda value: value["events"][1].update(ambiguity=True),
    )
    with pytest.raises(AttachmentError, match="invalid or ambiguous"):
        run_attach(inputs)


def test_rejects_same_time_transit_even_if_caller_updates_detection(tmp_path):
    inputs = build_inputs(tmp_path)
    first_time = inputs["trajectory"]["samples"][0]["timing"]["media_timestamp_utc"]
    inputs["trajectory"]["samples"][1]["timing"].update({
        "media_timestamp_utc": first_time,
        "pts_seconds": 0.0,
    })
    rows = [json.loads(line) for line in inputs["detections_path"].read_text().splitlines()]
    rows[1]["timestamp_utc"] = first_time
    rows[1]["media_timestamp_utc"] = first_time
    rows[1]["media_clock"]["position_milliseconds"] = 0.0
    body = b"".join(canonical_json_bytes(row) for row in rows)
    inputs["detections_path"].write_bytes(body)
    inputs["trajectory"]["source"]["detections_ndjson_sha256"] = sha256_bytes(body)
    save_trajectory(inputs)
    with pytest.raises(AttachmentError, match="timing/session linkage"):
        run_attach(inputs)


def test_rejects_correlated_covariance_using_largest_eigenvalue(tmp_path):
    inputs = build_inputs(tmp_path)
    placement = inputs["trajectory"]["samples"][0]["placement"]
    placement["covariance_m2"] = [
        [3.0, 3.0, 0.0],
        [3.0, 3.0, 0.0],
        [0.0, 0.0, 0.1],
    ]
    placement["uncertainty_m"] = 2.0
    rewrite_artifact(
        inputs,
        "factor_graph",
        lambda value: value["events"][0].update(
            placement=copy.deepcopy(placement)
        ),
    )
    with pytest.raises(AttachmentError, match="covariance/uncertainty"):
        run_attach(inputs)


def test_rejects_mask_contact_that_is_not_on_ground_boundary(tmp_path):
    inputs = build_inputs(tmp_path)
    contact = inputs["trajectory"]["samples"][0]["contact"]
    contact.update({
        "left_ground_pixel": [500.0, 600.0],
        "right_ground_pixel": [700.0, 600.0],
        "footprint_midpoint_pixel": [600.0, 600.0],
    })
    rewrite_artifact(
        inputs,
        "consensus",
        lambda value: value["events"][0].update(
            contact=copy.deepcopy(contact)
        ),
    )
    with pytest.raises(AttachmentError, match="mask ground boundary"):
        run_attach(inputs)


def test_rejects_blueprint_geometry_not_in_reviewed_catalog(tmp_path):
    inputs = build_inputs(tmp_path)
    placement = inputs["trajectory"]["samples"][0]["placement"]
    placement["dimensions_m"]["length"] = 5.0
    rewrite_artifact(
        inputs,
        "factor_graph",
        lambda value: value["events"][0].update(
            placement=copy.deepcopy(placement)
        ),
    )
    with pytest.raises(AttachmentError, match="selected blueprint dimensions"):
        run_attach(inputs)


def test_rejects_unbound_authority_and_failed_static_gate(tmp_path):
    inputs = build_inputs(tmp_path / "authority")
    inputs["trajectory"]["authority_key_id"] = "untrusted-reviewer"
    save_trajectory(inputs)
    with pytest.raises(AttachmentError, match="reviewer authority"):
        run_attach(inputs)

    inputs = build_inputs(tmp_path / "static")
    rewrite_artifact(
        inputs,
        "static_calibration",
        lambda value: value["map"].update(name="wrong-map"),
    )
    with pytest.raises(AttachmentError, match="static calibration rejected"):
        run_attach(inputs)
