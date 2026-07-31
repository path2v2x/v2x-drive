#!/usr/bin/env python3
"""Audit one authenticated terminal outcome for every vehicle observation.

Acceptance is fail-closed.  The audit trusts only an out-of-band pinned Ed25519
key allowlist, an independently signed audit-authority manifest, signed producer
reports, and (for unavailable source pixels) signed retention-policy and
retrieval receipts.  All retained evidence is opened beneath one pinned root
without following symlinks and is summarized in a content-addressed snapshot.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


LEDGER_SCHEMA = "v2x-detection-observation-ledger/v2"
OBSERVATION_SCHEMA = "v2x-detection-observation/v2"
OUTCOME_SCHEMA = "v2x-vehicle-observation-outcome/v1"
REPORT_SCHEMA = "v2x-vehicle-observation-terminal-audit/v2"
AUTHORITY_SCHEMA = "v2x-vehicle-observation-audit-authority/v1"
ACCEPTANCE_REPORT_SCHEMA = "v2x-vehicle-observation-acceptance/v2"
UNAVAILABILITY_REPORT_SCHEMA = "v2x-vehicle-observation-unavailability/v2"
RETENTION_POLICY_SCHEMA = "v2x-kvs-retention-policy-snapshot/v1"
RETRIEVAL_RECEIPT_SCHEMA = "v2x-kvs-retrieval-attempt-receipt/v1"
SNAPSHOT_SCHEMA = "v2x-retained-evidence-snapshot/v1"
VERIFIER_RELEASE = "v2x-terminal-observation-audit-strict/v2"

TERMINAL_STATES = {"accepted", "rejected", "unavailable"}
UNAVAILABLE_REASON = "exact_source_pixels_aged_out"
MINIMUM_ACCEPTED_FRACTION = 0.80
FUTURE_SKEW_SECONDS = 300
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_BYTES = 512 * 1024 * 1024

ACCEPTANCE_GATES = {
    "exact_frame",
    "measured_intrinsics",
    "static_camera",
    "reviewed_contact",
    "localization",
    "identity",
    "temporal_tracking",
    "ue5_visual",
    "cleanup",
}
ACCEPTANCE_ARTIFACTS = {
    "exact_frame",
    "static_calibration",
    "reviewed_contact",
    "identity_track",
    "ue5_replay",
}
ACCEPTANCE_ARTIFACT_SCHEMAS = {
    "exact_frame": "v2x-exact-vehicle-frame-acceptance/v2",
    "static_calibration": "v2x-static-camera-acceptance/v2",
    "reviewed_contact": "v2x-reviewed-vehicle-contact-acceptance/v2",
    "identity_track": "v2x-vehicle-identity-track-acceptance/v2",
    "ue5_replay": "v2x-heldout-ue5-same-car-acceptance/v2",
}
ARTIFACT_GATES = {
    "exact_frame": {
        "detector_instance_mask_bound",
        "exact_source_pixels",
        "lossless_frame_identity",
        "same_session_pts",
    },
    "static_calibration": {
        "camera_epoch_valid",
        "heldout_geometry_passed",
        "map_and_config_bound",
        "measured_intrinsics_passed",
    },
    "reviewed_contact": {
        "finite_covariance",
        "independent_review_passed",
        "mask_bound",
        "visible_road_contact_passed",
    },
    "identity_track": {
        "appearance_model_pinned",
        "cross_camera_identity_passed",
        "temporal_stability_passed",
        "zero_identity_switches",
    },
    "ue5_replay": {
        "actor_cleanup_passed",
        "same_car_actor_bound",
        "temporal_motion_passed",
        "visual_geometry_passed",
    },
}
SIGNED_ROLES = {"acceptance_report", *ACCEPTANCE_ARTIFACTS}
RETENTION_ROLES = {
    "unavailability_report",
    "retention_policy",
    "retrieval_receipt",
}
SUPPORTED_RETRIEVAL_ERRORS = {
    "FragmentNotFound",
    "NoMediaForTimestamp",
}

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REASON_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
UTC_RE = re.compile(
    r"^(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})T"
    r"(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})(?P<fraction>\.[0-9]{1,6})?Z$"
)


class OutcomeAuditError(RuntimeError):
    """A controlled, machine-distinguishable audit rejection."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def fail(code, message):
    raise OutcomeAuditError(code, message)


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def canonical_json_bytes(value):
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise OutcomeAuditError("json_not_canonical", "value is not strict JSON") from exc
    return (encoded + "\n").encode("utf-8")


def _reject_constant(value):
    fail("json_nonfinite", f"non-finite JSON constant {value!r} is forbidden")


def _strict_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        fail("json_nonfinite", f"non-finite JSON number {value!r} is forbidden")
    return parsed


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            fail("json_duplicate_key", f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def strict_json_loads(raw, label):
    if len(raw) > MAX_JSON_BYTES:
        fail("json_too_large", f"{label} exceeds the JSON size limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OutcomeAuditError("json_encoding", f"{label} is not UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_strict_float,
        )
    except OutcomeAuditError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise OutcomeAuditError("json_invalid", f"{label} is invalid JSON") from exc


def strict_json_object(raw, label, *, canonical=False):
    value = strict_json_loads(raw, label)
    if not isinstance(value, dict):
        fail("json_shape", f"{label} is not an object")
    if canonical and canonical_json_bytes(value) != raw:
        fail("json_not_canonical", f"{label} is not canonical JSON")
    return value


def parse_utc(value, label):
    if not isinstance(value, str) or UTC_RE.fullmatch(value) is None:
        fail("timestamp_invalid", f"{label} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise OutcomeAuditError("timestamp_invalid", f"{label} is invalid") from exc
    return parsed.astimezone(timezone.utc)


def _object(properties, required=None, *, additional=False):
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(required if required is not None else properties),
        "additionalProperties": additional,
    }


SHA_SCHEMA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
UTC_SCHEMA = {"type": "string", "pattern": UTC_RE.pattern}
EVENT_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 512}
CAMERA_SCHEMA = {"enum": ["ch1", "ch2", "ch3", "ch4"]}
PATH_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 4096}
KEY_SCHEMA = {"type": "string", "pattern": KEY_ID_RE.pattern}
ROLE_SCHEMA = {"type": "string", "pattern": ROLE_RE.pattern}
COMMIT_SCHEMA = {"type": "string", "pattern": COMMIT_RE.pattern}

UNSIGNED_DESCRIPTOR_SCHEMA = _object(
    {"role": ROLE_SCHEMA, "path": PATH_SCHEMA, "sha256": SHA_SCHEMA}
)
SIGNED_DESCRIPTOR_SCHEMA = _object(
    {
        "role": ROLE_SCHEMA,
        "path": PATH_SCHEMA,
        "sha256": SHA_SCHEMA,
        "signature_path": PATH_SCHEMA,
        "signature_sha256": SHA_SCHEMA,
        "key_id": KEY_SCHEMA,
    }
)
DESCRIPTOR_BINDING_SCHEMA = SIGNED_DESCRIPTOR_SCHEMA

PRODUCER_IDENTITY_SCHEMA = _object(
    {
        "principal_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "role": {"const": "evidence_producer"},
        "key_id": KEY_SCHEMA,
        "tool_commit": COMMIT_SCHEMA,
        "tool_digest": SHA_SCHEMA,
    }
)
RETENTION_IDENTITY_SCHEMA = _object(
    {
        "principal_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "role": {"const": "retention_authority"},
        "key_id": KEY_SCHEMA,
        "tool_commit": COMMIT_SCHEMA,
        "tool_digest": SHA_SCHEMA,
    }
)


def _true_gates(keys):
    return _object({key: {"const": True} for key in sorted(keys)})


def _artifact_schema(role):
    properties = {
        "schema": {"const": ACCEPTANCE_ARTIFACT_SCHEMAS[role]},
        "event_id": EVENT_SCHEMA,
        "source_observation_sha256": SHA_SCHEMA,
        "camera_id": CAMERA_SCHEMA,
        "producer": PRODUCER_IDENTITY_SCHEMA,
        "acceptance_eligible": {"const": True},
        "gates": _true_gates(ARTIFACT_GATES[role]),
        "acceptance_failures": {"type": "array", "maxItems": 0},
    }
    if role == "static_calibration":
        properties.update({
            "camera_config_sha256": SHA_SCHEMA,
            "map_sha256": SHA_SCHEMA,
            "calibration_manifest_sha256": SHA_SCHEMA,
            "media_timestamp_utc": UTC_SCHEMA,
            "valid_from_utc": UTC_SCHEMA,
            "valid_until_utc": UTC_SCHEMA,
        })
    return _object(properties)


