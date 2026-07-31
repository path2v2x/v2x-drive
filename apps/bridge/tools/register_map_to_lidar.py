#!/usr/bin/env python3
"""Register immutable map polylines to authoritative LiDAR development control.

The executable reads evidence offline except for one authenticated, allowlisted
append-only holdout-registry consumption.  It fits one site-wide SE(2) transform
and one additive Z bias from manually identified, hash-bound finite polylines.
Fit and holdout identities are disjoint, every direction is scored, and old
USGS QL2 data remains development-only even when its numerical fit is good.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
from importlib import metadata as importlib_metadata
import io
import json
import logging
import math
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import sys
import warnings
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlparse
import zipfile

# Import numerical libraries only after establishing deterministic single-thread
# defaults. Explicit non-1 caller values are rejected by the toolchain gate.
for _thread_variable in (
    "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
):
    os.environ.setdefault(_thread_variable, "1")
# The retained NumPy wheel uses DYNAMIC_ARCH OpenBLAS. Pin one supported kernel
# family so identical x86_64 hosts cannot silently choose different kernels.
os.environ.setdefault("OPENBLAS_CORETYPE", "Haswell")

import numpy as np
from scipy.optimize import least_squares


ANNOTATION_SCHEMA = "v2x-map-lidar-registration-annotations/v1"
REPORT_SCHEMA = "v2x-map-lidar-registration-report/v1"
GEOMETRY_SCHEMA = "v2x-map-calibration-geometry/v1"
SURVEY_SCHEMA = "v2x-current-horizontal-survey/v2"
SURVEY_AUTHORITY_ATTESTATION_SCHEMA = "v2x-survey-authority-attestation/v1"
CRS_AUTHORITY_ATTESTATION_SCHEMA = "v2x-crs-authority-attestation/v1"
ANNOTATION_AUTHORITY_ATTESTATION_SCHEMA = "v2x-annotation-authority-attestation/v1"
LIDAR_VALIDATION_AUTHORITY_ATTESTATION_SCHEMA = (
    "v2x-lidar-validation-authority-attestation/v1"
)
VERTICAL_DATUM_RECONCILIATION_SCHEMA = "v2x-vertical-datum-reconciliation/v1"
VERTICAL_DATUM_AUTHORITY_ATTESTATION_SCHEMA = (
    "v2x-vertical-datum-authority-attestation/v1"
)
ANNOTATION_REVIEW_SCHEMA = "v2x-map-lidar-annotation-review/v1"
HOLDOUT_LEDGER_SCHEMA = "v2x-map-lidar-holdout-ledger/v1"
HOLDOUT_BURN_RECEIPT_SCHEMA = "v2x-map-lidar-holdout-burn-receipt/v1"
HOLDOUT_REGISTRY_HEAD_SCHEMA = "v2x-map-lidar-holdout-registry-head/v1"
HOLDOUT_REGISTRY_ENTRY_SCHEMA = "v2x-map-lidar-holdout-registry-entry/v1"
HOLDOUT_REGISTRY_CONSUME_SCHEMA = "v2x-map-lidar-holdout-registry-consume/v1"
HOLDOUT_REGISTRY_RECEIPT_SCHEMA = "v2x-map-lidar-holdout-registry-receipt/v1"
TOOLCHAIN_LOCK_SCHEMA = "v2x-map-lidar-toolchain-lock/v1"

HORIZONTAL_RMSE_MAX_M = 0.25
HORIZONTAL_MAX_M = 0.50
HAUSDORFF_MAX_M = 0.50
VERTICAL_RMSE_MAX_M = 0.10
VERTICAL_P95_MAX_M = 0.20
VERTICAL_MAX_M = 0.30
FOLD_TRANSLATION_SPREAD_MAX_M = 0.10
FOLD_YAW_SPREAD_MAX_DEG = 0.10
JACOBIAN_CONDITION_MAX = 1e8
FEATURE_REGRESSION_TOLERANCE_M = 0.01
MAX_HORIZONTAL_QUANTIZATION_M = 0.05
MAX_VERTICAL_QUANTIZATION_M = 0.05
RAW_POINT_REPRODUCTION_TOLERANCE_M = 1e-6
EVALUATION_SPACING_M = 0.10
MIN_APPROACHES = 4
MIN_FIT_FEATURES = 4
MIN_HOLDOUT_FEATURES = 4
MIN_ANNOTATED_POINTS_PER_FEATURE = 3
TRANSLATION_BOUND_RADIUS_M = 25.0
YAW_BOUND_RADIUS_DEG = 15.0
Z_BIAS_BOUND_RADIUS_M = 10.0
BOUND_PROXIMITY_FRACTION = 1e-5
NEAR_OPTIMAL_COST_FRACTION = 0.05
CURRENT_SURVEY_MAX_AGE_DAYS = 90.0
GEOMETRIC_DUPLICATE_TOLERANCE_M = 0.02
SPATIAL_EXCLUSION_BUFFER_M = 0.50
MAX_SURVEY_CONTROL_UNCERTAINTY_M = 0.10
MIN_SURVEY_FIT_CONTROLS = 10
MIN_SURVEY_HOLDOUT_CONTROLS = 4
MIN_SURVEY_STABLE_LANDMARKS = MIN_SURVEY_FIT_CONTROLS + MIN_SURVEY_HOLDOUT_CONTROLS
MIN_CRS_AUTHORITY_CHECKPOINTS = 6
MIN_VERTICAL_AUTHORITY_CHECKPOINTS = 6
ANNOTATION_REPEATABILITY_MAX_M = 0.10
MIN_AUTHORITY_PDF_BYTES = 4096
MAX_AUTHORITY_PDF_BYTES = 25 * 1024 * 1024
MAX_SURVEY_DELIVERABLE_BYTES = 25 * 1024 * 1024
MAX_VERTICAL_SOURCE_ARTIFACT_BYTES = 256 * 1024 * 1024
HOLDOUT_REGISTRY_HEAD_MAX_AGE_SECONDS = 300.0
HOLDOUT_REGISTRY_HTTP_TIMEOUT_SECONDS = 10.0
HOLDOUT_REGISTRY_RESPONSE_MAX_BYTES = 1024 * 1024
AUTHORITY_ATTESTATION_MAX_AGE_DAYS = 90.0
AUTHORITY_ATTESTATION_MAX_VALIDITY_DAYS = 366.0
NATIVE_OBJECT_SEMANTIC_SCHEMA = "v2x-carla-native-environment-object/v1"
NATIVE_OBJECT_API = "carla.World.get_environment_objects"
STABLE_NATIVE_TYPES = {
    "CityObjectLabel.TrafficLight": "TrafficLight",
    "CityObjectLabel.TrafficSigns": "TrafficSigns",
}
# Real authority keys must be reviewed and pinned in source before deployment.
# Empty defaults deliberately keep production acceptance closed; tests install
# explicit ephemeral test-only keys through monkeypatching.
TRUSTED_SURVEY_AUTHORITY_SIGNERS = {}
TRUSTED_CRS_AUTHORITY_SIGNERS = {}
TRUSTED_ANNOTATION_AUTHORITY_SIGNERS = {}
TRUSTED_LIDAR_VALIDATION_SIGNERS = {}
TRUSTED_VERTICAL_DATUM_SIGNERS = {}
TRUSTED_HOLDOUT_REGISTRY_SIGNERS = {}
TRUSTED_HOLDOUT_REGISTRY_ENDPOINTS = {}
SURVEY_DELIVERABLE_ROLES = {
    "raw_observations", "survey_license", "instrument_calibration"
}
SURVEY_OBSERVATION_COLUMNS = (
    "observation_id", "physical_control_id", "stable_landmark_id", "split",
    "easting_m", "northing_m", "horizontal_uncertainty_m", "observed_at_utc",
    "surveyor_license", "instrument_serial", "source_id",
)
MANUAL_PROVENANCE = "manually_verified_map_lidar_polyline"
FEATURE_KINDS = {"road_edge", "lane_marking", "crosswalk_edge", "stable_landmark"}
CAMERA_IDS = ("ch1", "ch2", "ch3", "ch4")


class RegistrationError(ValueError):
    """An immutable input or strict registration precondition failed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _parse_authority_time(value, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RegistrationError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegistrationError(f"{label} is malformed") from exc
    if parsed.tzinfo is None:
        raise RegistrationError(f"{label} has no timezone")
    return parsed.astimezone(timezone.utc)


def _verify_detached_attestation(attestation_path: Path | None,
                                 signature_path: Path | None,
                                 trusted_signers: dict, expected_schema: str,
                                 label: str) -> tuple[dict, dict]:
    """Verify exact signed bytes against a source-pinned Ed25519 authority key."""
    if attestation_path is None or signature_path is None:
        raise RegistrationError(f"{label} detached authority evidence is required")
    attestation_path, signature_path = attestation_path.resolve(), signature_path.resolve()
    if not attestation_path.is_file() or not signature_path.is_file():
        raise RegistrationError(f"{label} detached authority evidence is unavailable")
    attestation_bytes, signature = attestation_path.read_bytes(), signature_path.read_bytes()
    if not attestation_bytes or len(signature) != 64:
        raise RegistrationError(f"{label} detached signature is malformed")
    try:
        attestation = json.loads(attestation_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistrationError(f"{label} attestation is malformed") from exc
    if not isinstance(attestation, dict) or attestation.get("schema") != expected_schema:
        raise RegistrationError(f"{label} attestation schema is unsupported")
    key_id = attestation.get("signing_key_id")
    signer = trusted_signers.get(key_id) if isinstance(key_id, str) else None
    if not isinstance(signer, dict):
        raise RegistrationError(f"{label} signer is not pinned and trusted")
    if (
        attestation.get("producer") != signer.get("producer")
        or attestation.get("source") != signer.get("source")
    ):
        raise RegistrationError(f"{label} producer/source is not allowlisted")
    pem = signer.get("public_key_pem")
    if isinstance(pem, str):
        pem = pem.encode("ascii")
    if not isinstance(pem, bytes) or not pem:
        raise RegistrationError(f"{label} pinned public key is unavailable")
    key_hash = hashlib.sha256(pem).hexdigest()
    if attestation.get("public_key_sha256") != key_hash:
        raise RegistrationError(f"{label} pinned public key fingerprint is not bound")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise RegistrationError(
            f"{label} signature verification dependency is unavailable"
        ) from exc
    try:
        public_key = serialization.load_pem_public_key(pem)
        if not isinstance(public_key, Ed25519PublicKey):
            raise RegistrationError(f"{label} pinned key is not Ed25519")
        public_key.verify(signature, attestation_bytes)
    except InvalidSignature as exc:
        raise RegistrationError(f"{label} detached signature is invalid") from exc
    except RegistrationError:
        raise
    except Exception as exc:
        raise RegistrationError(f"{label} pinned public key is malformed") from exc
    verified = _parse_authority_time(attestation.get("verified_at_utc"), f"{label} verification time")
    expires = _parse_authority_time(attestation.get("expires_at_utc"), f"{label} expiry time")
    now = datetime.now(timezone.utc)
    age_days = (now - verified).total_seconds() / 86400
    validity_days = (expires - verified).total_seconds() / 86400
    if (
        age_days < -(5.0 / 1440.0)
        or age_days > AUTHORITY_ATTESTATION_MAX_AGE_DAYS
        or expires <= now
        or validity_days <= 0
        or validity_days > AUTHORITY_ATTESTATION_MAX_VALIDITY_DAYS
    ):
        raise RegistrationError(f"{label} verification time window is invalid")
    return attestation, {
        "attestation_path": str(attestation_path),
        "attestation_sha256": hashlib.sha256(attestation_bytes).hexdigest(),
        "signature_path": str(signature_path),
        "signature_sha256": hashlib.sha256(signature).hexdigest(),
        "signing_key_id": key_id,
        "public_key_sha256": key_hash,
        "producer": signer["producer"],
        "source": signer["source"],
        "verified_at_utc": attestation["verified_at_utc"],
        "expires_at_utc": attestation["expires_at_utc"],
    }


def _holdout_registry_config(registry_id: str) -> dict:
    endpoint = TRUSTED_HOLDOUT_REGISTRY_ENDPOINTS.get(registry_id)
    if not isinstance(endpoint, dict) or endpoint.get("registry_id") != registry_id:
        raise RegistrationError("holdout registry endpoint is not pinned and trusted")
    base_url = endpoint.get("base_url")
    if not isinstance(base_url, str):
        raise RegistrationError("holdout registry endpoint is malformed")
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.query or parsed.fragment
        or base_url.endswith("/")
    ):
        raise RegistrationError("holdout registry endpoint is not an exact HTTPS origin")
    return {"registry_id": registry_id, "base_url": base_url}


def _verify_holdout_registry_head(envelope: dict, registry_id: str) -> dict:
    if not isinstance(envelope, dict) or set(envelope) != {"head", "signature_base64"}:
        raise RegistrationError("holdout registry signed head envelope is malformed")
    head = envelope.get("head")
    expected_keys = {
        "schema", "registry_id", "sequence", "entry_sha256", "prior_head_sha256",
        "observed_at_utc", "signing_key_id", "public_key_sha256", "producer", "source",
    }
    if not isinstance(head, dict) or set(head) != expected_keys:
        raise RegistrationError("holdout registry signed head is malformed")
    sequence = head.get("sequence")
    if (
        head.get("schema") != HOLDOUT_REGISTRY_HEAD_SCHEMA
        or head.get("registry_id") != registry_id
        or not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0
        or re.fullmatch(r"[0-9a-f]{64}", str(head.get("entry_sha256"))) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(head.get("prior_head_sha256"))) is None
    ):
        raise RegistrationError("holdout registry signed head binding is invalid")
    key_id = head.get("signing_key_id")
    signer = TRUSTED_HOLDOUT_REGISTRY_SIGNERS.get(key_id) if isinstance(key_id, str) else None
    if (
        not isinstance(signer, dict)
        or signer.get("registry_id") != registry_id
        or head.get("producer") != signer.get("producer")
        or head.get("source") != signer.get("source")
    ):
        raise RegistrationError("holdout registry signer is not pinned and trusted")
    pem = signer.get("public_key_pem")
    if isinstance(pem, str):
        pem = pem.encode("ascii")
    if not isinstance(pem, bytes) or not pem:
        raise RegistrationError("holdout registry pinned public key is unavailable")
    key_hash = hashlib.sha256(pem).hexdigest()
    if head.get("public_key_sha256") != key_hash:
        raise RegistrationError("holdout registry pinned public key fingerprint is not bound")
    try:
        signature = base64.b64decode(envelope["signature_base64"], validate=True)
    except (TypeError, ValueError) as exc:
        raise RegistrationError("holdout registry head signature is malformed") from exc
    if len(signature) != 64:
        raise RegistrationError("holdout registry head signature is malformed")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        public_key = serialization.load_pem_public_key(pem)
        if not isinstance(public_key, Ed25519PublicKey):
            raise RegistrationError("holdout registry pinned key is not Ed25519")
        public_key.verify(signature, canonical_json_bytes(head))
    except InvalidSignature as exc:
        raise RegistrationError("holdout registry head signature is invalid") from exc
    except RegistrationError:
        raise
    except Exception as exc:
        raise RegistrationError("holdout registry pinned public key is malformed") from exc
    observed = _parse_authority_time(
        head.get("observed_at_utc"), "holdout registry head observation time"
    )
    age_seconds = (datetime.now(timezone.utc) - observed).total_seconds()
    if (
        age_seconds < -HOLDOUT_REGISTRY_HEAD_MAX_AGE_SECONDS
        or age_seconds > HOLDOUT_REGISTRY_HEAD_MAX_AGE_SECONDS
    ):
        raise RegistrationError("holdout registry signed head is stale")
    return {
        "head": head,
        "head_sha256": canonical_hash(head),
        "signature_sha256": hashlib.sha256(signature).hexdigest(),
        "signing_key_id": key_id,
        "public_key_sha256": key_hash,
        "producer": head["producer"],
        "source": head["source"],
    }


