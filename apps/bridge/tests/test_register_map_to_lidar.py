import base64
import copy
import csv
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import logging
import math
from pathlib import Path
import re
import threading
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import numpy as np
import pytest


TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "register_map_to_lidar.py"
SPEC = importlib.util.spec_from_file_location("register_map_to_lidar", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


SURVEY_AUTHORITY_KEY_ID = "test-survey-authority-ed25519"
CRS_AUTHORITY_KEY_ID = "test-crs-authority-ed25519"
ANNOTATION_AUTHORITY_KEY_ID = "test-annotation-authority-ed25519"
LIDAR_AUTHORITY_KEY_ID = "test-lidar-authority-ed25519"
VERTICAL_AUTHORITY_KEY_ID = "test-vertical-authority-ed25519"
HOLDOUT_REGISTRY_KEY_ID = "test-holdout-registry-ed25519"
HOLDOUT_REGISTRY_ID = "test-annotation-holdout-registry"
SURVEY_AUTHORITY_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
CRS_AUTHORITY_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
ANNOTATION_AUTHORITY_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(65, 97)))
LIDAR_AUTHORITY_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(97, 129)))
VERTICAL_AUTHORITY_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(129, 161)))
HOLDOUT_REGISTRY_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(161, 193)))


def public_key_pem(private_key):
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


@pytest.fixture(autouse=True)
def pinned_test_authority_keys(monkeypatch):
    monkeypatch.setattr(tool, "TRUSTED_SURVEY_AUTHORITY_SIGNERS", {
        SURVEY_AUTHORITY_KEY_ID: {
            "producer": "Independent State Survey Verification Service",
            "source": "state-license-registry-test-fixture",
            "public_key_pem": public_key_pem(SURVEY_AUTHORITY_PRIVATE_KEY),
        },
    })
    monkeypatch.setattr(tool, "TRUSTED_CRS_AUTHORITY_SIGNERS", {
        CRS_AUTHORITY_KEY_ID: {
            "producer": "Independent Geodetic Authority",
            "source": "official-operation-registry-test-fixture",
            "public_key_pem": public_key_pem(CRS_AUTHORITY_PRIVATE_KEY),
        },
    })
    monkeypatch.setattr(tool, "TRUSTED_ANNOTATION_AUTHORITY_SIGNERS", {
        ANNOTATION_AUTHORITY_KEY_ID: {
            "producer": "Independent Annotation Authority",
            "source": "annotation-review-registry-test-fixture",
            "public_key_pem": public_key_pem(ANNOTATION_AUTHORITY_PRIVATE_KEY),
        },
    })
    monkeypatch.setattr(tool, "TRUSTED_LIDAR_VALIDATION_SIGNERS", {
        LIDAR_AUTHORITY_KEY_ID: {
            "producer": "Independent LiDAR Data Authority",
            "source": "lidar-validation-registry-test-fixture",
            "public_key_pem": public_key_pem(LIDAR_AUTHORITY_PRIVATE_KEY),
        },
    })
    monkeypatch.setattr(tool, "TRUSTED_VERTICAL_DATUM_SIGNERS", {
        VERTICAL_AUTHORITY_KEY_ID: {
            "producer": "Independent Vertical Datum Authority",
            "source": "vertical-operation-registry-test-fixture",
            "public_key_pem": public_key_pem(VERTICAL_AUTHORITY_PRIVATE_KEY),
        },
    })
    monkeypatch.setattr(tool, "TRUSTED_HOLDOUT_REGISTRY_SIGNERS", {
        HOLDOUT_REGISTRY_KEY_ID: {
            "registry_id": HOLDOUT_REGISTRY_ID,
            "producer": "Independent Append-Only Holdout Registry",
            "source": "external-holdout-registry-test-fixture",
            "public_key_pem": public_key_pem(HOLDOUT_REGISTRY_PRIVATE_KEY),
        },
    })
    monkeypatch.setattr(tool, "TRUSTED_HOLDOUT_REGISTRY_ENDPOINTS", {
        HOLDOUT_REGISTRY_ID: {
            "registry_id": HOLDOUT_REGISTRY_ID,
            "base_url": "https://holdout-registry.invalid/v1",
        },
    })


def signed_registry_head(sequence, entry_sha256, prior_head_sha256, *, observed_at=None):
    head = {
        "schema": tool.HOLDOUT_REGISTRY_HEAD_SCHEMA,
        "registry_id": HOLDOUT_REGISTRY_ID,
        "sequence": sequence,
        "entry_sha256": entry_sha256,
        "prior_head_sha256": prior_head_sha256,
        "observed_at_utc": (observed_at or datetime.now(timezone.utc)).isoformat(),
        "signing_key_id": HOLDOUT_REGISTRY_KEY_ID,
        "public_key_sha256": tool.hashlib.sha256(
            public_key_pem(HOLDOUT_REGISTRY_PRIVATE_KEY)
        ).hexdigest(),
        "producer": "Independent Append-Only Holdout Registry",
        "source": "external-holdout-registry-test-fixture",
    }
    return {
        "head": head,
        "signature_base64": base64.b64encode(
            HOLDOUT_REGISTRY_PRIVATE_KEY.sign(tool.canonical_json_bytes(head))
        ).decode(),
    }


class FakeHoldoutRegistry:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}
        self.evaluations = set()
        self.head_envelope = signed_registry_head(0, "0" * 64, "0" * 64)

    def get_head(self):
        with self.lock:
            return copy.deepcopy(self.head_envelope)

    def consume(self, request):
        with self.lock:
            current = self.head_envelope["head"]
            if (
                request["expected_prior_sequence"] != current["sequence"]
                or request["expected_prior_head_sha256"]
                != tool.canonical_hash(current)
                or request["evaluation_id"] in self.evaluations
            ):
                raise tool.RegistrationError("holdout registry atomic consume conflict")
            entry = {
                "schema": tool.HOLDOUT_REGISTRY_ENTRY_SCHEMA,
                "registry_id": HOLDOUT_REGISTRY_ID,
                "sequence": current["sequence"] + 1,
                "evaluation_id": request["evaluation_id"],
                "holdout_ledger_sha256": request["holdout_ledger_sha256"],
                "annotation_sha256": request["annotation_sha256"],
                "holdout_set_sha256": request["holdout_set_sha256"],
                "registration_tool_sha256": request["registration_tool_sha256"],
                "toolchain_lock_sha256": request["toolchain_lock_sha256"],
                "annotation_authority_attestation_sha256": request[
                    "annotation_authority_attestation_sha256"
                ],
                "prior_head_sha256": request["expected_prior_head_sha256"],
                "request_nonce": request["request_nonce"],
                "consumed_at_utc": datetime.now(timezone.utc).isoformat(),
                "status": "consumed",
            }
            self.entries[entry["sequence"]] = copy.deepcopy(entry)
            self.evaluations.add(entry["evaluation_id"])
            self.head_envelope = signed_registry_head(
                entry["sequence"], tool.canonical_hash(entry),
                request["expected_prior_head_sha256"],
            )
            return {
                "schema": tool.HOLDOUT_REGISTRY_RECEIPT_SCHEMA,
                "registry_id": HOLDOUT_REGISTRY_ID,
                "consume_request_sha256": tool.canonical_hash(request),
                "entry": copy.deepcopy(entry),
                "head_envelope": copy.deepcopy(self.head_envelope),
            }

    def get_entry(self, sequence):
        with self.lock:
            return {"entry": copy.deepcopy(self.entries[sequence])}


def write_detached_attestation(path, value, private_key):
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
    signature_path = path.with_suffix(".sig")
    signature_path.write_bytes(private_key.sign(path.read_bytes()))
    return path, signature_path


def survey_authority_paths(survey_path):
    attestation = survey_path.parent / "survey-authority-attestation.json"
    return attestation, attestation.with_suffix(".sig")


def apply_transform(points, tx=100.0, ty=-50.0, yaw_deg=2.0, z_bias=5.0):
    yaw = math.radians(yaw_deg)
    cosine, sine = math.cos(yaw), math.sin(yaw)
    points = np.asarray(points, dtype=float)
    output = points.copy()
    output[:, 0] = cosine * points[:, 0] - sine * points[:, 1] + tx
    output[:, 1] = sine * points[:, 0] + cosine * points[:, 1] + ty
    output[:, 2] += z_bias
    return output


def synthetic_evidence(warp_holdout=None, initial=None):
    definitions = {
        "north": ([[0, 0, 0], [0, 8, 0], [0, 16, 0]], [[2, 0, 0], [2, 8, 0], [2, 16, 0]]),
        "east": ([[0, 20, 1], [8, 20, 1], [16, 20, 1]], [[0, 22, 1], [8, 22, 1], [16, 22, 1]]),
        "south": ([[20, 0, 2], [20, 8, 2], [20, 16, 2]], [[22, 0, 2], [22, 8, 2], [22, 16, 2]]),
        "west": ([[4, 4, 3], [10, 10, 3], [16, 16, 3]], [[4, 6, 3], [10, 12, 3], [16, 18, 3]]),
    }
    geometry_items, annotation_features, raw_points = [], [], []
    for approach, split_lines in definitions.items():
        for split, map_points in zip(("fit", "holdout"), split_lines):
            feature_id = f"{approach}-{split}"
            geometry_items.append({"id": feature_id, "left_boundary_world": map_points})
            lidar_points = apply_transform(map_points)
            if split == "holdout" and warp_holdout == approach:
                lidar_points[:, 0] += 0.8
            start = len(raw_points)
            raw_points.extend(lidar_points.tolist())
            annotation_features.append({
                "id": feature_id,
                "approach_id": approach,
                "split": split,
                "kind": "road_edge",
                "provenance": tool.MANUAL_PROVENANCE,
                "map": {
                    "collection": "lanes",
                    "feature_id": feature_id,
                    "polyline_field": "left_boundary_world",
                },
                "lidar": {
                    "tile_sha256": "tile-hash",
                    "point_indices": list(range(start, start + len(lidar_points))),
                    "physical_control_ids": [
                        f"physical-{feature_id}-{index}" for index in range(len(lidar_points))
                    ],
                    "xyz": lidar_points.tolist(),
                },
            })
    raw_points = np.asarray(raw_points)
    landmark_coordinates = [
        [0.0, 30.0, 0.0], [5.0, 30.0, 0.0], [10.0, 30.0, 0.0],
        [15.0, 30.0, 0.0], [20.0, 30.0, 0.0], [0.0, 35.0, 0.0],
        [5.0, 35.0, 0.0], [10.0, 35.0, 0.0], [15.0, 35.0, 0.0],
        [20.0, 35.0, 0.0], [30.0, 40.0, 0.0], [35.0, 40.0, 0.0],
        [30.0, 45.0, 0.0], [35.0, 45.0, 0.0],
    ]
    objects = [{
        "id": f"environment-TrafficLight-landmark-{index:02d}",
        "source_object_id": f"landmark-{index:02d}", "category": "TrafficLight",
        "semantic_source": {
            "schema": tool.NATIVE_OBJECT_SEMANTIC_SCHEMA,
            "api": tool.NATIVE_OBJECT_API,
            "native_type": "CityObjectLabel.TrafficLight",
            "native_subtype": None,
        },
        "name": f"survey monument {index:02d}", "center_world": point,
        "extent": [0.1, 0.1, 0.5],
    } for index, point in enumerate(landmark_coordinates)]
    geometry = {
        "schema": tool.GEOMETRY_SCHEMA,
        "opendrive_sha256": "xodr-hash",
        "geometry": {
            "lanes": geometry_items, "crosswalks": [], "road_mark_segments": [],
            "objects": objects,
        },
    }
    annotation = {
        "schema": tool.ANNOTATION_SCHEMA,
        "initial_transform": initial or {
            "tx_m": 100.2, "ty_m": -50.2, "yaw_deg": 2.2, "z_bias_m": 5.1,
        },
        "features": annotation_features,
    }
    tile = {
        "sha256": "tile-hash",
        "validation_sha256": "validation-hash",
        "points": raw_points,
        "point_count": len(raw_points),
        "scales": [0.01, 0.01, 0.01],
    }
    metadata = {
        "collect_start": int(datetime(2018, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        "ql": "QL 2",
    }
    survey = {
        "present": False, "passed": False,
        "reasons": ["current_horizontal_survey_missing"],
    }
    return annotation, geometry, {"tile-hash": tile}, metadata, survey


def run_synthetic(**kwargs):
    annotation, geometry, tiles, metadata, survey = synthetic_evidence(**kwargs)
    return tool.register(annotation, geometry, tiles, metadata, {"source": "synthetic"}, survey)


def test_production_authority_allowlists_default_empty():
    fresh_spec = importlib.util.spec_from_file_location("register_map_defaults", TOOL_PATH)
    fresh = importlib.util.module_from_spec(fresh_spec)
    fresh_spec.loader.exec_module(fresh)
    assert fresh.TRUSTED_SURVEY_AUTHORITY_SIGNERS == {}
    assert fresh.TRUSTED_CRS_AUTHORITY_SIGNERS == {}
    assert fresh.TRUSTED_ANNOTATION_AUTHORITY_SIGNERS == {}
    assert fresh.TRUSTED_LIDAR_VALIDATION_SIGNERS == {}
    assert fresh.TRUSTED_VERTICAL_DATUM_SIGNERS == {}
    assert fresh.TRUSTED_HOLDOUT_REGISTRY_SIGNERS == {}
    assert fresh.TRUSTED_HOLDOUT_REGISTRY_ENDPOINTS == {}


def test_tracked_deterministic_toolchain_lock_matches_current_runtime():
    evidence = tool.validate_toolchain_lock()
    assert evidence["passed"] is True
    assert evidence["runtime"]["thread_environment"] == {
        "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1", "OPENBLAS_CORETYPE": "Haswell",
    }


def test_toolchain_lock_rejects_nondeterministic_thread_override(monkeypatch):
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "4")
    with pytest.raises(tool.RegistrationError, match="deterministic toolchain"):
        tool.validate_toolchain_lock()


def test_toolchain_lock_rejects_different_openblas_kernel(monkeypatch):
    monkeypatch.setenv("OPENBLAS_CORETYPE", "SkylakeX")
    with pytest.raises(tool.RegistrationError, match="deterministic toolchain"):
        tool.validate_toolchain_lock()


def write_valid_pdf(path, title, *, encrypted=False):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title, "/Subject": "retained-evidence-" * 400})
    if encrypted:
        writer.encrypt("test-password")
    with path.open("wb") as stream:
        writer.write(stream)
    assert path.stat().st_size >= tool.MIN_AUTHORITY_PDF_BYTES