ARTIFACT_JSON_SCHEMAS = {
    role: _artifact_schema(role) for role in sorted(ACCEPTANCE_ARTIFACTS)
}
ACCEPTANCE_JSON_SCHEMA = _object({
    "schema": {"const": ACCEPTANCE_REPORT_SCHEMA},
    "event_id": EVENT_SCHEMA,
    "source_observation_sha256": SHA_SCHEMA,
    "camera_id": CAMERA_SCHEMA,
    "producer": PRODUCER_IDENTITY_SCHEMA,
    "acceptance_eligible": {"const": True},
    "gates": _true_gates(ACCEPTANCE_GATES),
    "artifacts": _object({
        role: {
            "allOf": [
                SIGNED_DESCRIPTOR_SCHEMA,
                {"properties": {"role": {"const": role}}},
            ]
        }
        for role in sorted(ACCEPTANCE_ARTIFACTS)
    }),
    "acceptance_failures": {"type": "array", "maxItems": 0},
})

RETENTION_POLICY_JSON_SCHEMA = _object({
    "schema": {"const": RETENTION_POLICY_SCHEMA},
    "policy_id": {"type": "string", "minLength": 1, "maxLength": 256},
    "stream_name": {"type": "string", "pattern": "^v2x-backend-cam-ch[1-4]$"},
    "retention_seconds": {"type": "integer", "minimum": 1},
    "effective_from_utc": UTC_SCHEMA,
    "effective_until_utc": UTC_SCHEMA,
    "captured_at_utc": UTC_SCHEMA,
    "authority": RETENTION_IDENTITY_SCHEMA,
})
RETRIEVAL_RECEIPT_JSON_SCHEMA = _object({
    "schema": {"const": RETRIEVAL_RECEIPT_SCHEMA},
    "event_id": EVENT_SCHEMA,
    "source_observation_sha256": SHA_SCHEMA,
    "camera_id": CAMERA_SCHEMA,
    "stream_name": {"type": "string", "pattern": "^v2x-backend-cam-ch[1-4]$"},
    "requested_media_timestamp_utc": UTC_SCHEMA,
    "attempt_number": {"type": "integer", "minimum": 1},
    "attempted_at_utc": UTC_SCHEMA,
    "error_code": {"enum": sorted(SUPPORTED_RETRIEVAL_ERRORS)},
    "policy_sha256": SHA_SCHEMA,
    "authority": RETENTION_IDENTITY_SCHEMA,
})
UNAVAILABILITY_JSON_SCHEMA = _object({
    "schema": {"const": UNAVAILABILITY_REPORT_SCHEMA},
    "event_id": EVENT_SCHEMA,
    "source_observation_sha256": SHA_SCHEMA,
    "camera_id": CAMERA_SCHEMA,
    "reason_code": {"const": UNAVAILABLE_REASON},
    "requested_media_timestamp_utc": UTC_SCHEMA,
    "stream_name": {"type": "string", "pattern": "^v2x-backend-cam-ch[1-4]$"},
    "attempt_count": {"type": "integer", "minimum": 1},
    "last_attempt_utc": UTC_SCHEMA,
    "retention_expired_before_utc": UTC_SCHEMA,
    "policy": {
        "allOf": [
            SIGNED_DESCRIPTOR_SCHEMA,
            {"properties": {"role": {"const": "retention_policy"}}},
        ]
    },
    "receipts": {
        "type": "array",
        "minItems": 1,
        "items": {
            "allOf": [
                SIGNED_DESCRIPTOR_SCHEMA,
                {"properties": {"role": {"const": "retrieval_receipt"}}},
            ]
        },
    },
    "authority": RETENTION_IDENTITY_SCHEMA,
})

OBSERVATION_JSON_SCHEMA = _object(
    {
        "schema": {"const": OBSERVATION_SCHEMA},
        "event_id": EVENT_SCHEMA,
        "camera_id": CAMERA_SCHEMA,
        "media_timestamp_utc": UTC_SCHEMA,
    },
    additional=True,
)
LEDGER_MANIFEST_JSON_SCHEMA = _object({
    "schema": {"const": LEDGER_SCHEMA},
    "observations_sha256": SHA_SCHEMA,
    "counts": _object({"observations": {"type": "integer", "minimum": 1}}),
})
OUTCOME_JSON_SCHEMA = _object(
    {
        "schema": {"const": OUTCOME_SCHEMA},
        "event_id": EVENT_SCHEMA,
        "source_observation_sha256": SHA_SCHEMA,
        "state": {"enum": sorted(TERMINAL_STATES)},
        "reason_code": {"type": "string", "pattern": REASON_RE.pattern},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {"oneOf": [UNSIGNED_DESCRIPTOR_SCHEMA, SIGNED_DESCRIPTOR_SCHEMA]},
        },
        "acceptance_report": SIGNED_DESCRIPTOR_SCHEMA,
        "unavailability_report": SIGNED_DESCRIPTOR_SCHEMA,
    },
    required={
        "schema", "event_id", "source_observation_sha256", "state",
        "reason_code", "evidence",
    },
)

OUTPUT_POLICY_SCHEMA = _object({
    "schema": {"type": "string", "minLength": 1},
    "schema_sha256": SHA_SCHEMA,
    "tool_digest": SHA_SCHEMA,
})


def _output_policy_object(roles):
    return _object({role: OUTPUT_POLICY_SCHEMA for role in sorted(roles)})


PRINCIPAL_SCHEMA = _object({
    "principal_id": {"type": "string", "minLength": 1, "maxLength": 256},
    "role": {"enum": ["evidence_producer", "retention_authority"]},
    "key_id": KEY_SCHEMA,
    "public_key_sha256": SHA_SCHEMA,
    "tool_commit": COMMIT_SCHEMA,
    "tool_digest": SHA_SCHEMA,
    "allowed_outputs": {"type": "object"},
})
ACCEPTED_AUTHORITY_BINDING_SCHEMA = _object({
    "acceptance_report": DESCRIPTOR_BINDING_SCHEMA,
    "artifacts": _object({
        role: DESCRIPTOR_BINDING_SCHEMA for role in sorted(ACCEPTANCE_ARTIFACTS)
    }),
})
UNAVAILABLE_AUTHORITY_BINDING_SCHEMA = _object({
    "unavailability_report": DESCRIPTOR_BINDING_SCHEMA,
    "policy": DESCRIPTOR_BINDING_SCHEMA,
    "receipts": {
        "type": "array", "minItems": 1, "items": DESCRIPTOR_BINDING_SCHEMA
    },
})
AUTHORITY_JSON_SCHEMA = _object({
    "schema": {"const": AUTHORITY_SCHEMA},
    "audit_run_id": {"type": "string", "format": "uuid"},
    "valid_from_utc": UTC_SCHEMA,
    "trusted_audit_time_utc": UTC_SCHEMA,
    "valid_until_utc": UTC_SCHEMA,
    "audit_authority": _object({
        "authority_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "role": {"const": "audit_authority"},
        "key_id": KEY_SCHEMA,
        "public_key_sha256": SHA_SCHEMA,
    }),
    "verifier": _object({
        "release": {"const": VERIFIER_RELEASE},
        "schema_bundle_sha256": SHA_SCHEMA,
    }),
    "inputs": _object({
        "ledger_manifest_sha256": SHA_SCHEMA,
        "ledger_observations_sha256": SHA_SCHEMA,
        "outcomes_sha256": SHA_SCHEMA,
    }),
    "principals": _object({
        "producer": PRINCIPAL_SCHEMA,
        "retention": PRINCIPAL_SCHEMA,
    }),
    "accepted_events": {
        "type": "object", "additionalProperties": ACCEPTED_AUTHORITY_BINDING_SCHEMA
    },
    "unavailable_events": {
        "type": "object", "additionalProperties": UNAVAILABLE_AUTHORITY_BINDING_SCHEMA
    },
})