class HoldoutRegistryClient:
    """Minimal pinned HTTPS client for one atomic holdout consume operation."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def _request(self, method: str, suffix: str, payload: dict | None = None) -> dict:
        try:
            import requests

            response = requests.request(
                method, f"{self.base_url}{suffix}",
                json=payload, timeout=HOLDOUT_REGISTRY_HTTP_TIMEOUT_SECONDS,
                allow_redirects=False,
                headers={"Accept": "application/json"},
            )
        except Exception as exc:
            raise RegistrationError("holdout registry request failed") from exc
        if response.is_redirect or response.status_code >= 400:
            raise RegistrationError(
                f"holdout registry request was refused with HTTP {response.status_code}"
            )
        content = response.content
        if len(content) > HOLDOUT_REGISTRY_RESPONSE_MAX_BYTES:
            raise RegistrationError("holdout registry response is too large")
        try:
            value = response.json()
        except (TypeError, ValueError) as exc:
            raise RegistrationError("holdout registry response is not JSON") from exc
        if not isinstance(value, dict):
            raise RegistrationError("holdout registry response is malformed")
        return value

    def get_head(self) -> dict:
        return self._request("GET", "/head")

    def consume(self, request: dict) -> dict:
        return self._request("POST", "/consume", request)

    def get_entry(self, sequence: int) -> dict:
        return self._request("GET", f"/entries/{quote(str(sequence), safe='')}")


def _new_holdout_registry_client(config: dict) -> HoldoutRegistryClient:
    return HoldoutRegistryClient(config["base_url"])


def _runtime_toolchain_identity() -> dict:
    try:
        configuration = np.show_config(mode="dicts")
        blas = configuration["Build Dependencies"]["blas"]
    except (KeyError, TypeError) as exc:
        raise RegistrationError("numerical BLAS identity is unavailable") from exc
    return {
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "packages": {
            name: importlib_metadata.version(distribution)
            for name, distribution in {
                "numpy": "numpy", "scipy": "scipy", "laspy": "laspy",
                "pyproj": "pyproj", "Pillow": "Pillow",
                "cryptography": "cryptography", "pypdf": "pypdf",
            }.items()
        },
        "blas": {
            "name": blas.get("name"),
            "version": blas.get("version"),
            "configuration": blas.get("openblas configuration"),
        },
        "thread_environment": {
            name: os.environ.get(name)
            for name in (
                "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "OPENBLAS_CORETYPE",
            )
        },
    }


def validate_toolchain_lock() -> dict:
    lock_path = Path(__file__).with_name("map_lidar_toolchain_lock.json").resolve()
    if not lock_path.is_file():
        raise RegistrationError("tracked deterministic toolchain lock is missing")
    lock = json.loads(lock_path.read_text())
    actual = _runtime_toolchain_identity()
    if lock.get("schema") != TOOLCHAIN_LOCK_SCHEMA or lock.get("runtime") != actual:
        raise RegistrationError("runtime does not match tracked deterministic toolchain lock")
    return {
        "passed": True,
        "lock_path": str(lock_path),
        "lock_sha256": sha256(lock_path),
        "registration_tool_sha256": sha256(Path(__file__)),
        "runtime": actual,
    }


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = fraction * (len(ordered) - 1)
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def write_json_exclusive(path: Path | str, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def finite_xyz(value, label: str, minimum_points: int = 2) -> np.ndarray:
    array = np.asarray(value, dtype=float)
    if array.ndim != 2 or array.shape[1] != 3 or len(array) < minimum_points:
        raise RegistrationError(f"{label} must contain at least {minimum_points} XYZ rows")
    if not np.isfinite(array).all():
        raise RegistrationError(f"{label} contains non-finite coordinates")
    if np.any(np.linalg.norm(np.diff(array[:, :2], axis=0), axis=1) <= 1e-9):
        raise RegistrationError(f"{label} contains a zero-length segment")
    return array


def crs_components(crs) -> tuple[int | None, int | None]:
    if crs is None:
        return None, None
    horizontal, vertical = None, None
    components = list(getattr(crs, "sub_crs_list", ()) or ())
    if not components:
        components = [crs]
    for component in components:
        epsg = component.to_epsg()
        if getattr(component, "is_vertical", False):
            vertical = epsg
        elif getattr(component, "is_projected", False):
            horizontal = epsg
    return horizontal, vertical


def _crs_component_objects(crs):
    horizontal = vertical = None
    components = list(getattr(crs, "sub_crs_list", ()) or ()) or [crs]
    for component in components:
        if getattr(component, "is_vertical", False):
            vertical = component
        elif getattr(component, "is_projected", False):
            horizontal = component
    return horizontal, vertical


def _require_metre_axes(crs, count: int, label: str) -> list[str]:
    axes = list(getattr(crs, "axis_info", ()) or ())
    if len(axes) < count or any(
        not math.isclose(float(axis.unit_conversion_factor), 1.0, abs_tol=1e-12)
        for axis in axes[:count]
    ):
        raise RegistrationError(f"{label} coordinate axes are not metres")
    return [axis.unit_name for axis in axes[:count]]


def _coordinate_epoch(crs) -> float | None:
    value = getattr(crs, "coordinate_epoch", None)
    if value is not None:
        return float(value)
    match = re.search(r"FRAMEEPOCH\[([0-9.+-]+)\]", crs.to_wkt())
    return None if match is None else float(match.group(1))


def _semantic_crs_equal(left, right) -> bool:
    try:
        from pyproj import CRS

        return CRS.from_user_input(left).equals(CRS.from_user_input(right))
    except Exception as exc:
        raise RegistrationError(f"unable to parse declared LiDAR CRS: {exc}") from exc


def _verify_lidar_validation_authority(lidar_path: Path, validation_path: Path,
                                       validation: dict, lidar_hash: str,
                                       crs_wkt: str, point_count: int,
                                       attestation_path: Path | None,
                                       signature_path: Path | None) -> dict | None:
    if attestation_path is None and signature_path is None:
        return None
    attestation, evidence = _verify_detached_attestation(
        attestation_path, signature_path, TRUSTED_LIDAR_VALIDATION_SIGNERS,
        LIDAR_VALIDATION_AUTHORITY_ATTESTATION_SCHEMA,
        "LiDAR validation authority",
    )
    expected = {
        "lidar_sha256": lidar_hash,
        "lidar_bytes": lidar_path.stat().st_size,
        "validation_sha256": sha256(validation_path),
        "validation_payload_sha256": canonical_hash(validation),
        "point_count": point_count,
        "crs_wkt_sha256": hashlib.sha256(crs_wkt.encode()).hexdigest(),
        "bounds_sha256": canonical_hash({
            "mins": validation.get("mins"), "maxs": validation.get("maxs"),
        }),
    }
    if any(attestation.get(key) != value for key, value in expected.items()):
        raise RegistrationError(
            "LiDAR authority attestation does not bind exact validation evidence"
        )
    if attestation.get("verification_result") != {
        "source_data_status": "authoritative",
        "validation_status": "verified",
    }:
        raise RegistrationError("LiDAR authority verification result is not accepted")
    return {**evidence, **expected, "verification_result": attestation["verification_result"]}


def load_lidar_tile(lidar_path: Path, validation_path: Path,
                    authority_attestation_path: Path | None = None,
                    authority_signature_path: Path | None = None) -> dict:
    try:
        import laspy
    except ImportError as exc:
        raise RegistrationError(
            "laspy with a LAZ backend is required to decode the complete raw cloud"
        ) from exc

    if lidar_path.suffix.lower() not in {".las", ".laz"}:
        raise RegistrationError("raw LiDAR inputs must be LAS or LAZ files")
    validation = json.loads(validation_path.read_text())
    lidar_hash, validation_hash = sha256(lidar_path), sha256(validation_path)
    cloud = laspy.read(lidar_path)
    points = np.column_stack((cloud.x, cloud.y, cloud.z)).astype(float, copy=False)
    if len(points) == 0 or not np.isfinite(points).all():
        raise RegistrationError(f"{lidar_path}: decoded cloud is empty or non-finite")
    header = cloud.header
    crs = header.parse_crs()
    if crs is None:
        raise RegistrationError(f"{lidar_path}: LAS/LAZ contains no parseable CRS")
    horizontal_epsg, vertical_epsg = crs_components(crs)
    horizontal_crs, vertical_crs = _crs_component_objects(crs)
    if horizontal_epsg is None or horizontal_crs is None or not horizontal_crs.is_projected:
        raise RegistrationError(f"{lidar_path}: horizontal CRS is not projected")
    if vertical_epsg is None or vertical_crs is None or not vertical_crs.is_vertical:
        raise RegistrationError(f"{lidar_path}: vertical CRS is missing or non-vertical")
    horizontal_units = _require_metre_axes(horizontal_crs, 2, f"{lidar_path}: horizontal CRS")
    vertical_units = _require_metre_axes(vertical_crs, 1, f"{lidar_path}: vertical CRS")
    scales = np.asarray(header.scales, dtype=float)
    if (
        len(scales) != 3
        or not np.isfinite(scales).all()
        or np.any(scales <= 0)
        or max(scales[:2]) > MAX_HORIZONTAL_QUANTIZATION_M
        or scales[2] > MAX_VERTICAL_QUANTIZATION_M
    ):
        raise RegistrationError(
            f"{lidar_path}: coordinate quantization is too coarse for fixed gates"
        )

    expected_hash = validation.get("sha256") or validation.get("lidar_sha256")
    if expected_hash is not None and expected_hash != lidar_hash:
        raise RegistrationError(f"{lidar_path}: validation raw hash mismatch")
    checks = {
        "bytes": (validation.get("bytes"), lidar_path.stat().st_size),
        "points": (validation.get("points"), len(points)),
    }
    for name, (declared, actual) in checks.items():
        if declared is None or int(declared) != int(actual):
            raise RegistrationError(f"{lidar_path}: validation {name} mismatch")
    for name, actual in (("mins", np.min(points, axis=0)), ("maxs", np.max(points, axis=0))):
        declared = np.asarray(validation.get(name), dtype=float)
        if declared.shape != (3,) or not np.allclose(
            declared, actual, atol=np.maximum(scales, 1e-6), rtol=0
        ):
            raise RegistrationError(f"{lidar_path}: validation {name} mismatch")
    declared_crs = validation.get("crs") or validation.get("crs_wkt")
    if not declared_crs or not _semantic_crs_equal(declared_crs, crs):
        raise RegistrationError(f"{lidar_path}: validation CRS mismatch")
    authority_evidence = _verify_lidar_validation_authority(
        lidar_path, validation_path, validation, lidar_hash, crs.to_wkt(),
        len(points), authority_attestation_path, authority_signature_path,
    )
    return {
        "path": str(lidar_path.resolve()),
        "sha256": lidar_hash,
        "validation_path": str(validation_path.resolve()),
        "validation_sha256": validation_hash,
        "points": points,
        "point_count": len(points),
        "bytes": lidar_path.stat().st_size,
        "bounds": {"min": np.min(points, axis=0).tolist(), "max": np.max(points, axis=0).tolist()},
        "scales": scales.tolist(),
        "crs_wkt": crs.to_wkt(),
        "horizontal_epsg": horizontal_epsg,
        "vertical_epsg": vertical_epsg,
        "horizontal_units": horizontal_units,
        "vertical_units": vertical_units,
        "horizontal_crs_wkt": horizontal_crs.to_wkt(),
        "horizontal_datum": horizontal_crs.datum.name if horizontal_crs.datum else None,
        "vertical_datum": vertical_crs.datum.name if vertical_crs.datum else None,
        "vertical_crs_wkt": vertical_crs.to_wkt(),
        "horizontal_coordinate_epoch": _coordinate_epoch(horizontal_crs),
        "vertical_coordinate_epoch": _coordinate_epoch(vertical_crs),
        "validation_authority_attestation": authority_evidence,
    }


def select_metadata_record(metadata: dict, selector: dict) -> dict:
    if metadata.get("schema") == "v2x-lidar-authoritative-metadata/v1":
        record = metadata.get("project")
        if not isinstance(record, dict):
            raise RegistrationError("authoritative metadata project record is missing")
        return record
    features = metadata.get("features")
    if not isinstance(features, list):
        raise RegistrationError("authoritative metadata format is unsupported")
    required = {key: selector.get(key) for key in ("project_id", "workunit")}
    if any(value in (None, "") for value in required.values()):
        raise RegistrationError("metadata selector requires project_id and workunit")
    matches = [
        feature.get("attributes", {})
        for feature in features
        if all(feature.get("attributes", {}).get(key) == value for key, value in required.items())
    ]
    if len(matches) != 1:
        raise RegistrationError("metadata selector did not identify exactly one project")
    return matches[0]


def _metadata_year(value, label: str) -> int:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).year
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).year
        except ValueError as exc:
            raise RegistrationError(f"metadata {label} is malformed") from exc
    raise RegistrationError(f"metadata {label} is missing")


def parse_acquisition_years(record: dict) -> list[int]:
    start = _metadata_year(
        record.get("collect_start") or record.get("acquisition_start"), "acquisition start"
    )
    end_value = record.get("collect_end") or record.get("acquisition_end")
    end = _metadata_year(end_value, "acquisition end") if end_value is not None else start
    if end < start or end - start > 5:
        raise RegistrationError("metadata acquisition year range is invalid")
    return list(range(start, end + 1))


def parse_acquisition_year(record: dict) -> int:
    return parse_acquisition_years(record)[0]


def parse_opendrive(path: Path) -> dict:
    root = ET.parse(path).getroot()
    if root.tag != "OpenDRIVE":
        raise RegistrationError("map file is not an OpenDRIVE document")
    georeference = (root.findtext("./header/geoReference") or "").strip()
    if not georeference:
        raise RegistrationError("OpenDRIVE georeference is missing")
    try:
        from pyproj import CRS

        projected_crs = CRS.from_user_input(georeference)
        if not projected_crs.is_projected:
            raise RegistrationError("OpenDRIVE georeference is not projected")
        units = _require_metre_axes(projected_crs, 2, "OpenDRIVE georeference")
    except RegistrationError:
        raise
    except Exception as exc:
        raise RegistrationError("OpenDRIVE georeference is not parseable") from exc
    return {
        "path": str(path.resolve()),
        "sha256": sha256(path),
        "georeference": georeference,
        "georeference_sha256": hashlib.sha256(georeference.encode()).hexdigest(),
        "georeference_wkt": projected_crs.to_wkt(),
        "georeference_linear_units": units,
        "georeference_epsg": projected_crs.to_epsg(),
        "georeference_datum": projected_crs.datum.name if projected_crs.datum else None,
        "georeference_coordinate_epoch": _coordinate_epoch(projected_crs),
    }


def verify_artifact_bindings(annotation: dict, tiles: dict, metadata_path: Path,
                             opendrive: dict, geometry_path: Path, geometry: dict) -> None:
    if annotation.get("schema") != ANNOTATION_SCHEMA:
        raise RegistrationError("manual annotation schema is unsupported")
    bindings = annotation.get("bindings")
    if not isinstance(bindings, dict):
        raise RegistrationError("manual annotations have no immutable bindings")
    declared_tiles = bindings.get("lidar_tiles")
    expected_tiles = sorted(
        (item["sha256"], item["validation_sha256"]) for item in tiles.values()
    )
    actual_tiles = sorted(
        (item.get("lidar_sha256"), item.get("validation_sha256"))
        for item in declared_tiles or []
    )
    if actual_tiles != expected_tiles or len(actual_tiles) != len(set(actual_tiles)):
        raise RegistrationError("manual annotations do not bind every raw/validation tile")
    exact = {
        "metadata_sha256": sha256(metadata_path),
        "opendrive_sha256": opendrive["sha256"],
        "opendrive_georeference_sha256": opendrive["georeference_sha256"],
        "geometry_sha256": sha256(geometry_path),
    }
    for key, expected in exact.items():
        if bindings.get(key) != expected:
            raise RegistrationError(f"manual annotation {key} mismatch")
    if geometry.get("schema") != GEOMETRY_SCHEMA:
        raise RegistrationError("map geometry schema is unsupported")
    if geometry.get("opendrive_sha256") != opendrive["sha256"]:
        raise RegistrationError("map geometry was exported from a different OpenDRIVE")


def _load_exporter_module():
    import importlib.util

    path = Path(__file__).with_name("export_map_calibration_geometry.py")
    spec = importlib.util.spec_from_file_location("v2x_export_map_geometry_for_validation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def validate_geometry_provenance(geometry: dict, geometry_path: Path,
                                 opendrive_path: Path, opendrive: dict,
                                 pair_path: Path, cameras_path: Path,
                                 carla_source_path: Path) -> dict:
    from PIL import Image
    from types import SimpleNamespace

    provenance = geometry.get("geometry_provenance")
    if not isinstance(provenance, dict) or provenance.get("schema") != "v2x-map-geometry-provenance/v1":
        raise RegistrationError("map geometry has no strict exporter provenance")
    pair, cameras = json.loads(pair_path.read_text()), json.loads(cameras_path.read_text())
    carla_source = json.loads(carla_source_path.read_text())
    pair_hash, cameras_hash = sha256(pair_path), sha256(cameras_path)
    carla_source_hash = sha256(carla_source_path)
    if pair.get("schema") != "v2x-observational-calibration-pairs/v1":
        raise RegistrationError("geometry pair manifest schema is unsupported")
    if pair.get("cameras_file_sha256") != cameras_hash:
        raise RegistrationError("geometry pair manifest does not bind cameras file")
    exporter_path, exporter = _load_exporter_module()
    exact_ranges = exporter.opendrive_road_mark_ranges(opendrive_path.read_bytes())
    payload = geometry.get("geometry")
    if not isinstance(payload, dict):
        raise RegistrationError("map geometry payload is missing")
    expected = {
        "exporter_sha256": sha256(exporter_path),
        "map": geometry.get("map"),
        "opendrive_sha256": opendrive["sha256"],
        "opendrive_georeference_sha256": opendrive["georeference_sha256"],
        "pair_manifest_sha256": pair_hash,
        "cameras_file_sha256": cameras_hash,
        "carla_source_export_sha256": carla_source_hash,
        "radius_m": geometry.get("radius_m"),
        "lane_spacing_m": geometry.get("lane_spacing_m"),
        "geometry_payload_sha256": canonical_hash(payload),
        "exact_road_mark_ranges_sha256": canonical_hash(exact_ranges),
    }
    for key, value in expected.items():
        if provenance.get(key) != value:
            raise RegistrationError(f"map geometry provenance {key} mismatch")
    if (
        geometry.get("pair_manifest_sha256") != pair_hash
        or geometry.get("cameras_file_sha256") != cameras_hash
        or geometry.get("carla_source_export_sha256") != carla_source_hash
    ):
        raise RegistrationError("map geometry top-level pair/camera binding mismatch")
    if (
        carla_source.get("schema") != "v2x-retained-carla-map-export/v2"
        or carla_source.get("map") != geometry.get("map")
        or carla_source.get("opendrive_sha256") != opendrive["sha256"]
        or carla_source.get("radius_m") != geometry.get("radius_m")
        or carla_source.get("lane_spacing_m") != geometry.get("lane_spacing_m")
    ):
        raise RegistrationError("retained CARLA source export binding mismatch")
    try:
        rebuilt_payload = exporter.geometry_from_carla_source(carla_source, exact_ranges)
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise RegistrationError("retained CARLA source geometry cannot be rebuilt") from exc
    if canonical_hash(rebuilt_payload) != canonical_hash(payload):
        raise RegistrationError("map geometry does not reproduce retained CARLA/XODR source")
    if payload.get("opendrive_road_mark_ranges") != exact_ranges:
        raise RegistrationError("map geometry exact road-mark ranges do not reproduce OpenDRIVE")

    collection_ids = {}
    for collection in ("crosswalks", "lanes", "road_mark_segments", "objects"):
        values = payload.get(collection)
        if not isinstance(values, list):
            raise RegistrationError(f"map geometry collection {collection} is missing")
        identities = [item.get("id") for item in values]
        if any(not isinstance(identity, str) or not identity for identity in identities) or len(identities) != len(set(identities)):
            raise RegistrationError(f"map geometry collection {collection} identities are invalid")
        collection_ids[collection] = set(identities)
    for item in payload["crosswalks"]:
        finite_xyz(item.get("world"), f"crosswalk {item['id']}", minimum_points=4)
        if item["id"] != exporter.stable_crosswalk_id(item["world"]):
            raise RegistrationError("map geometry crosswalk stable identity mismatch")
    for lane in payload["lanes"]:
        expected_id = f"road-{lane.get('road_id')}-section-{lane.get('section_id')}-lane-{lane.get('lane_id')}"
        if lane["id"] != expected_id:
            raise RegistrationError("map geometry lane stable identity mismatch")
        for field in ("center_world", "left_boundary_world", "right_boundary_world"):
            finite_xyz(lane.get(field), f"lane {lane['id']} {field}")
        if any(identity not in collection_ids["road_mark_segments"] for identity in lane.get("road_mark_segment_ids", [])):
            raise RegistrationError("map geometry lane references an unknown road-mark segment")
    exact_by_id = {item["id"]: item for item in exact_ranges}
    sampled_by_lane = defaultdict(list)
    for segment in payload["road_mark_segments"]:
        exact = exact_by_id.get(segment.get("opendrive_range_id"))
        boundary = finite_xyz(segment.get("boundary_world"), f"road mark {segment['id']}")
        if (
            exact is None or segment.get("opendrive_range") != exact
            or not segment["id"].startswith(exact["id"] + "-sample-")
            or segment.get("boundary_world_sha256") != canonical_hash(boundary.tolist())
        ):
            raise RegistrationError("sampled road-mark geometry is not bound to exact OpenDRIVE")
        for key in ("type", "color", "lane_change", "width_m"):
            if exact.get(key) is not None and segment.get(key) != exact.get(key):
                raise RegistrationError("sampled road-mark attributes differ from OpenDRIVE")
        try:
            lane_key = (
                str(segment["road_id"]), int(segment["section_id"]),
                int(segment["lane_id"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RegistrationError("sampled road-mark source lane is invalid") from exc
        sampled_by_lane[lane_key].append({
            key: value for key, value in segment.items() if key not in {
                "opendrive_range_id", "opendrive_source_lane_id",
                "opendrive_range", "boundary_world_sha256",
            }
        })
    rebound_lanes = []
    for lane in payload["lanes"]:
        try:
            lane_key = (
                str(lane["road_id"]), int(lane["section_id"]), int(lane["lane_id"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RegistrationError("map geometry lane source identity is invalid") from exc
        candidate = dict(lane)
        candidate["road_mark_segments"] = sampled_by_lane.pop(lane_key, [])
        rebound_lanes.append(candidate)
    if sampled_by_lane:
        raise RegistrationError("sampled road mark references an unknown source lane")
    try:
        rebound = exporter.bind_sampled_road_marks(
            rebound_lanes, exact_ranges, float(geometry.get("lane_spacing_m"))
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise RegistrationError("sampled road-mark binding cannot be reproduced") from exc
    if canonical_hash(rebound) != canonical_hash(payload["road_mark_segments"]):
        raise RegistrationError("sampled road-mark binding does not reproduce exporter output")
    for original, recomputed in zip(payload["lanes"], rebound_lanes):
        if set(original.get("road_mark_segment_ids", [])) != set(
            recomputed.get("road_mark_segment_ids", [])
        ):
            raise RegistrationError("lane road-mark references do not reproduce exporter output")
    for item in payload["objects"]:
        try:
            derived_category = exporter.native_object_category(item.get("semantic_source"))
        except RuntimeError as exc:
            raise RegistrationError(
                "map environment-object native semantic provenance is invalid"
            ) from exc
        if (
            item.get("category") != derived_category
            or item["id"]
            != f"environment-{derived_category}-{item.get('source_object_id')}"
        ):
            raise RegistrationError("map environment-object stable identity mismatch")
        finite_xyz([item.get("center_world"), [
            item["center_world"][0] + 1.0, item["center_world"][1], item["center_world"][2]
        ]], f"object {item['id']}")

    configured = {item.get("id"): item for item in cameras.get("cameras", [])}
    if set(configured) != set(CAMERA_IDS):
        raise RegistrationError("geometry cameras file must contain exactly ch1-ch4")
    if set(pair.get("cameras", {})) != set(CAMERA_IDS) or set(geometry.get("cameras", {})) != set(CAMERA_IDS):
        raise RegistrationError("geometry pair/report camera sets are incomplete")
    for camera_id in CAMERA_IDS:
        camera_hash = canonical_hash(configured[camera_id])
        pair_camera, report_camera = pair["cameras"][camera_id], geometry["cameras"][camera_id]
        if pair_camera.get("twin", {}).get("camera_config_sha256") != camera_hash or report_camera.get("camera_config_sha256") != camera_hash:
            raise RegistrationError(f"{camera_id}: geometry camera object hash mismatch")
        camera_model = pair_camera.get("twin", {}).get("camera_model", {})
        observed_transform = camera_model.get("transform", {})
        try:
            expected_transform = {
                "location": [
                    float(observed_transform["location"][axis]) for axis in ("x", "y", "z")
                ],
                "rotation": [
                    float(observed_transform["rotation"][axis])
                    for axis in ("pitch", "yaw", "roll")
                ],
            }
            fov = float(camera_model["image"]["horizontal_fov_deg"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RegistrationError(f"{camera_id}: retained camera model is malformed") from exc
        if (
            report_camera.get("baseline_source") != "retained_twin_actor_metadata"
            or canonical_hash(report_camera.get("baseline_transform"))
            != canonical_hash(expected_transform)
            or not math.isclose(float(report_camera.get("horizontal_fov_deg")), fov, abs_tol=1e-12)
        ):
            raise RegistrationError(f"{camera_id}: retained camera model does not reproduce export")
        transform = SimpleNamespace(
            location=SimpleNamespace(**dict(zip(("x", "y", "z"), expected_transform["location"]))),
            rotation=SimpleNamespace(
                **dict(zip(("pitch", "yaw", "roll"), expected_transform["rotation"]))
            ),
        )
        for kind in ("real", "twin"):
            frame = pair_camera.get(kind, {})
            frame_path = (pair_path.parent / str(frame.get("file"))).resolve()
            if sha256(frame_path) != frame.get("sha256"):
                raise RegistrationError(f"{camera_id}: geometry source frame hash mismatch")
            with Image.open(frame_path) as image:
                dimensions = image.size
                image.verify()
            report_frame = report_camera.get(kind, {})
            if (
                report_frame.get("frame_sha256") != frame.get("sha256")
                or (report_frame.get("width"), report_frame.get("height")) != dimensions
            ):
                raise RegistrationError(f"{camera_id}: geometry frame provenance mismatch")
            recomputed_projection = exporter.projected_geometry(
                rebuilt_payload, transform, fov, dimensions[0], dimensions[1]
            )
            if canonical_hash(report_frame.get("projection")) != canonical_hash(recomputed_projection):
                raise RegistrationError(f"{camera_id}: geometry projection cannot be reproduced")
            overlay_name = report_frame.get("overlay")
            if not isinstance(overlay_name, str) or Path(overlay_name).name != overlay_name:
                raise RegistrationError(f"{camera_id}: geometry overlay identity is invalid")
            overlay_path = geometry_path.parent / overlay_name
            if sha256(overlay_path) != report_frame.get("overlay_sha256"):
                raise RegistrationError(f"{camera_id}: geometry overlay hash mismatch")
            rebuilt_overlay = exporter.render_overlay_bytes(frame_path, recomputed_projection)
            if rebuilt_overlay != overlay_path.read_bytes():
                raise RegistrationError(f"{camera_id}: geometry overlay pixels cannot be reproduced")
    return {
        "geometry_path": str(geometry_path.resolve()),
        "geometry_sha256": sha256(geometry_path),
        "pair_manifest_sha256": pair_hash,
        "cameras_file_sha256": cameras_hash,
        "carla_source_export_sha256": carla_source_hash,
        "geometry_payload_sha256": expected["geometry_payload_sha256"],
        "exporter_sha256": expected["exporter_sha256"],
    }


def resolve_map_polyline(geometry: dict, reference: dict, label: str) -> np.ndarray:
    collection = reference.get("collection")
    feature_id = reference.get("feature_id")
    field = reference.get("polyline_field")
    allowed = {"lanes", "crosswalks", "road_mark_segments"}
    if collection not in allowed or not isinstance(feature_id, str) or not feature_id:
        raise RegistrationError(f"{label}: stable map feature reference is invalid")
    features = geometry.get("geometry", {}).get(collection)
    if not isinstance(features, list):
        raise RegistrationError(f"{label}: map collection {collection} is unavailable")
    matches = [item for item in features if item.get("id") == feature_id]
    if len(matches) != 1:
        raise RegistrationError(f"{label}: map feature identity is missing or ambiguous")
    if not isinstance(field, str) or field not in matches[0]:
        raise RegistrationError(f"{label}: map polyline field is unavailable")
    points = finite_xyz(matches[0][field], f"{label} map polyline")
    indices = reference.get("vertex_indices")
    if indices is not None:
        if (
            not isinstance(indices, list)
            or len(indices) < 2
            or len(indices) != len(set(indices))
            or any(not isinstance(index, int) or index < 0 or index >= len(points) for index in indices)
        ):
            raise RegistrationError(f"{label}: map vertex indices are invalid")
        points = finite_xyz(points[indices], f"{label} selected map polyline")
    return points


def load_features(annotation: dict, geometry: dict, tiles: dict) -> list[dict]:
    raw_features = annotation.get("features")
    if not isinstance(raw_features, list):
        raise RegistrationError("manual annotations have no feature list")
    features, identities, raw_identities, map_identities = [], set(), {}, {}
    source_identities, physical_control_identities = {}, {}
    for raw in raw_features:
        identity = raw.get("id")
        approach = raw.get("approach_id")
        split = raw.get("split")
        if not isinstance(identity, str) or not identity or identity in identities:
            raise RegistrationError("feature identities must be unique and nonblank")
        if not isinstance(approach, str) or not approach:
            raise RegistrationError(f"{identity}: approach identity is missing")
        if split not in {"fit", "holdout"}:
            raise RegistrationError(f"{identity}: split must be fit or holdout")
        if raw.get("provenance") != MANUAL_PROVENANCE:
            raise RegistrationError(f"{identity}: manual feature provenance is not accepted")
        if raw.get("kind") not in FEATURE_KINDS:
            raise RegistrationError(f"{identity}: feature kind is not accepted")
        identities.add(identity)
        map_reference = raw.get("map", {})
        map_points = resolve_map_polyline(geometry, map_reference, identity)
        if len(map_points) < MIN_ANNOTATED_POINTS_PER_FEATURE:
            raise RegistrationError(
                f"{identity}: map polyline has fewer than "
                f"{MIN_ANNOTATED_POINTS_PER_FEATURE} independently retained vertices"
            )
        map_identity = (
            map_reference.get("collection"), map_reference.get("feature_id"),
            map_reference.get("polyline_field"),
            tuple(map_reference.get("vertex_indices") or range(len(map_points))),
        )
        previous_map = map_identities.get(map_identity)
        if previous_map is not None:
            raise RegistrationError(
                f"map polyline identity leaks between {previous_map[0]}:{previous_map[1]} "
                f"and {identity}:{split}"
            )
        map_identities[map_identity] = (identity, split)
        source_identity = (
            map_reference.get("collection"), map_reference.get("feature_id"),
            map_reference.get("polyline_field"),
        )
        previous_source = source_identities.get(source_identity)
        if previous_source is not None:
            raise RegistrationError(
                f"map source feature identity leaks between {previous_source[0]}:{previous_source[1]} "
                f"and {identity}:{split}"
            )
        source_identities[source_identity] = (identity, split)
        lidar = raw.get("lidar", {})
        tile_hash = lidar.get("tile_sha256")
        tile = tiles.get(tile_hash)
        if tile is None:
            raise RegistrationError(f"{identity}: LiDAR tile binding is unavailable")
        point_indices = lidar.get("point_indices")
        physical_control_ids = lidar.get("physical_control_ids")
        recorded = finite_xyz(lidar.get("xyz"), f"{identity} recorded LiDAR polyline")
        if (
            not isinstance(point_indices, list)
            or len(recorded) < MIN_ANNOTATED_POINTS_PER_FEATURE
            or len(point_indices) != len(recorded)
            or len(point_indices) != len(set(point_indices))
            or any(not isinstance(index, int) or index < 0 or index >= tile["point_count"] for index in point_indices)
            or not isinstance(physical_control_ids, list)
            or len(physical_control_ids) != len(point_indices)
            or len(physical_control_ids) != len(set(physical_control_ids))
            or any(not isinstance(value, str) or not value for value in physical_control_ids)
        ):
            raise RegistrationError(f"{identity}: raw LiDAR point indices are invalid")
        decoded = tile["points"][point_indices]
        tolerance = max(RAW_POINT_REPRODUCTION_TOLERANCE_M, max(tile["scales"]) / 2.0 + 1e-9)
        if not np.allclose(recorded, decoded, atol=tolerance, rtol=0):
            raise RegistrationError(f"{identity}: recorded LiDAR XYZ does not reproduce raw points")
        for point_index in point_indices:
            key = (tile_hash, point_index)
            previous = raw_identities.get(key)
            if previous is not None:
                raise RegistrationError(
                    f"raw LiDAR point identity leaks between {previous[0]}:{previous[1]} "
                    f"and {identity}:{split}"
                )
            raw_identities[key] = (identity, split)
        for physical_control_id in physical_control_ids:
            previous = physical_control_identities.get(physical_control_id)
            if previous is not None:
                raise RegistrationError(
                    f"physical control identity leaks between {previous[0]}:{previous[1]} "
                    f"and {identity}:{split}"
                )
            physical_control_identities[physical_control_id] = (identity, split)
        features.append({
            "id": identity,
            "approach_id": approach,
            "split": split,
            "kind": raw.get("kind"),
            "map_reference": map_reference,
            "map_points": map_points,
            "lidar_tile_sha256": tile_hash,
            "lidar_point_indices": list(point_indices),
            "physical_control_ids": list(physical_control_ids),
            "lidar_points": recorded,
        })
    fit = [item for item in features if item["split"] == "fit"]
    holdout = [item for item in features if item["split"] == "holdout"]
    if len(fit) < MIN_FIT_FEATURES or len(holdout) < MIN_HOLDOUT_FEATURES:
        raise RegistrationError("insufficient fit or holdout feature identities")
    approaches = sorted({item["approach_id"] for item in features})
    if len(approaches) < MIN_APPROACHES:
        raise RegistrationError(f"at least {MIN_APPROACHES} approaches are required")
    for approach in approaches:
        splits = {item["split"] for item in features if item["approach_id"] == approach}
        if splits != {"fit", "holdout"}:
            raise RegistrationError(f"approach {approach} lacks disjoint fit/holdout truth")
    for label, selected in (("fit", fit), ("holdout", holdout)):
        for side in ("map_points", "lidar_points"):
            xy = np.vstack([item[side][:, :2] for item in selected])
            if np.linalg.matrix_rank(xy - np.mean(xy, axis=0), tol=1e-6) != 2:
                raise RegistrationError(f"{label} {side} geometry is rank deficient")
    for left_index, left in enumerate(features):
        for right in features[left_index + 1:]:
            for side in ("map_points", "lidar_points"):
                if polylines_geometrically_duplicate(left[side], right[side]):
                    raise RegistrationError(
                        f"{side} geometric duplicate/resampling leaks between "
                        f"{left['id']}:{left['split']} and {right['id']}:{right['split']}"
                    )
                if left["split"] != right["split"]:
                    pairwise = np.linalg.norm(
                        left[side][:, None, :2] - right[side][None, :, :2], axis=2
                    )
                    endpoint_pairwise = np.linalg.norm(
                        left[side][[0, -1], None, :2]
                        - right[side][None, [0, -1], :2], axis=2
                    )
                    if np.min(pairwise) <= 1e-9 or np.min(endpoint_pairwise) <= 1e-9:
                        raise RegistrationError(
                            f"{side} fit/holdout coordinate or endpoint overlap between "
                            f"{left['id']} and {right['id']}"
                        )
                    if minimum_polyline_distance(left[side], right[side]) <= SPATIAL_EXCLUSION_BUFFER_M:
                        raise RegistrationError(
                            f"{side} fit/holdout spatial exclusion buffer violated between "
                            f"{left['id']} and {right['id']}"
                        )
    return features


def resample_polyline(points: np.ndarray, spacing_m: float = EVALUATION_SPACING_M) -> np.ndarray:
    distances = np.linalg.norm(np.diff(points[:, :2], axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(distances)))
    if cumulative[-1] <= 1e-9:
        raise RegistrationError("polyline has no finite horizontal extent")
    samples = np.linspace(0.0, cumulative[-1], max(2, int(math.ceil(cumulative[-1] / spacing_m)) + 1))
    output = np.column_stack([
        np.interp(samples, cumulative, points[:, axis]) for axis in range(3)
    ])
    return output


def polylines_geometrically_duplicate(left: np.ndarray, right: np.ndarray) -> bool:
    left_samples, right_samples = resample_polyline(left), resample_polyline(right)
    left_to_right = nearest_segments(left_samples, right_samples)["horizontal"]
    right_to_left = nearest_segments(right_samples, left_samples)["horizontal"]
    # A fully contained resampling/subsegment is leakage even when the other
    # feature extends farther along the same physical control.
    return bool(
        np.max(left_to_right) <= GEOMETRIC_DUPLICATE_TOLERANCE_M
        or np.max(right_to_left) <= GEOMETRIC_DUPLICATE_TOLERANCE_M
    )


def _point_segment_distance_2d(point: np.ndarray, start: np.ndarray,
                               end: np.ndarray) -> float:
    delta = end - start
    squared_length = float(np.dot(delta, delta))
    if squared_length <= 1e-24:
        return float(np.linalg.norm(point - start))
    fraction = float(np.dot(point - start, delta) / squared_length)
    projected = start + min(1.0, max(0.0, fraction)) * delta
    return float(np.linalg.norm(point - projected))


def _point_on_segment_2d(point: np.ndarray, start: np.ndarray,
                         end: np.ndarray) -> bool:
    delta, offset = end - start, point - start
    scale = max(1.0, float(np.linalg.norm(delta)), float(np.linalg.norm(offset)))
    tolerance = 1e-12 * scale * scale
    cross = float(delta[0] * offset[1] - delta[1] * offset[0])
    return bool(
        abs(cross) <= tolerance
        and np.all(point >= np.minimum(start, end) - tolerance)
        and np.all(point <= np.maximum(start, end) + tolerance)
    )


def _segment_segment_distance_2d(left_start: np.ndarray, left_end: np.ndarray,
                                 right_start: np.ndarray, right_end: np.ndarray) -> float:
    left_degenerate = np.linalg.norm(left_end - left_start) <= 1e-12
    right_degenerate = np.linalg.norm(right_end - right_start) <= 1e-12
    if left_degenerate and right_degenerate:
        return float(np.linalg.norm(left_start - right_start))
    if left_degenerate:
        return _point_segment_distance_2d(left_start, right_start, right_end)
    if right_degenerate:
        return _point_segment_distance_2d(right_start, left_start, left_end)

    def orientation(start, end, point):
        cross = float(
            (end[0] - start[0]) * (point[1] - start[1])
            - (end[1] - start[1]) * (point[0] - start[0])
        )
        scale = max(
            1.0, float(np.linalg.norm(end - start)), float(np.linalg.norm(point - start))
        )
        tolerance = 1e-12 * scale * scale
        return 0 if abs(cross) <= tolerance else (1 if cross > 0 else -1)

    left_right_start = orientation(left_start, left_end, right_start)
    left_right_end = orientation(left_start, left_end, right_end)
    right_left_start = orientation(right_start, right_end, left_start)
    right_left_end = orientation(right_start, right_end, left_end)
    intersects = (
        left_right_start * left_right_end < 0
        and right_left_start * right_left_end < 0
    ) or any((
        left_right_start == 0 and _point_on_segment_2d(right_start, left_start, left_end),
        left_right_end == 0 and _point_on_segment_2d(right_end, left_start, left_end),
        right_left_start == 0 and _point_on_segment_2d(left_start, right_start, right_end),
        right_left_end == 0 and _point_on_segment_2d(left_end, right_start, right_end),
    ))
    if intersects:
        return 0.0
    return min(
        _point_segment_distance_2d(left_start, right_start, right_end),
        _point_segment_distance_2d(left_end, right_start, right_end),
        _point_segment_distance_2d(right_start, left_start, left_end),
        _point_segment_distance_2d(right_end, left_start, left_end),
    )


def minimum_polyline_distance(left: np.ndarray, right: np.ndarray) -> float:
    left_xy, right_xy = np.asarray(left, dtype=float)[:, :2], np.asarray(right, dtype=float)[:, :2]
    if len(left_xy) < 2 or len(right_xy) < 2:
        raise RegistrationError("polyline distance requires two finite vertices per polyline")
    if not np.isfinite(left_xy).all() or not np.isfinite(right_xy).all():
        raise RegistrationError("polyline distance received non-finite coordinates")
    minimum = math.inf
    for left_start, left_end in zip(left_xy[:-1], left_xy[1:]):
        for right_start, right_end in zip(right_xy[:-1], right_xy[1:]):
            minimum = min(
                minimum,
                _segment_segment_distance_2d(
                    left_start, left_end, right_start, right_end
                ),
            )
            if minimum == 0.0:
                return 0.0
    return float(minimum)


def annotation_holdout_set_sha256(features: list[dict]) -> str:
    return canonical_hash([
        {
            "id": item["id"],
            "approach_id": item["approach_id"],
            "map_reference": item["map_reference"],
            "map_points": item["map_points"].tolist(),
            "lidar_tile_sha256": item["lidar_tile_sha256"],
            "lidar_point_indices": item["lidar_point_indices"],
            "physical_control_ids": item["physical_control_ids"],
            "lidar_points": item["lidar_points"].tolist(),
        }
        for item in features if item["split"] == "holdout"
    ])


def validate_annotation_review(annotation_path: Path, review_path: Path | None,
                               features: list[dict]) -> dict:
    if review_path is None:
        return {
            "present": False, "passed": False,
            "reasons": ["annotation_interreview_missing"],
            "holdout_set_sha256": annotation_holdout_set_sha256(features),
        }
    review_path = review_path.resolve()
    review = json.loads(review_path.read_text())
    if (
        review.get("schema") != ANNOTATION_REVIEW_SCHEMA
        or review.get("annotation_sha256") != sha256(annotation_path)
    ):
        raise RegistrationError("annotation inter-review binding is invalid")
    reviewers = review.get("reviewers")
    if not isinstance(reviewers, list) or len(reviewers) != 2:
        raise RegistrationError("annotation review requires exactly two reviewers")
    identities, organizations, reviewer_features = set(), set(), []
    expected_ids = {item["id"] for item in features}
    now = datetime.now(timezone.utc)
    for reviewer in reviewers:
        if not isinstance(reviewer, dict):
            raise RegistrationError("annotation reviewer record is malformed")
        identity, organization = reviewer.get("reviewer_id"), reviewer.get("organization")
        if (
            not isinstance(identity, str) or not identity or identity in identities
            or not isinstance(organization, str) or not organization
            or organization in organizations
        ):
            raise RegistrationError("annotation reviewers are not independent")
        reviewed = _parse_authority_time(
            reviewer.get("reviewed_at_utc"), "annotation review time"
        )
        age_days = (now - reviewed).total_seconds() / 86400
        if age_days < -(5.0 / 1440.0) or age_days > AUTHORITY_ATTESTATION_MAX_AGE_DAYS:
            raise RegistrationError("annotation review is not current")
        values = reviewer.get("features")
        if not isinstance(values, list):
            raise RegistrationError("annotation reviewer feature evidence is missing")
        by_id = {
            item.get("feature_id"): item for item in values if isinstance(item, dict)
        }
        if set(by_id) != expected_ids or len(by_id) != len(values):
            raise RegistrationError("annotation reviewer feature denominator is not exact")
        reviewer_features.append(by_id)
        identities.add(identity)
        organizations.add(organization)

    deviations = []
    for feature in features:
        feature_id = feature["id"]
        reviewed_arrays = []
        for by_id in reviewer_features:
            record = by_id[feature_id]
            map_points = np.asarray(record.get("map_xyz"), dtype=float)
            lidar_points = np.asarray(record.get("lidar_xyz"), dtype=float)
            if (
                map_points.shape != feature["map_points"].shape
                or lidar_points.shape != feature["lidar_points"].shape
                or not np.isfinite(map_points).all()
                or not np.isfinite(lidar_points).all()
            ):
                raise RegistrationError("annotation reviewer geometry shape is not exact")
            reviewed_arrays.append((map_points, lidar_points))
            deviations.extend(np.linalg.norm(
                map_points - feature["map_points"], axis=1
            ).tolist())
            deviations.extend(np.linalg.norm(
                lidar_points - feature["lidar_points"], axis=1
            ).tolist())
        deviations.extend(np.linalg.norm(
            reviewed_arrays[0][0] - reviewed_arrays[1][0], axis=1
        ).tolist())
        deviations.extend(np.linalg.norm(
            reviewed_arrays[0][1] - reviewed_arrays[1][1], axis=1
        ).tolist())
    maximum = max(deviations, default=math.inf)
    rmse = math.sqrt(float(np.mean(np.square(deviations)))) if deviations else math.inf
    if not math.isfinite(maximum) or maximum > ANNOTATION_REPEATABILITY_MAX_M:
        raise RegistrationError("annotation inter-review repeatability exceeds fixed limit")
    return {
        "present": True, "passed": True,
        "path": str(review_path), "sha256": sha256(review_path),
        "annotation_sha256": sha256(annotation_path),
        "reviewers": [
            {
                "reviewer_id": item["reviewer_id"],
                "organization": item["organization"],
                "reviewed_at_utc": item["reviewed_at_utc"],
            }
            for item in reviewers
        ],
        "feature_count": len(features),
        "repeatability_rmse_m": rmse,
        "repeatability_max_m": maximum,
        "repeatability_max_allowed_m": ANNOTATION_REPEATABILITY_MAX_M,
        "holdout_set_sha256": annotation_holdout_set_sha256(features),
        "reasons": [],
    }


def validate_holdout_ledger(annotation_path: Path, ledger_path: Path | None,
                            review: dict) -> dict:
    if ledger_path is None:
        return {
            "present": False, "passed": False,
            "reasons": ["holdout_evaluation_ledger_missing"],
        }
    ledger_path = ledger_path.resolve()
    ledger = json.loads(ledger_path.read_text())
    authorized = _parse_authority_time(
        ledger.get("authorized_at_utc"), "holdout authorization time"
    )
    expires = _parse_authority_time(
        ledger.get("expires_at_utc"), "holdout authorization expiry"
    )
    now = datetime.now(timezone.utc)
    receipt_value = ledger.get("burn_receipt_path")
    receipt_path = Path(receipt_value) if isinstance(receipt_value, str) else None
    registry_id = ledger.get("registry_id")
    prior_sequence = ledger.get("registry_prior_sequence")
    prior_head_sha256 = ledger.get("registry_prior_head_sha256")
    if (
        ledger.get("schema") != HOLDOUT_LEDGER_SCHEMA
        or ledger.get("annotation_sha256") != sha256(annotation_path)
        or ledger.get("holdout_set_sha256") != review.get("holdout_set_sha256")
        or not isinstance(ledger.get("evaluation_id"), str)
        or not ledger["evaluation_id"].strip()
        or ledger.get("purpose") != "final_acceptance"
        or ledger.get("prior_evaluation_count") != 0
        or ledger.get("maximum_evaluation_count") != 1
        or ledger.get("prior_evaluation_ids") != []
        or not isinstance(registry_id, str) or not registry_id.strip()
        or not isinstance(prior_sequence, int) or isinstance(prior_sequence, bool)
        or prior_sequence < 0
        or re.fullmatch(r"[0-9a-f]{64}", str(prior_head_sha256)) is None
        or receipt_path is None or not receipt_path.is_absolute()
        or receipt_path.parent != ledger_path.parent
        or receipt_path.suffix != ".json"
        or authorized > now
        or expires <= now
        or (expires - authorized).total_seconds() > 30 * 86400
    ):
        raise RegistrationError("holdout one-time evaluation ledger is invalid")
    if receipt_path.exists():
        raise RegistrationError("holdout evaluation authorization was already burned")
    return {
        "present": True, "passed": True,
        "path": str(ledger_path), "sha256": sha256(ledger_path),
        "evaluation_id": ledger["evaluation_id"],
        "annotation_sha256": ledger["annotation_sha256"],
        "holdout_set_sha256": ledger["holdout_set_sha256"],
        "registry_id": registry_id,
        "registry_prior_sequence": prior_sequence,
        "registry_prior_head_sha256": prior_head_sha256,
        "burn_receipt_path": str(receipt_path),
        "reasons": [],
    }


def validate_annotation_authority(annotation_path: Path, review: dict, ledger: dict,
                                  attestation_path: Path | None,
                                  signature_path: Path | None) -> dict | None:
    if attestation_path is None and signature_path is None:
        return None
    if not review.get("passed") or not ledger.get("passed"):
        raise RegistrationError(
            "annotation authority evidence requires review and holdout ledger"
        )
    attestation, evidence = _verify_detached_attestation(
        attestation_path, signature_path, TRUSTED_ANNOTATION_AUTHORITY_SIGNERS,
        ANNOTATION_AUTHORITY_ATTESTATION_SCHEMA, "annotation authority",
    )
    expected = {
        "annotation_sha256": sha256(annotation_path),
        "annotation_review_sha256": review["sha256"],
        "holdout_ledger_sha256": ledger["sha256"],
        "holdout_set_sha256": review["holdout_set_sha256"],
        "reviewers": review["reviewers"],
        "registry_id": ledger["registry_id"],
        "registry_prior_sequence": ledger["registry_prior_sequence"],
        "registry_prior_head_sha256": ledger["registry_prior_head_sha256"],
    }
    if any(attestation.get(key) != value for key, value in expected.items()):
        raise RegistrationError(
            "annotation authority attestation does not bind exact review evidence"
        )
    if attestation.get("verification_result") != {
        "annotation_status": "verified",
        "interreview_status": "independent",
        "holdout_status": "authorized_once",
    }:
        raise RegistrationError("annotation authority verification result is not accepted")
    if evidence["producer"] in {item["organization"] for item in review["reviewers"]}:
        raise RegistrationError("annotation authority is not independent from reviewers")
    return {**evidence, **expected, "verification_result": attestation["verification_result"]}


def consume_holdout_registry(ledger: dict, annotation_authority: dict,
                             registry_client=None) -> dict:
    registry_id = ledger["registry_id"]
    config = _holdout_registry_config(registry_id)
    client = registry_client or _new_holdout_registry_client(config)
    initial = _verify_holdout_registry_head(client.get_head(), registry_id)
    if (
        initial["producer"] == annotation_authority.get("producer")
        or initial["source"] == annotation_authority.get("source")
    ):
        raise RegistrationError(
            "holdout registry is not independent from annotation authority"
        )
    if (
        initial["head"]["sequence"] != ledger["registry_prior_sequence"]
        or initial["head_sha256"] != ledger["registry_prior_head_sha256"]
    ):
        raise RegistrationError(
            "holdout registry head does not match the signed authorization base"
        )
    tool_hash = sha256(Path(__file__))
    toolchain_lock_hash = sha256(
        Path(__file__).with_name("map_lidar_toolchain_lock.json")
    )
    request = {
        "schema": HOLDOUT_REGISTRY_CONSUME_SCHEMA,
        "registry_id": registry_id,
        "evaluation_id": ledger["evaluation_id"],
        "holdout_ledger_sha256": ledger["sha256"],
        "annotation_sha256": ledger["annotation_sha256"],
        "holdout_set_sha256": ledger["holdout_set_sha256"],
        "registration_tool_sha256": tool_hash,
        "toolchain_lock_sha256": toolchain_lock_hash,
        "expected_prior_sequence": ledger["registry_prior_sequence"],
        "expected_prior_head_sha256": ledger["registry_prior_head_sha256"],
        "annotation_authority_attestation_sha256": annotation_authority[
            "attestation_sha256"
        ],
        "request_nonce": secrets.token_hex(32),
        "requested_at_utc": utc_now(),
    }
    receipt = client.consume(request)
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema", "registry_id", "consume_request_sha256", "entry", "head_envelope"
    }:
        raise RegistrationError("holdout registry consume receipt is malformed")
    request_hash = canonical_hash(request)
    if (
        receipt.get("schema") != HOLDOUT_REGISTRY_RECEIPT_SCHEMA
        or receipt.get("registry_id") != registry_id
        or receipt.get("consume_request_sha256") != request_hash
    ):
        raise RegistrationError("holdout registry consume receipt binding is invalid")
    entry = receipt.get("entry")
    expected_entry_keys = {
        "schema", "registry_id", "sequence", "evaluation_id",
        "holdout_ledger_sha256", "annotation_sha256", "holdout_set_sha256",
        "registration_tool_sha256", "toolchain_lock_sha256",
        "annotation_authority_attestation_sha256",
        "prior_head_sha256", "request_nonce", "consumed_at_utc", "status",
    }
    expected_entry = {
        "schema": HOLDOUT_REGISTRY_ENTRY_SCHEMA,
        "registry_id": registry_id,
        "sequence": ledger["registry_prior_sequence"] + 1,
        "evaluation_id": ledger["evaluation_id"],
        "holdout_ledger_sha256": ledger["sha256"],
        "annotation_sha256": ledger["annotation_sha256"],
        "holdout_set_sha256": ledger["holdout_set_sha256"],
        "registration_tool_sha256": tool_hash,
        "toolchain_lock_sha256": toolchain_lock_hash,
        "annotation_authority_attestation_sha256": annotation_authority[
            "attestation_sha256"
        ],
        "prior_head_sha256": ledger["registry_prior_head_sha256"],
        "request_nonce": request["request_nonce"],
        "status": "consumed",
    }
    if (
        not isinstance(entry, dict) or set(entry) != expected_entry_keys
        or any(entry.get(key) != value for key, value in expected_entry.items())
    ):
        raise RegistrationError("holdout registry consumed entry binding is invalid")
    consumed = _parse_authority_time(
        entry.get("consumed_at_utc"), "holdout registry consumption time"
    )
    consumed_age = (datetime.now(timezone.utc) - consumed).total_seconds()
    if (
        consumed_age < -HOLDOUT_REGISTRY_HEAD_MAX_AGE_SECONDS
        or consumed_age > HOLDOUT_REGISTRY_HEAD_MAX_AGE_SECONDS
    ):
        raise RegistrationError("holdout registry consumed entry is stale")
    final = _verify_holdout_registry_head(receipt["head_envelope"], registry_id)
    if (
        final["head"]["sequence"] != entry["sequence"]
        or final["head"]["entry_sha256"] != canonical_hash(entry)
        or final["head"]["prior_head_sha256"] != ledger["registry_prior_head_sha256"]
    ):
        raise RegistrationError("holdout registry append chain is invalid")
    confirmed_entry = client.get_entry(entry["sequence"])
    if confirmed_entry != {"entry": entry}:
        raise RegistrationError("holdout registry consumed entry inclusion is not confirmed")
    confirmed_head = _verify_holdout_registry_head(client.get_head(), registry_id)
    if confirmed_head["head_sha256"] != final["head_sha256"]:
        raise RegistrationError("holdout registry consumed head confirmation changed")
    return {
        "registry_id": registry_id,
        "endpoint": config["base_url"],
        "consume_request_sha256": request_hash,
        "entry": entry,
        "entry_sha256": canonical_hash(entry),
        "head": final["head"],
        "head_sha256": final["head_sha256"],
        "head_signature_sha256": final["signature_sha256"],
        "signing_key_id": final["signing_key_id"],
        "confirmed": True,
    }


def burn_holdout_evaluation(ledger: dict, annotation_authority: dict,
                            registry_consumption: dict) -> dict:
    receipt_path = Path(ledger["burn_receipt_path"])
    receipt = {
        "schema": HOLDOUT_BURN_RECEIPT_SCHEMA,
        "burned_at_utc": utc_now(),
        "evaluation_id": ledger["evaluation_id"],
        "holdout_ledger_sha256": ledger["sha256"],
        "holdout_set_sha256": ledger["holdout_set_sha256"],
        "annotation_authority_attestation_sha256": annotation_authority[
            "attestation_sha256"
        ],
        "registry_id": registry_consumption["registry_id"],
        "registry_entry_sha256": registry_consumption["entry_sha256"],
        "registry_head_sha256": registry_consumption["head_sha256"],
        "registry_sequence": registry_consumption["entry"]["sequence"],
        "status": "evaluation_started_holdout_burned",
    }
    write_json_exclusive(receipt_path, receipt)
    return {
        **ledger,
        "burned": True,
        "burn_receipt_sha256": sha256(receipt_path),
        "registry_consumption": registry_consumption,
    }


def authorize_and_burn_holdout(review: dict, ledger: dict,
                               annotation_authority: dict | None,
                               registry_client=None) -> dict:
    """Fail closed before any CLI path can compute sealed holdout metrics."""
    if not review.get("passed"):
        raise RegistrationError(
            "refusing to evaluate sealed holdout without independent annotation review"
        )
    if not ledger.get("passed"):
        raise RegistrationError(
            "refusing to evaluate sealed holdout without one-time authorization ledger"
        )
    if annotation_authority is None:
        raise RegistrationError(
            "refusing to evaluate sealed holdout without authenticated annotation authority"
        )
    registry_consumption = consume_holdout_registry(
        ledger, annotation_authority, registry_client
    )
    return burn_holdout_evaluation(
        ledger, annotation_authority, registry_consumption
    )


def transform_points(points: np.ndarray, parameters: np.ndarray) -> np.ndarray:
    tx, ty, yaw, z_bias = parameters
    cosine, sine = math.cos(yaw), math.sin(yaw)
    output = points.copy()
    output[:, 0] = cosine * points[:, 0] - sine * points[:, 1] + tx
    output[:, 1] = sine * points[:, 0] + cosine * points[:, 1] + ty
    output[:, 2] = points[:, 2] + z_bias
    return output


def nearest_segments(source: np.ndarray, target: np.ndarray) -> dict:
    starts, vectors = target[:-1], np.diff(target, axis=0)
    horizontal = vectors[:, :2]
    lengths_squared = np.sum(horizontal * horizontal, axis=1)
    if np.any(lengths_squared <= 1e-18):
        raise RegistrationError("nearest-segment target contains zero-length geometry")
    delta = source[:, None, :2] - starts[None, :, :2]
    fractions = np.clip(
        np.sum(delta * horizontal[None, :, :], axis=2) / lengths_squared[None, :],
        0.0,
        1.0,
    )
    closest_xy = starts[None, :, :2] + fractions[:, :, None] * horizontal[None, :, :]
    squared = np.sum((source[:, None, :2] - closest_xy) ** 2, axis=2)
    selected = np.argmin(squared, axis=1)
    rows = np.arange(len(source))
    chosen_fraction = fractions[rows, selected]
    chosen_xy = closest_xy[rows, selected]
    chosen_z = starts[selected, 2] + chosen_fraction * vectors[selected, 2]
    tangents = horizontal[selected]
    tangents = tangents / np.linalg.norm(tangents, axis=1)[:, None]
    normals = np.column_stack((-tangents[:, 1], tangents[:, 0]))
    differences = source[:, :2] - chosen_xy
    return {
        "normal": np.sum(differences * normals, axis=1),
        "horizontal": np.linalg.norm(differences, axis=1),
        "vertical": source[:, 2] - chosen_z,
    }


def sampled_feature(feature: dict) -> tuple[np.ndarray, np.ndarray]:
    return resample_polyline(feature["map_points"]), resample_polyline(feature["lidar_points"])


def balanced_residuals(parameters: np.ndarray, features: list[dict]) -> np.ndarray:
    approaches = sorted({item["approach_id"] for item in features})
    per_approach = Counter(item["approach_id"] for item in features)
    residuals = []
    for feature in features:
        map_points, lidar_points = sampled_feature(feature)
        transformed = transform_points(map_points, parameters)
        forward = nearest_segments(transformed, lidar_points)
        reverse = nearest_segments(lidar_points, transformed)
        feature_weight = 1.0 / math.sqrt(len(approaches) * per_approach[feature["approach_id"]])
        for direction in (forward, reverse):
            count_weight = feature_weight / math.sqrt(len(direction["normal"]))
            residuals.extend(direction["normal"] * count_weight)
            residuals.extend(direction["vertical"] * count_weight)
    return np.asarray(residuals, dtype=float)


def parameter_bounds(initial: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    radii = np.asarray([
        TRANSLATION_BOUND_RADIUS_M,
        TRANSLATION_BOUND_RADIUS_M,
        math.radians(YAW_BOUND_RADIUS_DEG),
        Z_BIAS_BOUND_RADIUS_M,
    ])
    return initial - radii, initial + radii


def deterministic_seeds(initial: np.ndarray, lower: np.ndarray, upper: np.ndarray) -> list[np.ndarray]:
    epsilon = np.maximum((upper - lower) * 1e-8, 1e-10)
    seeds = [np.clip(initial, lower + epsilon, upper - epsilon)]
    for axis in range(len(initial)):
        for boundary in (lower, upper):
            seed = seeds[0].copy()
            seed[axis] = boundary[axis] + epsilon[axis] if boundary is lower else boundary[axis] - epsilon[axis]
            seeds.append(seed)

    def radical_inverse(index, base):
        value, factor = 0.0, 1.0 / base
        while index:
            value += factor * (index % base)
            index //= base
            factor /= base
        return value

    for index in range(1, 9):
        fractions = np.asarray([
            radical_inverse(index, base) for base in (2, 3, 5, 7)
        ])
        seeds.append(lower + epsilon + fractions * (upper - lower - 2 * epsilon))
    return seeds


def cluster_solution_basins(solutions: list, translation_m=0.05,
                            yaw_deg=0.05, z_m=0.05) -> list[dict]:
    clusters = []
    for result in sorted(solutions, key=lambda item: item.cost):
        selected = None
        for cluster in clusters:
            representative = cluster["representative"]
            translation = math.hypot(
                result.x[0] - representative.x[0], result.x[1] - representative.x[1]
            )
            yaw = abs(math.degrees(math.atan2(
                math.sin(result.x[2] - representative.x[2]),
                math.cos(result.x[2] - representative.x[2]),
            )))
            z = abs(result.x[3] - representative.x[3])
            if translation <= translation_m and yaw <= yaw_deg and z <= z_m:
                selected = cluster
                break
        if selected is None:
            selected = {"representative": result, "members": []}
            clusters.append(selected)
        selected["members"].append(result)
    return [{
        "minimum_cost": float(cluster["representative"].cost),
        "representative_parameters": cluster["representative"].x.tolist(),
        "member_count": len(cluster["members"]),
        "member_costs": [float(item.cost) for item in cluster["members"]],
    } for cluster in clusters]


def solve(features: list[dict], initial: np.ndarray, multi_start: bool = True) -> dict:
    lower, upper = parameter_bounds(initial)
    seeds = deterministic_seeds(initial, lower, upper) if multi_start else [initial]
    solutions = []
    for seed in seeds:
        result = least_squares(
            balanced_residuals,
            seed,
            args=(features,),
            bounds=(lower, upper),
            method="trf",
            loss="soft_l1",
            f_scale=0.10,
            x_scale=np.asarray([1.0, 1.0, math.radians(1.0), 1.0]),
            max_nfev=500,
            ftol=1e-12,
            xtol=1e-12,
            gtol=1e-12,
        )
        solutions.append(result)
    successful = [item for item in solutions if item.success and np.isfinite(item.cost)]
    if not successful:
        raise RegistrationError("all deterministic registration starts failed")
    best = min(successful, key=lambda item: item.cost)
    basin_clusters = cluster_solution_basins(successful)
    singular = np.linalg.svd(best.jac, compute_uv=False)
    rank = int(np.linalg.matrix_rank(best.jac))
    condition = float(singular[0] / singular[-1]) if len(singular) and singular[-1] > 0 else None
    dof = max(1, len(best.fun) - len(best.x))
    covariance = None
    if rank == len(best.x):
        covariance = (np.linalg.pinv(best.jac.T @ best.jac) * (2.0 * best.cost / dof)).tolist()
    span = upper - lower
    bound_hits = [
        name for name, value, low, high, width in zip(
            ("tx_m", "ty_m", "yaw_rad", "z_bias_m"), best.x, lower, upper, span
        ) if min(value - low, high - value) <= BOUND_PROXIMITY_FRACTION * width
    ]
    alternatives = []
    cost_limit = best.cost * (1.0 + NEAR_OPTIMAL_COST_FRACTION) + 1e-12
    for cluster in basin_clusters:
        candidate = np.asarray(cluster["representative_parameters"], dtype=float)
        if np.allclose(candidate, best.x, atol=1e-12, rtol=0) or cluster["minimum_cost"] > cost_limit:
            continue
        translation = math.hypot(candidate[0] - best.x[0], candidate[1] - best.x[1])
        yaw = abs(math.degrees(math.atan2(
            math.sin(candidate[2] - best.x[2]), math.cos(candidate[2] - best.x[2])
        )))
        z = abs(candidate[3] - best.x[3])
        if (
            translation > FOLD_TRANSLATION_SPREAD_MAX_M
            or yaw > FOLD_YAW_SPREAD_MAX_DEG
            or z > VERTICAL_RMSE_MAX_M
        ):
            alternatives.append({
                "cost": cluster["minimum_cost"],
                "translation_separation_m": translation,
                "yaw_separation_deg": yaw,
                "z_separation_m": z,
                "parameters": candidate.tolist(),
            })
    seed_values = np.asarray(seeds)
    seed_coverage = {
        name: {
            "minimum": float(np.min(seed_values[:, index])),
            "maximum": float(np.max(seed_values[:, index])),
            "lower_bound": float(lower[index]),
            "upper_bound": float(upper[index]),
        }
        for index, name in enumerate(("tx_m", "ty_m", "yaw_rad", "z_bias_m"))
    }
    seed_bounds_covered = all(
        values["minimum"] - values["lower_bound"] <= (values["upper_bound"] - values["lower_bound"]) * 2e-8
        and values["upper_bound"] - values["maximum"] <= (values["upper_bound"] - values["lower_bound"]) * 2e-8
        for values in seed_coverage.values()
    )
    return {
        "x": best.x,
        "cost": float(best.cost),
        "success": bool(best.success),
        "message": str(best.message),
        "nfev": int(best.nfev),
        "jacobian_rank": rank,
        "jacobian_singular_values": singular.tolist(),
        "jacobian_condition": condition,
        "covariance": covariance,
        "bound_hits": bound_hits,
        "near_optimal_separated_modes": alternatives,
        "basin_clusters": basin_clusters,
        "seed_coverage": seed_coverage,
        "seed_bounds_covered": seed_bounds_covered,
        "starts": [
            {"cost": float(item.cost), "success": bool(item.success), "parameters": item.x.tolist()}
            for item in solutions
        ],
    }


def metric_summary(horizontal: list[float], vertical: list[float]) -> dict:
    return {
        "sample_count": len(horizontal),
        "horizontal_rmse_m": math.sqrt(float(np.mean(np.square(horizontal)))) if horizontal else None,
        "horizontal_max_m": max(horizontal) if horizontal else None,
        "symmetric_hausdorff_m": max(horizontal) if horizontal else None,
        "vertical_rmse_m": math.sqrt(float(np.mean(np.square(vertical)))) if vertical else None,
        "vertical_p95_m": percentile([abs(value) for value in vertical], 0.95),
        "vertical_max_m": max((abs(value) for value in vertical), default=None),
    }


def feature_distances(feature: dict, parameters: np.ndarray) -> tuple[list[float], list[float]]:
    map_points, lidar_points = sampled_feature(feature)
    transformed = transform_points(map_points, parameters)
    forward, reverse = nearest_segments(transformed, lidar_points), nearest_segments(lidar_points, transformed)
    return (
        list(forward["horizontal"]) + list(reverse["horizontal"]),
        list(forward["vertical"]) + list(reverse["vertical"]),
    )


def metrics_for_features(features: list[dict], parameters: np.ndarray,
                         initial: np.ndarray) -> dict:
    horizontal, vertical = [], []
    per_feature, per_approach_raw = {}, defaultdict(lambda: ([], []))
    for feature in features:
        h_after, v_after = feature_distances(feature, parameters)
        h_before, v_before = feature_distances(feature, initial)
        horizontal.extend(h_after)
        vertical.extend(v_after)
        approach_h, approach_v = per_approach_raw[feature["approach_id"]]
        approach_h.extend(h_after)
        approach_v.extend(v_after)
        after, before = metric_summary(h_after, v_after), metric_summary(h_before, v_before)
        per_feature[feature["id"]] = {
            "approach_id": feature["approach_id"],
            "split": feature["split"],
            "kind": feature["kind"],
            "map_reference": feature["map_reference"],
            "after": after,
            "before": before,
            "horizontal_rmse_delta_m": after["horizontal_rmse_m"] - before["horizontal_rmse_m"],
            "vertical_rmse_delta_m": after["vertical_rmse_m"] - before["vertical_rmse_m"],
        }
    return {
        "global": metric_summary(horizontal, vertical),
        "per_feature": per_feature,
        "per_approach": {
            key: metric_summary(values[0], values[1]) for key, values in sorted(per_approach_raw.items())
        },
    }


def absolute_metric_failures(prefix: str, metrics: dict) -> list[str]:
    checks = (
        ("horizontal_rmse", metrics["horizontal_rmse_m"], HORIZONTAL_RMSE_MAX_M),
        ("horizontal_max", metrics["horizontal_max_m"], HORIZONTAL_MAX_M),
        ("symmetric_hausdorff", metrics["symmetric_hausdorff_m"], HAUSDORFF_MAX_M),
        ("vertical_rmse", metrics["vertical_rmse_m"], VERTICAL_RMSE_MAX_M),
        ("vertical_p95", metrics["vertical_p95_m"], VERTICAL_P95_MAX_M),
        ("vertical_max", metrics["vertical_max_m"], VERTICAL_MAX_M),
    )
    return [f"{prefix}_{name}" for name, value, limit in checks if value is None or value > limit]


def leave_one_approach_out(fit: list[dict], initial: np.ndarray,
                           full_parameters: np.ndarray) -> dict:
    folds, failures = [], []
    for approach in sorted({item["approach_id"] for item in fit}):
        training = [item for item in fit if item["approach_id"] != approach]
        omitted = [item for item in fit if item["approach_id"] == approach]
        try:
            solution = solve(training, initial, multi_start=False)
            parameters = solution["x"]
            translation_delta = math.hypot(
                parameters[0] - full_parameters[0], parameters[1] - full_parameters[1]
            )
            yaw_delta = abs(math.degrees(math.atan2(
                math.sin(parameters[2] - full_parameters[2]),
                math.cos(parameters[2] - full_parameters[2]),
            )))
            folds.append({
                "omitted_approach_id": approach,
                "training_feature_ids": [item["id"] for item in training],
                "evaluation_feature_ids": [item["id"] for item in omitted],
                "parameters": parameters.tolist(),
                "translation_delta_m": translation_delta,
                "yaw_delta_deg": yaw_delta,
                "omitted_metrics": metrics_for_features(omitted, parameters, initial)["global"],
                "jacobian_rank": solution["jacobian_rank"],
                "jacobian_condition": solution["jacobian_condition"],
                "bound_hits": solution["bound_hits"],
            })
        except (RegistrationError, ValueError) as exc:
            failures.append({"omitted_approach_id": approach, "error": str(exc)})
    return {
        "folds": folds,
        "failures": failures,
        "translation_spread_m": max((item["translation_delta_m"] for item in folds), default=None),
        "yaw_spread_deg": max((item["yaw_delta_deg"] for item in folds), default=None),
    }


def resolve_map_control(geometry: dict, reference: dict, label: str) -> np.ndarray:
    collection = reference.get("collection")
    feature_id = reference.get("feature_id")
    field = reference.get("point_field")
    index = reference.get("vertex_index")
    if collection not in {"lanes", "crosswalks", "road_mark_segments", "objects"}:
        raise RegistrationError(f"{label}: survey map collection is invalid")
    features = geometry.get("geometry", {}).get(collection)
    matches = [item for item in features or [] if item.get("id") == feature_id]
    if len(matches) != 1 or not isinstance(field, str) or field not in matches[0]:
        raise RegistrationError(f"{label}: survey map feature identity is unavailable")
    value = np.asarray(matches[0][field], dtype=float)
    if value.shape == (3,):
        if index not in (None, 0):
            raise RegistrationError(f"{label}: scalar map point cannot use a vertex index")
        point = value
    else:
        if value.ndim != 2 or value.shape[1] != 3 or not isinstance(index, int):
            raise RegistrationError(f"{label}: survey map point reference is malformed")
        if index < 0 or index >= len(value):
            raise RegistrationError(f"{label}: survey map vertex index is out of range")
        point = value[index]
    if not np.isfinite(point).all():
        raise RegistrationError(f"{label}: survey map point is non-finite")
    return point


def eligible_stable_landmark(item: dict) -> bool:
    semantic = item.get("semantic_source") if isinstance(item, dict) else None
    if not isinstance(semantic, dict) or set(semantic) != {
        "schema", "api", "native_type", "native_subtype"
    }:
        return False
    category = STABLE_NATIVE_TYPES.get(semantic.get("native_type"))
    source_id = item.get("source_object_id")
    return bool(
        semantic.get("schema") == NATIVE_OBJECT_SEMANTIC_SCHEMA
        and semantic.get("api") == NATIVE_OBJECT_API
        and semantic.get("native_subtype") is None
        and category is not None
        and item.get("category") == category
        and isinstance(source_id, str) and source_id
        and item.get("id") == f"environment-{category}-{source_id}"
    )


def _weighted_se2(map_xy: np.ndarray, surveyed_xy: np.ndarray,
                  uncertainty_m: np.ndarray) -> np.ndarray:
    weights = 1.0 / np.square(uncertainty_m)
    weights /= np.sum(weights)
    map_center = np.sum(map_xy * weights[:, None], axis=0)
    survey_center = np.sum(surveyed_xy * weights[:, None], axis=0)
    source = map_xy - map_center
    target = surveyed_xy - survey_center
    covariance = source.T @ (weights[:, None] * target)
    left, _, right_transpose = np.linalg.svd(covariance)
    rotation = right_transpose.T @ left.T
    if np.linalg.det(rotation) < 0:
        right_transpose[-1, :] *= -1
        rotation = right_transpose.T @ left.T
    yaw = math.atan2(rotation[1, 0], rotation[0, 0])
    translation = survey_center - rotation @ map_center
    return np.asarray([translation[0], translation[1], yaw], dtype=float)


def _survey_residual_metrics(controls: list[dict], parameters: np.ndarray) -> dict:
    map_xy = np.asarray([item["map_xyz"][:2] for item in controls])
    survey_xy = np.asarray([item["survey_xy"] for item in controls])
    transformed = transform_points(
        np.column_stack((map_xy, np.zeros(len(map_xy)))),
        np.asarray([parameters[0], parameters[1], parameters[2], 0.0]),
    )[:, :2]
    residuals = np.linalg.norm(transformed - survey_xy, axis=1)
    return {
        "control_count": len(controls),
        "control_ids": [item["id"] for item in controls],
        "horizontal_rmse_m": math.sqrt(float(np.mean(np.square(residuals)))),
        "horizontal_max_m": float(np.max(residuals)),
        "residuals_m": {item["id"]: float(value) for item, value in zip(controls, residuals)},
    }


def _validate_survey_crs(block: dict, horizontal_epsg: int | None,
                         horizontal_wkt: str | None = None,
                         coordinate_epoch: float | None = None) -> dict:
    from pyproj import CRS

    if not isinstance(block, dict) or horizontal_epsg is None:
        raise RegistrationError("current horizontal survey CRS block is missing")
    try:
        declared = CRS.from_wkt(block["wkt"])
        expected = CRS.from_wkt(horizontal_wkt) if horizontal_wkt else CRS.from_epsg(horizontal_epsg)
    except (KeyError, TypeError, ValueError) as exc:
        raise RegistrationError("current horizontal survey CRS is malformed") from exc
    axes = declared.axis_info
    metre_axes = len(axes) >= 2 and all(
        math.isclose(float(axis.unit_conversion_factor), 1.0, abs_tol=1e-12)
        for axis in axes[:2]
    )
    if (
        not declared.is_projected
        or not declared.equals(expected)
        or declared.to_epsg() != horizontal_epsg
        or block.get("epsg") != horizontal_epsg
        or not metre_axes
    ):
        raise RegistrationError("current horizontal survey CRS does not match metre LiDAR CRS")
    unit_names = [axis.unit_name for axis in axes[:2]]
    if block.get("linear_units") not in set(unit_names) or len(set(unit_names)) != 1:
        raise RegistrationError("current horizontal survey linear units are not exact")
    datum = declared.datum.name if declared.datum is not None else None
    if not datum or block.get("datum") != datum:
        raise RegistrationError("current horizontal survey datum is not exact")
    if "coordinate_epoch" not in block or block.get("coordinate_epoch") != coordinate_epoch:
        raise RegistrationError("current horizontal survey coordinate epoch is not exact")
    return {
        "epsg": horizontal_epsg, "wkt": declared.to_wkt(),
        "linear_units": unit_names[0], "datum": datum,
        "coordinate_epoch": coordinate_epoch,
    }


def _pdf_ascii_padding_only(content: bytes) -> bool:
    """Accept only PDF whitespace and printable comments between objects/xref."""
    index = 0
    while index < len(content):
        value = content[index]
        if value in b" \t\f\r\n":
            index += 1
            continue
        if value != ord("%"):
            return False
        index += 1
        while index < len(content) and content[index] not in b"\r\n":
            value = content[index]
            if value != ord("\t") and not 0x20 <= value <= 0x7E:
                return False
            index += 1
        if index == len(content):
            return False
    return True


def _read_terminal_pdf_object(
    content: bytes, reader, offset: int, label: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[object, int]:
    """Parse one indirect object and return its trusted ``endobj`` boundary."""
    from pypdf.generic import read_object

    stream = io.BytesIO(content)
    stream.seek(offset)
    object_identity = reader.read_object_header(stream)
    if expected_identity is not None and object_identity != expected_identity:
        raise RegistrationError(
            f"{label} terminal PDF object does not match its cross-reference entry"
        )
    parsed = read_object(stream, reader)

    marker = stream.tell()
    while marker < len(content) and content[marker] in b" \t\f\r\n":
        marker += 1
    while marker < len(content) and content[marker] == ord("%"):
        line_end = marker + 1
        while line_end < len(content) and content[line_end] not in b"\r\n":
            value = content[line_end]
            if value != ord("\t") and not 0x20 <= value <= 0x7E:
                raise RegistrationError(
                    f"{label} terminal PDF object has non-printable lexical padding"
                )
            line_end += 1
        if line_end == len(content):
            raise RegistrationError(f"{label} terminal PDF object is not closed")
        marker = line_end
        while marker < len(content) and content[marker] in b" \t\f\r\n":
            marker += 1
    if content[marker:marker + len(b"endobj")] != b"endobj":
        raise RegistrationError(f"{label} terminal PDF object is not closed")
    return parsed, marker + len(b"endobj")


def _read_final_pdf_trailer_end(
    content: bytes, reader, xref_offset: int, label: str,
) -> int:
    """Strictly reparse the final classic xref table and its trailer."""
    from pypdf._utils import read_non_whitespace
    from pypdf.generic import read_object

    stream = io.BytesIO(content)
    # PdfReader has already consumed the leading ``x`` when it dispatches to
    # this parser, whose first required token is therefore ``ref``.
    stream.seek(xref_offset + 1)
    reader._read_standard_xref_table(stream)
    read_non_whitespace(stream)
    stream.seek(-1, 1)
    trailer = read_object(stream, reader)
    if not hasattr(trailer, "get") or trailer.get("/Size") is None:
        raise RegistrationError(f"{label} final PDF trailer dictionary is invalid")
    return stream.tell()


def _validate_pdf_container_boundary(content: bytes, label: str, reader) -> None:
    tail = re.search(
        rb"startxref[ \t\f\r\n]+([0-9]+)[ \t\f\r\n]+%%EOF[ \t\f\r\n]*\Z",
        content,
    )
    if tail is None:
        raise RegistrationError(f"{label} has no exact final PDF cross-reference boundary")
    xref_offset = int(tail.group(1))
    if not 0 < xref_offset < tail.start():
        raise RegistrationError(f"{label} final PDF cross-reference offset is invalid")
    target = content[xref_offset:]
    xref_table = re.match(rb"xref(?:[ \t\f\r\n])", target) is not None
    xref_stream = re.match(
        rb"[1-9][0-9]*[ \t\f\r\n]+[0-9]+[ \t\f\r\n]+obj(?:[ \t\f\r\n]|<<)",
        target,
    ) is not None
    if not (xref_table or xref_stream):
        raise RegistrationError(f"{label} startxref does not name a PDF xref section")

    if xref_table:
        candidates = [
            (offset, object_number, generation)
            for generation, entries in reader.xref.items()
            for object_number, offset in entries.items()
            if 0 < offset < xref_offset
        ]
        if not candidates:
            raise RegistrationError(f"{label} has no terminal cross-referenced PDF object")
        object_offset, object_number, generation = max(candidates)
        _, terminal_object_end = _read_terminal_pdf_object(
            content,
            reader,
            object_offset,
            label,
            expected_identity=(object_number, generation),
        )
        trailer_end = _read_final_pdf_trailer_end(
            content, reader, xref_offset, label
        )
        padding_ranges = (
            content[terminal_object_end:xref_offset],
            content[trailer_end:tail.start()],
        )
    else:
        parsed_xref, terminal_object_end = _read_terminal_pdf_object(
            content, reader, xref_offset, label
        )
        if not hasattr(parsed_xref, "get") or str(parsed_xref.get("/Type")) != "/XRef":
            raise RegistrationError(f"{label} startxref object is not an xref stream")
        padding_ranges = (content[terminal_object_end:tail.start()],)

    if any(
        not padding or not _pdf_ascii_padding_only(padding)
        for padding in padding_ranges
    ):
        raise RegistrationError(
            f"{label} contains foreign/polyglot bytes before the final PDF xref"
        )

    # ZIP readers intentionally accept leading and trailing bytes.  Detecting a
    # second successful whole-file interpretation is therefore required in
    # addition to checking PDF lexical padding around the final xref.
    if zipfile.is_zipfile(io.BytesIO(content)):
        raise RegistrationError(f"{label} is also a parseable ZIP archive")


def _pdf_has_embedded_payloads(root, pages: list) -> bool:
    def resolved(value):
        return value.get_object() if hasattr(value, "get_object") else value

    if root.get("/AF") is not None or root.get("/Collection") is not None:
        return True
    names = resolved(root.get("/Names"))
    if isinstance(names, dict) and names.get("/EmbeddedFiles") is not None:
        return True
    for page in pages:
        if page.get("/AF") is not None:
            return True
        annotations = resolved(page.get("/Annots"))
        if annotations is None:
            continue
        for reference in annotations:
            annotation = resolved(reference)
            if isinstance(annotation, dict) and (
                str(annotation.get("/Subtype")) == "/FileAttachment"
                or annotation.get("/FS") is not None
            ):
                return True
    return False


def _strict_pdf_evidence(content: bytes, label: str) -> dict:
    if (
        len(content) < MIN_AUTHORITY_PDF_BYTES
        or len(content) > MAX_AUTHORITY_PDF_BYTES
        or not content.startswith(b"%PDF-")
        or not content.rstrip().endswith(b"%%EOF")
        or b"startxref" not in content
    ):
        raise RegistrationError(f"{label} is not a complete bounded PDF document")
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise RegistrationError("strict pinned PDF parser is unavailable") from exc
    warning_log = io.StringIO()
    handler = logging.StreamHandler(warning_log)
    pdf_logger = logging.getLogger("pypdf")
    old_level, old_propagate = pdf_logger.level, pdf_logger.propagate
    pdf_logger.setLevel(logging.WARNING)
    pdf_logger.propagate = False
    pdf_logger.addHandler(handler)
    recorded = []
    try:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            reader = PdfReader(io.BytesIO(content), strict=True)
            _validate_pdf_container_boundary(content, label, reader)
            if reader.is_encrypted:
                raise RegistrationError(f"{label} is encrypted")
            trailer = reader.trailer
            root_reference = trailer.get("/Root") if trailer is not None else None
            root = root_reference.get_object() if root_reference is not None else None
            if root is None or str(root.get("/Type")) != "/Catalog":
                raise RegistrationError(f"{label} has no valid trailer root Catalog")
            if not reader.xref and not reader.xref_objStm:
                raise RegistrationError(f"{label} has no parsed cross-reference table")
            pages = list(reader.pages)
            if not pages or any(str(page.get("/Type")) != "/Page" for page in pages):
                raise RegistrationError(f"{label} has no valid PDF page tree")
            if _pdf_has_embedded_payloads(root, pages):
                raise RegistrationError(f"{label} contains embedded foreign payloads")
            recorded = list(captured)
    except RegistrationError:
        raise
    except (PdfReadError, OSError, TypeError, ValueError, KeyError) as exc:
        raise RegistrationError(f"{label} failed strict PDF parsing") from exc
    except Exception as exc:
        # Parser/library failures must remain controlled audit failures rather
        # than escaping the deliverable-validation boundary in a new type.
        raise RegistrationError(f"{label} failed strict PDF parsing") from exc
    finally:
        pdf_logger.removeHandler(handler)
        pdf_logger.setLevel(old_level)
        pdf_logger.propagate = old_propagate
    if recorded or warning_log.getvalue().strip():
        raise RegistrationError(f"{label} emitted strict PDF parser warnings")
    return {
        "parser": "pypdf",
        "parser_version": importlib_metadata.version("pypdf"),
        "strict": True,
        "page_count": len(pages),
        "xref_sections": len(reader.xref),
        "object_stream_entries": len(reader.xref_objStm),
        "encrypted": False,
        "container_ambiguity_checked": True,
        "embedded_payloads": False,
    }


def _survey_deliverable_evidence(survey: dict, deliverable_paths: list[Path] | None,
                                 observations_path: Path | None) -> dict:
    paths = [Path(value).resolve() for value in (deliverable_paths or [])]
    if observations_path is None or not paths:
        raise RegistrationError("licensed survey raw deliverables are required")
    observations_path = observations_path.resolve()
    if observations_path.suffix.lower() != ".csv" or observations_path not in paths:
        raise RegistrationError("licensed survey observations must be a retained CSV deliverable")
    if any(path.suffix.lower() == ".json" or not path.is_file() for path in paths):
        raise RegistrationError("self-authored JSON is not a licensed survey raw deliverable")
    if len(paths) != len(set(paths)):
        raise RegistrationError("licensed survey raw deliverable paths are duplicated")
    declarations = survey.get("raw_deliverables")
    if not isinstance(declarations, list) or len(declarations) != len(paths):
        raise RegistrationError("licensed survey raw deliverable manifest is incomplete")
    declared_by_hash = {}
    for item in declarations:
        if not isinstance(item, dict) or item.get("role") not in SURVEY_DELIVERABLE_ROLES:
            raise RegistrationError("licensed survey deliverable role is invalid")
        identity = item.get("sha256")
        if not isinstance(identity, str) or identity in declared_by_hash:
            raise RegistrationError("licensed survey deliverable hash is invalid")
        declared_by_hash[identity] = item
    actual = []
    for path in paths:
        file_status = path.stat()
        if (
            not stat.S_ISREG(file_status.st_mode) or file_status.st_size <= 0
            or file_status.st_size > MAX_SURVEY_DELIVERABLE_BYTES
        ):
            raise RegistrationError(
                "licensed survey raw deliverable is not a bounded regular file"
            )
        content = path.read_bytes()
        if len(content) != file_status.st_size:
            raise RegistrationError(
                "licensed survey raw deliverable changed size while being read"
            )
        digest = hashlib.sha256(content).hexdigest()
        declared = declared_by_hash.get(digest)
        try:
            declared_bytes = int((declared or {}).get("bytes", -1))
        except (TypeError, ValueError) as exc:
            raise RegistrationError(
                "licensed survey raw deliverable byte count is malformed"
            ) from exc
        if not content or declared is None or declared.get("file_name") != path.name or declared_bytes != len(content):
            raise RegistrationError("licensed survey raw deliverable hash/size/name mismatch")
        pdf_validation = None
        if declared["role"] in {"survey_license", "instrument_calibration"}:
            if path.suffix.lower() != ".pdf":
                raise RegistrationError(
                    "licensed survey authority evidence is not a substantive retained PDF"
                )
            pdf_validation = _strict_pdf_evidence(
                content, f"licensed survey {declared['role']} deliverable"
            )
        actual.append({
            "path": str(path), "file_name": path.name, "sha256": digest,
            "bytes": len(content), "role": declared["role"],
            **({"pdf_validation": pdf_validation} if pdf_validation else {}),
        })
    roles = {item["role"] for item in actual}
    if roles != SURVEY_DELIVERABLE_ROLES:
        raise RegistrationError("licensed survey deliverable roles are incomplete")
    observation_digest = sha256(observations_path)
    observation_binding = survey.get("observations")
    try:
        observation_bytes = int((observation_binding or {}).get("bytes", -1))
    except (TypeError, ValueError) as exc:
        raise RegistrationError("licensed survey observation byte count is malformed") from exc
    if (
        not isinstance(observation_binding, dict)
        or observation_binding.get("format") != "csv"
        or observation_binding.get("sha256") != observation_digest
        or observation_binding.get("file_name") != observations_path.name
        or observation_bytes != observations_path.stat().st_size
        or declared_by_hash[observation_digest].get("role") != "raw_observations"
    ):
        raise RegistrationError("licensed survey observation binding mismatch")
    return {
        "deliverables": sorted(actual, key=lambda item: item["sha256"]),
        "observations_path": str(observations_path),
        "observations_sha256": observation_digest,
    }


def _survey_identity(survey: dict, deliverable_evidence: dict) -> dict:
    source = survey.get("licensed_source")
    if not isinstance(source, dict):
        raise RegistrationError("licensed survey source identity is missing")
    surveyor, instrument = source.get("surveyor"), source.get("instrument")
    required_source = (source.get("provider"), source.get("source_id"), source.get("project_id"))
    if any(not isinstance(value, str) or not value.strip() for value in required_source):
        raise RegistrationError("licensed survey provider/source/project identity is incomplete")
    if not isinstance(surveyor, dict) or any(
        not isinstance(surveyor.get(key), str) or not surveyor[key].strip()
        for key in ("name", "license_number", "licensing_authority")
    ):
        raise RegistrationError("licensed surveyor identity is incomplete")
    if not isinstance(instrument, dict) or any(
        not isinstance(instrument.get(key), str) or not instrument[key].strip()
        for key in ("manufacturer", "model", "serial_number")
    ):
        raise RegistrationError("licensed survey instrument identity is incomplete")
    hashes_by_role = {
        item["role"]: item["sha256"] for item in deliverable_evidence["deliverables"]
    }
    if (
        source.get("survey_license_deliverable_sha256") != hashes_by_role["survey_license"]
        or instrument.get("calibration_deliverable_sha256")
        != hashes_by_role["instrument_calibration"]
    ):
        raise RegistrationError("licensed survey authority/instrument evidence is not hash-bound")
    return {
        "provider": source["provider"], "source_id": source["source_id"],
        "project_id": source["project_id"],
        "surveyor_name": surveyor["name"],
        "surveyor_license": surveyor["license_number"],
        "licensing_authority": surveyor["licensing_authority"],
        "instrument": {
            "manufacturer": instrument["manufacturer"], "model": instrument["model"],
            "serial_number": instrument["serial_number"],
            "calibration_deliverable_sha256": instrument["calibration_deliverable_sha256"],
        },
    }


def _verify_survey_authority_attestation(survey_path: Path,
                                         deliverable_evidence: dict,
                                         source_identity: dict,
                                         attestation_path: Path | None,
                                         signature_path: Path | None) -> dict:
    attestation, evidence = _verify_detached_attestation(
        attestation_path, signature_path, TRUSTED_SURVEY_AUTHORITY_SIGNERS,
        SURVEY_AUTHORITY_ATTESTATION_SCHEMA, "survey authority",
    )
    expected_identity = {
        "provider": source_identity["provider"],
        "source_id": source_identity["source_id"],
        "project_id": source_identity["project_id"],
        "surveyor": {
            "name": source_identity["surveyor_name"],
            "license_number": source_identity["surveyor_license"],
            "licensing_authority": source_identity["licensing_authority"],
        },
        "instrument": {
            "manufacturer": source_identity["instrument"]["manufacturer"],
            "model": source_identity["instrument"]["model"],
            "serial_number": source_identity["instrument"]["serial_number"],
        },
    }
    expected_deliverables = sorted([
        {
            key: item[key]
            for key in ("role", "file_name", "sha256", "bytes")
        }
        for item in deliverable_evidence["deliverables"]
    ], key=lambda item: item["role"])
    if (
        attestation.get("survey_manifest_sha256") != sha256(survey_path)
        or attestation.get("licensed_source") != expected_identity
        or attestation.get("deliverables") != expected_deliverables
        or attestation.get("verification_result") != {
            "provider_status": "verified",
            "surveyor_license_status": "active",
            "instrument_calibration_status": "valid",
            "deliverable_integrity_status": "verified",
        }
    ):
        raise RegistrationError(
            "survey authority attestation does not bind the exact verified evidence"
        )
    if (
        evidence["producer"] == source_identity["provider"]
        or evidence["source"] == source_identity["source_id"]
    ):
        raise RegistrationError(
            "survey authority attestation is not independent from the survey source"
        )
    return {
        **evidence,
        "survey_manifest_sha256": attestation["survey_manifest_sha256"],
        "verification_result": attestation["verification_result"],
    }


def validate_current_survey(path: Path | None, geometry: dict, geometry_hash: str,
                            opendrive_hash: str, horizontal_epsg: int | None,
                            deliverable_paths: list[Path] | None = None,
                            observations_path: Path | None = None,
                            horizontal_wkt: str | None = None,
                            coordinate_epoch: float | None = None,
                            authority_attestation_path: Path | None = None,
                            authority_signature_path: Path | None = None) -> dict:
    if path is None:
        return {"present": False, "passed": False, "reasons": ["current_horizontal_survey_missing"]}
    survey = json.loads(path.read_text())
    reasons = []
    if survey.get("schema") != SURVEY_SCHEMA:
        reasons.append("current_horizontal_survey_schema")
    if survey.get("geometry_sha256") != geometry_hash:
        reasons.append("current_horizontal_survey_geometry_hash")
    if survey.get("opendrive_sha256") != opendrive_hash:
        reasons.append("current_horizontal_survey_opendrive_hash")
    try:
        crs_summary = _validate_survey_crs(
            survey.get("horizontal_crs"), horizontal_epsg, horizontal_wkt, coordinate_epoch
        )
    except RegistrationError:
        crs_summary = None
        reasons.append("current_horizontal_survey_crs")
    try:
        deliverable_evidence = _survey_deliverable_evidence(
            survey, deliverable_paths, observations_path
        )
        source_identity = _survey_identity(survey, deliverable_evidence)
    except RegistrationError:
        deliverable_evidence = source_identity = None
        reasons.append("current_horizontal_survey_licensed_deliverables")
    try:
        if deliverable_evidence is None or source_identity is None:
            raise RegistrationError("survey source evidence is not independently verifiable")
        authority_attestation = _verify_survey_authority_attestation(
            path.resolve(), deliverable_evidence, source_identity,
            authority_attestation_path, authority_signature_path,
        )
    except RegistrationError:
        authority_attestation = None
        reasons.append("current_horizontal_survey_authority_attestation")
    raw_controls = []
    age_days = None
    if deliverable_evidence is not None:
        try:
            with Path(deliverable_evidence["observations_path"]).open(newline="") as stream:
                reader = csv.DictReader(stream)
                if tuple(reader.fieldnames or ()) != SURVEY_OBSERVATION_COLUMNS:
                    raise RegistrationError("licensed survey observation columns are not exact")
                raw_controls = list(reader)
        except (OSError, csv.Error, RegistrationError):
            raw_controls = []
            reasons.append("current_horizontal_survey_raw_observations")
    bindings = survey.get("control_bindings")
    if not isinstance(bindings, list):
        bindings = []
        reasons.append("current_horizontal_survey_control_bindings")
    binding_by_id = {}
    for item in bindings:
        if (
            not isinstance(item, dict) or not isinstance(item.get("observation_id"), str)
            or item["observation_id"] in binding_by_id
        ):
            binding_by_id = {}
            reasons.append("current_horizontal_survey_control_bindings")
            break
        binding_by_id[item["observation_id"]] = item.get("map")
    controls, control_ids, physical_ids = [], set(), set()
    feature_splits, map_positions, survey_positions, observed_times = {}, [], [], []
    stable_landmark_ids = set()
    for raw in raw_controls:
        try:
            identity, physical_id, split = (
                raw["observation_id"].strip(), raw["physical_control_id"].strip(),
                raw["split"].strip(),
            )
            if (
                not isinstance(identity, str) or not identity or identity in control_ids
                or not isinstance(physical_id, str) or not physical_id or physical_id in physical_ids
                or split not in {"fit", "holdout"}
            ):
                raise RegistrationError("survey control identities/split are invalid")
            if source_identity is None or (
                raw["surveyor_license"].strip() != source_identity["surveyor_license"]
                or raw["instrument_serial"].strip()
                != source_identity["instrument"]["serial_number"]
                or raw["source_id"].strip() != source_identity["source_id"]
            ):
                raise RegistrationError("survey observation source identity mismatch")
            stable_id = raw["stable_landmark_id"].strip()
            reference = binding_by_id.get(identity)
            if (
                not stable_id or not isinstance(reference, dict)
                or reference.get("collection") != "objects"
                or reference.get("feature_id") != stable_id
                or reference.get("point_field") != "center_world"
                or reference.get("vertex_index") not in (None, 0)
            ):
                raise RegistrationError("survey control is not a bound stable landmark")
            landmark_matches = [
                item for item in geometry.get("geometry", {}).get("objects", [])
                if item.get("id") == stable_id
            ]
            if (
                len(landmark_matches) != 1
                or not eligible_stable_landmark(landmark_matches[0])
            ):
                raise RegistrationError(
                    "survey control does not reference an eligible stable landmark category"
                )
            map_point = resolve_map_control(geometry, reference, identity)
            surveyed_xy = np.asarray([raw["easting_m"], raw["northing_m"]], dtype=float)
            uncertainty = float(raw["horizontal_uncertainty_m"])
            observed = datetime.fromisoformat(raw["observed_at_utc"].strip().replace("Z", "+00:00"))
            if observed.tzinfo is None:
                raise RegistrationError("survey observation timestamp has no timezone")
            observed_times.append(observed.astimezone(timezone.utc))
            if (
                surveyed_xy.shape != (2,) or not np.isfinite(surveyed_xy).all()
                or not math.isfinite(uncertainty) or uncertainty <= 0
                or uncertainty > MAX_SURVEY_CONTROL_UNCERTAINTY_M
            ):
                raise RegistrationError("survey control coordinates/uncertainty are invalid")
            source_key = ("objects", stable_id, "center_world")
            if source_key in feature_splits and feature_splits[source_key] != split:
                raise RegistrationError("survey source feature leaks across fit and holdout")
            feature_splits[source_key] = split
            if any(np.linalg.norm(map_point[:2] - other) <= 1e-6 for other in map_positions):
                raise RegistrationError("survey controls contain duplicate geometry")
            if any(np.linalg.norm(surveyed_xy - other) <= 1e-6 for other in survey_positions):
                raise RegistrationError("survey observations contain duplicate coordinates")
            map_positions.append(map_point[:2])
            survey_positions.append(surveyed_xy)
            control_ids.add(identity)
            physical_ids.add(physical_id)
            stable_landmark_ids.add(stable_id)
            controls.append({
                "id": identity, "physical_control_id": physical_id, "split": split,
                "stable_landmark_id": stable_id,
                "map_reference": reference, "map_xyz": map_point,
                "survey_xy": surveyed_xy, "horizontal_uncertainty_m": uncertainty,
            })
        except (KeyError, TypeError, ValueError, RegistrationError):
            reasons.append("current_horizontal_survey_raw_observations")
            controls = []
            break
    if set(binding_by_id) != control_ids:
        reasons.append("current_horizontal_survey_control_bindings")
    if observed_times:
        age_days = (
            datetime.now(timezone.utc) - max(observed_times)
        ).total_seconds() / 86400
        if age_days < -1 or age_days > CURRENT_SURVEY_MAX_AGE_DAYS:
            reasons.append("current_horizontal_survey_age")
    else:
        reasons.append("current_horizontal_survey_timestamp")
    fit = [item for item in controls if item["split"] == "fit"]
    holdout = [item for item in controls if item["split"] == "holdout"]
    transform = fit_metrics = holdout_metrics = None
    fit_pairwise_distance_count = 0
    if len(fit) < MIN_SURVEY_FIT_CONTROLS:
        reasons.append("current_horizontal_survey_fit_control_count")
    if len(holdout) < MIN_SURVEY_HOLDOUT_CONTROLS:
        reasons.append("current_horizontal_survey_holdout_control_count")
    if len(stable_landmark_ids) < MIN_SURVEY_STABLE_LANDMARKS:
        reasons.append("current_horizontal_survey_stable_landmark_count")
    for left in fit:
        for right in holdout:
            if (
                np.linalg.norm(left["map_xyz"][:2] - right["map_xyz"][:2])
                <= SPATIAL_EXCLUSION_BUFFER_M
                or np.linalg.norm(left["survey_xy"] - right["survey_xy"])
                <= SPATIAL_EXCLUSION_BUFFER_M
            ):
                reasons.append("current_horizontal_survey_spatial_exclusion")
    if len(fit) >= MIN_SURVEY_FIT_CONTROLS and len(holdout) >= MIN_SURVEY_HOLDOUT_CONTROLS:
        fit_map = np.asarray([item["map_xyz"][:2] for item in fit])
        fit_survey = np.asarray([item["survey_xy"] for item in fit])
        holdout_map = np.asarray([item["map_xyz"][:2] for item in holdout])
        holdout_survey = np.asarray([item["survey_xy"] for item in holdout])
        fit_pairwise_distance_count = sum(
            np.linalg.norm(fit_map[left] - fit_map[right]) > 1e-6
            for left in range(len(fit_map)) for right in range(left + 1, len(fit_map))
        )
        if fit_pairwise_distance_count < 10:
            reasons.append("current_horizontal_survey_pairwise_distance_count")
        if any(np.linalg.matrix_rank(values - np.mean(values, axis=0), tol=1e-6) != 2 for values in (
            fit_map, fit_survey, holdout_map, holdout_survey
        )):
            reasons.append("current_horizontal_survey_noncollinear_geometry")
        else:
            transform = _weighted_se2(
                fit_map, fit_survey,
                np.asarray([item["horizontal_uncertainty_m"] for item in fit]),
            )
            fit_metrics = _survey_residual_metrics(fit, transform)
            holdout_metrics = _survey_residual_metrics(holdout, transform)
            for split, metrics in (("fit", fit_metrics), ("holdout", holdout_metrics)):
                if metrics["horizontal_rmse_m"] > HORIZONTAL_RMSE_MAX_M:
                    reasons.append(f"current_horizontal_survey_{split}_rmse")
                if metrics["horizontal_max_m"] > HORIZONTAL_MAX_M:
                    reasons.append(f"current_horizontal_survey_{split}_max")
    return {
        "present": True,
        "path": str(path.resolve()),
        "sha256": sha256(path),
        "age_days": age_days,
        "crs": crs_summary,
        "licensed_source": source_identity,
        "authority_attestation_evidence": authority_attestation,
        "raw_deliverable_evidence": deliverable_evidence,
        "raw_control_count": len(controls),
        "stable_landmark_count": len(stable_landmark_ids),
        "fit_control_ids": [item["id"] for item in fit],
        "holdout_control_ids": [item["id"] for item in holdout],
        "raw_control_coordinates": [{
            "observation_id": item["id"],
            "physical_control_id": item["physical_control_id"],
            "stable_landmark_id": item["stable_landmark_id"],
            "split": item["split"],
            "map_xy": item["map_xyz"][:2].tolist(),
            "survey_xy": item["survey_xy"].tolist(),
            "horizontal_uncertainty_m": item["horizontal_uncertainty_m"],
        } for item in controls],
        "fit_nonzero_pairwise_distance_count": fit_pairwise_distance_count,
        "recomputed_transform": None if transform is None else {
            "tx_m": float(transform[0]), "ty_m": float(transform[1]),
            "yaw_deg": math.degrees(float(transform[2])),
        },
        "recomputed_fit_metrics": fit_metrics,
        "recomputed_holdout_metrics": holdout_metrics,
        "raw_controls_recomputed": bool(
            controls and transform is not None and deliverable_evidence
            and source_identity and authority_attestation
        ),
        "passed": not reasons,
        "reasons": sorted(set(reasons)),
    }


def _verify_crs_authority_attestation(artifact_path: Path, artifact: dict,
                                      attestation_path: Path | None,
                                      signature_path: Path | None) -> dict:
    attestation, evidence = _verify_detached_attestation(
        attestation_path, signature_path, TRUSTED_CRS_AUTHORITY_SIGNERS,
        CRS_AUTHORITY_ATTESTATION_SCHEMA, "CRS authority",
    )
    expected = {
        "reconciliation_artifact_sha256": sha256(artifact_path),
        "proj_pipeline_sha256": artifact.get("proj_pipeline_sha256"),
        "source_crs_wkt_sha256": artifact.get("source_crs_wkt_sha256"),
        "target_crs_wkt_sha256": artifact.get("target_crs_wkt_sha256"),
        "source_coordinate_epoch": artifact.get("source_coordinate_epoch"),
        "target_coordinate_epoch": artifact.get("target_coordinate_epoch"),
        "authority": artifact.get("authority"),
        "operation_id": artifact.get("operation_id"),
        "check_points_sha256": canonical_hash(artifact.get("check_points")),
    }
    if any(attestation.get(key) != value for key, value in expected.items()):
        raise RegistrationError(
            "CRS authority attestation does not bind the exact transform evidence"
        )
    if attestation.get("verification_result") != {
        "transform_source_status": "official",
        "operation_status": "authorized",
        "control_source_status": "independent",
    }:
        raise RegistrationError("CRS authority verification result is not accepted")
    return {
        **evidence,
        "reconciliation_artifact_sha256": expected["reconciliation_artifact_sha256"],
        "verification_result": attestation["verification_result"],
    }


def validate_crs_reconciliation(opendrive: dict, lidar_tile: dict, survey: dict,
                                artifact_path: Path | None = None,
                                authority_attestation_path: Path | None = None,
                                authority_signature_path: Path | None = None) -> dict:
    from pyproj import CRS, Transformer

    lidar_crs = CRS.from_wkt(lidar_tile["horizontal_crs_wkt"])
    opendrive_crs = CRS.from_wkt(opendrive["georeference_wkt"])
    lidar_epoch = lidar_tile.get("horizontal_coordinate_epoch")
    opendrive_epoch = opendrive.get("georeference_coordinate_epoch")
    survey_crs = survey.get("crs") if survey.get("present") else None
    survey_equal = not survey.get("present") or (
        isinstance(survey_crs, dict)
        and CRS.from_wkt(survey_crs["wkt"]).equals(lidar_crs)
        and survey_crs.get("datum") == lidar_tile.get("horizontal_datum")
        and survey_crs.get("coordinate_epoch") == lidar_epoch
    )
    if not survey_equal:
        raise RegistrationError("current survey CRS/datum/epoch differs from LiDAR")
    direct_equal = (
        opendrive_crs.equals(lidar_crs)
        and opendrive.get("georeference_datum") == lidar_tile.get("horizontal_datum")
        and opendrive_epoch == lidar_epoch
        and survey_equal
    )
    if direct_equal:
        return {
            "method": "direct_crs_equality", "passed": True,
            "opendrive_epsg": opendrive.get("georeference_epsg"),
            "lidar_epsg": lidar_tile.get("horizontal_epsg"),
            "datum": lidar_tile.get("horizontal_datum"),
            "coordinate_epoch": lidar_epoch,
        }
    if artifact_path is None:
        raise RegistrationError(
            "OpenDRIVE, LiDAR, and survey CRS/datum/epoch differ without reconciliation"
        )
    artifact_path = artifact_path.resolve()
    artifact = json.loads(artifact_path.read_text())
    if artifact.get("schema") != "v2x-crs-reconciliation/v1":
        raise RegistrationError("CRS reconciliation schema is unsupported")
    pipeline = artifact.get("proj_pipeline")
    if (
        not isinstance(pipeline, str) or not pipeline.strip()
        or artifact.get("proj_pipeline_sha256")
        != hashlib.sha256(pipeline.encode()).hexdigest()
        or artifact.get("source_crs_wkt_sha256")
        != hashlib.sha256(opendrive["georeference_wkt"].encode()).hexdigest()
        or artifact.get("target_crs_wkt_sha256")
        != hashlib.sha256(lidar_tile["horizontal_crs_wkt"].encode()).hexdigest()
        or artifact.get("source_coordinate_epoch") != opendrive_epoch
        or artifact.get("target_coordinate_epoch") != lidar_epoch
    ):
        raise RegistrationError("CRS reconciliation pipeline/source/target binding mismatch")
    if any(
        not isinstance(artifact.get(key), str) or not artifact[key].strip()
        for key in ("authority", "operation_id")
    ):
        raise RegistrationError("CRS reconciliation authority identity is incomplete")
    authority_evidence = _verify_crs_authority_attestation(
        artifact_path, artifact, authority_attestation_path, authority_signature_path
    )
    checks = artifact.get("check_points")
    if not isinstance(checks, list) or len(checks) < MIN_CRS_AUTHORITY_CHECKPOINTS:
        raise RegistrationError("CRS reconciliation has insufficient independent check points")
    identities, physical_identities, source_points, target_points = set(), set(), [], []
    survey_controls = [
        item for item in survey.get("raw_control_coordinates", [])
        if isinstance(item, dict)
    ]
    survey_identities = {
        value
        for item in survey_controls
        for value in (
            item.get("observation_id"), item.get("physical_control_id"),
            item.get("stable_landmark_id"),
        )
        if isinstance(value, str) and value
    }
    for item in checks:
        try:
            identity = item["id"]
            physical_identity = item["physical_control_id"]
            source_xy = np.asarray(item["source_xy"], dtype=float)
            target_xy = np.asarray(item["target_xy"], dtype=float)
        except (KeyError, TypeError, ValueError) as exc:
            raise RegistrationError("CRS reconciliation check point is malformed") from exc
        if (
            not isinstance(identity, str) or not identity or identity in identities
            or not isinstance(physical_identity, str) or not physical_identity
            or physical_identity in physical_identities
            or item.get("provenance") != "independent_authority_control"
            or source_xy.shape != (2,) or target_xy.shape != (2,)
            or not np.isfinite(source_xy).all() or not np.isfinite(target_xy).all()
        ):
            raise RegistrationError("CRS reconciliation check point is invalid")
        if identity in survey_identities or physical_identity in survey_identities:
            raise RegistrationError(
                "CRS reconciliation controls are not independent from survey controls"
            )
        for raw_control in survey_controls:
            try:
                map_xy = np.asarray(raw_control["map_xy"], dtype=float)
                survey_xy = np.asarray(raw_control["survey_xy"], dtype=float)
            except (KeyError, TypeError, ValueError) as exc:
                raise RegistrationError("survey control evidence is malformed") from exc
            if (
                map_xy.shape != (2,) or survey_xy.shape != (2,)
                or np.linalg.norm(source_xy - map_xy) <= SPATIAL_EXCLUSION_BUFFER_M
                or np.linalg.norm(target_xy - survey_xy) <= SPATIAL_EXCLUSION_BUFFER_M
            ):
                raise RegistrationError(
                    "CRS reconciliation controls are not independent from survey controls"
                )
        identities.add(identity)
        physical_identities.add(physical_identity)
        source_points.append(source_xy)
        target_points.append(target_xy)
    source_array, target_array = np.asarray(source_points), np.asarray(target_points)
    if any(np.linalg.matrix_rank(values - np.mean(values, axis=0), tol=1e-6) != 2 for values in (
        source_array, target_array
    )):
        raise RegistrationError("CRS reconciliation check points are rank deficient")
    try:
        transformer = Transformer.from_pipeline(pipeline)
        transformed = np.column_stack(transformer.transform(
            source_array[:, 0], source_array[:, 1]
        ))
    except Exception as exc:
        raise RegistrationError("CRS reconciliation pipeline cannot be executed") from exc
    residuals = np.linalg.norm(transformed - target_array, axis=1)
    if not np.isfinite(residuals).all() or float(np.max(residuals)) > MAX_SURVEY_CONTROL_UNCERTAINTY_M:
        raise RegistrationError("CRS reconciliation independently recomputed residuals fail")
    return {
        "method": "signed_authority_proj_pipeline", "passed": True,
        "artifact_path": str(artifact_path), "artifact_sha256": sha256(artifact_path),
        "authority": artifact["authority"], "operation_id": artifact["operation_id"],
        "authority_attestation_evidence": authority_evidence,
        "check_point_count": len(checks),
        "recomputed_horizontal_rmse_m": math.sqrt(float(np.mean(np.square(residuals)))),
        "recomputed_horizontal_max_m": float(np.max(residuals)),
    }


def _read_all_from_fd(file_descriptor: int, expected_bytes: int) -> bytes:
    chunks, remaining = [], expected_bytes
    while remaining:
        chunk = os.read(file_descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    extra = os.read(file_descriptor, 1)
    if remaining or extra:
        raise RegistrationError("vertical source artifact changed size while being read")
    return b"".join(chunks)


def read_retained_vertical_source_artifact(path: Path | None) -> dict:
    if path is None:
        raise RegistrationError("retained vertical source artifact is required")
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(path, flags)
    except OSError as exc:
        raise RegistrationError(
            "retained vertical source artifact cannot be safely opened"
        ) from exc
    try:
        before = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_size <= 0 or before.st_size > MAX_VERTICAL_SOURCE_ARTIFACT_BYTES
        ):
            raise RegistrationError(
                "retained vertical source artifact is not a bounded single-link regular file"
            )
        content = _read_all_from_fd(file_descriptor, before.st_size)
        after = os.fstat(file_descriptor)
        try:
            path_after = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise RegistrationError(
                "retained vertical source artifact path changed while being read"
            ) from exc
        identity_before = (
            before.st_dev, before.st_ino, before.st_mode, before.st_nlink,
            before.st_size, before.st_mtime_ns, before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev, after.st_ino, after.st_mode, after.st_nlink,
            after.st_size, after.st_mtime_ns, after.st_ctime_ns,
        )
        path_identity = (
            path_after.st_dev, path_after.st_ino, path_after.st_mode,
            path_after.st_nlink, path_after.st_size, path_after.st_mtime_ns,
            path_after.st_ctime_ns,
        )
        if identity_after != identity_before or path_identity != identity_before:
            raise RegistrationError(
                "retained vertical source artifact changed while being read"
            )
    finally:
        os.close(file_descriptor)
    return {
        "path": str(path.absolute()),
        "file_name": path.name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "device": before.st_dev,
        "inode": before.st_ino,
        "link_count": before.st_nlink,
    }


def validate_vertical_datum_reconciliation(opendrive: dict, lidar_tile: dict,
                                           survey: dict,
                                           artifact_path: Path | None = None,
                                           authority_attestation_path: Path | None = None,
                                           authority_signature_path: Path | None = None,
                                           source_artifact_path: Path | None = None) -> dict:
    if (
        artifact_path is None and authority_attestation_path is None
        and authority_signature_path is None and source_artifact_path is None
    ):
        return {
            "present": False, "passed": False,
            "reasons": ["vertical_datum_reconciliation_missing"],
        }
    if artifact_path is None:
        raise RegistrationError("vertical datum reconciliation artifact is required")
    artifact_path = artifact_path.resolve()
    artifact = json.loads(artifact_path.read_text())
    if artifact.get("schema") != VERTICAL_DATUM_RECONCILIATION_SCHEMA:
        raise RegistrationError("vertical datum reconciliation schema is unsupported")
    source_reference = artifact.get("source_vertical_reference")
    target_reference = artifact.get("target_vertical_reference")
    operation = artifact.get("operation")
    if (
        artifact.get("opendrive_sha256") != opendrive["sha256"]
        or artifact.get("lidar_vertical_epsg") != lidar_tile.get("vertical_epsg")
        or artifact.get("lidar_vertical_datum") != lidar_tile.get("vertical_datum")
        or artifact.get("lidar_vertical_crs_wkt_sha256")
        != hashlib.sha256(lidar_tile["vertical_crs_wkt"].encode()).hexdigest()
        or not isinstance(source_reference, dict)
        or source_reference.get("linear_units") != "metre"
        or not isinstance(source_reference.get("datum"), str)
        or not source_reference["datum"].strip()
        or not isinstance(source_reference.get("source_artifact_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", source_reference["source_artifact_sha256"])
        is None
        or not isinstance(source_reference.get("source_artifact_file_name"), str)
        or not source_reference["source_artifact_file_name"].strip()
        or not isinstance(source_reference.get("source_artifact_bytes"), int)
        or isinstance(source_reference.get("source_artifact_bytes"), bool)
        or source_reference["source_artifact_bytes"] <= 0
        or target_reference != {
            "epsg": lidar_tile.get("vertical_epsg"),
            "datum": lidar_tile.get("vertical_datum"),
            "wkt_sha256": hashlib.sha256(
                lidar_tile["vertical_crs_wkt"].encode()
            ).hexdigest(),
            "coordinate_epoch": lidar_tile.get("vertical_coordinate_epoch"),
            "linear_units": "metre",
        }
        or not isinstance(operation, dict)
        or operation.get("method") != "constant_offset"
        or not isinstance(operation.get("offset_m"), (int, float))
        or not math.isfinite(float(operation["offset_m"]))
        or any(
            not isinstance(artifact.get(key), str) or not artifact[key].strip()
            for key in ("authority", "operation_id")
        )
    ):
        raise RegistrationError("vertical datum reconciliation binding is invalid")
    source_artifact_evidence = read_retained_vertical_source_artifact(
        source_artifact_path
    )
    if {
        "source_artifact_sha256": source_artifact_evidence["sha256"],
        "source_artifact_file_name": source_artifact_evidence["file_name"],
        "source_artifact_bytes": source_artifact_evidence["bytes"],
    } != {
        key: source_reference[key]
        for key in (
            "source_artifact_sha256", "source_artifact_file_name",
            "source_artifact_bytes",
        )
    }:
        raise RegistrationError(
            "vertical datum retained source artifact does not match signed reference"
        )
    attestation, authority_evidence = _verify_detached_attestation(
        authority_attestation_path, authority_signature_path,
        TRUSTED_VERTICAL_DATUM_SIGNERS,
        VERTICAL_DATUM_AUTHORITY_ATTESTATION_SCHEMA,
        "vertical datum authority",
    )
    expected_attestation = {
        "reconciliation_artifact_sha256": sha256(artifact_path),
        "opendrive_sha256": opendrive["sha256"],
        "lidar_vertical_crs_wkt_sha256": artifact["lidar_vertical_crs_wkt_sha256"],
        "source_vertical_reference_sha256": canonical_hash(source_reference),
        "target_vertical_reference_sha256": canonical_hash(target_reference),
        "operation_sha256": canonical_hash(operation),
        "authority": artifact["authority"],
        "operation_id": artifact["operation_id"],
        "check_points_sha256": canonical_hash(artifact.get("check_points")),
    }
    if any(attestation.get(key) != value for key, value in expected_attestation.items()):
        raise RegistrationError(
            "vertical authority attestation does not bind exact datum evidence"
        )
    if attestation.get("verification_result") != {
        "vertical_source_status": "official",
        "operation_status": "authorized",
        "control_source_status": "independent",
    }:
        raise RegistrationError("vertical authority verification result is not accepted")
    checks = artifact.get("check_points")
    if not isinstance(checks, list) or len(checks) < MIN_VERTICAL_AUTHORITY_CHECKPOINTS:
        raise RegistrationError("vertical datum reconciliation has insufficient controls")
    survey_ids = {
        value
        for item in survey.get("raw_control_coordinates", [])
        if isinstance(item, dict)
        for value in (
            item.get("observation_id"), item.get("physical_control_id"),
            item.get("stable_landmark_id"),
        )
        if isinstance(value, str) and value
    }
    identities, physical_identities, residuals = set(), set(), []
    for item in checks:
        try:
            identity, physical_identity = item["id"], item["physical_control_id"]
            source_z, target_z = float(item["source_z_m"]), float(item["target_z_m"])
            uncertainty = float(item["vertical_uncertainty_m"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RegistrationError("vertical datum control is malformed") from exc
        if (
            not isinstance(identity, str) or not identity or identity in identities
            or not isinstance(physical_identity, str) or not physical_identity
            or physical_identity in physical_identities
            or identity in survey_ids or physical_identity in survey_ids
            or item.get("provenance") != "independent_authority_control"
            or not all(math.isfinite(value) for value in (source_z, target_z, uncertainty))
            or uncertainty <= 0 or uncertainty > MAX_SURVEY_CONTROL_UNCERTAINTY_M
        ):
            raise RegistrationError("vertical datum control is invalid or not independent")
        residuals.append(abs(source_z + float(operation["offset_m"]) - target_z))
        identities.add(identity)
        physical_identities.add(physical_identity)
    if max(residuals) > VERTICAL_RMSE_MAX_M:
        raise RegistrationError("vertical datum independently recomputed residuals fail")
    return {
        "present": True, "passed": True,
        "artifact_path": str(artifact_path), "artifact_sha256": sha256(artifact_path),
        "authority": artifact["authority"], "operation_id": artifact["operation_id"],
        "authenticated_offset_m": float(operation["offset_m"]),
        "retained_source_artifact": source_artifact_evidence,
        "control_count": len(checks),
        "recomputed_vertical_rmse_m": math.sqrt(float(np.mean(np.square(residuals)))),
        "recomputed_vertical_max_m": max(residuals),
        "authority_attestation_evidence": authority_evidence,
        "reasons": [],
    }


def register(annotation: dict, geometry: dict, tiles: dict, metadata_record: dict,
             metadata_summary: dict, survey: dict) -> dict:
    features = load_features(annotation, geometry, tiles)
    initial_raw = annotation.get("initial_transform")
    if not isinstance(initial_raw, dict):
        raise RegistrationError("manual annotations require one initial site transform")
    initial = np.asarray([
        float(initial_raw["tx_m"]), float(initial_raw["ty_m"]),
        math.radians(float(initial_raw["yaw_deg"])), float(initial_raw["z_bias_m"]),
    ])
    if not np.isfinite(initial).all():
        raise RegistrationError("initial site transform is non-finite")
    fit = [item for item in features if item["split"] == "fit"]
    holdout = [item for item in features if item["split"] == "holdout"]
    solution = solve(fit, initial, multi_start=True)
    fit_metrics = metrics_for_features(fit, solution["x"], initial)
    holdout_metrics = metrics_for_features(holdout, solution["x"], initial)
    folds = leave_one_approach_out(fit, initial, solution["x"])

    reasons = []
    reasons.extend(absolute_metric_failures("fit", fit_metrics["global"]))
    reasons.extend(absolute_metric_failures("holdout", holdout_metrics["global"]))
    for split_name, group in (("fit", fit_metrics), ("holdout", holdout_metrics)):
        for identity, item in group["per_feature"].items():
            reasons.extend(absolute_metric_failures(f"{split_name}_feature_{identity}", item["after"]))
            if item["horizontal_rmse_delta_m"] > FEATURE_REGRESSION_TOLERANCE_M:
                reasons.append(f"{split_name}_feature_{identity}_horizontal_regression")
            if item["vertical_rmse_delta_m"] > FEATURE_REGRESSION_TOLERANCE_M:
                reasons.append(f"{split_name}_feature_{identity}_vertical_regression")
    if solution["jacobian_rank"] != 4:
        reasons.append("fit_jacobian_not_full_rank")
    if solution["jacobian_condition"] is None or solution["jacobian_condition"] > JACOBIAN_CONDITION_MAX:
        reasons.append("fit_jacobian_condition")
    if solution["bound_hits"]:
        reasons.append("fit_parameter_bound_hit")
    if solution["near_optimal_separated_modes"]:
        reasons.append("fit_multimodal")
    if not solution["seed_bounds_covered"]:
        reasons.append("fit_seed_bounds_not_covered")
    if folds["failures"]:
        reasons.append("leave_one_approach_out_failure")
    if folds["translation_spread_m"] is None or folds["translation_spread_m"] > FOLD_TRANSLATION_SPREAD_MAX_M:
        reasons.append("leave_one_approach_out_translation_spread")
    if folds["yaw_spread_deg"] is None or folds["yaw_spread_deg"] > FOLD_YAW_SPREAD_MAX_DEG:
        reasons.append("leave_one_approach_out_yaw_spread")
    for fold in folds["folds"]:
        if fold["jacobian_rank"] != 4:
            reasons.append(f"leave_one_approach_out_{fold['omitted_approach_id']}_rank")
        if fold["jacobian_condition"] is None or fold["jacobian_condition"] > JACOBIAN_CONDITION_MAX:
            reasons.append(f"leave_one_approach_out_{fold['omitted_approach_id']}_condition")
        if fold["bound_hits"]:
            reasons.append(f"leave_one_approach_out_{fold['omitted_approach_id']}_bound")

    acquisition_years = parse_acquisition_years(metadata_record)
    quality_level = str(metadata_record.get("ql") or metadata_record.get("quality_level") or "")
    old_ql2 = 2018 in acquisition_years and quality_level.lower().replace(" ", "") == "ql2"
    if old_ql2:
        reasons.append("2018_ql2_is_development_control_only")
    parameters = solution["x"]
    if not all(item.get("validation_authority_attestation") for item in tiles.values()):
        reasons.append("lidar_validation_authority_attestation_missing")
    annotation_review = metadata_summary.get("annotation_review") or {}
    if not annotation_review.get("passed"):
        reasons.extend(annotation_review.get("reasons") or ["annotation_interreview_missing"])
    holdout_evaluation = metadata_summary.get("holdout_evaluation") or {}
    if not holdout_evaluation.get("passed") or not holdout_evaluation.get("burned"):
        reasons.extend(
            holdout_evaluation.get("reasons") or ["holdout_evaluation_burn_uncontrolled"]
        )
    if not metadata_summary.get("annotation_authority_attestation"):
        reasons.append("annotation_authority_attestation_missing")
    if not (metadata_summary.get("toolchain") or {}).get("passed"):
        reasons.append("deterministic_toolchain_lock_missing")
    vertical_reconciliation = metadata_summary.get("vertical_datum_reconciliation") or {}
    if not vertical_reconciliation.get("passed"):
        reasons.extend(
            vertical_reconciliation.get("reasons")
            or ["vertical_datum_reconciliation_missing"]
        )
    elif abs(
        float(vertical_reconciliation["authenticated_offset_m"]) - float(parameters[3])
    ) > VERTICAL_RMSE_MAX_M:
        reasons.append("vertical_datum_registration_offset_disagreement")
    survey_transform = survey.get("recomputed_transform") if survey.get("passed") else None
    if survey.get("passed") and not survey.get("raw_controls_recomputed"):
        reasons.append("current_horizontal_survey_recomputation_missing")
    if survey_transform is not None:
        survey_translation_delta = math.hypot(
            float(survey_transform["tx_m"]) - parameters[0],
            float(survey_transform["ty_m"]) - parameters[1],
        )
        survey_yaw_delta = abs(math.degrees(math.atan2(
            math.sin(math.radians(float(survey_transform["yaw_deg"])) - parameters[2]),
            math.cos(math.radians(float(survey_transform["yaw_deg"])) - parameters[2]),
        )))
        survey["registration_transform_agreement"] = {
            "translation_delta_m": survey_translation_delta,
            "yaw_delta_deg": survey_yaw_delta,
            "translation_limit_m": FOLD_TRANSLATION_SPREAD_MAX_M,
            "yaw_limit_deg": FOLD_YAW_SPREAD_MAX_DEG,
        }
        if survey_translation_delta > FOLD_TRANSLATION_SPREAD_MAX_M:
            reasons.append("current_horizontal_survey_registration_translation")
        if survey_yaw_delta > FOLD_YAW_SPREAD_MAX_DEG:
            reasons.append("current_horizontal_survey_registration_yaw")
    reasons.extend(survey["reasons"])
    reasons = sorted(set(reasons))
    acceptance_only_reasons = {
        "2018_ql2_is_development_control_only",
        "current_horizontal_survey_missing",
        "lidar_validation_authority_attestation_missing",
        "annotation_interreview_missing",
        "holdout_evaluation_ledger_missing",
        "holdout_evaluation_burn_uncontrolled",
        "annotation_authority_attestation_missing",
        "deterministic_toolchain_lock_missing",
        "vertical_datum_reconciliation_missing",
    }
    numerical_passed = not [
        reason for reason in reasons
        if reason not in acceptance_only_reasons
        and not reason.startswith("current_horizontal_survey_")
    ]
    return {
        "schema": REPORT_SCHEMA,
        "acceptance_eligible": False if old_ql2 else not reasons,
        "deployment_eligible": False if old_ql2 else not reasons,
        "numerical_registration_passed": numerical_passed,
        "created_at_utc": utc_now(),
        "model": {
            "degrees_of_freedom": ["tx_m", "ty_m", "yaw_deg", "z_bias_m"],
            "forbidden_degrees_of_freedom": [
                "per_approach_transform", "per_feature_transform", "scale", "shear", "local_warp"
            ],
            "transform": {
                "tx_m": float(parameters[0]), "ty_m": float(parameters[1]),
                "yaw_deg": math.degrees(float(parameters[2])), "z_bias_m": float(parameters[3]),
            },
            "initial_transform": initial_raw,
            "bounds_relative_to_initial": {
                "translation_m": TRANSLATION_BOUND_RADIUS_M,
                "yaw_deg": YAW_BOUND_RADIUS_DEG,
                "z_bias_m": Z_BIAS_BOUND_RADIUS_M,
            },
            "objective": {
                "horizontal": "symmetric_point_to_nearest_finite_segment_normal_residual",
                "vertical": "symmetric_nearest_finite_segment_interpolated_z_residual",
                "polyline_resample_spacing_m": EVALUATION_SPACING_M,
                "balancing": "equal_approach_then_equal_feature_then_equal_direction_sample",
                "robust_loss": "soft_l1",
                "robust_scale_m": 0.10,
            },
        },
        "fixed_gates": {
            "horizontal_rmse_max_m": HORIZONTAL_RMSE_MAX_M,
            "horizontal_max_m": HORIZONTAL_MAX_M,
            "symmetric_hausdorff_max_m": HAUSDORFF_MAX_M,
            "vertical_rmse_max_m": VERTICAL_RMSE_MAX_M,
            "vertical_p95_max_m": VERTICAL_P95_MAX_M,
            "vertical_max_m": VERTICAL_MAX_M,
            "fold_translation_spread_max_m": FOLD_TRANSLATION_SPREAD_MAX_M,
            "fold_yaw_spread_max_deg": FOLD_YAW_SPREAD_MAX_DEG,
            "jacobian_condition_max": JACOBIAN_CONDITION_MAX,
            "fit_holdout_spatial_exclusion_m": SPATIAL_EXCLUSION_BUFFER_M,
            "annotation_repeatability_max_m": ANNOTATION_REPEATABILITY_MAX_M,
            "survey_fit_control_minimum": MIN_SURVEY_FIT_CONTROLS,
            "survey_holdout_control_minimum": MIN_SURVEY_HOLDOUT_CONTROLS,
            "survey_distinct_stable_landmark_minimum": MIN_SURVEY_STABLE_LANDMARKS,
            "crs_authority_checkpoint_minimum": MIN_CRS_AUTHORITY_CHECKPOINTS,
            "vertical_authority_checkpoint_minimum": MIN_VERTICAL_AUTHORITY_CHECKPOINTS,
        },
        "evidence": metadata_summary,
        "feature_identities": {
            "fit": [item["id"] for item in fit],
            "holdout": [item["id"] for item in holdout],
            "approaches": sorted({item["approach_id"] for item in features}),
        },
        "fit_metrics": fit_metrics,
        "holdout_metrics": holdout_metrics,
        "optimizer": {key: value for key, value in solution.items() if key != "x"},
        "leave_one_approach_out": folds,
        "current_horizontal_survey": survey,
        "reasons": reasons,
        "limitations": [
            "manual_polyline_identity_is_not_a_current_horizontal_survey",
            *(["2018_USGS_QL2_does_not_certify_current_horizontal_site_alignment"] if old_ql2 else []),
            "report_never_modifies_or_deploys_map_or_camera_configuration",
        ],
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lidar", action="append", type=Path, required=True)
    parser.add_argument("--lidar-validation", action="append", type=Path, required=True)
    parser.add_argument(
        "--lidar-validation-authority-attestation", action="append", type=Path
    )
    parser.add_argument(
        "--lidar-validation-authority-signature", action="append", type=Path
    )
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--opendrive", type=Path, required=True)
    parser.add_argument("--geometry", type=Path, required=True)
    parser.add_argument("--pair-manifest", type=Path, required=True)
    parser.add_argument("--cameras-json", type=Path, required=True)
    parser.add_argument("--carla-source-export", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--annotation-review", type=Path)
    parser.add_argument("--holdout-evaluation-ledger", type=Path)
    parser.add_argument("--annotation-authority-attestation", type=Path)
    parser.add_argument("--annotation-authority-signature", type=Path)
    parser.add_argument("--current-horizontal-survey", type=Path)
    parser.add_argument("--survey-deliverable", action="append", type=Path)
    parser.add_argument("--survey-observations", type=Path)
    parser.add_argument("--survey-authority-attestation", type=Path)
    parser.add_argument("--survey-authority-signature", type=Path)
    parser.add_argument("--crs-reconciliation", type=Path)
    parser.add_argument("--crs-authority-attestation", type=Path)
    parser.add_argument("--crs-authority-signature", type=Path)
    parser.add_argument("--vertical-datum-reconciliation", type=Path)
    parser.add_argument("--vertical-source-artifact", type=Path)
    parser.add_argument("--vertical-datum-authority-attestation", type=Path)
    parser.add_argument("--vertical-datum-authority-signature", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deployment-output", type=Path)
    parser.add_argument(
        "--development-numeric-ok", action="store_true",
        help="return zero for numerical-only development reports that remain non-acceptable",
    )
    return parser.parse_args()


def report_exit_code(report: dict, development_numeric_ok: bool = False) -> int:
    if report.get("acceptance_eligible") is True:
        return 0
    if development_numeric_ok and report.get("numerical_registration_passed") is True:
        return 0
    return 2


def write_registration_outputs(report: dict, survey: dict, output: Path,
                               deployment_output: Path | None = None) -> None:
    write_json_exclusive(output, report)
    if deployment_output is not None:
        if (
            not survey.get("passed") or not survey.get("raw_controls_recomputed")
            or survey.get("stable_landmark_count", 0) < MIN_SURVEY_STABLE_LANDMARKS
            or not survey.get("licensed_source")
            or not (survey.get("raw_deliverable_evidence") or {}).get("deliverables")
            or not survey.get("authority_attestation_evidence")
        ):
            raise RegistrationError(
                "refusing deployment output without licensed recomputed survey evidence"
            )
        evidence = report.get("evidence") or {}
        lidar_evidence = evidence.get("lidar_tiles")
        if (
            not (evidence.get("toolchain") or {}).get("passed")
            or not (evidence.get("annotation_review") or {}).get("passed")
            or not (evidence.get("holdout_evaluation") or {}).get("burned")
            or not evidence.get("annotation_authority_attestation")
            or not (evidence.get("vertical_datum_reconciliation") or {}).get("passed")
            or not isinstance(lidar_evidence, list) or not lidar_evidence
            or not all(
                item.get("validation_authority_attestation")
                for item in lidar_evidence or []
            )
        ):
            raise RegistrationError(
                "refusing deployment output without authenticated evidence roots"
            )
        crs_evidence = (report.get("evidence") or {}).get("crs_reconciliation")
        if not isinstance(crs_evidence, dict) or crs_evidence.get("passed") is not True:
            raise RegistrationError("refusing deployment output without CRS reconciliation")
        if not report["deployment_eligible"]:
            raise RegistrationError(
                "refusing deployment output because strict registration gates did not pass"
            )
        write_json_exclusive(deployment_output, {
            "schema": "v2x-map-lidar-deployment-candidate/v1",
            "registration_report_sha256": canonical_hash(report),
            "transform": report["model"]["transform"],
        })


def main() -> int:
    args = parse_args()
    if len(args.lidar) != len(args.lidar_validation):
        raise SystemExit("one --lidar-validation is required for each --lidar")
    authority_attestations = args.lidar_validation_authority_attestation or []
    authority_signatures = args.lidar_validation_authority_signature or []
    if bool(authority_attestations) != bool(authority_signatures) or (
        authority_attestations and len(authority_attestations) != len(args.lidar)
    ) or (
        authority_signatures and len(authority_signatures) != len(args.lidar)
    ):
        raise SystemExit(
            "one signed LiDAR validation authority pair is required per LiDAR tile"
        )
    toolchain = validate_toolchain_lock()
    tile_list = [
        load_lidar_tile(
            path.resolve(), validation.resolve(),
            authority_attestations[index].resolve() if authority_attestations else None,
            authority_signatures[index].resolve() if authority_signatures else None,
        )
        for index, (path, validation) in enumerate(zip(args.lidar, args.lidar_validation))
    ]
    tiles = {item["sha256"]: item for item in tile_list}
    if len(tiles) != len(tile_list):
        raise SystemExit("duplicate raw LiDAR tiles are not allowed")
    crs_identities = {(item["horizontal_epsg"], item["vertical_epsg"]) for item in tile_list}
    if len(crs_identities) != 1 or any(
        not _semantic_crs_equal(item["horizontal_crs_wkt"], tile_list[0]["horizontal_crs_wkt"])
        or item["horizontal_coordinate_epoch"] != tile_list[0]["horizontal_coordinate_epoch"]
        or item["horizontal_datum"] != tile_list[0]["horizontal_datum"]
        for item in tile_list[1:]
    ):
        raise SystemExit("raw LiDAR tiles do not share one horizontal/vertical CRS")
    horizontal_epsg, vertical_epsg = next(iter(crs_identities))

    metadata_path, opendrive_path = args.metadata.resolve(), args.opendrive.resolve()
    geometry_path, annotation_path = args.geometry.resolve(), args.annotations.resolve()
    pair_path, cameras_path = args.pair_manifest.resolve(), args.cameras_json.resolve()
    carla_source_path = args.carla_source_export.resolve()
    metadata, geometry, annotation = (
        json.loads(metadata_path.read_text()), json.loads(geometry_path.read_text()),
        json.loads(annotation_path.read_text()),
    )
    opendrive = parse_opendrive(opendrive_path)
    verify_artifact_bindings(annotation, tiles, metadata_path, opendrive, geometry_path, geometry)
    geometry_validation = validate_geometry_provenance(
        geometry, geometry_path, opendrive_path, opendrive, pair_path, cameras_path,
        carla_source_path,
    )
    reviewed_features = load_features(annotation, geometry, tiles)
    annotation_review = validate_annotation_review(
        annotation_path,
        args.annotation_review.resolve() if args.annotation_review else None,
        reviewed_features,
    )
    holdout_evaluation = validate_holdout_ledger(
        annotation_path,
        args.holdout_evaluation_ledger.resolve()
        if args.holdout_evaluation_ledger else None,
        annotation_review,
    )
    annotation_authority = validate_annotation_authority(
        annotation_path, annotation_review, holdout_evaluation,
        args.annotation_authority_attestation.resolve()
        if args.annotation_authority_attestation else None,
        args.annotation_authority_signature.resolve()
        if args.annotation_authority_signature else None,
    )
    holdout_evaluation = authorize_and_burn_holdout(
        annotation_review, holdout_evaluation, annotation_authority
    )
    metadata_record = select_metadata_record(metadata, annotation.get("metadata_selector", {}))
    if int(metadata_record.get("horiz_crs") or metadata_record.get("horizontal_epsg")) != horizontal_epsg:
        raise RegistrationError("authoritative metadata horizontal CRS differs from raw LiDAR")
    if int(metadata_record.get("vert_crs") or metadata_record.get("vertical_epsg")) != vertical_epsg:
        raise RegistrationError("authoritative metadata vertical CRS differs from raw LiDAR")
    survey = validate_current_survey(
        args.current_horizontal_survey.resolve() if args.current_horizontal_survey else None,
        geometry, sha256(geometry_path), opendrive["sha256"], horizontal_epsg,
        [path.resolve() for path in (args.survey_deliverable or [])],
        args.survey_observations.resolve() if args.survey_observations else None,
        tile_list[0]["horizontal_crs_wkt"],
        tile_list[0]["horizontal_coordinate_epoch"],
        args.survey_authority_attestation.resolve()
        if args.survey_authority_attestation else None,
        args.survey_authority_signature.resolve()
        if args.survey_authority_signature else None,
    )
    crs_reconciliation = validate_crs_reconciliation(
        opendrive, tile_list[0], survey,
        args.crs_reconciliation.resolve() if args.crs_reconciliation else None,
        args.crs_authority_attestation.resolve()
        if args.crs_authority_attestation else None,
        args.crs_authority_signature.resolve()
        if args.crs_authority_signature else None,
    )
    vertical_reconciliation = validate_vertical_datum_reconciliation(
        opendrive, tile_list[0], survey,
        args.vertical_datum_reconciliation.resolve()
        if args.vertical_datum_reconciliation else None,
        args.vertical_datum_authority_attestation.resolve()
        if args.vertical_datum_authority_attestation else None,
        args.vertical_datum_authority_signature.resolve()
        if args.vertical_datum_authority_signature else None,
        args.vertical_source_artifact.resolve()
        if args.vertical_source_artifact else None,
    )
    metadata_summary = {
        "annotations": {"path": str(annotation_path), "sha256": sha256(annotation_path)},
        "geometry": {"path": str(geometry_path), "sha256": sha256(geometry_path)},
        "geometry_provenance_validation": geometry_validation,
        "toolchain": toolchain,
        "annotation_review": annotation_review,
        "holdout_evaluation": holdout_evaluation,
        "annotation_authority_attestation": annotation_authority,
        "crs_reconciliation": crs_reconciliation,
        "vertical_datum_reconciliation": vertical_reconciliation,
        "opendrive": opendrive,
        "metadata": {
            "path": str(metadata_path), "sha256": sha256(metadata_path),
            "selected_project_id": metadata_record.get("project_id"),
            "selected_workunit": metadata_record.get("workunit"),
            "quality_level": metadata_record.get("ql") or metadata_record.get("quality_level"),
            "acquisition_years": parse_acquisition_years(metadata_record),
        },
        "lidar_tiles": [
            {key: item[key] for key in (
                "path", "sha256", "validation_path", "validation_sha256", "point_count",
                "bytes", "bounds", "scales", "horizontal_epsg", "vertical_epsg",
                "horizontal_units", "vertical_units", "horizontal_datum", "vertical_datum",
                "horizontal_crs_wkt", "vertical_crs_wkt",
                "horizontal_coordinate_epoch", "vertical_coordinate_epoch",
                "validation_authority_attestation",
            )} for item in tile_list
        ],
    }
    report = register(annotation, geometry, tiles, metadata_record, metadata_summary, survey)
    write_registration_outputs(report, survey, args.output, args.deployment_output)
    print(json.dumps({
        "output": str(args.output),
        "acceptance_eligible": report["acceptance_eligible"],
        "numerical_registration_passed": report["numerical_registration_passed"],
        "development_numeric_ok_override": bool(args.development_numeric_ok),
        "reasons": report["reasons"],
    }, sort_keys=True))
    return report_exit_code(report, args.development_numeric_ok)


if __name__ == "__main__":
    raise SystemExit(main())