def insert_before_final_pdf_xref(content, payload):
    match = re.search(
        rb"startxref[ \t\r\n]+([0-9]+)[ \t\r\n]+%%EOF[ \t\r\n]*\Z",
        content,
    )
    assert match is not None
    xref_offset = int(match.group(1))
    assert content[xref_offset:xref_offset + 4] == b"xref"
    replacement = b"startxref\n" + str(xref_offset + len(payload)).encode() + b"\n%%EOF\n"
    return content[:xref_offset] + payload + content[xref_offset:match.start()] + replacement


def insert_before_final_pdf_startxref(content, payload):
    match = re.search(
        rb"startxref[ \t\r\n]+[0-9]+[ \t\r\n]+%%EOF[ \t\r\n]*\Z",
        content,
    )
    assert match is not None
    return content[:match.start()] + payload + content[match.start():]


def write_current_survey(tmp_path, geometry, geometry_hash="geometry", opendrive_hash="opendrive",
                         corrupt_summary=False):
    from pyproj import CRS

    objects = geometry["geometry"]["objects"]
    observations = tmp_path / "licensed-observations.csv"
    fieldnames = list(tool.SURVEY_OBSERVATION_COLUMNS)
    rows, bindings = [], []
    for index, feature in enumerate(objects[:14]):
        split = "fit" if index < 10 else "holdout"
        surveyed = apply_transform([feature["center_world"]])[0, :2]
        observation_id = f"survey-{split}-{index}"
        rows.append({
            "observation_id": observation_id,
            "physical_control_id": f"monument-{index}",
            "stable_landmark_id": feature["id"], "split": split,
            "easting_m": f"{surveyed[0]:.12f}", "northing_m": f"{surveyed[1]:.12f}",
            "horizontal_uncertainty_m": "0.02",
            "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "surveyor_license": "PLS-12345", "instrument_serial": "TS-9000-001",
            "source_id": "licensed-field-book-2026-07",
        })
        bindings.append({
            "observation_id": observation_id,
            "map": {
                "collection": "objects", "feature_id": feature["id"],
                "point_field": "center_world", "vertex_index": 0,
            },
        })
    with observations.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    survey_license = tmp_path / "surveyor-license.pdf"
    instrument_calibration = tmp_path / "instrument-calibration.pdf"
    write_valid_pdf(survey_license, "Licensed surveyor PLS-12345")
    write_valid_pdf(instrument_calibration, "Instrument TS-9000-001 calibration")
    deliverables = [observations, survey_license, instrument_calibration]
    roles = {
        observations: "raw_observations", survey_license: "survey_license",
        instrument_calibration: "instrument_calibration",
    }
    crs = CRS.from_epsg(26910)
    value = {
        "schema": tool.SURVEY_SCHEMA,
        "geometry_sha256": geometry_hash,
        "opendrive_sha256": opendrive_hash,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "horizontal_crs": {
            "epsg": 26910,
            "wkt": crs.to_wkt(),
            "linear_units": crs.axis_info[0].unit_name,
            "datum": crs.datum.name,
            "coordinate_epoch": None,
        },
        "licensed_source": {
            "provider": "Licensed Survey Provider LLC",
            "source_id": "licensed-field-book-2026-07",
            "project_id": "RICHMOND-V2X-2026",
            "surveyor": {
                "name": "Licensed Surveyor", "license_number": "PLS-12345",
                "licensing_authority": "California Board for Professional Engineers",
            },
            "instrument": {
                "manufacturer": "Leica", "model": "TS16",
                "serial_number": "TS-9000-001",
                "calibration_deliverable_sha256": tool.sha256(instrument_calibration),
            },
            "survey_license_deliverable_sha256": tool.sha256(survey_license),
        },
        "raw_deliverables": [{
            "file_name": item.name, "sha256": tool.sha256(item),
            "bytes": item.stat().st_size, "role": roles[item],
        } for item in deliverables],
        "observations": {
            "file_name": observations.name, "sha256": tool.sha256(observations),
            "bytes": observations.stat().st_size, "format": "csv",
        },
        "control_bindings": bindings,
    }
    if corrupt_summary:
        value.update({"horizontal_rmse_m": 0.0, "horizontal_max_m": 0.0})
    path = tmp_path / "survey.json"
    path.write_text(json.dumps(value))
    now = datetime.now(timezone.utc)
    attestation_value = {
        "schema": tool.SURVEY_AUTHORITY_ATTESTATION_SCHEMA,
        "signing_key_id": SURVEY_AUTHORITY_KEY_ID,
        "public_key_sha256": tool.hashlib.sha256(
            public_key_pem(SURVEY_AUTHORITY_PRIVATE_KEY)
        ).hexdigest(),
        "producer": "Independent State Survey Verification Service",
        "source": "state-license-registry-test-fixture",
        "verified_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(days=30)).isoformat(),
        "survey_manifest_sha256": tool.sha256(path),
        "licensed_source": {
            "provider": "Licensed Survey Provider LLC",
            "source_id": "licensed-field-book-2026-07",
            "project_id": "RICHMOND-V2X-2026",
            "surveyor": {
                "name": "Licensed Surveyor", "license_number": "PLS-12345",
                "licensing_authority": "California Board for Professional Engineers",
            },
            "instrument": {
                "manufacturer": "Leica", "model": "TS16",
                "serial_number": "TS-9000-001",
            },
        },
        "deliverables": sorted(value["raw_deliverables"], key=lambda item: item["role"]),
        "verification_result": {
            "provider_status": "verified",
            "surveyor_license_status": "active",
            "instrument_calibration_status": "valid",
            "deliverable_integrity_status": "verified",
        },
    }
    write_detached_attestation(
        tmp_path / "survey-authority-attestation.json",
        attestation_value, SURVEY_AUTHORITY_PRIVATE_KEY,
    )
    return path, deliverables, observations


def rebind_observations(path, observations):
    value = json.loads(path.read_text())
    digest = tool.sha256(observations)
    value["observations"].update({
        "sha256": digest, "bytes": observations.stat().st_size,
    })
    declaration = next(
        item for item in value["raw_deliverables"] if item["role"] == "raw_observations"
    )
    declaration.update({"sha256": digest, "bytes": observations.stat().st_size})
    path.write_text(json.dumps(value))


def rebind_survey_deliverable(path, deliverable, role):
    value = json.loads(path.read_text())
    digest = tool.sha256(deliverable)
    declaration = next(
        item for item in value["raw_deliverables"] if item["role"] == role
    )
    declaration.update({"sha256": digest, "bytes": deliverable.stat().st_size})
    if role == "survey_license":
        value["licensed_source"]["survey_license_deliverable_sha256"] = digest
    elif role == "instrument_calibration":
        value["licensed_source"]["instrument"][
            "calibration_deliverable_sha256"
        ] = digest
    path.write_text(json.dumps(value))


def validate_written_survey(path, geometry, deliverables, observations,
                            geometry_hash="geometry", opendrive_hash="opendrive",
                            horizontal_epsg=26910, horizontal_wkt=None,
                            coordinate_epoch=None):
    attestation, signature = survey_authority_paths(path)
    return tool.validate_current_survey(
        path, geometry, geometry_hash, opendrive_hash, horizontal_epsg,
        deliverables, observations, horizontal_wkt, coordinate_epoch,
        attestation, signature,
    )


def write_crs_authority_attestation(tmp_path, artifact_path):
    artifact = json.loads(artifact_path.read_text())
    now = datetime.now(timezone.utc)
    value = {
        "schema": tool.CRS_AUTHORITY_ATTESTATION_SCHEMA,
        "signing_key_id": CRS_AUTHORITY_KEY_ID,
        "public_key_sha256": tool.hashlib.sha256(
            public_key_pem(CRS_AUTHORITY_PRIVATE_KEY)
        ).hexdigest(),
        "producer": "Independent Geodetic Authority",
        "source": "official-operation-registry-test-fixture",
        "verified_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(days=30)).isoformat(),
        "reconciliation_artifact_sha256": tool.sha256(artifact_path),
        "proj_pipeline_sha256": artifact["proj_pipeline_sha256"],
        "source_crs_wkt_sha256": artifact["source_crs_wkt_sha256"],
        "target_crs_wkt_sha256": artifact["target_crs_wkt_sha256"],
        "source_coordinate_epoch": artifact["source_coordinate_epoch"],
        "target_coordinate_epoch": artifact["target_coordinate_epoch"],
        "authority": artifact["authority"],
        "operation_id": artifact["operation_id"],
        "check_points_sha256": tool.canonical_hash(artifact["check_points"]),
        "verification_result": {
            "transform_source_status": "official",
            "operation_status": "authorized",
            "control_source_status": "independent",
        },
    }
    return write_detached_attestation(
        tmp_path / "crs-authority-attestation.json", value,
        CRS_AUTHORITY_PRIVATE_KEY,
    )


def write_annotation_review_bundle(tmp_path, annotation, features, registry=None):
    registry = registry or FakeHoldoutRegistry()
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(json.dumps(annotation))
    now = datetime.now(timezone.utc)
    reviewed_features = [{
        "feature_id": item["id"],
        "map_xyz": item["map_points"].tolist(),
        "lidar_xyz": item["lidar_points"].tolist(),
    } for item in features]
    review_value = {
        "schema": tool.ANNOTATION_REVIEW_SCHEMA,
        "annotation_sha256": tool.sha256(annotation_path),
        "reviewers": [{
            "reviewer_id": "reviewer-a", "organization": "Review Org A",
            "reviewed_at_utc": now.isoformat(),
            "features": copy.deepcopy(reviewed_features),
        }, {
            "reviewer_id": "reviewer-b", "organization": "Review Org B",
            "reviewed_at_utc": now.isoformat(),
            "features": copy.deepcopy(reviewed_features),
        }],
    }
    review_path = tmp_path / "annotation-review.json"
    review_path.write_text(json.dumps(review_value))
    review = tool.validate_annotation_review(annotation_path, review_path, features)
    prior_head = registry.get_head()["head"]
    ledger_value = {
        "schema": tool.HOLDOUT_LEDGER_SCHEMA,
        "annotation_sha256": tool.sha256(annotation_path),
        "holdout_set_sha256": review["holdout_set_sha256"],
        "evaluation_id": "one-time-final-evaluation",
        "purpose": "final_acceptance",
        "prior_evaluation_count": 0,
        "maximum_evaluation_count": 1,
        "prior_evaluation_ids": [],
        "registry_id": HOLDOUT_REGISTRY_ID,
        "registry_prior_sequence": prior_head["sequence"],
        "registry_prior_head_sha256": tool.canonical_hash(prior_head),
        "authorized_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(days=7)).isoformat(),
        "burn_receipt_path": str(
            (tmp_path / "one-time-final-evaluation-burn.json").resolve()
        ),
    }
    ledger_path = tmp_path / "holdout-ledger.json"
    ledger_path.write_text(json.dumps(ledger_value))
    ledger = tool.validate_holdout_ledger(annotation_path, ledger_path, review)
    attestation_value = {
        "schema": tool.ANNOTATION_AUTHORITY_ATTESTATION_SCHEMA,
        "signing_key_id": ANNOTATION_AUTHORITY_KEY_ID,
        "public_key_sha256": tool.hashlib.sha256(
            public_key_pem(ANNOTATION_AUTHORITY_PRIVATE_KEY)
        ).hexdigest(),
        "producer": "Independent Annotation Authority",
        "source": "annotation-review-registry-test-fixture",
        "verified_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(days=30)).isoformat(),
        "annotation_sha256": tool.sha256(annotation_path),
        "annotation_review_sha256": review["sha256"],
        "holdout_ledger_sha256": ledger["sha256"],
        "holdout_set_sha256": review["holdout_set_sha256"],
        "reviewers": review["reviewers"],
        "registry_id": HOLDOUT_REGISTRY_ID,
        "registry_prior_sequence": prior_head["sequence"],
        "registry_prior_head_sha256": tool.canonical_hash(prior_head),
        "verification_result": {
            "annotation_status": "verified",
            "interreview_status": "independent",
            "holdout_status": "authorized_once",
        },
    }
    attestation_path, signature_path = write_detached_attestation(
        tmp_path / "annotation-authority-attestation.json",
        attestation_value, ANNOTATION_AUTHORITY_PRIVATE_KEY,
    )
    return (
        annotation_path, review_path, ledger_path,
        attestation_path, signature_path, registry,
    )