OUTPUT_JSON_SCHEMAS = {
    "acceptance_report": ACCEPTANCE_JSON_SCHEMA,
    **ARTIFACT_JSON_SCHEMAS,
    "retention_policy": RETENTION_POLICY_JSON_SCHEMA,
    "retrieval_receipt": RETRIEVAL_RECEIPT_JSON_SCHEMA,
    "unavailability_report": UNAVAILABILITY_JSON_SCHEMA,
}
OUTPUT_SCHEMA_NAMES = {
    "acceptance_report": ACCEPTANCE_REPORT_SCHEMA,
    **ACCEPTANCE_ARTIFACT_SCHEMAS,
    "retention_policy": RETENTION_POLICY_SCHEMA,
    "retrieval_receipt": RETRIEVAL_RECEIPT_SCHEMA,
    "unavailability_report": UNAVAILABILITY_REPORT_SCHEMA,
}


def schema_digest(schema):
    return sha256_bytes(canonical_json_bytes(schema))


SCHEMA_BUNDLE_SHA256 = schema_digest({
    "authority": AUTHORITY_JSON_SCHEMA,
    "ledger_manifest": LEDGER_MANIFEST_JSON_SCHEMA,
    "observation": OBSERVATION_JSON_SCHEMA,
    "outcome": OUTCOME_JSON_SCHEMA,
    "outputs": OUTPUT_JSON_SCHEMAS,
})


def expected_output_policies(roles, tool_digest):
    return {
        role: {
            "schema": OUTPUT_SCHEMA_NAMES[role],
            "schema_sha256": schema_digest(OUTPUT_JSON_SCHEMAS[role]),
            "tool_digest": tool_digest,
        }
        for role in sorted(roles)
    }


def validate_schema(value, schema, label, code="schema_invalid"):
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or "<root>"
        raise OutcomeAuditError(code, f"{label} fails schema at {location}: {exc.message}") from exc


@dataclass(frozen=True)
class PinnedKey:
    key_id: str
    path: Path
    fingerprint: str
    public_key: Ed25519PublicKey


def _read_pinned_public_key(key_id, fingerprint, path_value):
    path = Path(path_value).expanduser()
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise OutcomeAuditError("pinned_key_unreadable", f"pinned key {key_id} is unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        fail("pinned_key_unsafe", f"pinned key {key_id} is not a single-link regular file")
    if metadata.st_size > 64 * 1024:
        fail("pinned_key_unsafe", f"pinned key {key_id} is oversized")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > 64 * 1024
        ):
            fail("pinned_key_unsafe", f"pinned key {key_id} opened as an unsafe file")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            fail("pinned_key_replaced", f"pinned key {key_id} changed while it was opened")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
            raw = stream.read(64 * 1024 + 1)
            after = os.fstat(stream.fileno())
        path_after = os.lstat(path)
    except OSError as exc:
        raise OutcomeAuditError("pinned_key_unreadable", f"pinned key {key_id} is unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    expected_identity = (opened.st_dev, opened.st_ino)
    if (
        not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or (after.st_dev, after.st_ino) != expected_identity
        or not stat.S_ISREG(path_after.st_mode)
        or path_after.st_nlink != 1
        or (path_after.st_dev, path_after.st_ino) != expected_identity
    ):
        fail("pinned_key_replaced", f"pinned key {key_id} changed while it was read")
    if sha256_bytes(raw) != fingerprint:
        fail("pinned_key_fingerprint", f"pinned key {key_id} fingerprint does not match")
    try:
        public_key = load_pem_public_key(raw)
    except (TypeError, ValueError) as exc:
        raise OutcomeAuditError("pinned_key_invalid", f"pinned key {key_id} is invalid PEM") from exc
    if not isinstance(public_key, Ed25519PublicKey):
        fail("pinned_key_algorithm", f"pinned key {key_id} is not Ed25519")
    return PinnedKey(key_id, path.absolute(), fingerprint, public_key)


def load_pinned_keys(specifications):
    keys = {}
    for specification in specifications or []:
        try:
            key_id, remainder = specification.split("=", 1)
            fingerprint, path_value = remainder.split(":", 1)
        except ValueError as exc:
            raise OutcomeAuditError(
                "pinned_key_spec",
                "pinned keys use KEY_ID=PUBLIC_KEY_SHA256:PUBLIC_KEY_PATH",
            ) from exc
        if KEY_ID_RE.fullmatch(key_id) is None or SHA256_RE.fullmatch(fingerprint) is None:
            fail("pinned_key_spec", "pinned key ID or fingerprint is invalid")
        if key_id in keys:
            fail("pinned_key_duplicate", f"pinned key {key_id} is repeated")
        keys[key_id] = _read_pinned_public_key(key_id, fingerprint, path_value)
    return keys


def _safe_relative_path(value, label):
    if not isinstance(value, str) or not value or "\\" in value:
        fail("evidence_path_invalid", f"{label} path is invalid")
    if unicodedata.normalize("NFC", value) != value:
        fail("evidence_path_unicode", f"{label} path is not NFC normalized")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        fail("evidence_path_escape", f"{label} path must remain beneath the retained root")
    return pure


def _stable_file_fields(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _stable_directory_fields(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


class RetainedEvidenceStore:
    """Open and snapshot retained evidence without following any path symlink."""

    def __init__(self, root):
        path = Path(root).expanduser()
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise OutcomeAuditError("retained_root_unreadable", "retained root is unreadable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            fail("retained_root_unsafe", "retained root must be a real directory")
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            self.root_fd = os.open(path, flags)
        except OSError as exc:
            raise OutcomeAuditError("retained_root_unreadable", "retained root cannot be pinned") from exc
        pinned = os.fstat(self.root_fd)
        if (pinned.st_dev, pinned.st_ino) != (metadata.st_dev, metadata.st_ino):
            os.close(self.root_fd)
            fail("retained_root_replaced", "retained root changed while it was pinned")
        self.root = path.absolute()
        self._resources = {}
        self._identities = {}
        self._consumers = set()

    def close(self):
        if self.root_fd is not None:
            os.close(self.root_fd)
            self.root_fd = None

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()

    def argument_path(self, value, label):
        path = Path(value).expanduser()
        if path.is_absolute():
            try:
                relative = path.relative_to(self.root)
            except ValueError as exc:
                raise OutcomeAuditError(
                    "evidence_path_escape", f"{label} is outside the retained root"
                ) from exc
            value = relative.as_posix()
        return _safe_relative_path(str(value), label).as_posix()

    def _open_parent(self, pure, label):
        descriptor = os.dup(self.root_fd)
        try:
            for component in pure.parts[:-1]:
                flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                metadata = os.fstat(next_descriptor)
                if not stat.S_ISDIR(metadata.st_mode):
                    os.close(next_descriptor)
                    fail("evidence_path_unsafe", f"{label} has a non-directory component")
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except OutcomeAuditError:
            os.close(descriptor)
            raise
        except OSError as exc:
            os.close(descriptor)
            code = "evidence_symlink" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "evidence_unreadable"
            raise OutcomeAuditError(code, f"{label} parent path is unsafe or unreadable") from exc

    def read(self, consumer_role, content_role, path_value, expected_hash=None, *, max_bytes=MAX_EVIDENCE_BYTES):
        if consumer_role in self._consumers:
            fail("evidence_role_duplicate", f"evidence consumer role {consumer_role} is repeated")
        self._consumers.add(consumer_role)
        pure = _safe_relative_path(path_value, consumer_role)
        relative = pure.as_posix()
        existing = self._resources.get(relative)
        if existing is not None:
            if existing["content_role"] != content_role:
                fail("evidence_role_alias", f"{relative} is reused for a different evidence role")
            if expected_hash is not None and existing["sha256"] != expected_hash:
                fail("evidence_hash", f"{consumer_role} evidence hash does not match")
            existing["consumers"].append(consumer_role)
            return existing["raw"], existing

        parent_fd = self._open_parent(pure, consumer_role)
        file_fd = None
        try:
            parent_before = os.fstat(parent_fd)
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(pure.name, flags, dir_fd=parent_fd)
            before = os.fstat(file_fd)
            path_before = os.stat(pure.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(path_before.st_mode):
                fail("evidence_not_regular", f"{consumer_role} evidence is not a regular file")
            if before.st_nlink != 1 or path_before.st_nlink != 1:
                fail("evidence_hardlink", f"{consumer_role} evidence has multiple hard links")
            if (before.st_dev, before.st_ino) != (path_before.st_dev, path_before.st_ino):
                fail("evidence_replaced", f"{consumer_role} evidence changed before reading")
            if before.st_size > max_bytes:
                fail("evidence_too_large", f"{consumer_role} evidence exceeds its size limit")
            chunks = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(file_fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > max_bytes:
                fail("evidence_too_large", f"{consumer_role} evidence exceeds its size limit")
            after = os.fstat(file_fd)
            path_after = os.stat(pure.name, dir_fd=parent_fd, follow_symlinks=False)
            parent_after = os.fstat(parent_fd)
            if (
                _stable_file_fields(before) != _stable_file_fields(after)
                or _stable_file_fields(before) != _stable_file_fields(path_after)
                or _stable_directory_fields(parent_before) != _stable_directory_fields(parent_after)
            ):
                fail("evidence_replaced", f"{consumer_role} evidence changed while reading")
        except OutcomeAuditError:
            raise
        except OSError as exc:
            code = "evidence_symlink" if exc.errno == errno.ELOOP else "evidence_unreadable"
            raise OutcomeAuditError(code, f"{consumer_role} evidence is unsafe or unreadable") from exc
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(parent_fd)

        actual_hash = sha256_bytes(raw)
        if expected_hash is not None and actual_hash != expected_hash:
            fail("evidence_hash", f"{consumer_role} evidence hash does not match")
        identity = (before.st_dev, before.st_ino)
        if identity in self._identities:
            fail("evidence_hardlink", f"{consumer_role} duplicates another evidence inode")
        self._identities[identity] = relative
        record = {
            "content_role": content_role,
            "path": relative,
            "sha256": actual_hash,
            "size": len(raw),
            "device": before.st_dev,
            "inode": before.st_ino,
            "mtime_ns": before.st_mtime_ns,
            "ctime_ns": before.st_ctime_ns,
            "raw": raw,
            "consumers": [consumer_role],
        }
        self._resources[relative] = record
        return raw, record

    def verify_current(self):
        for relative, record in sorted(self._resources.items()):
            pure = PurePosixPath(relative)
            parent_fd = self._open_parent(pure, f"snapshot:{relative}")
            file_fd = None
            try:
                parent_before = os.fstat(parent_fd)
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                file_fd = os.open(pure.name, flags, dir_fd=parent_fd)
                before = os.fstat(file_fd)
                path_before = os.stat(
                    pure.name, dir_fd=parent_fd, follow_symlinks=False
                )
                expected = (
                    record["device"],
                    record["inode"],
                    record["size"],
                    record["mtime_ns"],
                    record["ctime_ns"],
                )
                actual = (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                if (
                    not stat.S_ISREG(before.st_mode)
                    or not stat.S_ISREG(path_before.st_mode)
                    or before.st_nlink != 1
                    or path_before.st_nlink != 1
                    or actual != expected
                    or _stable_file_fields(before) != _stable_file_fields(path_before)
                ):
                    fail("evidence_replaced", f"{relative} changed before snapshot publication")

                digest = hashlib.sha256()
                total = 0
                while True:
                    chunk = os.read(file_fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)

                after = os.fstat(file_fd)
                path_after = os.stat(
                    pure.name, dir_fd=parent_fd, follow_symlinks=False
                )
                parent_after = os.fstat(parent_fd)
            except OSError as exc:
                raise OutcomeAuditError(
                    "evidence_replaced", f"{relative} disappeared before snapshot"
                ) from exc
            finally:
                if file_fd is not None:
                    os.close(file_fd)
                os.close(parent_fd)
            if (
                _stable_file_fields(before) != _stable_file_fields(after)
                or _stable_file_fields(before) != _stable_file_fields(path_after)
                or _stable_directory_fields(parent_before)
                != _stable_directory_fields(parent_after)
                or total != record["size"]
                or digest.hexdigest() != record["sha256"]
            ):
                fail("evidence_replaced", f"{relative} changed before snapshot publication")

    def snapshot(self):
        self.verify_current()
        entries = [
            {
                "content_role": record["content_role"],
                "path": relative,
                "sha256": record["sha256"],
                "content_address": f"sha256:{record['sha256']}",
                "size": record["size"],
            }
            for relative, record in sorted(self._resources.items())
        ]
        body = {
            "schema": SNAPSHOT_SCHEMA,
            "retained_root": str(self.root),
            "entries": entries,
        }
        body["canonical_sha256"] = sha256_bytes(canonical_json_bytes(body))
        return body


class AuditReport(dict):
    """A report whose retained-evidence guard remains live through publication."""

    def __init__(self, value, evidence_store):
        super().__init__(value)
        self._evidence_store = evidence_store

    def verify_evidence_current(self):
        if self._evidence_store is None:
            fail("evidence_guard_closed", "audit report evidence guard is no longer active")
        self._evidence_store.verify_current()

    def close_evidence(self):
        if self._evidence_store is not None:
            self._evidence_store.close()
            self._evidence_store = None

    def __del__(self):
        try:
            self.close_evidence()
        except Exception:
            pass


def _verify_signature(key, raw, signature, label):
    try:
        key.public_key.verify(signature, raw)
    except InvalidSignature as exc:
        raise OutcomeAuditError("signature_invalid", f"{label} detached signature is invalid") from exc


def _descriptor_binding(entry):
    return {
        field: entry[field]
        for field in (
            "role", "path", "sha256", "signature_path", "signature_sha256", "key_id"
        )
    }


def _principal_identity(principal):
    return {
        field: principal[field]
        for field in ("principal_id", "role", "key_id", "tool_commit", "tool_digest")
    }


def _read_signed_descriptor(store, keys, event_id, entry, expected_role, expected_key_id):
    validate_schema(entry, SIGNED_DESCRIPTOR_SCHEMA, f"{event_id} {expected_role} descriptor")
    if entry["role"] != expected_role:
        fail("evidence_role", f"{event_id} expected {expected_role} evidence")
    if entry["key_id"] != expected_key_id:
        fail("signature_role", f"{event_id} {expected_role} uses an unapproved signing key")
    key = keys.get(entry["key_id"])
    if key is None:
        fail("pinned_key_missing", f"{event_id} {expected_role} signing key is not pinned")
    namespace = f"event:{event_id}:{expected_role}"
    raw, _record = store.read(
        f"{namespace}:content", expected_role, entry["path"], entry["sha256"],
        max_bytes=MAX_JSON_BYTES,
    )
    signature, _signature_record = store.read(
        f"{namespace}:signature",
        f"{expected_role}_signature",
        entry["signature_path"],
        entry["signature_sha256"],
        max_bytes=4096,
    )
    _verify_signature(key, raw, signature, f"{event_id} {expected_role}")
    return raw


def load_ledger(store, directory):
    directory = store.argument_path(directory, "ledger directory")
    manifest_path = f"{directory}/manifest.json"
    observations_path = f"{directory}/observations.ndjson"
    manifest_raw, manifest_record = store.read(
        "ledger:manifest", "ledger_manifest", manifest_path, max_bytes=MAX_JSON_BYTES
    )
    manifest = strict_json_object(manifest_raw, "ledger manifest", canonical=True)
    validate_schema(manifest, LEDGER_MANIFEST_JSON_SCHEMA, "ledger manifest")
    if type(manifest["counts"]["observations"]) is not int:
        fail("schema_invalid", "ledger observation count must be an exact JSON integer")
    observations_raw, observations_record = store.read(
        "ledger:observations",
        "ledger_observations",
        observations_path,
        manifest["observations_sha256"],
    )
    observations = {}
    for line_number, line in enumerate(observations_raw.splitlines(), 1):
        value = strict_json_object(line, f"ledger observation line {line_number}")
        validate_schema(value, OBSERVATION_JSON_SCHEMA, f"ledger observation line {line_number}")
        if line + b"\n" != canonical_json_bytes(value):
            fail("ledger_not_canonical", f"ledger observation line {line_number} is not canonical")
        event_id = value["event_id"]
        if event_id in observations:
            fail("ledger_duplicate_event", f"ledger event {event_id} is duplicated")
        observations[event_id] = {
            "value": value,
            "sha256": sha256_bytes(canonical_json_bytes(value)),
        }
    if len(observations) != manifest["counts"]["observations"]:
        fail("ledger_count", "ledger observation count does not match manifest")
    return {
        "manifest_path": manifest_path,
        "manifest_sha256": manifest_record["sha256"],
        "observations_path": observations_path,
        "observations_sha256": observations_record["sha256"],
        "observations": observations,
    }


def load_outcome_rows(store, path, observations):
    path = store.argument_path(path, "outcome ledger")
    raw, record = store.read("outcomes:ledger", "outcome_ledger", path)
    outcomes = {}
    for line_number, line in enumerate(raw.splitlines(), 1):
        value = strict_json_object(line, f"outcome line {line_number}")
        validate_schema(value, OUTCOME_JSON_SCHEMA, f"outcome line {line_number}")
        if line + b"\n" != canonical_json_bytes(value):
            fail("outcome_not_canonical", f"outcome line {line_number} is not canonical")
        event_id = value["event_id"]
        if event_id in outcomes:
            fail("outcome_duplicate_event", f"outcome event {event_id} is duplicated")
        observation = observations.get(event_id)
        if observation is None:
            fail("outcome_extra_event", f"outcome event {event_id} is outside the ledger")
        if value["source_observation_sha256"] != observation["sha256"]:
            fail("outcome_observation_binding", f"outcome event {event_id} has the wrong observation hash")
        state = value["state"]
        reason = value["reason_code"]
        if state == "accepted":
            if reason != "all_acceptance_gates_passed" or "acceptance_report" not in value:
                fail("outcome_state_shape", f"accepted event {event_id} lacks its acceptance binding")
            if "unavailability_report" in value:
                fail("outcome_state_shape", f"accepted event {event_id} carries unavailable proof")
        elif state == "unavailable":
            if reason != UNAVAILABLE_REASON or "unavailability_report" not in value:
                fail("outcome_state_shape", f"unavailable event {event_id} lacks expiry proof")
            if "acceptance_report" in value:
                fail("outcome_state_shape", f"unavailable event {event_id} carries acceptance proof")
        else:
            if reason in {"all_acceptance_gates_passed", UNAVAILABLE_REASON}:
                fail("outcome_state_shape", f"rejected event {event_id} has an incompatible reason")
            if "acceptance_report" in value or "unavailability_report" in value:
                fail("outcome_state_shape", f"rejected event {event_id} carries success or expiry proof")
        outcomes[event_id] = value
    missing = set(observations) - set(outcomes)
    if missing:
        fail("outcome_missing_event", "outcome ledger does not account for every observation")
    return {"path": path, "raw": raw, "sha256": record["sha256"], "values": outcomes}


def _validate_principal(principal, role, keys, output_roles):
    validate_schema(principal, PRINCIPAL_SCHEMA, f"{role} principal")
    if principal["role"] != role:
        fail("authority_role", f"{role} principal declares the wrong role")
    key = keys.get(principal["key_id"])
    if key is None:
        fail("pinned_key_missing", f"{role} principal key is not pinned")
    if principal["public_key_sha256"] != key.fingerprint:
        fail("authority_key_binding", f"{role} principal key fingerprint does not match")
    expected = expected_output_policies(output_roles, principal["tool_digest"])
    if principal["allowed_outputs"] != expected:
        fail("authority_output_policy", f"{role} output schema or tool digest policy is wrong")
    return key


def load_authority(
    store,
    manifest_path,
    signature_path,
    pinned_keys,
    expected_audit_run_id,
    ledger,
    outcomes,
):
    if not pinned_keys:
        fail("pinned_key_missing", "the pinned key allowlist is empty")
    if not expected_audit_run_id:
        fail("audit_run_missing", "the expected audit-run ID is required")
    try:
        uuid.UUID(expected_audit_run_id)
    except (ValueError, AttributeError) as exc:
        raise OutcomeAuditError("audit_run_invalid", "expected audit-run ID is invalid") from exc
    manifest_path = store.argument_path(manifest_path, "authority manifest")
    signature_path = store.argument_path(signature_path, "authority signature")
    raw, manifest_record = store.read(
        "authority:manifest", "audit_authority_manifest", manifest_path,
        max_bytes=MAX_JSON_BYTES,
    )
    manifest = strict_json_object(raw, "audit-authority manifest", canonical=True)
    validate_schema(manifest, AUTHORITY_JSON_SCHEMA, "audit-authority manifest")
    signature, signature_record = store.read(
        "authority:signature", "audit_authority_signature", signature_path,
        max_bytes=4096,
    )
    authority = manifest["audit_authority"]
    authority_key = pinned_keys.get(authority["key_id"])
    if authority_key is None:
        fail("pinned_key_missing", "audit-authority signing key is not pinned")
    if authority["public_key_sha256"] != authority_key.fingerprint:
        fail("authority_key_binding", "audit-authority key fingerprint does not match")
    _verify_signature(authority_key, raw, signature, "audit-authority manifest")
    if manifest["audit_run_id"] != expected_audit_run_id:
        fail("audit_run_replay", "audit-authority run ID does not match the expected run")
    if manifest["verifier"] != {
        "release": VERIFIER_RELEASE,
        "schema_bundle_sha256": SCHEMA_BUNDLE_SHA256,
    }:
        fail("verifier_policy", "audit-authority manifest targets another verifier/schema bundle")
    valid_from = parse_utc(manifest["valid_from_utc"], "authority validity start")
    trusted_time = parse_utc(manifest["trusted_audit_time_utc"], "trusted audit time")
    valid_until = parse_utc(manifest["valid_until_utc"], "authority validity end")
    if not valid_from <= trusted_time <= valid_until:
        fail("authority_time", "trusted audit time is outside authority validity")
    if trusted_time > datetime.now(timezone.utc) + timedelta(seconds=FUTURE_SKEW_SECONDS):
        fail("authority_time", "trusted audit time is too far in the future")
    expected_inputs = {
        "ledger_manifest_sha256": ledger["manifest_sha256"],
        "ledger_observations_sha256": ledger["observations_sha256"],
        "outcomes_sha256": outcomes["sha256"],
    }
    if manifest["inputs"] != expected_inputs:
        fail("authority_input_binding", "audit-authority manifest does not bind the exact inputs")
    producer = manifest["principals"]["producer"]
    retention = manifest["principals"]["retention"]
    _validate_principal(producer, "evidence_producer", pinned_keys, SIGNED_ROLES)
    _validate_principal(retention, "retention_authority", pinned_keys, RETENTION_ROLES)
    principal_ids = {
        authority["authority_id"], producer["principal_id"], retention["principal_id"]
    }
    key_ids = {authority["key_id"], producer["key_id"], retention["key_id"]}
    fingerprints = {
        authority["public_key_sha256"],
        producer["public_key_sha256"],
        retention["public_key_sha256"],
    }
    if len(principal_ids) != 3 or len(key_ids) != 3 or len(fingerprints) != 3:
        fail("authority_role_separation", "audit, producer, and retention identities/keys must differ")
    accepted_ids = {event for event, value in outcomes["values"].items() if value["state"] == "accepted"}
    unavailable_ids = {event for event, value in outcomes["values"].items() if value["state"] == "unavailable"}
    if set(manifest["accepted_events"]) != accepted_ids:
        fail("authority_event_binding", "authority accepted-event bindings are not exact")
    if set(manifest["unavailable_events"]) != unavailable_ids:
        fail("authority_event_binding", "authority unavailable-event bindings are not exact")
    return {
        "value": manifest,
        "path": manifest_path,
        "sha256": manifest_record["sha256"],
        "signature_path": signature_path,
        "signature_sha256": signature_record["sha256"],
        "trusted_time": trusted_time,
        "producer": producer,
        "retention": retention,
    }


def _load_event_evidence(store, keys, event_id, entries, expected_roles, key_id):
    if not isinstance(entries, list):
        fail("evidence_shape", f"event {event_id} evidence is not a list")
    by_role = {}
    raw_by_role = {}
    for entry in entries:
        role = entry.get("role") if isinstance(entry, dict) else None
        if role not in expected_roles or role in by_role:
            fail("evidence_role", f"event {event_id} evidence roles are not exact")
        raw_by_role[role] = _read_signed_descriptor(
            store, keys, event_id, entry, role, key_id
        )
        by_role[role] = entry
    if set(by_role) != set(expected_roles):
        fail("evidence_role", f"event {event_id} evidence roles are incomplete")
    return by_role, raw_by_role


def validate_accepted_outcome(store, keys, event_id, outcome, observation, authority):
    producer = authority["producer"]
    expected_roles = {"acceptance_report", *ACCEPTANCE_ARTIFACTS}
    evidence, raw = _load_event_evidence(
        store, keys, event_id, outcome["evidence"], expected_roles, producer["key_id"]
    )
    if outcome["acceptance_report"] != evidence["acceptance_report"]:
        fail("acceptance_binding", f"event {event_id} acceptance descriptor is not in evidence")
    authority_binding = authority["value"]["accepted_events"][event_id]
    expected_authority_binding = {
        "acceptance_report": _descriptor_binding(evidence["acceptance_report"]),
        "artifacts": {
            role: _descriptor_binding(evidence[role])
            for role in sorted(ACCEPTANCE_ARTIFACTS)
        },
    }
    if authority_binding != expected_authority_binding:
        fail("authority_artifact_binding", f"authority does not bind every artifact for {event_id}")

    report = strict_json_object(raw["acceptance_report"], f"{event_id} acceptance report", canonical=True)
    validate_schema(report, ACCEPTANCE_JSON_SCHEMA, f"{event_id} acceptance report")
    expected_identity = _principal_identity(producer)
    if report["producer"] != expected_identity:
        fail("producer_identity", f"event {event_id} acceptance report producer is not approved")
    if (
        report["event_id"] != event_id
        or report["source_observation_sha256"] != observation["sha256"]
        or report["camera_id"] != observation["value"]["camera_id"]
    ):
        fail("acceptance_binding", f"event {event_id} acceptance report is not observation-bound")
    if report["artifacts"] != {
        role: evidence[role] for role in sorted(ACCEPTANCE_ARTIFACTS)
    }:
        fail("acceptance_binding", f"event {event_id} acceptance report artifact bindings differ")

    artifacts = {}
    for role in sorted(ACCEPTANCE_ARTIFACTS):
        artifact = strict_json_object(raw[role], f"{event_id} {role} artifact", canonical=True)
        validate_schema(artifact, ARTIFACT_JSON_SCHEMAS[role], f"{event_id} {role} artifact")
        if artifact["producer"] != expected_identity:
            fail("producer_identity", f"event {event_id} {role} producer is not approved")
        if (
            artifact["event_id"] != event_id
            or artifact["source_observation_sha256"] != observation["sha256"]
            or artifact["camera_id"] != observation["value"]["camera_id"]
        ):
            fail("artifact_observation_binding", f"event {event_id} {role} is not observation-bound")
        if role == "static_calibration":
            media_time = parse_utc(observation["value"]["media_timestamp_utc"], "observation media time")
            if artifact["media_timestamp_utc"] != observation["value"]["media_timestamp_utc"]:
                fail("static_epoch_binding", f"event {event_id} static artifact has another media time")
            valid_from = parse_utc(artifact["valid_from_utc"], "static validity start")
            valid_until = parse_utc(artifact["valid_until_utc"], "static validity end")
            if not valid_from <= media_time <= valid_until:
                fail("static_epoch_binding", f"event {event_id} lies outside the static camera epoch")
            for name in (
                "camera_config_sha256", "map_sha256", "calibration_manifest_sha256"
            ):
                if SHA256_RE.fullmatch(artifact[name]) is None:
                    fail("static_hash_binding", f"event {event_id} static {name} is invalid")
        artifacts[role] = {
            **_descriptor_binding(evidence[role]),
            "schema": artifact["schema"],
        }
    return {
        "acceptance_report": {
            **_descriptor_binding(evidence["acceptance_report"]),
            "schema": report["schema"],
        },
        "artifacts": artifacts,
    }


def validate_unavailable_outcome(store, keys, event_id, outcome, observation, authority):
    retention = authority["retention"]
    report_entry = outcome["unavailability_report"]
    report_raw = _read_signed_descriptor(
        store, keys, event_id, report_entry, "unavailability_report", retention["key_id"]
    )
    report = strict_json_object(report_raw, f"{event_id} unavailability report", canonical=True)
    validate_schema(report, UNAVAILABILITY_JSON_SCHEMA, f"{event_id} unavailability report")
    if type(report["attempt_count"]) is not int:
        fail("schema_invalid", f"event {event_id} attempt count must be an exact JSON integer")
    receipt_count = report["attempt_count"]
    expected_roles = {"unavailability_report", "retention_policy"}
    expected_receipt_entries = report["receipts"]
    if len(expected_receipt_entries) != receipt_count:
        fail("retention_receipt_count", f"event {event_id} attempt and receipt counts differ")

    evidence_by_role = {}
    for entry in outcome["evidence"]:
        role = entry.get("role") if isinstance(entry, dict) else None
        if role == "retrieval_receipt":
            evidence_by_role.setdefault(role, []).append(entry)
        elif role in expected_roles and role not in evidence_by_role:
            evidence_by_role[role] = entry
        else:
            fail("evidence_role", f"event {event_id} unavailable evidence roles are invalid")
    if evidence_by_role.get("unavailability_report") != report_entry:
        fail("unavailability_binding", f"event {event_id} report descriptor is not in evidence")
    if evidence_by_role.get("retention_policy") != report["policy"]:
        fail("unavailability_binding", f"event {event_id} policy descriptor differs")
    if evidence_by_role.get("retrieval_receipt") != expected_receipt_entries:
        fail("unavailability_binding", f"event {event_id} receipt descriptors differ")

    policy_raw = _read_signed_descriptor(
        store, keys, event_id, report["policy"], "retention_policy", retention["key_id"]
    )
    receipt_raws = []
    for index, entry in enumerate(expected_receipt_entries, 1):
        validate_schema(entry, SIGNED_DESCRIPTOR_SCHEMA, f"{event_id} receipt descriptor")
        if entry["role"] != "retrieval_receipt" or entry["key_id"] != retention["key_id"]:
            fail("signature_role", f"event {event_id} receipt {index} uses the wrong role/key")
        key = keys.get(entry["key_id"])
        if key is None:
            fail("pinned_key_missing", f"event {event_id} receipt key is not pinned")
        namespace = f"event:{event_id}:retrieval_receipt:{index}"
        raw, _ = store.read(
            f"{namespace}:content", "retrieval_receipt", entry["path"], entry["sha256"],
            max_bytes=MAX_JSON_BYTES,
        )
        signature, _ = store.read(
            f"{namespace}:signature", "retrieval_receipt_signature",
            entry["signature_path"], entry["signature_sha256"], max_bytes=4096,
        )
        _verify_signature(key, raw, signature, f"{event_id} receipt {index}")
        receipt_raws.append(raw)

    expected_identity = _principal_identity(retention)
    if report["authority"] != expected_identity:
        fail("retention_identity", f"event {event_id} report authority is not approved")
    expected_stream = f"v2x-backend-cam-{observation['value']['camera_id']}"
    requested_text = observation["value"]["media_timestamp_utc"]
    if (
        report["event_id"] != event_id
        or report["source_observation_sha256"] != observation["sha256"]
        or report["camera_id"] != observation["value"]["camera_id"]
        or report["requested_media_timestamp_utc"] != requested_text
        or report["stream_name"] != expected_stream
    ):
        fail("unavailability_binding", f"event {event_id} report is not exact-stream/time bound")

    policy = strict_json_object(policy_raw, f"{event_id} retention policy", canonical=True)
    validate_schema(policy, RETENTION_POLICY_JSON_SCHEMA, f"{event_id} retention policy")
    if type(policy["retention_seconds"]) is not int:
        fail("schema_invalid", f"event {event_id} retention seconds must be an exact JSON integer")
    if policy["authority"] != expected_identity or policy["stream_name"] != expected_stream:
        fail("retention_policy_binding", f"event {event_id} retention policy is not approved/bound")
    trusted = authority["trusted_time"]
    policy_from = parse_utc(policy["effective_from_utc"], "retention policy start")
    policy_until = parse_utc(policy["effective_until_utc"], "retention policy end")
    captured = parse_utc(policy["captured_at_utc"], "retention policy capture")
    if captured > trusted + timedelta(seconds=FUTURE_SKEW_SECONDS):
        fail("retention_policy_time", f"event {event_id} policy snapshot is future-dated")
    if not policy_from <= captured <= policy_until:
        fail("retention_policy_time", f"event {event_id} policy capture is outside policy validity")

    receipts = []
    previous_attempt = None
    for index, raw in enumerate(receipt_raws, 1):
        receipt = strict_json_object(raw, f"{event_id} receipt {index}", canonical=True)
        validate_schema(receipt, RETRIEVAL_RECEIPT_JSON_SCHEMA, f"{event_id} receipt {index}")
        if type(receipt["attempt_number"]) is not int:
            fail("schema_invalid", f"event {event_id} receipt attempt number must be an exact JSON integer")
        attempted = parse_utc(receipt["attempted_at_utc"], f"receipt {index} attempt time")
        if (
            receipt["authority"] != expected_identity
            or receipt["event_id"] != event_id
            or receipt["source_observation_sha256"] != observation["sha256"]
            or receipt["camera_id"] != observation["value"]["camera_id"]
            or receipt["stream_name"] != expected_stream
            or receipt["requested_media_timestamp_utc"] != requested_text
            or receipt["attempt_number"] != index
            or receipt["policy_sha256"] != report["policy"]["sha256"]
        ):
            fail("retention_receipt_binding", f"event {event_id} receipt {index} is not exact-bound")
        if attempted > trusted + timedelta(seconds=FUTURE_SKEW_SECONDS):
            fail("retention_receipt_future", f"event {event_id} receipt {index} is future-dated")
        if not policy_from <= attempted <= policy_until:
            fail("retention_policy_time", f"event {event_id} receipt {index} is outside policy validity")
        if previous_attempt is not None and attempted < previous_attempt:
            fail("retention_receipt_order", f"event {event_id} receipts are not ordered")
        previous_attempt = attempted
        receipts.append(receipt)

    requested = parse_utc(requested_text, "unavailable requested timestamp")
    last_attempt = parse_utc(report["last_attempt_utc"], "last retrieval attempt")
    reported_boundary = parse_utc(
        report["retention_expired_before_utc"], "retention expiry boundary"
    )
    if previous_attempt != last_attempt:
        fail("retention_receipt_order", f"event {event_id} final receipt is not the last attempt")
    if not policy_from <= last_attempt <= policy_until:
        fail("retention_policy_time", f"event {event_id} last attempt is outside policy validity")
    computed_boundary = last_attempt - timedelta(seconds=policy["retention_seconds"])
    if reported_boundary != computed_boundary or not requested < computed_boundary:
        fail("retention_expiry", f"event {event_id} proof does not establish source-pixel expiry")

    authority_binding = authority["value"]["unavailable_events"][event_id]
    expected_binding = {
        "unavailability_report": _descriptor_binding(report_entry),
        "policy": _descriptor_binding(report["policy"]),
        "receipts": [_descriptor_binding(entry) for entry in expected_receipt_entries],
    }
    if authority_binding != expected_binding:
        fail("authority_artifact_binding", f"authority does not bind unavailable proof for {event_id}")
    return {
        "unavailability_report": _descriptor_binding(report_entry),
        "policy": _descriptor_binding(report["policy"]),
        "receipts": [_descriptor_binding(entry) for entry in expected_receipt_entries],
    }


def validate_rejected_outcome(store, event_id, outcome):
    validated = []
    roles = set()
    for index, entry in enumerate(outcome["evidence"], 1):
        validate_schema(entry, UNSIGNED_DESCRIPTOR_SCHEMA, f"{event_id} rejection evidence {index}")
        role = entry["role"]
        if not role.startswith("rejection_") or role in roles:
            fail("evidence_role", f"event {event_id} rejection evidence role is invalid/duplicate")
        roles.add(role)
        _raw, _record = store.read(
            f"event:{event_id}:{role}", role, entry["path"], entry["sha256"]
        )
        validated.append(dict(entry))
    return validated


def _assemble_report(validated, authority, ledger, outcomes, snapshot):
    counts = Counter(value["state"] for value in validated.values())
    by_camera = {}
    for camera in sorted({value["camera_id"] for value in validated.values()}):
        camera_counts = Counter(
            value["state"] for value in validated.values() if value["camera_id"] == camera
        )
        by_camera[camera] = {
            state: camera_counts.get(state, 0) for state in sorted(TERMINAL_STATES)
        }
    recoverable = len(validated) - counts.get("unavailable", 0)
    accepted = counts.get("accepted", 0)
    accepted_fraction = accepted / recoverable if recoverable else 0.0
    threshold_passed = (
        math.isfinite(accepted_fraction)
        and accepted_fraction >= MINIMUM_ACCEPTED_FRACTION
    )
    return {
        "schema": REPORT_SCHEMA,
        "acceptance_eligible": threshold_passed,
        "audit_run_id": authority["value"]["audit_run_id"],
        "trusted_audit_time_utc": authority["value"]["trusted_audit_time_utc"],
        "authority": {
            "manifest": {"path": authority["path"], "sha256": authority["sha256"]},
            "signature": {
                "path": authority["signature_path"],
                "sha256": authority["signature_sha256"],
                "key_id": authority["value"]["audit_authority"]["key_id"],
            },
        },
        "inputs": {
            "ledger_manifest": {
                "path": ledger["manifest_path"], "sha256": ledger["manifest_sha256"]
            },
            "ledger_observations": {
                "path": ledger["observations_path"], "sha256": ledger["observations_sha256"]
            },
            "outcomes": {"path": outcomes["path"], "sha256": outcomes["sha256"]},
        },
        "evidence_snapshot": snapshot,
        "counts": {
            "observations": len(validated),
            "accepted_fraction_numerator": accepted,
            "recoverable_denominator": recoverable,
            "denominator_exclusions": {"unavailable": counts.get("unavailable", 0)},
            **{state: counts.get(state, 0) for state in sorted(TERMINAL_STATES)},
            "by_camera": by_camera,
        },
        "accepted_fraction_of_recoverable": accepted_fraction,
        "gates": {
            "every_observation_has_exactly_one_terminal_outcome": True,
            "all_evidence_is_retained_root_bound_and_descriptor_pinned": True,
            "accepted_reports_are_signed_role_separated_and_observation_bound": True,
            "unavailable_has_signed_policy_and_exact_retrieval_receipts": True,
            "minimum_accepted_fraction": MINIMUM_ACCEPTED_FRACTION,
            "minimum_accepted_fraction_passed": threshold_passed,
        },
        "acceptance_failures": (
            [] if threshold_passed else ["minimum_accepted_fraction_not_met"]
        ),
        "outcomes": [validated[event_id] for event_id in sorted(validated)],
    }


def build_report(
    ledger_dir,
    outcomes_path,
    *,
    retained_root=None,
    authority_manifest=None,
    authority_signature=None,
    expected_audit_run_id=None,
    pinned_keys=None,
):
    if retained_root is None or authority_manifest is None or authority_signature is None:
        fail("authority_not_configured", "retained root and signed audit authority are required")
    if not isinstance(pinned_keys, dict) or not pinned_keys:
        fail("pinned_key_missing", "the pinned key allowlist is empty")
    store = RetainedEvidenceStore(retained_root)
    try:
        ledger = load_ledger(store, ledger_dir)
        outcomes = load_outcome_rows(store, outcomes_path, ledger["observations"])
        authority = load_authority(
            store,
            authority_manifest,
            authority_signature,
            pinned_keys,
            expected_audit_run_id,
            ledger,
            outcomes,
        )
        validated = {}
        for event_id in sorted(outcomes["values"]):
            outcome = outcomes["values"][event_id]
            observation = ledger["observations"][event_id]
            state = outcome["state"]
            item = {
                "event_id": event_id,
                "camera_id": observation["value"]["camera_id"],
                "state": state,
                "reason_code": outcome["reason_code"],
                "source_observation_sha256": observation["sha256"],
            }
            if state == "accepted":
                item.update(validate_accepted_outcome(
                    store, pinned_keys, event_id, outcome, observation, authority
                ))
            elif state == "unavailable":
                item.update(validate_unavailable_outcome(
                    store, pinned_keys, event_id, outcome, observation, authority
                ))
            else:
                item["evidence"] = validate_rejected_outcome(store, event_id, outcome)
            validated[event_id] = item
        snapshot = store.snapshot()
        report = _assemble_report(validated, authority, ledger, outcomes, snapshot)
        # The snapshot check occurs before report assembly.  Recheck every retained
        # path immediately before returning so evidence removed in that interval
        # cannot produce a publishable report.
        store.verify_current()
        guarded_report = AuditReport(report, store)
        store = None
        return guarded_report
    finally:
        if store is not None:
            store.close()


def write_report_exclusive(path, report):
    try:
        return _write_report_exclusive(path, report)
    finally:
        if isinstance(report, AuditReport):
            report.close_evidence()


def _write_report_exclusive(path, report):
    original = Path(path).expanduser().absolute()
    if os.path.lexists(original):
        fail("output_exists", "audit output already exists")
    try:
        payload = json.dumps(
            report, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True
        ).encode("utf-8") + b"\n"
        original.parent.mkdir(parents=True, exist_ok=True)
        parent = original.parent.resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise OutcomeAuditError("output_unwritable", "audit output directory is unavailable") from exc
    path = parent / original.name
    if os.path.lexists(path):
        fail("output_exists", "audit output already exists")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    published_descriptor = None
    returned_directory_fd = None
    returned_descriptor = None
    temporary_name = None
    directory_fd = None
    published_identity = None
    publication_succeeded = False
    try:
        directory_fd = os.open(parent, flags)
        directory_metadata = os.fstat(directory_fd)
        path_metadata = os.stat(parent, follow_symlinks=False)
        directory_identity = (directory_metadata.st_dev, directory_metadata.st_ino)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or not stat.S_ISDIR(path_metadata.st_mode)
            or (path_metadata.st_dev, path_metadata.st_ino) != directory_identity
        ):
            fail("output_directory_replaced", "audit output directory changed while it was pinned")
        try:
            os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            fail("output_exists", "audit output already exists")

        for _attempt in range(100):
            temporary_name = f".{path.name}.tmp-{uuid.uuid4().hex}"
            temporary_flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                descriptor = os.open(
                    temporary_name, temporary_flags, 0o600, dir_fd=directory_fd
                )
                break
            except FileExistsError:
                temporary_name = None
        if descriptor is None:
            fail("output_unwritable", "audit output temporary name space is exhausted")

        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError(errno.EIO, "short audit output write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        temporary_metadata = os.fstat(descriptor)

        # Do not make the staged report visible if evidence changed while the
        # payload was serialized and written.  The later post-fsync check also
        # covers changes racing with or following the no-replace link.
        if isinstance(report, AuditReport):
            report.verify_evidence_current()

        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise OutcomeAuditError("output_exists", "audit output already exists") from exc
        published_identity = (temporary_metadata.st_dev, temporary_metadata.st_ino)

        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_name = None
        os.close(descriptor)
        descriptor = None

        published_descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        published_before = os.fstat(published_descriptor)
        if (
            not stat.S_ISREG(published_before.st_mode)
            or published_before.st_nlink != 1
            or (published_before.st_dev, published_before.st_ino) != published_identity
            or published_before.st_size != len(payload)
        ):
            fail("output_replaced", "published audit output is not the staged report")
        expected_digest = sha256_bytes(payload)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(published_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        published_after = os.fstat(published_descriptor)
        current_entry = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            _stable_file_fields(published_before) != _stable_file_fields(published_after)
            or _stable_file_fields(published_before) != _stable_file_fields(current_entry)
            or total != len(payload)
            or digest.hexdigest() != expected_digest
        ):
            fail("output_replaced", "published audit output bytes changed during verification")

        current_parent = os.stat(parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current_parent.st_mode)
            or (current_parent.st_dev, current_parent.st_ino) != directory_identity
        ):
            fail("output_directory_replaced", "audit output directory changed during publication")
        current_path = os.stat(path, follow_symlinks=False)
        if (current_path.st_dev, current_path.st_ino) != published_identity:
            fail("output_replaced", "audit output path does not name the staged report")
        os.fsync(directory_fd)

        # Reopen the directory through the pathname that will be returned and
        # descriptor-verify the final inode and bytes after fsync.  The earlier
        # checks protect the pinned publication directory; these checks also
        # prove that the caller-visible pathname still reaches that directory.
        returned_directory_fd = os.open(parent, flags)
        returned_directory_metadata = os.fstat(returned_directory_fd)
        if (
            not stat.S_ISDIR(returned_directory_metadata.st_mode)
            or (returned_directory_metadata.st_dev, returned_directory_metadata.st_ino)
            != directory_identity
        ):
            fail(
                "output_directory_replaced",
                "audit output directory changed after publication fsync",
            )
        returned_descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=returned_directory_fd,
        )
        returned_before = os.fstat(returned_descriptor)
        returned_path_before = os.stat(
            path.name, dir_fd=returned_directory_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(returned_before.st_mode)
            or returned_before.st_nlink != 1
            or (returned_before.st_dev, returned_before.st_ino) != published_identity
            or returned_before.st_size != len(payload)
            or _stable_file_fields(returned_before)
            != _stable_file_fields(returned_path_before)
        ):
            fail("output_replaced", "returned audit output is not the staged report")
        returned_digest = hashlib.sha256()
        returned_total = 0
        while True:
            chunk = os.read(returned_descriptor, 1024 * 1024)
            if not chunk:
                break
            returned_digest.update(chunk)
            returned_total += len(chunk)
        returned_after = os.fstat(returned_descriptor)
        returned_path_after = os.stat(
            path.name, dir_fd=returned_directory_fd, follow_symlinks=False
        )
        final_parent = os.stat(parent, follow_symlinks=False)
        final_path = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(final_parent.st_mode)
            or (final_parent.st_dev, final_parent.st_ino) != directory_identity
        ):
            fail(
                "output_directory_replaced",
                "audit output directory changed during final publication verification",
            )
        if (
            _stable_file_fields(returned_before) != _stable_file_fields(returned_after)
            or _stable_file_fields(returned_before)
            != _stable_file_fields(returned_path_after)
            or _stable_file_fields(returned_before) != _stable_file_fields(final_path)
            or returned_total != len(payload)
            or returned_digest.hexdigest() != expected_digest
        ):
            fail("output_replaced", "returned audit output bytes changed after fsync")

        # Keep every exact retained input present and byte-identical through the
        # publication boundary.  Only lightweight output identity checks follow
        # this final full evidence re-read so neither side has a long unchecked
        # interval before success.
        if isinstance(report, AuditReport):
            report.verify_evidence_current()
        final_descriptor = os.fstat(returned_descriptor)
        final_parent = os.stat(parent, follow_symlinks=False)
        final_path = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(final_parent.st_mode)
            or (final_parent.st_dev, final_parent.st_ino) != directory_identity
        ):
            fail(
                "output_directory_replaced",
                "audit output directory changed after final evidence verification",
            )
        if (
            _stable_file_fields(returned_before) != _stable_file_fields(final_descriptor)
            or _stable_file_fields(returned_before) != _stable_file_fields(final_path)
        ):
            fail("output_replaced", "audit output changed after final evidence verification")
        publication_succeeded = True
    except OutcomeAuditError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise OutcomeAuditError("output_unwritable", "audit output could not be published") from exc
    finally:
        if returned_descriptor is not None:
            os.close(returned_descriptor)
        if returned_directory_fd is not None:
            os.close(returned_directory_fd)
        if published_descriptor is not None:
            os.close(published_descriptor)
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            try:
                if temporary_name is not None:
                    try:
                        os.unlink(temporary_name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                if published_identity is not None and not publication_succeeded:
                    try:
                        current = os.stat(
                            path.name, dir_fd=directory_fd, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        current = None
                    if current is not None and (
                        current.st_dev, current.st_ino
                    ) == published_identity:
                        os.unlink(path.name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            directory_fd = None
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retained-root", required=True)
    parser.add_argument("--ledger", required=True, help="path beneath retained root")
    parser.add_argument("--outcomes", required=True, help="path beneath retained root")
    parser.add_argument("--authority-manifest", required=True, help="path beneath retained root")
    parser.add_argument("--authority-signature", required=True, help="path beneath retained root")
    parser.add_argument("--expected-audit-run-id", required=True)
    parser.add_argument(
        "--pinned-key",
        action="append",
        default=[],
        metavar="KEY_ID=SHA256:PATH",
        help="out-of-band pinned Ed25519 public key (repeat for each role)",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        keys = load_pinned_keys(args.pinned_key)
        report = build_report(
            args.ledger,
            args.outcomes,
            retained_root=args.retained_root,
            authority_manifest=args.authority_manifest,
            authority_signature=args.authority_signature,
            expected_audit_run_id=args.expected_audit_run_id,
            pinned_keys=keys,
        )
        output = write_report_exclusive(args.output, report)
    except OutcomeAuditError as exc:
        parser.error(str(exc))
    print(json.dumps({
        "output": str(output),
        "acceptance_eligible": report["acceptance_eligible"],
        "counts": report["counts"],
    }, allow_nan=False, sort_keys=True))
    return 0 if report["acceptance_eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