def test_known_site_transform_passes_numerical_gates_but_2018_ql2_stays_ineligible():
    report = run_synthetic()
    transform = report["model"]["transform"]
    assert transform["tx_m"] == pytest.approx(100.0, abs=2e-3)
    assert transform["ty_m"] == pytest.approx(-50.0, abs=2e-3)
    assert transform["yaw_deg"] == pytest.approx(2.0, abs=2e-3)
    assert transform["z_bias_m"] == pytest.approx(5.0, abs=2e-3)
    assert report["numerical_registration_passed"] is True
    assert report["acceptance_eligible"] is False
    assert "2018_ql2_is_development_control_only" in report["reasons"]
    assert "current_horizontal_survey_missing" in report["reasons"]
    assert "lidar_validation_authority_attestation_missing" in report["reasons"]
    assert "annotation_interreview_missing" in report["reasons"]
    assert "holdout_evaluation_burn_uncontrolled" in report["reasons"]
    assert "annotation_authority_attestation_missing" in report["reasons"]
    assert "vertical_datum_reconciliation_missing" in report["reasons"]
    assert report["optimizer"]["jacobian_rank"] == 4
    assert report["leave_one_approach_out"]["translation_spread_m"] <= 0.10
    assert report["leave_one_approach_out"]["yaw_spread_deg"] <= 0.10


def test_ql2_collection_spanning_december_2017_into_2018_is_development_only():
    annotation, geometry, tiles, metadata, survey = synthetic_evidence()
    metadata["collect_start"] = int(datetime(2017, 12, 1, tzinfo=timezone.utc).timestamp() * 1000)
    metadata["collect_end"] = int(datetime(2018, 4, 24, tzinfo=timezone.utc).timestamp() * 1000)
    report = tool.register(annotation, geometry, tiles, metadata, {}, survey)
    assert report["acceptance_eligible"] is False
    assert "2018_ql2_is_development_control_only" in report["reasons"]


def test_current_survey_does_not_promote_2018_ql2_to_acceptance():
    annotation, geometry, tiles, metadata, _ = synthetic_evidence()
    survey = {"present": True, "passed": True, "reasons": [], "sha256": "survey"}
    report = tool.register(annotation, geometry, tiles, metadata, {}, survey)
    assert report["numerical_registration_passed"] is True
    assert report["acceptance_eligible"] is False
    assert report["deployment_eligible"] is False


def test_local_holdout_warp_is_rejected_by_one_global_model():
    report = run_synthetic(warp_holdout="east")
    assert report["numerical_registration_passed"] is False
    assert any(reason.startswith("holdout") for reason in report["reasons"])
    assert report["model"]["forbidden_degrees_of_freedom"][-1] == "local_warp"


def test_distance_uses_finite_segment_endpoint_not_an_infinite_line():
    source = np.asarray([[2.0, 1.0, 0.0]])
    target = np.asarray([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    distance = tool.nearest_segments(source, target)
    assert distance["horizontal"][0] == pytest.approx(math.sqrt(2.0))
    assert abs(distance["normal"][0]) == pytest.approx(1.0)


def test_raw_point_identity_cannot_leak_between_fit_and_holdout():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    annotation["features"][1]["lidar"]["point_indices"][0] = annotation["features"][0]["lidar"]["point_indices"][0]
    annotation["features"][1]["lidar"]["xyz"][0] = annotation["features"][0]["lidar"]["xyz"][0]
    with pytest.raises(tool.RegistrationError, match="leaks between"):
        tool.load_features(annotation, geometry, tiles)


def test_map_polyline_identity_cannot_leak_between_fit_and_holdout():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    annotation["features"][1]["map"] = copy.deepcopy(annotation["features"][0]["map"])
    with pytest.raises(tool.RegistrationError, match="map polyline identity leaks"):
        tool.load_features(annotation, geometry, tiles)


def test_source_feature_identity_cannot_leak_through_different_vertex_slices():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    geometry["geometry"]["lanes"][0]["left_boundary_world"] = [
        [0, 0, 0], [0, 4, 0], [0, 8, 0], [0, 12, 0], [0, 16, 0],
    ]
    annotation["features"][0]["map"]["vertex_indices"] = [0, 1, 2]
    annotation["features"][1]["map"] = copy.deepcopy(annotation["features"][0]["map"])
    annotation["features"][1]["map"]["vertex_indices"] = [2, 3, 4]
    with pytest.raises(tool.RegistrationError, match="map source feature identity leaks"):
        tool.load_features(annotation, geometry, tiles)


def test_physical_control_identity_cannot_be_renamed_across_splits():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    annotation["features"][1]["lidar"]["physical_control_ids"][0] = (
        annotation["features"][0]["lidar"]["physical_control_ids"][0]
    )
    with pytest.raises(tool.RegistrationError, match="physical control identity leaks"):
        tool.load_features(annotation, geometry, tiles)


def test_geometric_resampling_duplicate_with_new_source_id_is_rejected():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    original = geometry["geometry"]["lanes"][0]["left_boundary_world"]
    duplicate = {
        "id": "renamed-resample",
        "left_boundary_world": [[0, 0, 0], [0, 4, 0], [0, 8, 0], [0, 12, 0], [0, 16, 0]],
    }
    geometry["geometry"]["lanes"].append(duplicate)
    annotation["features"][1]["map"] = {
        "collection": "lanes", "feature_id": duplicate["id"],
        "polyline_field": "left_boundary_world",
    }
    assert duplicate["left_boundary_world"] != original
    with pytest.raises(tool.RegistrationError, match="geometric duplicate/resampling"):
        tool.load_features(annotation, geometry, tiles)


def test_fit_holdout_endpoint_overlap_is_rejected_even_with_distinct_ids():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    fit_feature, holdout_feature = annotation["features"][0], annotation["features"][1]
    geometry["geometry"]["lanes"][1]["left_boundary_world"][0] = list(
        geometry["geometry"]["lanes"][0]["left_boundary_world"][0]
    )
    holdout_feature["lidar"]["xyz"][0] = list(fit_feature["lidar"]["xyz"][0])
    holdout_index = holdout_feature["lidar"]["point_indices"][0]
    tiles["tile-hash"]["points"][holdout_index] = fit_feature["lidar"]["xyz"][0]
    with pytest.raises(tool.RegistrationError, match="coordinate or endpoint overlap"):
        tool.load_features(annotation, geometry, tiles)


def test_fit_holdout_polylines_inside_spatial_exclusion_buffer_are_rejected():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    holdout_map = [[0.495, 0.0, 0.0], [0.495, 8.0, 0.0], [0.495, 16.0, 0.0]]
    geometry["geometry"]["lanes"][1]["left_boundary_world"] = holdout_map
    transformed = apply_transform(holdout_map)
    annotation["features"][1]["lidar"]["xyz"] = transformed.tolist()
    indices = annotation["features"][1]["lidar"]["point_indices"]
    tiles["tile-hash"]["points"][indices] = transformed
    with pytest.raises(tool.RegistrationError, match="spatial exclusion buffer"):
        tool.load_features(annotation, geometry, tiles)


def test_exact_segment_distance_rejects_0_495_m_split_separation():
    left = np.asarray([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
    right = np.asarray([[0.123, 0.495, 0.0], [10.123, 0.495, 0.0]])
    assert tool.minimum_polyline_distance(left, right) == pytest.approx(0.495)
    assert tool.minimum_polyline_distance(left, right) <= tool.SPATIAL_EXCLUSION_BUFFER_M


def test_exact_segment_distance_handles_intersections_and_degenerate_segments():
    crossing = tool._segment_segment_distance_2d(
        np.asarray([0.0, 0.0]), np.asarray([2.0, 2.0]),
        np.asarray([0.0, 2.0]), np.asarray([2.0, 0.0]),
    )
    point_to_segment = tool._segment_segment_distance_2d(
        np.asarray([0.0, 0.0]), np.asarray([0.0, 0.0]),
        np.asarray([1.0, -1.0]), np.asarray([1.0, 1.0]),
    )
    two_points = tool._segment_segment_distance_2d(
        np.asarray([0.0, 0.0]), np.asarray([0.0, 0.0]),
        np.asarray([3.0, 4.0]), np.asarray([3.0, 4.0]),
    )
    assert crossing == 0.0
    assert point_to_segment == pytest.approx(1.0)
    assert two_points == pytest.approx(5.0)


def test_annotation_interreview_and_one_time_holdout_burn_are_enforced(tmp_path):
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    features = tool.load_features(annotation, geometry, tiles)
    annotation_path, review_path, ledger_path, attestation_path, signature_path, registry = (
        write_annotation_review_bundle(tmp_path, annotation, features)
    )
    review = tool.validate_annotation_review(annotation_path, review_path, features)
    ledger = tool.validate_holdout_ledger(annotation_path, ledger_path, review)
    authority = tool.validate_annotation_authority(
        annotation_path, review, ledger, attestation_path, signature_path
    )
    burned = tool.authorize_and_burn_holdout(review, ledger, authority, registry)
    assert review["repeatability_max_m"] == 0.0
    assert authority["signing_key_id"] == ANNOTATION_AUTHORITY_KEY_ID
    assert burned["burned"] is True
    assert burned["registry_consumption"]["confirmed"] is True
    with pytest.raises(tool.RegistrationError, match="already burned"):
        tool.validate_holdout_ledger(annotation_path, ledger_path, review)
    copied_dir = tmp_path / "copied-ledger-bypass"
    copied_dir.mkdir()
    copied_ledger = copied_dir / ledger_path.name
    copied_ledger.write_bytes(ledger_path.read_bytes())
    with pytest.raises(tool.RegistrationError, match="ledger is invalid"):
        tool.validate_holdout_ledger(annotation_path, copied_ledger, review)


def test_deleted_or_restored_local_receipt_cannot_reuse_external_consumption(tmp_path):
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    features = tool.load_features(annotation, geometry, tiles)
    annotation_path, review_path, ledger_path, attestation_path, signature_path, registry = (
        write_annotation_review_bundle(tmp_path, annotation, features)
    )
    review = tool.validate_annotation_review(annotation_path, review_path, features)
    ledger = tool.validate_holdout_ledger(annotation_path, ledger_path, review)
    authority = tool.validate_annotation_authority(
        annotation_path, review, ledger, attestation_path, signature_path
    )
    burned = tool.authorize_and_burn_holdout(review, ledger, authority, registry)
    Path(burned["burn_receipt_path"]).unlink()
    restored_local_ledger = tool.validate_holdout_ledger(
        annotation_path, ledger_path, review
    )
    with pytest.raises(tool.RegistrationError, match="registry head"):
        tool.authorize_and_burn_holdout(
            review, restored_local_ledger, authority, registry
        )


def test_offline_local_receipt_never_substitutes_for_allowlisted_registry(
    tmp_path, monkeypatch
):
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    features = tool.load_features(annotation, geometry, tiles)
    annotation_path, review_path, ledger_path, attestation_path, signature_path, registry = (
        write_annotation_review_bundle(tmp_path, annotation, features)
    )
    review = tool.validate_annotation_review(annotation_path, review_path, features)
    ledger = tool.validate_holdout_ledger(annotation_path, ledger_path, review)
    authority = tool.validate_annotation_authority(
        annotation_path, review, ledger, attestation_path, signature_path
    )
    monkeypatch.setattr(tool, "TRUSTED_HOLDOUT_REGISTRY_ENDPOINTS", {})
    with pytest.raises(tool.RegistrationError, match="endpoint is not pinned"):
        tool.authorize_and_burn_holdout(review, ledger, authority, registry)
    assert not Path(ledger["burn_receipt_path"]).exists()


@pytest.mark.parametrize("mode", ["rollback", "fork"])
def test_registry_rollback_or_fork_head_is_rejected(tmp_path, mode):
    registry = FakeHoldoutRegistry()
    genesis = registry.head_envelope["head"]
    authorized_head = signed_registry_head(
        1, "1" * 64, tool.canonical_hash(genesis)
    )
    registry.head_envelope = authorized_head
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    features = tool.load_features(annotation, geometry, tiles)
    annotation_path, review_path, ledger_path, attestation_path, signature_path, _ = (
        write_annotation_review_bundle(tmp_path, annotation, features, registry)
    )
    review = tool.validate_annotation_review(annotation_path, review_path, features)
    ledger = tool.validate_holdout_ledger(annotation_path, ledger_path, review)
    authority = tool.validate_annotation_authority(
        annotation_path, review, ledger, attestation_path, signature_path
    )
    if mode == "rollback":
        registry.head_envelope = signed_registry_head(0, "0" * 64, "0" * 64)
    else:
        registry.head_envelope = signed_registry_head(
            1, "f" * 64, tool.canonical_hash(genesis)
        )
    with pytest.raises(tool.RegistrationError, match="signed authorization base"):
        tool.consume_holdout_registry(ledger, authority, registry)


@pytest.mark.parametrize("failure", ["stale", "signature"])
def test_registry_head_freshness_and_signature_are_fail_closed(tmp_path, failure):
    registry = FakeHoldoutRegistry()
    if failure == "stale":
        registry.head_envelope = signed_registry_head(
            0, "0" * 64, "0" * 64,
            observed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
    else:
        registry.head_envelope["signature_base64"] = base64.b64encode(
            b"\x00" * 64
        ).decode()
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    features = tool.load_features(annotation, geometry, tiles)
    annotation_path, review_path, ledger_path, attestation_path, signature_path, _ = (
        write_annotation_review_bundle(tmp_path, annotation, features, registry)
    )
    review = tool.validate_annotation_review(annotation_path, review_path, features)
    ledger = tool.validate_holdout_ledger(annotation_path, ledger_path, review)
    authority = tool.validate_annotation_authority(
        annotation_path, review, ledger, attestation_path, signature_path
    )
    with pytest.raises(
        tool.RegistrationError, match="stale|signature is invalid"
    ):
        tool.consume_holdout_registry(ledger, authority, registry)


@pytest.mark.parametrize("failure", ["head_registry", "signer_registry"])
def test_registry_head_cannot_cross_registry_or_signer_namespace(
    monkeypatch, failure
):
    other_key_id = "other-registry-key"
    other_registry_id = "other-registry"
    other_key = Ed25519PrivateKey.from_private_bytes(bytes(range(193, 225)))
    signers = copy.deepcopy(tool.TRUSTED_HOLDOUT_REGISTRY_SIGNERS)
    signers[other_key_id] = {
        "registry_id": other_registry_id,
        "producer": "Other Registry",
        "source": "other-registry-source",
        "public_key_pem": public_key_pem(other_key),
    }
    monkeypatch.setattr(tool, "TRUSTED_HOLDOUT_REGISTRY_SIGNERS", signers)
    head = copy.deepcopy(signed_registry_head(0, "0" * 64, "0" * 64)["head"])
    if failure == "head_registry":
        head["registry_id"] = other_registry_id
    else:
        head.update({
            "signing_key_id": other_key_id,
            "public_key_sha256": tool.hashlib.sha256(public_key_pem(other_key)).hexdigest(),
            "producer": "Other Registry",
            "source": "other-registry-source",
        })
    envelope = {
        "head": head,
        "signature_base64": base64.b64encode(
            other_key.sign(tool.canonical_json_bytes(head))
        ).decode(),
    }
    with pytest.raises(
        tool.RegistrationError, match="binding is invalid|signer is not pinned"
    ):
        tool._verify_holdout_registry_head(envelope, HOLDOUT_REGISTRY_ID)


def test_registry_producer_must_be_independent_from_annotation_authority(
    tmp_path, monkeypatch
):
    registry = FakeHoldoutRegistry()
    signers = copy.deepcopy(tool.TRUSTED_HOLDOUT_REGISTRY_SIGNERS)
    signers[HOLDOUT_REGISTRY_KEY_ID].update({
        "producer": "Independent Annotation Authority",
        "source": "annotation-review-registry-test-fixture",
    })
    monkeypatch.setattr(tool, "TRUSTED_HOLDOUT_REGISTRY_SIGNERS", signers)
    head = registry.head_envelope["head"]
    head.update({
        "producer": "Independent Annotation Authority",
        "source": "annotation-review-registry-test-fixture",
    })
    registry.head_envelope["signature_base64"] = base64.b64encode(
        HOLDOUT_REGISTRY_PRIVATE_KEY.sign(tool.canonical_json_bytes(head))
    ).decode()
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    features = tool.load_features(annotation, geometry, tiles)
    annotation_path, review_path, ledger_path, attestation_path, signature_path, _ = (
        write_annotation_review_bundle(tmp_path, annotation, features, registry)
    )
    review = tool.validate_annotation_review(annotation_path, review_path, features)
    ledger = tool.validate_holdout_ledger(annotation_path, ledger_path, review)
    authority = tool.validate_annotation_authority(
        annotation_path, review, ledger, attestation_path, signature_path
    )
    with pytest.raises(tool.RegistrationError, match="not independent"):
        tool.consume_holdout_registry(ledger, authority, registry)


def test_registry_receipt_fork_and_missing_inclusion_are_rejected(tmp_path):
    class ForkingRegistry(FakeHoldoutRegistry):
        def consume(self, request):
            receipt = super().consume(request)
            receipt["head_envelope"] = signed_registry_head(
                receipt["entry"]["sequence"],
                tool.canonical_hash(receipt["entry"]), "f" * 64,
            )
            return receipt

    class MissingEntryRegistry(FakeHoldoutRegistry):
        def get_entry(self, sequence):
            return {"entry": {"sequence": sequence, "status": "missing"}}

    for registry, message in (
        (ForkingRegistry(), "append chain"),
        (MissingEntryRegistry(), "inclusion"),
    ):
        case = tmp_path / message.replace(" ", "-")
        case.mkdir()
        annotation, geometry, tiles, _, _ = synthetic_evidence()
        features = tool.load_features(annotation, geometry, tiles)
        annotation_path, review_path, ledger_path, attestation_path, signature_path, _ = (
            write_annotation_review_bundle(case, annotation, features, registry)
        )
        review = tool.validate_annotation_review(annotation_path, review_path, features)
        ledger = tool.validate_holdout_ledger(annotation_path, ledger_path, review)
        authority = tool.validate_annotation_authority(
            annotation_path, review, ledger, attestation_path, signature_path
        )
        with pytest.raises(tool.RegistrationError, match=message):
            tool.consume_holdout_registry(ledger, authority, registry)


def test_registry_atomic_compare_and_append_allows_only_one_concurrent_consumer(tmp_path):
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    features = tool.load_features(annotation, geometry, tiles)
    annotation_path, review_path, ledger_path, attestation_path, signature_path, registry = (
        write_annotation_review_bundle(tmp_path, annotation, features)
    )
    review = tool.validate_annotation_review(annotation_path, review_path, features)
    ledger = tool.validate_holdout_ledger(annotation_path, ledger_path, review)
    authority = tool.validate_annotation_authority(
        annotation_path, review, ledger, attestation_path, signature_path
    )
    results = []

    def consume():
        try:
            results.append(("ok", tool.consume_holdout_registry(ledger, authority, registry)))
        except tool.RegistrationError as exc:
            results.append(("error", str(exc)))

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [item[0] for item in results].count("ok") == 1
    assert [item[0] for item in results].count("error") == 1
    assert len(registry.entries) == 1


def test_cli_holdout_evaluation_fails_closed_before_any_metric_is_computed(tmp_path):
    receipt_path = tmp_path / "burn.json"
    review = {"passed": True}
    ledger = {
        "passed": True,
        "burn_receipt_path": str(receipt_path),
        "evaluation_id": "sealed-final-1",
        "sha256": "ledger-hash",
        "holdout_set_sha256": "holdout-hash",
    }
    authority = {"attestation_sha256": "authority-hash"}
    with pytest.raises(tool.RegistrationError, match="independent annotation review"):
        tool.authorize_and_burn_holdout({"passed": False}, ledger, authority)
    with pytest.raises(tool.RegistrationError, match="one-time authorization ledger"):
        tool.authorize_and_burn_holdout(review, {"passed": False}, authority)
    with pytest.raises(tool.RegistrationError, match="authenticated annotation authority"):
        tool.authorize_and_burn_holdout(review, ledger, None)
    assert receipt_path.exists() is False


def test_annotation_interreview_repeatability_above_fixed_limit_fails(tmp_path):
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    features = tool.load_features(annotation, geometry, tiles)
    annotation_path, review_path, _, _, _, _ = write_annotation_review_bundle(
        tmp_path, annotation, features
    )
    review = json.loads(review_path.read_text())
    review["reviewers"][1]["features"][0]["lidar_xyz"][0][0] += 0.101
    review_path.write_text(json.dumps(review))
    with pytest.raises(tool.RegistrationError, match="repeatability"):
        tool.validate_annotation_review(annotation_path, review_path, features)


def test_automatic_or_unspecified_feature_provenance_is_rejected():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    annotation["features"][0]["provenance"] = "automatic_matcher"
    with pytest.raises(tool.RegistrationError, match="provenance"):
        tool.load_features(annotation, geometry, tiles)


def test_two_point_annotation_is_rejected_as_underconstrained():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    feature = annotation["features"][0]
    feature["map"]["vertex_indices"] = [0, 2]
    feature["lidar"]["point_indices"] = feature["lidar"]["point_indices"][:2]
    feature["lidar"]["physical_control_ids"] = (
        feature["lidar"]["physical_control_ids"][:2]
    )
    feature["lidar"]["xyz"] = feature["lidar"]["xyz"][:2]
    with pytest.raises(tool.RegistrationError, match="fewer than 3"):
        tool.load_features(annotation, geometry, tiles)


def test_rank_deficient_manual_geometry_is_rejected_before_fit():
    annotation, geometry, tiles, _, _ = synthetic_evidence()
    for feature in geometry["geometry"]["lanes"]:
        feature["left_boundary_world"] = [[0, 0, 0], [4, 0, 0], [8, 0, 0]]
    for feature in annotation["features"]:
        indices = feature["lidar"]["point_indices"]
        replacement = apply_transform([[0, 0, 0], [4, 0, 0], [8, 0, 0]])
        tiles["tile-hash"]["points"][indices] = replacement
        feature["lidar"]["xyz"] = replacement.tolist()
    with pytest.raises(tool.RegistrationError, match="rank deficient"):
        tool.load_features(annotation, geometry, tiles)


def test_solution_at_fixed_optimizer_bound_is_not_accepted():
    report = run_synthetic(initial={
        "tx_m": 60.0, "ty_m": -50.0, "yaw_deg": 2.0, "z_bias_m": 5.0,
    })
    assert report["numerical_registration_passed"] is False
    assert "fit_parameter_bound_hit" in report["reasons"]


def test_deterministic_seeds_cover_every_declared_parameter_bound():
    initial = np.asarray([10.0, 20.0, math.radians(3.0), 4.0])
    lower, upper = tool.parameter_bounds(initial)
    seeds = np.asarray(tool.deterministic_seeds(initial, lower, upper))
    span = upper - lower
    assert len(seeds) >= 17
    assert np.all(np.min(seeds, axis=0) - lower <= span * 2e-8)
    assert np.all(upper - np.max(seeds, axis=0) <= span * 2e-8)


def test_near_optimal_solutions_are_clustered_into_separate_basins():
    class Result:
        def __init__(self, cost, values):
            self.cost = cost
            self.x = np.asarray(values, dtype=float)

    clusters = tool.cluster_solution_basins([
        Result(1.0, [0, 0, 0, 0]),
        Result(1.01, [0.01, 0.01, math.radians(0.01), 0.01]),
        Result(1.02, [1.0, 0, math.radians(1.0), 0]),
    ])
    assert len(clusters) == 2
    assert sorted(item["member_count"] for item in clusters) == [1, 2]


def test_cli_fails_nonacceptance_by_default_and_requires_explicit_dev_override():
    report = {"acceptance_eligible": False, "numerical_registration_passed": True}
    assert tool.report_exit_code(report) == 2
    assert tool.report_exit_code(report, development_numeric_ok=True) == 0
    assert tool.report_exit_code({"acceptance_eligible": True}) == 0


def binding_fixture(tmp_path):
    metadata = tmp_path / "metadata.json"
    xodr = tmp_path / "map.xodr"
    geometry_path = tmp_path / "geometry.json"
    metadata.write_text('{"features":[]}')
    xodr.write_text("<OpenDRIVE/>")
    opendrive = {
        "sha256": tool.sha256(xodr),
        "georeference_sha256": "georef-hash",
    }
    geometry = {"schema": tool.GEOMETRY_SCHEMA, "opendrive_sha256": opendrive["sha256"]}
    geometry_path.write_text(json.dumps(geometry))
    tiles = {"tile": {"sha256": "tile", "validation_sha256": "validation"}}
    annotation = {
        "schema": tool.ANNOTATION_SCHEMA,
        "bindings": {
            "lidar_tiles": [{"lidar_sha256": "tile", "validation_sha256": "validation"}],
            "metadata_sha256": tool.sha256(metadata),
            "opendrive_sha256": opendrive["sha256"],
            "opendrive_georeference_sha256": "georef-hash",
            "geometry_sha256": tool.sha256(geometry_path),
        },
    }
    return annotation, tiles, metadata, opendrive, geometry_path, geometry


def strict_geometry_fixture(tmp_path):
    from PIL import Image
    from types import SimpleNamespace

    tmp_path.mkdir(parents=True, exist_ok=True)
    exporter_path = TOOL_PATH.with_name("export_map_calibration_geometry.py")
    exporter_spec = importlib.util.spec_from_file_location("exporter_fixture", exporter_path)
    exporter = importlib.util.module_from_spec(exporter_spec)
    exporter_spec.loader.exec_module(exporter)
    xodr = tmp_path / "map.xodr"
    xodr.write_text("""<OpenDRIVE><header><geoReference>EPSG:26910</geoReference></header>
<road id="7" length="4"><lanes><laneSection s="0">
<center><lane id="0"><roadMark sOffset="0" type="solid" color="white" width="0.15" laneChange="both"/></lane></center>
<right><lane id="-1"><roadMark sOffset="0" type="solid" color="white" width="0.15" laneChange="both"/></lane></right>
</laneSection></lanes></road></OpenDRIVE>""")
    opendrive = tool.parse_opendrive(xodr)
    cameras_value = {"cameras": [{"id": camera, "value": index} for index, camera in enumerate(tool.CAMERA_IDS)]}
    cameras = tmp_path / "cameras.json"
    cameras.write_text(json.dumps(cameras_value))
    pair_cameras, report_cameras = {}, {}
    for camera in tool.CAMERA_IDS:
        camera_object = next(item for item in cameras_value["cameras"] if item["id"] == camera)
        camera_hash = tool.canonical_hash(camera_object)
        camera_model = {
            "transform": {
                "location": {"x": 1.0, "y": 2.0, "z": 3.0},
                "rotation": {"pitch": -5.0, "yaw": 10.0, "roll": 0.0},
            },
            "image": {"horizontal_fov_deg": 90.0},
        }
        pair_camera, report_camera = {}, {
            "camera_config_sha256": camera_hash,
            "horizontal_fov_deg": 90.0,
            "baseline_source": "retained_twin_actor_metadata",
            "baseline_transform": {
                "location": [1.0, 2.0, 3.0],
                "rotation": [-5.0, 10.0, 0.0],
            },
        }
        for kind, color in (("real", "red"), ("twin", "blue")):
            image_path = tmp_path / f"{camera}-{kind}.jpg"
            Image.new("RGB", (16, 12), color=color).save(image_path)
            frame = {"file": image_path.name, "sha256": tool.sha256(image_path)}
            if kind == "twin":
                frame["camera_config_sha256"] = camera_hash
                frame["camera_model"] = camera_model
            pair_camera[kind] = frame
            overlay_path = tmp_path / f"{camera}-{kind}-overlay.jpg"
            Image.new("RGB", (16, 12), color=color).save(overlay_path)
            report_camera[kind] = {
                "frame_sha256": frame["sha256"], "width": 16, "height": 12,
                "overlay": overlay_path.name,
                "overlay_sha256": tool.sha256(overlay_path),
                "projection": {"lanes": [], "crosswalks": [], "objects": []},
            }
        pair_cameras[camera] = pair_camera
        report_cameras[camera] = report_camera
    pair_value = {
        "schema": "v2x-observational-calibration-pairs/v1",
        "cameras_file_sha256": tool.sha256(cameras),
        "cameras": pair_cameras,
    }
    pair = tmp_path / "pairs.json"
    pair.write_text(json.dumps(pair_value))
    exact_ranges = exporter.opendrive_road_mark_ranges(xodr.read_bytes())
    marking = {"type": "solid", "color": "white", "width_m": 0.15, "lane_change": "both"}
    source_value = {
        "schema": exporter.CARLA_SOURCE_SCHEMA,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "map": "SyntheticMap", "opendrive_sha256": opendrive["sha256"],
        "radius_m": 80.0, "lane_spacing_m": 1.0,
        "anchor_world": [1.0, 2.0, 3.0],
        "crosswalk_polygons": [
            [[8.0, -1.0, 0.0], [9.0, -1.0, 0.0], [9.0, 1.0, 0.0],
             [8.0, 1.0, 0.0], [8.0, -1.0, 0.0]],
        ],
        "lane_waypoint_groups": [{
            "road_id": 7, "section_id": 0, "lane_id": -1,
            "waypoints": [{
                "s_m": float(value), "location": [float(value), 0.0, 0.0],
                "yaw_deg": 0.0, "lane_width_m": 4.0,
                "left_lane_marking": marking, "right_lane_marking": marking,
            } for value in range(5)],
        }],
        "environment_objects": [{
            "source_object_id": "signal-1", "name": "signal",
            "semantic_source": {
                "schema": exporter.NATIVE_OBJECT_SEMANTIC_SCHEMA,
                "api": exporter.NATIVE_OBJECT_API,
                "native_type": "CityObjectLabel.TrafficLight",
                "native_subtype": None,
            },
            "center_world": [5.0, 0.0, 2.0],
            "extent": [0.2, 0.2, 1.0],
        }],
    }
    carla_source = tmp_path / "carla-source-export.json"
    carla_source.write_text(json.dumps(source_value))
    payload = exporter.geometry_from_carla_source(source_value, exact_ranges)
    transform = SimpleNamespace(
        location=SimpleNamespace(x=1.0, y=2.0, z=3.0),
        rotation=SimpleNamespace(pitch=-5.0, yaw=10.0, roll=0.0),
    )
    for camera_id, report_camera in report_cameras.items():
        for kind in ("real", "twin"):
            report_camera[kind]["projection"] = exporter.projected_geometry(
                payload, transform, 90.0, 16, 12
            )
            image_path = tmp_path / pair_cameras[camera_id][kind]["file"]
            overlay_path = tmp_path / report_camera[kind]["overlay"]
            exporter.render_overlay(
                image_path, report_camera[kind]["projection"], overlay_path
            )
            report_camera[kind]["overlay_sha256"] = tool.sha256(overlay_path)
    geometry_value = {
        "schema": tool.GEOMETRY_SCHEMA,
        "map": "SyntheticMap",
        "opendrive_sha256": opendrive["sha256"],
        "pair_manifest_sha256": tool.sha256(pair),
        "cameras_file_sha256": tool.sha256(cameras),
        "carla_source_export_sha256": tool.sha256(carla_source),
        "radius_m": 80.0,
        "lane_spacing_m": 1.0,
        "geometry": payload,
        "cameras": report_cameras,
    }
    geometry_value["geometry_provenance"] = {
        "schema": "v2x-map-geometry-provenance/v1",
        "exporter_sha256": tool.sha256(exporter_path),
        "map": "SyntheticMap",
        "opendrive_sha256": opendrive["sha256"],
        "opendrive_georeference_sha256": opendrive["georeference_sha256"],
        "pair_manifest_sha256": tool.sha256(pair),
        "cameras_file_sha256": tool.sha256(cameras),
        "carla_source_export_sha256": tool.sha256(carla_source),
        "radius_m": 80.0,
        "lane_spacing_m": 1.0,
        "geometry_payload_sha256": tool.canonical_hash(payload),
        "exact_road_mark_ranges_sha256": tool.canonical_hash(exact_ranges),
    }
    geometry = tmp_path / "geometry-strict.json"
    geometry.write_text(json.dumps(geometry_value))
    return geometry, geometry_value, xodr, opendrive, pair, cameras, carla_source


def test_geometry_provenance_recomputes_exporter_pair_frames_and_payload(tmp_path):
    geometry, value, xodr, opendrive, pair, cameras, source = strict_geometry_fixture(tmp_path)
    result = tool.validate_geometry_provenance(
        value, geometry, xodr, opendrive, pair, cameras, source
    )
    assert result["geometry_payload_sha256"] == tool.canonical_hash(value["geometry"])
    assert result["exporter_sha256"] == tool.sha256(TOOL_PATH.with_name("export_map_calibration_geometry.py"))


def test_schema_and_xodr_hash_without_full_geometry_provenance_is_rejected(tmp_path):
    geometry, value, xodr, opendrive, pair, cameras, source = strict_geometry_fixture(tmp_path)
    del value["geometry_provenance"]
    with pytest.raises(tool.RegistrationError, match="no strict exporter provenance"):
        tool.validate_geometry_provenance(
            value, geometry, xodr, opendrive, pair, cameras, source
        )


def test_geometry_payload_or_retained_frame_tamper_is_rejected(tmp_path):
    geometry, value, xodr, opendrive, pair, cameras, source = strict_geometry_fixture(tmp_path)
    value["geometry"]["objects"].append({"id": "fabricated"})
    with pytest.raises(tool.RegistrationError, match="geometry_payload_sha256 mismatch"):
        tool.validate_geometry_provenance(
            value, geometry, xodr, opendrive, pair, cameras, source
        )
    geometry, value, xodr, opendrive, pair, cameras, source = strict_geometry_fixture(
        tmp_path / "frames"
    )
    (pair.parent / "ch1-real.jpg").write_bytes(b"tampered")
    with pytest.raises(tool.RegistrationError, match="source frame hash mismatch"):
        tool.validate_geometry_provenance(
            value, geometry, xodr, opendrive, pair, cameras, source
        )


def test_geometry_camera_projection_is_independently_recomputed(tmp_path):
    geometry, value, xodr, opendrive, pair, cameras, source = strict_geometry_fixture(tmp_path)
    value["cameras"]["ch1"]["real"]["projection"]["objects"].append({"fabricated": True})
    with pytest.raises(tool.RegistrationError, match="projection cannot be reproduced"):
        tool.validate_geometry_provenance(
            value, geometry, xodr, opendrive, pair, cameras, source
        )


def test_geometry_world_coordinates_are_rebuilt_from_retained_carla_source(tmp_path):
    geometry, value, xodr, opendrive, pair, cameras, source = strict_geometry_fixture(tmp_path)
    source_value = json.loads(source.read_text())
    source_value["lane_waypoint_groups"][0]["waypoints"][2]["location"][1] += 1.0
    source.write_text(json.dumps(source_value))
    source_hash = tool.sha256(source)
    value["carla_source_export_sha256"] = source_hash
    value["geometry_provenance"]["carla_source_export_sha256"] = source_hash
    with pytest.raises(tool.RegistrationError, match="does not reproduce retained CARLA/XODR"):
        tool.validate_geometry_provenance(
            value, geometry, xodr, opendrive, pair, cameras, source
        )


def test_geometry_overlay_pixels_are_rerendered_not_trusted_by_hash(tmp_path):
    from PIL import Image

    geometry, value, xodr, opendrive, pair, cameras, source = strict_geometry_fixture(tmp_path)
    report_frame = value["cameras"]["ch1"]["real"]
    overlay = geometry.parent / report_frame["overlay"]
    Image.new("RGB", (16, 12), color="black").save(overlay, quality=94)
    report_frame["overlay_sha256"] = tool.sha256(overlay)
    with pytest.raises(tool.RegistrationError, match="overlay pixels cannot be reproduced"):
        tool.validate_geometry_provenance(
            value, geometry, xodr, opendrive, pair, cameras, source
        )


def test_geometry_road_mark_binding_is_independently_recomputed(tmp_path):
    geometry, value, xodr, opendrive, pair, cameras, source = strict_geometry_fixture(tmp_path)
    value["geometry"]["road_mark_segments"][0]["opendrive_source_lane_id"] = 999
    value["geometry_provenance"]["geometry_payload_sha256"] = tool.canonical_hash(
        value["geometry"]
    )
    with pytest.raises(tool.RegistrationError, match="does not reproduce retained CARLA/XODR"):
        tool.validate_geometry_provenance(
            value, geometry, xodr, opendrive, pair, cameras, source
        )


@pytest.mark.parametrize("binding", [
    "metadata_sha256", "opendrive_sha256", "opendrive_georeference_sha256", "geometry_sha256"
])
def test_every_manual_artifact_hash_is_fail_closed(tmp_path, binding):
    annotation, tiles, metadata, opendrive, geometry_path, geometry = binding_fixture(tmp_path)
    annotation["bindings"][binding] = "wrong"
    with pytest.raises(tool.RegistrationError, match="mismatch"):
        tool.verify_artifact_bindings(annotation, tiles, metadata, opendrive, geometry_path, geometry)


def test_raw_lidar_and_validation_hash_pair_is_fail_closed(tmp_path):
    annotation, tiles, metadata, opendrive, geometry_path, geometry = binding_fixture(tmp_path)
    annotation["bindings"]["lidar_tiles"][0]["validation_sha256"] = "wrong"
    with pytest.raises(tool.RegistrationError, match="raw/validation"):
        tool.verify_artifact_bindings(annotation, tiles, metadata, opendrive, geometry_path, geometry)


def test_old_geometry_cannot_be_combined_with_live_opendrive(tmp_path):
    annotation, tiles, metadata, opendrive, geometry_path, geometry = binding_fixture(tmp_path)
    geometry["opendrive_sha256"] = "old-map-hash"
    with pytest.raises(tool.RegistrationError, match="different OpenDRIVE"):
        tool.verify_artifact_bindings(annotation, tiles, metadata, opendrive, geometry_path, geometry)


def write_las_and_validation(tmp_path, scales=(0.01, 0.01, 0.01), crs_epsg=26910,
                             vertical_epsg=5703):
    import laspy
    from pyproj import CRS

    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "control.las"
    cloud = laspy.create(point_format=6, file_version="1.4")
    cloud.header.scales = np.asarray(scales)
    cloud.header.offsets = np.asarray([500000.0, 4200000.0, 0.0])
    cloud.header.add_crs(CRS.from_user_input(f"EPSG:{crs_epsg}+{vertical_epsg}"))
    cloud.x = [500000.0, 500001.0, 500002.0]
    cloud.y = [4200000.0, 4200001.0, 4200002.0]
    cloud.z = [10.0, 10.5, 11.0]
    cloud.write(path)
    decoded = laspy.read(path)
    points = np.column_stack((decoded.x, decoded.y, decoded.z))
    validation = tmp_path / "validation.json"
    validation.write_text(json.dumps({
        "bytes": path.stat().st_size,
        "points": len(points),
        "mins": np.min(points, axis=0).tolist(),
        "maxs": np.max(points, axis=0).tolist(),
        "crs": decoded.header.parse_crs().to_wkt(),
        "sha256": tool.sha256(path),
    }))
    return path, validation


def write_lidar_validation_attestation(tmp_path, lidar_path, validation_path):
    import laspy

    validation = json.loads(validation_path.read_text())
    crs_wkt = laspy.read(lidar_path).header.parse_crs().to_wkt()
    now = datetime.now(timezone.utc)
    value = {
        "schema": tool.LIDAR_VALIDATION_AUTHORITY_ATTESTATION_SCHEMA,
        "signing_key_id": LIDAR_AUTHORITY_KEY_ID,
        "public_key_sha256": tool.hashlib.sha256(
            public_key_pem(LIDAR_AUTHORITY_PRIVATE_KEY)
        ).hexdigest(),
        "producer": "Independent LiDAR Data Authority",
        "source": "lidar-validation-registry-test-fixture",
        "verified_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(days=30)).isoformat(),
        "lidar_sha256": tool.sha256(lidar_path),
        "lidar_bytes": lidar_path.stat().st_size,
        "validation_sha256": tool.sha256(validation_path),
        "validation_payload_sha256": tool.canonical_hash(validation),
        "point_count": validation["points"],
        "crs_wkt_sha256": tool.hashlib.sha256(crs_wkt.encode()).hexdigest(),
        "bounds_sha256": tool.canonical_hash({
            "mins": validation["mins"], "maxs": validation["maxs"],
        }),
        "verification_result": {
            "source_data_status": "authoritative",
            "validation_status": "verified",
        },
    }
    return write_detached_attestation(
        tmp_path / "lidar-validation-authority.json", value,
        LIDAR_AUTHORITY_PRIVATE_KEY,
    )


def write_vertical_reconciliation_bundle(tmp_path, opendrive, tile):
    now = datetime.now(timezone.utc)
    source_artifact = tmp_path / "vertical-source-reference.txt"
    source_artifact.write_bytes(
        b"Independent retained engineering vertical datum record\n" * 128
    )
    source_reference = {
        "datum": "Authenticated Richmond engineering datum",
        "coordinate_epoch": 2026.5,
        "linear_units": "metre",
        "source_artifact_sha256": tool.sha256(source_artifact),
        "source_artifact_file_name": source_artifact.name,
        "source_artifact_bytes": source_artifact.stat().st_size,
    }
    target_reference = {
        "epsg": tile["vertical_epsg"], "datum": tile["vertical_datum"],
        "wkt_sha256": tool.hashlib.sha256(tile["vertical_crs_wkt"].encode()).hexdigest(),
        "coordinate_epoch": tile["vertical_coordinate_epoch"],
        "linear_units": "metre",
    }
    operation = {"method": "constant_offset", "offset_m": 5.0}
    check_points = [{
        "id": f"vertical-control-{index}",
        "physical_control_id": f"vertical-monument-{index}",
        "provenance": "independent_authority_control",
        "source_z_m": float(index), "target_z_m": float(index + 5),
        "vertical_uncertainty_m": 0.02,
    } for index in range(6)]
    artifact_value = {
        "schema": tool.VERTICAL_DATUM_RECONCILIATION_SCHEMA,
        "opendrive_sha256": opendrive["sha256"],
        "lidar_vertical_epsg": tile["vertical_epsg"],
        "lidar_vertical_datum": tile["vertical_datum"],
        "lidar_vertical_crs_wkt_sha256": tool.hashlib.sha256(
            tile["vertical_crs_wkt"].encode()
        ).hexdigest(),
        "source_vertical_reference": source_reference,
        "target_vertical_reference": target_reference,
        "operation": operation,
        "authority": "Independent Vertical Datum Authority",
        "operation_id": "richmond-vertical-operation-test",
        "check_points": check_points,
    }
    artifact_path = tmp_path / "vertical-datum-reconciliation.json"
    artifact_path.write_text(json.dumps(artifact_value))
    attestation_value = {
        "schema": tool.VERTICAL_DATUM_AUTHORITY_ATTESTATION_SCHEMA,
        "signing_key_id": VERTICAL_AUTHORITY_KEY_ID,
        "public_key_sha256": tool.hashlib.sha256(
            public_key_pem(VERTICAL_AUTHORITY_PRIVATE_KEY)
        ).hexdigest(),
        "producer": "Independent Vertical Datum Authority",
        "source": "vertical-operation-registry-test-fixture",
        "verified_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(days=30)).isoformat(),
        "reconciliation_artifact_sha256": tool.sha256(artifact_path),
        "opendrive_sha256": opendrive["sha256"],
        "lidar_vertical_crs_wkt_sha256": artifact_value[
            "lidar_vertical_crs_wkt_sha256"
        ],
        "source_vertical_reference_sha256": tool.canonical_hash(source_reference),
        "target_vertical_reference_sha256": tool.canonical_hash(target_reference),
        "operation_sha256": tool.canonical_hash(operation),
        "authority": artifact_value["authority"],
        "operation_id": artifact_value["operation_id"],
        "check_points_sha256": tool.canonical_hash(check_points),
        "verification_result": {
            "vertical_source_status": "official",
            "operation_status": "authorized",
            "control_source_status": "independent",
        },
    }
    attestation_path, signature_path = write_detached_attestation(
        tmp_path / "vertical-datum-authority.json", attestation_value,
        VERTICAL_AUTHORITY_PRIVATE_KEY,
    )
    return artifact_path, attestation_path, signature_path, source_artifact


def test_lidar_validation_requires_independently_signed_authority_for_acceptance(tmp_path):
    path, validation = write_las_and_validation(tmp_path)
    unsigned = tool.load_lidar_tile(path, validation)
    assert unsigned["validation_authority_attestation"] is None
    attestation, signature = write_lidar_validation_attestation(
        tmp_path, path, validation
    )
    signed = tool.load_lidar_tile(path, validation, attestation, signature)
    assert signed["validation_authority_attestation"]["signing_key_id"] == LIDAR_AUTHORITY_KEY_ID


def test_lidar_validation_authority_signature_tamper_fails(tmp_path):
    path, validation = write_las_and_validation(tmp_path)
    attestation, signature = write_lidar_validation_attestation(
        tmp_path, path, validation
    )
    signature.write_bytes(b"\x00" * 64)
    with pytest.raises(tool.RegistrationError, match="detached signature"):
        tool.load_lidar_tile(path, validation, attestation, signature)


def test_vertical_datum_reconciliation_missing_stays_ineligible(tmp_path):
    path, validation = write_las_and_validation(tmp_path)
    tile = tool.load_lidar_tile(path, validation)
    xodr = tmp_path / "map.xodr"
    xodr.write_text(
        "<OpenDRIVE><header><geoReference>EPSG:26910</geoReference></header></OpenDRIVE>"
    )
    result = tool.validate_vertical_datum_reconciliation(
        tool.parse_opendrive(xodr), tile, {"present": False}
    )
    assert result["passed"] is False
    assert result["reasons"] == ["vertical_datum_reconciliation_missing"]


def test_signed_vertical_datum_reconciliation_recomputes_independent_controls(tmp_path):
    path, validation = write_las_and_validation(tmp_path)
    tile = tool.load_lidar_tile(path, validation)
    xodr = tmp_path / "map.xodr"
    xodr.write_text(
        "<OpenDRIVE><header><geoReference>EPSG:26910</geoReference></header></OpenDRIVE>"
    )
    opendrive = tool.parse_opendrive(xodr)
    artifact, attestation, signature, source_artifact = write_vertical_reconciliation_bundle(
        tmp_path, opendrive, tile
    )
    result = tool.validate_vertical_datum_reconciliation(
        opendrive, tile, {"present": False}, artifact, attestation, signature,
        source_artifact,
    )
    assert result["passed"] is True
    assert result["authenticated_offset_m"] == 5.0
    assert result["recomputed_vertical_max_m"] == 0.0


def test_self_declared_vertical_offset_without_pinned_signature_fails(tmp_path):
    path, validation = write_las_and_validation(tmp_path)
    tile = tool.load_lidar_tile(path, validation)
    xodr = tmp_path / "map.xodr"
    xodr.write_text(
        "<OpenDRIVE><header><geoReference>EPSG:26910</geoReference></header></OpenDRIVE>"
    )
    opendrive = tool.parse_opendrive(xodr)
    artifact, _, _, source_artifact = write_vertical_reconciliation_bundle(
        tmp_path, opendrive, tile
    )
    with pytest.raises(tool.RegistrationError, match="vertical datum authority detached"):
        tool.validate_vertical_datum_reconciliation(
            opendrive, tile, {"present": False}, artifact,
            source_artifact_path=source_artifact,
        )


def vertical_reconciliation_fixture(tmp_path):
    path, validation = write_las_and_validation(tmp_path)
    tile = tool.load_lidar_tile(path, validation)
    xodr = tmp_path / "map.xodr"
    xodr.write_text(
        "<OpenDRIVE><header><geoReference>EPSG:26910</geoReference></header></OpenDRIVE>"
    )
    opendrive = tool.parse_opendrive(xodr)
    return (
        opendrive, tile,
        *write_vertical_reconciliation_bundle(tmp_path, opendrive, tile),
    )


def test_vertical_source_artifact_is_required_and_exactly_hash_bound(tmp_path):
    opendrive, tile, artifact, attestation, signature, source = (
        vertical_reconciliation_fixture(tmp_path)
    )
    with pytest.raises(tool.RegistrationError, match="source artifact is required"):
        tool.validate_vertical_datum_reconciliation(
            opendrive, tile, {"present": False}, artifact, attestation, signature
        )
    source.write_bytes(source.read_bytes() + b"tampered")
    with pytest.raises(tool.RegistrationError, match="does not match signed reference"):
        tool.validate_vertical_datum_reconciliation(
            opendrive, tile, {"present": False}, artifact, attestation, signature,
            source,
        )


def test_vertical_source_artifact_substitution_and_symlink_fail_closed(tmp_path):
    opendrive, tile, artifact, attestation, signature, source = (
        vertical_reconciliation_fixture(tmp_path)
    )
    substitute_dir = tmp_path / "substitute"
    substitute_dir.mkdir()
    substitute = substitute_dir / source.name
    substitute.write_bytes(b"different retained source" * 128)
    with pytest.raises(tool.RegistrationError, match="does not match signed reference"):
        tool.validate_vertical_datum_reconciliation(
            opendrive, tile, {"present": False}, artifact, attestation, signature,
            substitute,
        )
    symlink = tmp_path / "vertical-source-link.txt"
    symlink.symlink_to(source)
    with pytest.raises(tool.RegistrationError, match="safely opened"):
        tool.validate_vertical_datum_reconciliation(
            opendrive, tile, {"present": False}, artifact, attestation, signature,
            symlink,
        )


def test_vertical_source_artifact_hardlink_empty_and_oversize_fail_closed(tmp_path):
    opendrive, tile, artifact, attestation, signature, source = (
        vertical_reconciliation_fixture(tmp_path)
    )
    hardlink = tmp_path / "hardlink.txt"
    hardlink.hardlink_to(source)
    with pytest.raises(tool.RegistrationError, match="single-link regular file"):
        tool.validate_vertical_datum_reconciliation(
            opendrive, tile, {"present": False}, artifact, attestation, signature,
            source,
        )
    hardlink.unlink()
    empty = tmp_path / "empty.txt"
    empty.touch()
    with pytest.raises(tool.RegistrationError, match="single-link regular file"):
        tool.read_retained_vertical_source_artifact(empty)
    oversize = tmp_path / "oversize.txt"
    with oversize.open("wb") as stream:
        stream.truncate(tool.MAX_VERTICAL_SOURCE_ARTIFACT_BYTES + 1)
    with pytest.raises(tool.RegistrationError, match="single-link regular file"):
        tool.read_retained_vertical_source_artifact(oversize)


def test_vertical_source_artifact_concurrent_path_replacement_is_rejected(
    tmp_path, monkeypatch
):
    opendrive, tile, artifact, attestation, signature, source = (
        vertical_reconciliation_fixture(tmp_path)
    )
    original_reader = tool._read_all_from_fd

    def replace_after_read(file_descriptor, expected_bytes):
        content = original_reader(file_descriptor, expected_bytes)
        replacement = source.with_suffix(".replacement")
        replacement.write_bytes(content)
        replacement.replace(source)
        return content

    monkeypatch.setattr(tool, "_read_all_from_fd", replace_after_read)
    with pytest.raises(tool.RegistrationError, match="changed while being read"):
        tool.validate_vertical_datum_reconciliation(
            opendrive, tile, {"present": False}, artifact, attestation, signature,
            source,
        )


def test_raw_las_crs_mismatch_is_rejected(tmp_path):
    path, validation = write_las_and_validation(tmp_path)
    value = json.loads(validation.read_text())
    value["crs"] = "EPSG:4326"
    validation.write_text(json.dumps(value))
    with pytest.raises(tool.RegistrationError, match="validation CRS mismatch"):
        tool.load_lidar_tile(path, validation)


def test_coarse_raw_las_resolution_is_rejected(tmp_path):
    path, validation = write_las_and_validation(tmp_path, scales=(0.1, 0.1, 0.1))
    with pytest.raises(tool.RegistrationError, match="quantization is too coarse"):
        tool.load_lidar_tile(path, validation)


def test_non_metre_horizontal_and_vertical_las_crs_are_rejected(tmp_path):
    horizontal_path, horizontal_validation = write_las_and_validation(
        tmp_path / "horizontal", crs_epsg=2227, vertical_epsg=5703
    )
    with pytest.raises(tool.RegistrationError, match="horizontal CRS coordinate axes are not metres"):
        tool.load_lidar_tile(horizontal_path, horizontal_validation)
    vertical_path, vertical_validation = write_las_and_validation(
        tmp_path / "vertical", crs_epsg=26910, vertical_epsg=6360
    )
    with pytest.raises(tool.RegistrationError, match="vertical CRS coordinate axes are not metres"):
        tool.load_lidar_tile(vertical_path, vertical_validation)


def test_opendrive_georeference_must_be_projected_metres(tmp_path):
    path = tmp_path / "feet.xodr"
    path.write_text("<OpenDRIVE><header><geoReference>EPSG:2227</geoReference></header></OpenDRIVE>")
    with pytest.raises(tool.RegistrationError, match="not metres"):
        tool.parse_opendrive(path)


def test_epsg3857_opendrive_cannot_silently_mix_with_epsg26910_lidar(tmp_path):
    from pyproj import CRS

    path = tmp_path / "web-mercator.xodr"
    path.write_text(
        "<OpenDRIVE><header><geoReference>EPSG:3857</geoReference></header></OpenDRIVE>"
    )
    opendrive = tool.parse_opendrive(path)
    lidar_crs = CRS.from_epsg(26910)
    lidar = {
        "horizontal_crs_wkt": lidar_crs.to_wkt(), "horizontal_epsg": 26910,
        "horizontal_datum": lidar_crs.datum.name,
        "horizontal_coordinate_epoch": None,
    }
    survey = {"present": False, "passed": False, "reasons": []}
    with pytest.raises(tool.RegistrationError, match="differ without reconciliation"):
        tool.validate_crs_reconciliation(opendrive, lidar, survey)


def test_crs_reconciliation_pipeline_hash_and_licensed_source_are_fail_closed(tmp_path):
    from pyproj import CRS

    path = tmp_path / "web-mercator.xodr"
    path.write_text(
        "<OpenDRIVE><header><geoReference>EPSG:3857</geoReference></header></OpenDRIVE>"
    )
    opendrive = tool.parse_opendrive(path)
    lidar_crs = CRS.from_epsg(26910)
    lidar = {
        "horizontal_crs_wkt": lidar_crs.to_wkt(), "horizontal_epsg": 26910,
        "horizontal_datum": lidar_crs.datum.name,
        "horizontal_coordinate_epoch": None,
    }
    survey = {
        "present": True, "crs": {
            "wkt": lidar_crs.to_wkt(), "datum": lidar_crs.datum.name,
            "coordinate_epoch": None,
        },
        "raw_deliverable_evidence": {
            "deliverables": [{"sha256": "licensed-deliverable"}],
        },
    }
    artifact = tmp_path / "crs-reconciliation.json"
    artifact.write_text(json.dumps({
        "schema": "v2x-crs-reconciliation/v1",
        "proj_pipeline": "+proj=noop", "proj_pipeline_sha256": "fabricated",
        "source_crs_wkt_sha256": tool.hashlib.sha256(
            opendrive["georeference_wkt"].encode()
        ).hexdigest(),
        "target_crs_wkt_sha256": tool.hashlib.sha256(
            lidar["horizontal_crs_wkt"].encode()
        ).hexdigest(),
        "source_coordinate_epoch": None, "target_coordinate_epoch": None,
        "authority": "licensed authority", "operation_id": "operation-1",
        "source_deliverable_sha256": "licensed-deliverable", "check_points": [],
    }))
    with pytest.raises(tool.RegistrationError, match="pipeline/source/target binding"):
        tool.validate_crs_reconciliation(opendrive, lidar, survey, artifact)


def test_self_declared_arbitrary_crs_pipeline_without_pinned_signature_fails(tmp_path):
    from pyproj import CRS

    xodr = tmp_path / "self-declared.xodr"
    xodr.write_text(
        "<OpenDRIVE><header><geoReference>EPSG:3857</geoReference></header></OpenDRIVE>"
    )
    opendrive = tool.parse_opendrive(xodr)
    target_crs = CRS.from_epsg(26910)
    lidar = {
        "horizontal_crs_wkt": target_crs.to_wkt(), "horizontal_epsg": 26910,
        "horizontal_datum": target_crs.datum.name,
        "horizontal_coordinate_epoch": None,
    }
    pipeline = "+proj=pipeline +step +proj=affine +xoff=1 +yoff=2"
    controls = [{
        "id": f"caller-{index}", "physical_control_id": f"caller-physical-{index}",
        "provenance": "independent_authority_control",
        "source_xy": point, "target_xy": [point[0] + 1, point[1] + 2],
    } for index, point in enumerate((
        [0, 0], [10, 0], [0, 10], [10, 10], [5, 15], [15, 5]
    ))]
    artifact = tmp_path / "self-declared-reconciliation.json"
    artifact.write_text(json.dumps({
        "schema": "v2x-crs-reconciliation/v1", "proj_pipeline": pipeline,
        "proj_pipeline_sha256": tool.hashlib.sha256(pipeline.encode()).hexdigest(),
        "source_crs_wkt_sha256": tool.hashlib.sha256(
            opendrive["georeference_wkt"].encode()
        ).hexdigest(),
        "target_crs_wkt_sha256": tool.hashlib.sha256(
            lidar["horizontal_crs_wkt"].encode()
        ).hexdigest(),
        "source_coordinate_epoch": None, "target_coordinate_epoch": None,
        "authority": "caller says official", "operation_id": "arbitrary-affine",
        "check_points": controls,
    }))
    with pytest.raises(tool.RegistrationError, match="CRS authority detached"):
        tool.validate_crs_reconciliation(
            opendrive, lidar, {"present": False}, artifact
        )


def test_signed_crs_pipeline_recomputes_independent_authority_check_points(tmp_path):
    from pyproj import CRS

    _, geometry, _, _, _ = synthetic_evidence()
    survey_path, deliverables, observations = write_current_survey(tmp_path, geometry)
    survey = validate_written_survey(
        survey_path, geometry, deliverables, observations
    )
    xodr = tmp_path / "different-crs.xodr"
    xodr.write_text(
        "<OpenDRIVE><header><geoReference>EPSG:3857</geoReference></header></OpenDRIVE>"
    )
    opendrive = tool.parse_opendrive(xodr)
    lidar_crs = CRS.from_epsg(26910)
    lidar = {
        "horizontal_crs_wkt": lidar_crs.to_wkt(), "horizontal_epsg": 26910,
        "horizontal_datum": lidar_crs.datum.name,
        "horizontal_coordinate_epoch": None,
    }
    cosine, sine = math.cos(math.radians(2.0)), math.sin(math.radians(2.0))
    pipeline = (
        "+proj=pipeline +step +proj=affine +xoff=100 +yoff=-50 "
        f"+s11={cosine} +s12={-sine} +s21={sine} +s22={cosine}"
    )
    source_points = np.asarray([
        [100.0, 100.0], [110.0, 100.0], [100.0, 110.0],
        [110.0, 110.0], [105.0, 120.0], [120.0, 105.0],
    ])
    target_points = apply_transform(
        np.column_stack((source_points, np.zeros(len(source_points))))
    )[:, :2]
    artifact = tmp_path / "crs-reconciliation-valid.json"
    artifact.write_text(json.dumps({
        "schema": "v2x-crs-reconciliation/v1", "proj_pipeline": pipeline,
        "proj_pipeline_sha256": tool.hashlib.sha256(pipeline.encode()).hexdigest(),
        "source_crs_wkt_sha256": tool.hashlib.sha256(
            opendrive["georeference_wkt"].encode()
        ).hexdigest(),
        "target_crs_wkt_sha256": tool.hashlib.sha256(
            lidar["horizontal_crs_wkt"].encode()
        ).hexdigest(),
        "source_coordinate_epoch": None, "target_coordinate_epoch": None,
        "authority": "Independent Geodetic Authority", "operation_id": "site-grid-2026",
        "check_points": [{
            "id": f"crs-control-{index}",
            "physical_control_id": f"crs-monument-{index}",
            "provenance": "independent_authority_control",
            "source_xy": source.tolist(), "target_xy": target.tolist(),
        } for index, (source, target) in enumerate(zip(source_points, target_points))],
    }))
    authority_attestation, authority_signature = write_crs_authority_attestation(
        tmp_path, artifact
    )
    result = tool.validate_crs_reconciliation(
        opendrive, lidar, survey, artifact,
        authority_attestation, authority_signature,
    )
    assert result["passed"] is True
    assert result["method"] == "signed_authority_proj_pipeline"
    assert result["check_point_count"] == 6
    assert result["recomputed_horizontal_max_m"] < 1e-8
    assert result["authority_attestation_evidence"]["signing_key_id"] == CRS_AUTHORITY_KEY_ID


def test_signed_crs_pipeline_cannot_reuse_renamed_survey_controls(tmp_path):
    from pyproj import CRS

    _, geometry, _, _, _ = synthetic_evidence()
    survey_path, deliverables, observations = write_current_survey(tmp_path, geometry)
    survey = validate_written_survey(
        survey_path, geometry, deliverables, observations
    )
    xodr = tmp_path / "disjoint-required.xodr"
    xodr.write_text(
        "<OpenDRIVE><header><geoReference>EPSG:3857</geoReference></header></OpenDRIVE>"
    )
    opendrive = tool.parse_opendrive(xodr)
    target_crs = CRS.from_epsg(26910)
    lidar = {
        "horizontal_crs_wkt": target_crs.to_wkt(), "horizontal_epsg": 26910,
        "horizontal_datum": target_crs.datum.name,
        "horizontal_coordinate_epoch": None,
    }
    cosine, sine = math.cos(math.radians(2.0)), math.sin(math.radians(2.0))
    pipeline = (
        "+proj=pipeline +step +proj=affine +xoff=100 +yoff=-50 "
        f"+s11={cosine} +s12={-sine} +s21={sine} +s22={cosine}"
    )
    artifact = tmp_path / "reused-survey-controls.json"
    artifact.write_text(json.dumps({
        "schema": "v2x-crs-reconciliation/v1", "proj_pipeline": pipeline,
        "proj_pipeline_sha256": tool.hashlib.sha256(pipeline.encode()).hexdigest(),
        "source_crs_wkt_sha256": tool.hashlib.sha256(
            opendrive["georeference_wkt"].encode()
        ).hexdigest(),
        "target_crs_wkt_sha256": tool.hashlib.sha256(
            lidar["horizontal_crs_wkt"].encode()
        ).hexdigest(),
        "source_coordinate_epoch": None, "target_coordinate_epoch": None,
        "authority": "Independent Geodetic Authority", "operation_id": "reused-controls",
        "check_points": [{
            "id": f"renamed-crs-{index}",
            "physical_control_id": f"renamed-crs-physical-{index}",
            "provenance": "independent_authority_control",
            "source_xy": item["map_xy"], "target_xy": item["survey_xy"],
        } for index, item in enumerate(survey["raw_control_coordinates"])],
    }))
    attestation, signature = write_crs_authority_attestation(tmp_path, artifact)
    with pytest.raises(tool.RegistrationError, match="not independent from survey"):
        tool.validate_crs_reconciliation(
            opendrive, lidar, survey, artifact, attestation, signature
        )


def test_deployment_output_requires_current_survey(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    survey = tool.validate_current_survey(None, geometry, "geometry", "opendrive", 26910)
    assert survey == {
        "present": False, "passed": False,
        "reasons": ["current_horizontal_survey_missing"],
    }
    report = {
        "deployment_eligible": True,
        "model": {"transform": {"tx_m": 1, "ty_m": 2, "yaw_deg": 3, "z_bias_m": 4}},
    }
    with pytest.raises(tool.RegistrationError, match="without licensed recomputed survey evidence"):
        tool.write_registration_outputs(
            report, survey, tmp_path / "report.json", tmp_path / "deployment.json"
        )
    assert (tmp_path / "report.json").exists()
    assert not (tmp_path / "deployment.json").exists()


def test_2018_ql2_cannot_emit_deployment_even_with_current_survey(tmp_path):
    report = run_synthetic()
    report["evidence"].update({
        "crs_reconciliation": {"passed": True},
        "toolchain": {"passed": True},
        "annotation_review": {"passed": True},
        "holdout_evaluation": {"passed": True, "burned": True},
        "annotation_authority_attestation": {"signing_key_id": "pinned"},
        "vertical_datum_reconciliation": {"passed": True},
        "lidar_tiles": [{
            "validation_authority_attestation": {"signing_key_id": "pinned"},
        }],
    })
    survey = {
        "passed": True, "raw_controls_recomputed": True, "stable_landmark_count": 14,
        "licensed_source": {"provider": "licensed"},
        "raw_deliverable_evidence": {"deliverables": [{"sha256": "bound"}]},
        "authority_attestation_evidence": {"signing_key_id": "pinned"},
        "reasons": [],
    }
    with pytest.raises(tool.RegistrationError, match="strict registration gates"):
        tool.write_registration_outputs(
            report, survey, tmp_path / "report.json", tmp_path / "deployment.json"
        )
    assert (tmp_path / "report.json").exists()
    assert not (tmp_path / "deployment.json").exists()


def test_summary_only_current_survey_cannot_pass(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    path = tmp_path / "survey.json"
    path.write_text(json.dumps({
        "schema": tool.SURVEY_SCHEMA,
        "geometry_sha256": "geometry",
        "opendrive_sha256": "opendrive",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "control_point_count": 6,
        "independent_holdout_count": 3,
        "horizontal_rmse_m": 0.0,
        "horizontal_max_m": 0.1,
    }))
    survey = tool.validate_current_survey(path, geometry, "geometry", "opendrive", 26910)
    assert survey["passed"] is False
    assert "current_horizontal_survey_licensed_deliverables" in survey["reasons"]


def test_current_survey_recomputes_raw_fit_and_holdout_controls(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    path, deliverables, observations = write_current_survey(
        tmp_path, geometry, corrupt_summary=True
    )
    survey = validate_written_survey(path, geometry, deliverables, observations)
    assert survey["passed"] is True
    assert survey["raw_controls_recomputed"] is True
    assert survey["raw_control_count"] == 14
    assert survey["fit_nonzero_pairwise_distance_count"] >= 10
    assert survey["recomputed_fit_metrics"]["horizontal_rmse_m"] < 1e-8
    assert survey["recomputed_holdout_metrics"]["horizontal_max_m"] < 1e-8
    assert survey["recomputed_transform"]["tx_m"] == pytest.approx(100.0)
    assert survey["authority_attestation_evidence"]["signing_key_id"] == SURVEY_AUTHORITY_KEY_ID


def test_self_declared_pdf_and_identity_strings_without_trusted_attestation_fail(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    survey = tool.validate_current_survey(
        path, geometry, "geometry", "opendrive", 26910,
        deliverables, observations,
    )
    assert survey["passed"] is False
    assert survey["raw_deliverable_evidence"] is not None
    assert survey["licensed_source"]["surveyor_license"] == "PLS-12345"
    assert "current_horizontal_survey_authority_attestation" in survey["reasons"]


def test_tiny_fake_pdf_is_rejected_even_when_caller_rebinds_its_hash(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    license_path = next(item for item in deliverables if item.name == "surveyor-license.pdf")
    license_path.write_bytes(b"%PDF-1.7\ncaller says active PLS-12345\n%%EOF\n")
    value = json.loads(path.read_text())
    digest = tool.sha256(license_path)
    declaration = next(
        item for item in value["raw_deliverables"] if item["role"] == "survey_license"
    )
    declaration.update({"sha256": digest, "bytes": license_path.stat().st_size})
    value["licensed_source"]["survey_license_deliverable_sha256"] = digest
    path.write_text(json.dumps(value))
    survey = tool.validate_current_survey(
        path, geometry, "geometry", "opendrive", 26910,
        deliverables, observations,
    )
    assert survey["passed"] is False
    assert "current_horizontal_survey_licensed_deliverables" in survey["reasons"]


def test_large_padded_fake_pdf_is_rejected_even_with_rebound_hash(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    license_path = next(item for item in deliverables if item.name == "surveyor-license.pdf")
    license_path.write_bytes(
        b"%PDF-1.7\ncaller text without objects or xref\n%"
        + b"X" * 8192 + b"\n%%EOF\n"
    )
    rebind_survey_deliverable(path, license_path, "survey_license")
    survey = tool.validate_current_survey(
        path, geometry, "geometry", "opendrive", 26910,
        deliverables, observations,
    )
    assert survey["passed"] is False
    assert "current_horizontal_survey_licensed_deliverables" in survey["reasons"]


def test_oversize_survey_deliverable_is_rejected_before_read(tmp_path, monkeypatch):
    _, geometry, _, _, _ = synthetic_evidence()
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    license_path = next(item for item in deliverables if item.name == "surveyor-license.pdf")
    with license_path.open("r+b") as stream:
        stream.truncate(tool.MAX_SURVEY_DELIVERABLE_BYTES + 1)
    actual_read_bytes = Path.read_bytes

    def guarded_read_bytes(value):
        if value.resolve() == license_path.resolve():
            raise AssertionError("oversize deliverable was read before its bound")
        return actual_read_bytes(value)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    survey = tool.validate_current_survey(
        path, geometry, "geometry", "opendrive", 26910,
        deliverables, observations,
    )
    assert survey["passed"] is False
    assert "current_horizontal_survey_licensed_deliverables" in survey["reasons"]


@pytest.mark.parametrize("failure", ["truncated", "encrypted", "polyglot"])
def test_structurally_unsafe_pdf_variants_fail_closed(tmp_path, failure):
    _, geometry, _, _, _ = synthetic_evidence()
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    license_path = next(item for item in deliverables if item.name == "surveyor-license.pdf")
    if failure == "truncated":
        license_path.write_bytes(license_path.read_bytes()[:-32])
    elif failure == "encrypted":
        write_valid_pdf(license_path, "Encrypted license", encrypted=True)
    else:
        license_path.write_bytes(
            license_path.read_bytes() + b"\nPK\x03\x04polyglot-archive-after-eof"
        )
    rebind_survey_deliverable(path, license_path, "survey_license")
    survey = tool.validate_current_survey(
        path, geometry, "geometry", "opendrive", 26910,
        deliverables, observations,
    )
    assert survey["passed"] is False
    assert "current_horizontal_survey_licensed_deliverables" in survey["reasons"]


def test_strict_pdf_parser_warning_is_rejected(tmp_path, monkeypatch):
    import pypdf

    pdf = tmp_path / "valid-but-warning.pdf"
    write_valid_pdf(pdf, "Parser warning coverage")
    actual_reader = pypdf.PdfReader

    def warning_reader(*args, **kwargs):
        reader = actual_reader(*args, **kwargs)
        logging.getLogger("pypdf").warning("synthetic strict-parser warning")
        return reader

    monkeypatch.setattr(pypdf, "PdfReader", warning_reader)
    with pytest.raises(tool.RegistrationError, match="parser warnings"):
        tool._strict_pdf_evidence(pdf.read_bytes(), "warning fixture")


def test_zip_archive_inserted_before_pdf_xref_is_rejected(tmp_path):
    from pypdf import PdfReader

    pdf = tmp_path / "before-xref-polyglot.pdf"
    write_valid_pdf(pdf, "Before-xref polyglot regression")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as writer:
        writer.writestr("conflicting-authority.txt", b"different authority bytes")
    # The newline keeps the shifted xref at a legal PDF token boundary while
    # remaining valid trailing data to ZIP's end-of-central-directory record.
    content = insert_before_final_pdf_xref(pdf.read_bytes(), archive.getvalue() + b"\n")
    pdf.write_bytes(content)

    assert content.startswith(b"%PDF-")
    assert content.rstrip().endswith(b"%%EOF")
    assert len(PdfReader(io.BytesIO(content), strict=True).pages) == 1
    assert zipfile.is_zipfile(io.BytesIO(content))
    with pytest.raises(tool.RegistrationError, match="foreign|polyglot|archive"):
        tool._strict_pdf_evidence(content, "before-xref polyglot")


def test_foreign_bytes_ending_in_fake_endobj_before_pdf_xref_are_rejected(tmp_path):
    from pypdf import PdfReader

    pdf = tmp_path / "fake-endobj-boundary.pdf"
    write_valid_pdf(pdf, "Fake endobj boundary regression")
    content = insert_before_final_pdf_xref(
        pdf.read_bytes(), b"\x00foreign-payload\xffendobj\n"
    )

    assert len(PdfReader(io.BytesIO(content), strict=True).pages) == 1
    assert not zipfile.is_zipfile(io.BytesIO(content))
    with pytest.raises(tool.RegistrationError, match="foreign|polyglot"):
        tool._strict_pdf_evidence(content, "fake endobj boundary")


def test_foreign_bytes_between_pdf_trailer_and_startxref_are_rejected(tmp_path):
    from pypdf import PdfReader

    pdf = tmp_path / "after-trailer-payload.pdf"
    write_valid_pdf(pdf, "After trailer boundary regression")
    content = insert_before_final_pdf_startxref(
        pdf.read_bytes(), b"\x00foreign-payload\xffendobj\n"
    )

    assert len(PdfReader(io.BytesIO(content), strict=True).pages) == 1
    assert not zipfile.is_zipfile(io.BytesIO(content))
    with pytest.raises(tool.RegistrationError, match="foreign|polyglot"):
        tool._strict_pdf_evidence(content, "after trailer boundary")


def test_ordinary_pdf_and_ascii_boundary_comments_remain_valid(tmp_path):
    ordinary = tmp_path / "ordinary.pdf"
    write_valid_pdf(ordinary, "Ordinary retained authority PDF")
    ordinary_result = tool._strict_pdf_evidence(ordinary.read_bytes(), "ordinary PDF")
    assert ordinary_result["page_count"] == 1
    assert ordinary_result["encrypted"] is False

    commented = tmp_path / "ordinary-commented.pdf"
    commented.write_bytes(insert_before_final_pdf_xref(
        ordinary.read_bytes(), b"\n% ordinary printable retained-evidence comment\n"
    ))
    commented_result = tool._strict_pdf_evidence(commented.read_bytes(), "commented PDF")
    assert commented_result["page_count"] == 1
    assert commented_result["encrypted"] is False

    trailer_commented = tmp_path / "ordinary-trailer-commented.pdf"
    trailer_commented.write_bytes(insert_before_final_pdf_startxref(
        ordinary.read_bytes(), b"\n% ordinary printable trailer comment\n"
    ))
    trailer_result = tool._strict_pdf_evidence(
        trailer_commented.read_bytes(), "trailer-commented PDF"
    )
    assert trailer_result["page_count"] == 1
    assert trailer_result["encrypted"] is False


def test_valid_pdf_with_embedded_attachment_is_rejected(tmp_path):
    from pypdf import PdfWriter

    pdf = tmp_path / "embedded-attachment.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({
        "/Title": "Attachment rejection",
        "/Subject": "retained-evidence-" * 400,
    })
    writer.add_attachment("conflicting-authority.bin", b"different authority bytes")
    with pdf.open("wb") as stream:
        writer.write(stream)

    assert pdf.stat().st_size >= tool.MIN_AUTHORITY_PDF_BYTES
    with pytest.raises(tool.RegistrationError, match="embedded foreign payloads"):
        tool._strict_pdf_evidence(pdf.read_bytes(), "attachment PDF")


def test_unexpected_strict_pdf_parser_exception_is_controlled(tmp_path, monkeypatch):
    import pypdf

    pdf = tmp_path / "unexpected-parser-failure.pdf"
    write_valid_pdf(pdf, "Unexpected parser failure")

    def failed_reader(*_args, **_kwargs):
        raise RuntimeError("synthetic parser implementation failure")

    monkeypatch.setattr(pypdf, "PdfReader", failed_reader)
    with pytest.raises(tool.RegistrationError, match="failed strict PDF parsing"):
        tool._strict_pdf_evidence(pdf.read_bytes(), "parser failure fixture")


def test_survey_authority_detached_signature_tamper_fails(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    _, signature = survey_authority_paths(path)
    signature.write_bytes(b"\x00" * 64)
    survey = validate_written_survey(path, geometry, deliverables, observations)
    assert survey["passed"] is False
    assert "current_horizontal_survey_authority_attestation" in survey["reasons"]


def test_removed_or_revoked_survey_authority_key_fails_closed(tmp_path, monkeypatch):
    _, geometry, _, _, _ = synthetic_evidence()
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    monkeypatch.setattr(tool, "TRUSTED_SURVEY_AUTHORITY_SIGNERS", {})
    survey = validate_written_survey(path, geometry, deliverables, observations)
    assert survey["passed"] is False
    assert "current_horizontal_survey_authority_attestation" in survey["reasons"]


def test_survey_raw_control_uncertainty_and_datum_fail_closed(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    value = json.loads(path.read_text())
    value["horizontal_crs"]["datum"] = "fabricated datum"
    path.write_text(json.dumps(value))
    with observations.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["horizontal_uncertainty_m"] = "0.5"
    with observations.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(tool.SURVEY_OBSERVATION_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    observation_hash = tool.sha256(observations)
    value["observations"]["sha256"] = observation_hash
    value["observations"]["bytes"] = observations.stat().st_size
    observation_declaration = next(
        item for item in value["raw_deliverables"] if item["role"] == "raw_observations"
    )
    observation_declaration["sha256"] = observation_hash
    observation_declaration["bytes"] = observations.stat().st_size
    path.write_text(json.dumps(value))
    survey = validate_written_survey(path, geometry, deliverables, observations)
    assert survey["passed"] is False
    assert "current_horizontal_survey_crs" in survey["reasons"]
    assert "current_horizontal_survey_raw_observations" in survey["reasons"]


def test_survey_geometric_control_duplicate_across_splits_is_rejected(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    objects = geometry["geometry"]["objects"]
    objects[10]["center_world"] = list(objects[0]["center_world"])
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    survey = validate_written_survey(path, geometry, deliverables, observations)
    assert survey["passed"] is False
    assert "current_horizontal_survey_raw_observations" in survey["reasons"]


def test_survey_raw_deliverable_byte_tamper_is_rejected(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    license_path = next(item for item in deliverables if item.name == "surveyor-license.pdf")
    license_path.write_bytes(license_path.read_bytes() + b"tampered")
    survey = validate_written_survey(path, geometry, deliverables, observations)
    assert survey["passed"] is False
    assert "current_horizontal_survey_licensed_deliverables" in survey["reasons"]


def test_survey_observation_surveyor_instrument_and_source_identity_are_enforced(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    with observations.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["surveyor_license"] = "self-authored"
    rows[0]["instrument_serial"] = "unknown"
    rows[0]["source_id"] = "renamed-source"
    with observations.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(tool.SURVEY_OBSERVATION_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    rebind_observations(path, observations)
    survey = validate_written_survey(path, geometry, deliverables, observations)
    assert survey["passed"] is False
    assert "current_horizontal_survey_raw_observations" in survey["reasons"]


def test_survey_requires_one_distinct_stable_landmark_per_control(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    geometry["geometry"]["objects"] = geometry["geometry"]["objects"][:5]
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    survey = validate_written_survey(path, geometry, deliverables, observations)
    assert survey["passed"] is False
    assert "current_horizontal_survey_stable_landmark_count" in survey["reasons"]


def test_survey_rejects_caller_label_stable_landmark(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    item = geometry["geometry"]["objects"][0]
    item["category"] = "StableLandmark"
    item["id"] = f"environment-StableLandmark-{item['source_object_id']}"
    item.pop("semantic_source")
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    survey = validate_written_survey(path, geometry, deliverables, observations)
    assert survey["passed"] is False
    assert "current_horizontal_survey_raw_observations" in survey["reasons"]


def test_survey_rejects_caller_traffic_light_without_native_semantics(tmp_path):
    _, geometry, _, _, _ = synthetic_evidence()
    geometry["geometry"]["objects"][0]["semantic_source"] = {
        "schema": "caller-defined", "api": "caller",
        "native_type": "CityObjectLabel.TrafficLight", "native_subtype": None,
    }
    path, deliverables, observations = write_current_survey(tmp_path, geometry)
    survey = validate_written_survey(path, geometry, deliverables, observations)
    assert survey["passed"] is False
    assert "current_horizontal_survey_raw_observations" in survey["reasons"]
