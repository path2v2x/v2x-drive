import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest

from apps.perception.tools import audit_vehicle_observation_outcomes as audit


def digest(label):
    return hashlib.sha256(label.encode()).hexdigest()


def utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class AuditFixture:
    def __init__(self, tmp_path, *, count=5, accepted=4, unavailable=0):
        self.tmp = tmp_path
        self.root = tmp_path / "retained"
        self.root.mkdir(parents=True)
        self.keys_dir = tmp_path / "keys"
        self.keys_dir.mkdir()
        self.private = {}
        self.key_specs = {}
        for key_id in ("audit-key", "producer-key", "retention-key"):
            self.add_key(key_id)
        self.keys = audit.load_pinned_keys(list(self.key_specs.values()))
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.audit_run_id = str(uuid.uuid4())
        self.producer = {
            "principal_id": "independent-evidence-producer",
            "role": "evidence_producer",
            "key_id": "producer-key",
            "tool_commit": "1" * 40,
            "tool_digest": digest("producer-tool-release"),
        }
        self.retention = {
            "principal_id": "independent-retention-authority",
            "role": "retention_authority",
            "key_id": "retention-key",
            "tool_commit": "2" * 40,
            "tool_digest": digest("retention-tool-release"),
        }
        self.observations = []
        requested = self.now - timedelta(days=2)
        for index in range(count):
            self.observations.append({
                "schema": audit.OBSERVATION_SCHEMA,
                "event_id": f"event-{index}",
                "camera_id": f"ch{index % 4 + 1}",
                "media_timestamp_utc": utc(requested + timedelta(seconds=index)),
            })
        self.ledger_dir = "ledger"
        (self.root / self.ledger_dir).mkdir()
        self.observations_path = self.root / self.ledger_dir / "observations.ndjson"
        self.manifest_path = self.root / self.ledger_dir / "manifest.json"
        self.write_ledger()

        self.artifact_values = {}
        self.artifact_descriptors = {}
        self.acceptance_values = {}
        self.acceptance_descriptors = {}
        self.policy_values = {}
        self.policy_descriptors = {}
        self.receipt_values = {}
        self.receipt_descriptors = {}
        self.unavailability_values = {}
        self.unavailability_descriptors = {}
        self.rows = []
        for index, observation in enumerate(self.observations):
            if index < accepted:
                self.rows.append(self.make_accepted(observation))
            elif index < accepted + unavailable:
                self.rows.append(self.make_unavailable(observation))
            else:
                self.rows.append(self.make_rejected(observation))
        self.outcomes_rel = "outcomes.ndjson"
        self.outcomes_path = self.root / self.outcomes_rel
        self.authority_rel = "authority/manifest.json"
        self.authority_signature_rel = "authority/manifest.sig"
        (self.root / "authority").mkdir()
        self.authority = None
        self.rewrite_outcomes_and_authority()

    def add_key(self, key_id):
        private = Ed25519PrivateKey.generate()
        public_raw = private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        path = self.keys_dir / f"{key_id}.pem"
        path.write_bytes(public_raw)
        fingerprint = hashlib.sha256(public_raw).hexdigest()
        self.private[key_id] = private
        self.key_specs[key_id] = f"{key_id}={fingerprint}:{path}"
        return fingerprint

    def relative(self, path):
        return path.relative_to(self.root).as_posix()

    def write_json(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = audit.canonical_json_bytes(value)
        path.write_bytes(raw)
        return raw

    def write_ledger(self):
        body = b"".join(audit.canonical_json_bytes(value) for value in self.observations)
        self.observations_path.write_bytes(body)
        self.manifest_path.write_bytes(audit.canonical_json_bytes({
            "schema": audit.LEDGER_SCHEMA,
            "observations_sha256": hashlib.sha256(body).hexdigest(),
            "counts": {"observations": len(self.observations)},
        }))

    def sign_value(self, event_id, role, value, key_id, *, suffix=""):
        stem = f"evidence/{event_id}/{role}{suffix}"
        raw = self.write_json(f"{stem}.json", value)
        signature = self.private[key_id].sign(raw)
        signature_path = self.root / f"{stem}.sig"
        signature_path.write_bytes(signature)
        return {
            "role": role,
            "path": f"{stem}.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "signature_path": f"{stem}.sig",
            "signature_sha256": hashlib.sha256(signature).hexdigest(),
            "key_id": key_id,
        }

    def observation_hash(self, observation):
        return hashlib.sha256(audit.canonical_json_bytes(observation)).hexdigest()

    def artifact_value(self, observation, role):
        value = {
            "schema": audit.ACCEPTANCE_ARTIFACT_SCHEMAS[role],
            "event_id": observation["event_id"],
            "source_observation_sha256": self.observation_hash(observation),
            "camera_id": observation["camera_id"],
            "producer": dict(self.producer),
            "acceptance_eligible": True,
            "gates": {gate: True for gate in sorted(audit.ARTIFACT_GATES[role])},
            "acceptance_failures": [],
        }
        if role == "static_calibration":
            value.update({
                "camera_config_sha256": digest(f"camera-config-{observation['camera_id']}"),
                "map_sha256": digest("approved-map"),
                "calibration_manifest_sha256": digest(f"calibration-{observation['camera_id']}"),
                "media_timestamp_utc": observation["media_timestamp_utc"],
                "valid_from_utc": utc(self.now - timedelta(days=3)),
                "valid_until_utc": utc(self.now + timedelta(days=1)),
            })
        return value

    def make_accepted(self, observation):
        event_id = observation["event_id"]
        values = {}
        descriptors = {}
        for role in sorted(audit.ACCEPTANCE_ARTIFACTS):
            values[role] = self.artifact_value(observation, role)
            descriptors[role] = self.sign_value(
                event_id, role, values[role], "producer-key"
            )
        self.artifact_values[event_id] = values
        self.artifact_descriptors[event_id] = descriptors
        report = {
            "schema": audit.ACCEPTANCE_REPORT_SCHEMA,
            "event_id": event_id,
            "source_observation_sha256": self.observation_hash(observation),
            "camera_id": observation["camera_id"],
            "producer": dict(self.producer),
            "acceptance_eligible": True,
            "gates": {gate: True for gate in sorted(audit.ACCEPTANCE_GATES)},
            "artifacts": dict(descriptors),
            "acceptance_failures": [],
        }
        self.acceptance_values[event_id] = report
        report_descriptor = self.sign_value(
            event_id, "acceptance_report", report, "producer-key"
        )
        self.acceptance_descriptors[event_id] = report_descriptor
        return {
            "schema": audit.OUTCOME_SCHEMA,
            "event_id": event_id,
            "source_observation_sha256": self.observation_hash(observation),
            "state": "accepted",
            "reason_code": "all_acceptance_gates_passed",
            "evidence": [
                report_descriptor,
                *[descriptors[role] for role in sorted(descriptors)],
            ],
            "acceptance_report": report_descriptor,
        }

    def make_unavailable(self, observation):
        event_id = observation["event_id"]
        observation_hash = self.observation_hash(observation)
        stream = f"v2x-backend-cam-{observation['camera_id']}"
        policy = {
            "schema": audit.RETENTION_POLICY_SCHEMA,
            "policy_id": f"policy-{event_id}",
            "stream_name": stream,
            "retention_seconds": 86400,
            "effective_from_utc": utc(self.now - timedelta(days=1)),
            "effective_until_utc": utc(self.now + timedelta(days=1)),
            "captured_at_utc": utc(self.now),
            "authority": dict(self.retention),
        }
        policy_descriptor = self.sign_value(
            event_id, "retention_policy", policy, "retention-key"
        )
        receipt = {
            "schema": audit.RETRIEVAL_RECEIPT_SCHEMA,
            "event_id": event_id,
            "source_observation_sha256": observation_hash,
            "camera_id": observation["camera_id"],
            "stream_name": stream,
            "requested_media_timestamp_utc": observation["media_timestamp_utc"],
            "attempt_number": 1,
            "attempted_at_utc": utc(self.now),
            "error_code": "NoMediaForTimestamp",
            "policy_sha256": policy_descriptor["sha256"],
            "authority": dict(self.retention),
        }
        receipt_descriptor = self.sign_value(
            event_id, "retrieval_receipt", receipt, "retention-key", suffix="-1"
        )
        report = {
            "schema": audit.UNAVAILABILITY_REPORT_SCHEMA,
            "event_id": event_id,
            "source_observation_sha256": observation_hash,
            "camera_id": observation["camera_id"],
            "reason_code": audit.UNAVAILABLE_REASON,
            "requested_media_timestamp_utc": observation["media_timestamp_utc"],
            "stream_name": stream,
            "attempt_count": 1,
            "last_attempt_utc": utc(self.now),
            "retention_expired_before_utc": utc(self.now - timedelta(days=1)),
            "policy": policy_descriptor,
            "receipts": [receipt_descriptor],
            "authority": dict(self.retention),
        }
        report_descriptor = self.sign_value(
            event_id, "unavailability_report", report, "retention-key"
        )
        self.policy_values[event_id] = policy
        self.policy_descriptors[event_id] = policy_descriptor
        self.receipt_values[event_id] = [receipt]
        self.receipt_descriptors[event_id] = [receipt_descriptor]
        self.unavailability_values[event_id] = report
        self.unavailability_descriptors[event_id] = report_descriptor
        return {
            "schema": audit.OUTCOME_SCHEMA,
            "event_id": event_id,
            "source_observation_sha256": observation_hash,
            "state": "unavailable",
            "reason_code": audit.UNAVAILABLE_REASON,
            "evidence": [report_descriptor, policy_descriptor, receipt_descriptor],
            "unavailability_report": report_descriptor,
        }

    def make_rejected(self, observation):
        event_id = observation["event_id"]
        relative = f"evidence/{event_id}/rejection.json"
        raw = self.write_json(relative, {"reason": "occluded_vehicle"})
        return {
            "schema": audit.OUTCOME_SCHEMA,
            "event_id": event_id,
            "source_observation_sha256": self.observation_hash(observation),
            "state": "rejected",
            "reason_code": "occluded_vehicle",
            "evidence": [{
                "role": "rejection_occlusion",
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }],
        }

    def refresh_accepted(self, event_id):
        report = self.acceptance_values[event_id]
        report["artifacts"] = dict(self.artifact_descriptors[event_id])
        descriptor = self.sign_value(
            event_id, "acceptance_report", report, "producer-key"
        )
        self.acceptance_descriptors[event_id] = descriptor
        row = next(row for row in self.rows if row["event_id"] == event_id)
        row["acceptance_report"] = descriptor
        row["evidence"] = [
            descriptor,
            *[
                self.artifact_descriptors[event_id][role]
                for role in sorted(self.artifact_descriptors[event_id])
            ],
        ]

    def resign_artifact(self, event_id, role):
        descriptor = self.sign_value(
            event_id, role, self.artifact_values[event_id][role], "producer-key"
        )
        self.artifact_descriptors[event_id][role] = descriptor
        self.refresh_accepted(event_id)

    def refresh_unavailable(self, event_id):
        policy = self.policy_values[event_id]
        policy_descriptor = self.sign_value(
            event_id, "retention_policy", policy, "retention-key"
        )
        self.policy_descriptors[event_id] = policy_descriptor
        receipt_descriptors = []
        for index, receipt in enumerate(self.receipt_values[event_id], 1):
            receipt["policy_sha256"] = policy_descriptor["sha256"]
            receipt_descriptors.append(self.sign_value(
                event_id,
                "retrieval_receipt",
                receipt,
                "retention-key",
                suffix=f"-{index}",
            ))
        self.receipt_descriptors[event_id] = receipt_descriptors
        report = self.unavailability_values[event_id]
        report["policy"] = policy_descriptor
        report["receipts"] = receipt_descriptors
        report["attempt_count"] = len(receipt_descriptors)
        descriptor = self.sign_value(
            event_id, "unavailability_report", report, "retention-key"
        )
        self.unavailability_descriptors[event_id] = descriptor
        row = next(row for row in self.rows if row["event_id"] == event_id)
        row["unavailability_report"] = descriptor
        row["evidence"] = [descriptor, policy_descriptor, *receipt_descriptors]

    def resign_unavailability_report(self, event_id):
        report = self.unavailability_values[event_id]
        descriptor = self.sign_value(
            event_id, "unavailability_report", report, "retention-key"
        )
        self.unavailability_descriptors[event_id] = descriptor
        row = next(row for row in self.rows if row["event_id"] == event_id)
        row["unavailability_report"] = descriptor
        row["evidence"][0] = descriptor

    def authority_value(self):
        producer_key = self.keys[self.producer["key_id"]]
        retention_key = self.keys[self.retention["key_id"]]
        audit_key_id = self.authority["audit_authority"]["key_id"] if self.authority else "audit-key"
        audit_key = self.keys[audit_key_id]
        accepted_events = {}
        unavailable_events = {}
        for row in self.rows:
            event_id = row["event_id"]
            if row["state"] == "accepted":
                accepted_events[event_id] = {
                    "acceptance_report": audit._descriptor_binding(row["acceptance_report"]),
                    "artifacts": {
                        role: audit._descriptor_binding(self.artifact_descriptors[event_id][role])
                        for role in sorted(audit.ACCEPTANCE_ARTIFACTS)
                    },
                }
            elif row["state"] == "unavailable":
                unavailable_events[event_id] = {
                    "unavailability_report": audit._descriptor_binding(row["unavailability_report"]),
                    "policy": audit._descriptor_binding(self.policy_descriptors[event_id]),
                    "receipts": [
                        audit._descriptor_binding(value)
                        for value in self.receipt_descriptors[event_id]
                    ],
                }
        return {
            "schema": audit.AUTHORITY_SCHEMA,
            "audit_run_id": self.audit_run_id,
            "valid_from_utc": utc(self.now - timedelta(hours=1)),
            "trusted_audit_time_utc": utc(self.now),
            "valid_until_utc": utc(self.now + timedelta(hours=1)),
            "audit_authority": {
                "authority_id": "independent-audit-authority",
                "role": "audit_authority",
                "key_id": audit_key_id,
                "public_key_sha256": audit_key.fingerprint,
            },
            "verifier": {
                "release": audit.VERIFIER_RELEASE,
                "schema_bundle_sha256": audit.SCHEMA_BUNDLE_SHA256,
            },
            "inputs": {
                "ledger_manifest_sha256": hashlib.sha256(self.manifest_path.read_bytes()).hexdigest(),
                "ledger_observations_sha256": hashlib.sha256(self.observations_path.read_bytes()).hexdigest(),
                "outcomes_sha256": hashlib.sha256(self.outcomes_path.read_bytes()).hexdigest(),
            },
            "principals": {
                "producer": {
                    **self.producer,
                    "public_key_sha256": producer_key.fingerprint,
                    "allowed_outputs": audit.expected_output_policies(
                        audit.SIGNED_ROLES, self.producer["tool_digest"]
                    ),
                },
                "retention": {
                    **self.retention,
                    "public_key_sha256": retention_key.fingerprint,
                    "allowed_outputs": audit.expected_output_policies(
                        audit.RETENTION_ROLES, self.retention["tool_digest"]
                    ),
                },
            },
            "accepted_events": accepted_events,
            "unavailable_events": unavailable_events,
        }

    def rewrite_authority(self):
        prior = self.authority
        self.authority = self.authority_value()
        if prior is not None:
            # Preserve intentional authority-level mutations made by a test.
            for name in ("audit_authority", "verifier", "principals"):
                if prior[name] != self.authority_value()[name]:
                    self.authority[name] = prior[name]
        raw = self.write_json(self.authority_rel, self.authority)
        key_id = self.authority["audit_authority"]["key_id"]
        signature = self.private[key_id].sign(raw)
        (self.root / self.authority_signature_rel).write_bytes(signature)

    def rewrite_outcomes_and_authority(self):
        self.outcomes_path.write_bytes(
            b"".join(audit.canonical_json_bytes(row) for row in self.rows)
        )
        self.rewrite_authority()

    def build(self, *, pinned_keys=None, run_id=None):
        return audit.build_report(
            self.ledger_dir,
            self.outcomes_rel,
            retained_root=self.root,
            authority_manifest=self.authority_rel,
            authority_signature=self.authority_signature_rel,
            expected_audit_run_id=run_id or self.audit_run_id,
            pinned_keys=self.keys if pinned_keys is None else pinned_keys,
        )


def assert_error(code, operation):
    with pytest.raises(audit.OutcomeAuditError) as caught:
        operation()
    assert caught.value.code == code


def test_accepts_complete_signed_role_separated_audit(tmp_path):
    fixture = AuditFixture(tmp_path)
    report = fixture.build()
    assert report["acceptance_eligible"] is True
    assert report["counts"]["accepted"] == 4
    assert report["counts"]["rejected"] == 1
    assert report["accepted_fraction_of_recoverable"] == 0.8
    assert report["evidence_snapshot"]["canonical_sha256"]


def test_default_empty_allowlist_and_missing_authority_fail_closed(tmp_path):
    fixture = AuditFixture(tmp_path)
    assert_error("pinned_key_missing", lambda: fixture.build(pinned_keys={}))
    assert_error(
        "authority_not_configured",
        lambda: audit.build_report(fixture.ledger_dir, fixture.outcomes_rel),
    )


def test_unpinned_and_rotated_authority_keys(tmp_path):
    fixture = AuditFixture(tmp_path)
    without_audit = {key: value for key, value in fixture.keys.items() if key != "audit-key"}
    assert_error("pinned_key_missing", lambda: fixture.build(pinned_keys=without_audit))

    fingerprint = fixture.add_key("audit-key-v2")
    fixture.keys = audit.load_pinned_keys(list(fixture.key_specs.values()))
    fixture.authority["audit_authority"] = {
        "authority_id": "independent-audit-authority-v2",
        "role": "audit_authority",
        "key_id": "audit-key-v2",
        "public_key_sha256": fingerprint,
    }
    fixture.rewrite_outcomes_and_authority()
    assert fixture.build()["acceptance_eligible"] is True
    old_only = {key: value for key, value in fixture.keys.items() if key != "audit-key-v2"}
    assert_error("pinned_key_missing", lambda: fixture.build(pinned_keys=old_only))


def test_replayed_run_id_is_rejected(tmp_path):
    fixture = AuditFixture(tmp_path)
    assert_error("audit_run_replay", lambda: fixture.build(run_id=str(uuid.uuid4())))


def test_role_reuse_is_rejected(tmp_path):
    fixture = AuditFixture(tmp_path)
    producer_key = fixture.keys["producer-key"]
    fixture.authority["principals"]["retention"].update({
        "principal_id": fixture.producer["principal_id"],
        "key_id": "producer-key",
        "public_key_sha256": producer_key.fingerprint,
    })
    fixture.rewrite_outcomes_and_authority()
    assert_error("authority_role_separation", fixture.build)


def test_exact_byte_signature_mutation_is_rejected(tmp_path):
    fixture = AuditFixture(tmp_path)
    event_id = "event-0"
    role = "exact_frame"
    descriptor = fixture.artifact_descriptors[event_id][role]
    signature_path = fixture.root / descriptor["signature_path"]
    mutated = bytearray(signature_path.read_bytes())
    mutated[0] ^= 1
    signature_path.write_bytes(mutated)
    descriptor["signature_sha256"] = hashlib.sha256(mutated).hexdigest()
    fixture.refresh_accepted(event_id)
    fixture.rewrite_outcomes_and_authority()
    assert_error("signature_invalid", fixture.build)


def test_wrong_schema_or_tool_policy_is_rejected(tmp_path):
    fixture = AuditFixture(tmp_path)
    fixture.authority["principals"]["producer"]["allowed_outputs"]["exact_frame"][
        "schema_sha256"
    ] = "0" * 64
    fixture.rewrite_outcomes_and_authority()
    assert_error("authority_output_policy", fixture.build)


def test_verifier_schema_bundle_mismatch_is_rejected(tmp_path):
    fixture = AuditFixture(tmp_path)
    fixture.authority["verifier"]["schema_bundle_sha256"] = "0" * 64
    fixture.rewrite_outcomes_and_authority()
    assert_error("verifier_policy", fixture.build)


def test_artifact_gate_set_is_exact(tmp_path):
    fixture = AuditFixture(tmp_path)
    fixture.artifact_values["event-0"]["exact_frame"]["gates"].pop(
        "same_session_pts"
    )
    fixture.resign_artifact("event-0", "exact_frame")
    fixture.rewrite_outcomes_and_authority()
    assert_error("schema_invalid", fixture.build)


def test_cross_event_static_calibration_reuse_is_rejected(tmp_path):
    fixture = AuditFixture(tmp_path, count=5, accepted=5)
    fixture.artifact_descriptors["event-4"]["static_calibration"] = dict(
        fixture.artifact_descriptors["event-0"]["static_calibration"]
    )
    fixture.refresh_accepted("event-4")
    fixture.rewrite_outcomes_and_authority()
    assert_error("artifact_observation_binding", fixture.build)


def test_static_epoch_must_contain_exact_observation_time(tmp_path):
    fixture = AuditFixture(tmp_path)
    value = fixture.artifact_values["event-0"]["static_calibration"]
    value["valid_from_utc"] = utc(fixture.now + timedelta(days=2))
    value["valid_until_utc"] = utc(fixture.now + timedelta(days=3))
    fixture.resign_artifact("event-0", "static_calibration")
    fixture.rewrite_outcomes_and_authority()
    assert_error("static_epoch_binding", fixture.build)


def test_future_retention_receipt_cannot_remove_event_from_denominator(tmp_path):
    fixture = AuditFixture(tmp_path, count=5, accepted=4, unavailable=1)
    event_id = "event-4"
    future = fixture.now + timedelta(seconds=audit.FUTURE_SKEW_SECONDS + 1)
    fixture.receipt_values[event_id][0]["attempted_at_utc"] = utc(future)
    fixture.unavailability_values[event_id]["last_attempt_utc"] = utc(future)
    fixture.unavailability_values[event_id]["retention_expired_before_utc"] = utc(
        future - timedelta(days=1)
    )
    fixture.refresh_unavailable(event_id)
    fixture.rewrite_outcomes_and_authority()
    assert_error("retention_receipt_future", fixture.build)


def test_retention_receipt_must_bind_exact_stream_and_timestamp(tmp_path):
    fixture = AuditFixture(tmp_path, count=1, accepted=0, unavailable=1)
    fixture.receipt_values["event-0"][0]["stream_name"] = "v2x-backend-cam-ch2"
    fixture.refresh_unavailable("event-0")
    fixture.rewrite_outcomes_and_authority()
    assert_error("retention_receipt_binding", fixture.build)


def test_resource_not_found_never_proves_media_retention_expiry(tmp_path):
    fixture = AuditFixture(tmp_path, count=1, accepted=0, unavailable=1)
    fixture.receipt_values["event-0"][0]["error_code"] = "ResourceNotFoundException"
    fixture.refresh_unavailable("event-0")
    fixture.rewrite_outcomes_and_authority()

    assert_error("schema_invalid", fixture.build)


@pytest.mark.parametrize("field", ["retention_seconds", "attempt_number", "attempt_count"])
def test_retention_integer_fields_reject_integral_floats(tmp_path, field):
    fixture = AuditFixture(tmp_path, count=1, accepted=0, unavailable=1)
    event_id = "event-0"
    if field == "retention_seconds":
        fixture.policy_values[event_id][field] = 86400.0
        fixture.refresh_unavailable(event_id)
    elif field == "attempt_number":
        fixture.receipt_values[event_id][0][field] = 1.0
        fixture.refresh_unavailable(event_id)
    else:
        fixture.unavailability_values[event_id][field] = 1.0
        fixture.resign_unavailability_report(event_id)
    fixture.rewrite_outcomes_and_authority()
    assert_error("schema_invalid", fixture.build)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra"])
def test_exactly_one_outcome_per_event(tmp_path, mutation):
    fixture = AuditFixture(tmp_path)
    if mutation == "missing":
        fixture.rows.pop()
        code = "outcome_missing_event"
    elif mutation == "duplicate":
        fixture.rows.append(dict(fixture.rows[0]))
        code = "outcome_duplicate_event"
    else:
        extra = dict(fixture.rows[0])
        extra["event_id"] = "outside-ledger"
        fixture.rows.append(extra)
        code = "outcome_extra_event"
    if mutation == "extra":
        fixture.outcomes_path.write_bytes(
            b"".join(audit.canonical_json_bytes(row) for row in fixture.rows)
        )
    else:
        fixture.rewrite_outcomes_and_authority()
    assert_error(code, fixture.build)


def test_outside_root_and_parent_traversal_are_rejected(tmp_path):
    fixture = AuditFixture(tmp_path, count=1, accepted=0)
    fixture.rows[0]["evidence"][0]["path"] = "/etc/os-release"
    fixture.rows[0]["evidence"][0]["sha256"] = hashlib.sha256(
        Path("/etc/os-release").read_bytes()
    ).hexdigest()
    fixture.rewrite_outcomes_and_authority()
    assert_error("evidence_path_escape", fixture.build)

    fixture = AuditFixture(tmp_path / "second", count=1, accepted=0)
    fixture.rows[0]["evidence"][0]["path"] = "../outside"
    fixture.rewrite_outcomes_and_authority()
    assert_error("evidence_path_escape", fixture.build)


def test_intermediate_symlink_is_rejected(tmp_path):
    fixture = AuditFixture(tmp_path, count=1, accepted=0)
    (fixture.root / "escape").symlink_to("/etc")
    fixture.rows[0]["evidence"][0] = {
        "role": "rejection_occlusion",
        "path": "escape/os-release",
        "sha256": hashlib.sha256(Path("/etc/os-release").read_bytes()).hexdigest(),
    }
    fixture.rewrite_outcomes_and_authority()
    assert_error("evidence_symlink", fixture.build)


def test_duplicate_hardlinks_and_duplicate_content_roles_are_rejected(tmp_path):
    fixture = AuditFixture(tmp_path, count=1, accepted=0)
    first = fixture.root / fixture.rows[0]["evidence"][0]["path"]
    second = first.with_name("hardlink.json")
    os.link(first, second)
    fixture.rows[0]["evidence"].append({
        "role": "rejection_secondary",
        "path": fixture.relative(second),
        "sha256": hashlib.sha256(second.read_bytes()).hexdigest(),
    })
    fixture.rewrite_outcomes_and_authority()
    assert_error("evidence_hardlink", fixture.build)

    fixture = AuditFixture(tmp_path / "roles", count=1, accepted=0)
    relative = "evidence/event-0/other.json"
    raw = fixture.write_json(relative, {"other": True})
    fixture.rows[0]["evidence"].append({
        "role": "rejection_occlusion",
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
    })
    fixture.rewrite_outcomes_and_authority()
    assert_error("evidence_role", fixture.build)


def test_concurrent_file_replacement_is_detected(tmp_path, monkeypatch):
    fixture = AuditFixture(tmp_path, count=1, accepted=0)
    target = fixture.root / fixture.rows[0]["evidence"][0]["path"]
    replacement = target.with_name("replacement.json")
    replacement.write_bytes(target.read_bytes())
    original_read = audit.os.read
    replaced = False

    def replacing_read(descriptor, size):
        nonlocal replaced
        chunk = original_read(descriptor, size)
        if not replaced:
            try:
                opened = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            except OSError:
                opened = None
            if opened == target and chunk:
                os.replace(replacement, target)
                replaced = True
        return chunk

    monkeypatch.setattr(audit.os, "read", replacing_read)
    assert_error("evidence_replaced", fixture.build)
    assert replaced is True


def test_signed_receipt_deleted_before_build_returns_prevents_publication(tmp_path, monkeypatch):
    fixture = AuditFixture(tmp_path, count=1, accepted=0, unavailable=1)
    receipt = fixture.root / fixture.receipt_descriptors["event-0"][0]["path"]
    output = tmp_path / "audit.json"
    original_snapshot = audit.RetainedEvidenceStore.snapshot
    deleted = False

    def deleting_snapshot(store):
        nonlocal deleted
        snapshot = original_snapshot(store)
        receipt.unlink()
        deleted = True
        return snapshot

    monkeypatch.setattr(audit.RetainedEvidenceStore, "snapshot", deleting_snapshot)

    def build_and_publish():
        report = fixture.build()
        audit.write_report_exclusive(output, report)

    assert_error("evidence_replaced", build_and_publish)
    assert deleted is True
    assert not output.exists()


def test_signed_receipt_deleted_after_build_returns_prevents_publication(tmp_path):
    fixture = AuditFixture(tmp_path, count=1, accepted=0, unavailable=1)
    receipt = fixture.root / fixture.receipt_descriptors["event-0"][0]["path"]
    output = tmp_path / "audit.json"
    report = fixture.build()

    receipt.unlink()

    assert_error(
        "evidence_replaced",
        lambda: audit.write_report_exclusive(output, report),
    )
    assert not output.exists()


def test_signed_receipt_deleted_after_output_link_prevents_publication(
    tmp_path, monkeypatch
):
    fixture = AuditFixture(tmp_path, count=1, accepted=0, unavailable=1)
    receipt = fixture.root / fixture.receipt_descriptors["event-0"][0]["path"]
    output = tmp_path / "audit.json"
    report = fixture.build()
    original_link = audit.os.link
    linked = False

    def deleting_link(source, destination, *args, **kwargs):
        nonlocal linked
        result = original_link(source, destination, *args, **kwargs)
        receipt.unlink()
        linked = True
        return result

    monkeypatch.setattr(audit.os, "link", deleting_link)

    assert_error(
        "evidence_replaced",
        lambda: audit.write_report_exclusive(output, report),
    )
    assert linked is True
    assert not output.exists()


def test_non_nfc_evidence_path_is_rejected(tmp_path):
    fixture = AuditFixture(tmp_path, count=1, accepted=0)
    fixture.rows[0]["evidence"][0]["path"] = "evidence/e\u0301.json"
    fixture.rewrite_outcomes_and_authority()
    assert_error("evidence_path_unicode", fixture.build)


def test_noncanonical_redundant_path_is_rejected(tmp_path):
    fixture = AuditFixture(tmp_path, count=1, accepted=0)
    fixture.rows[0]["evidence"][0]["path"] = "evidence//event-0/rejection.json"
    fixture.rewrite_outcomes_and_authority()
    assert_error("evidence_path_escape", fixture.build)


def test_nonfinite_and_duplicate_json_are_controlled_failures(tmp_path):
    fixture = AuditFixture(tmp_path)
    observation = dict(fixture.observations[0])
    raw = audit.canonical_json_bytes(observation).rstrip(b"}\n") + b',"score":NaN}\n'
    fixture.observations_path.write_bytes(raw)
    fixture.manifest_path.write_bytes(audit.canonical_json_bytes({
        "schema": audit.LEDGER_SCHEMA,
        "observations_sha256": hashlib.sha256(raw).hexdigest(),
        "counts": {"observations": 1},
    }))
    assert_error("json_nonfinite", fixture.build)

    fixture = AuditFixture(tmp_path / "duplicate")
    manifest = fixture.manifest_path.read_bytes().decode().rstrip("\n}")
    fixture.manifest_path.write_text(manifest + ',"schema":"' + audit.LEDGER_SCHEMA + '"}\n')
    assert_error("json_duplicate_key", fixture.build)


def test_overflow_number_and_oversized_json_fail_closed(tmp_path, monkeypatch):
    fixture = AuditFixture(tmp_path / "overflow")
    observation = dict(fixture.observations[0])
    raw = audit.canonical_json_bytes(observation).rstrip(b"}\n") + b',"score":1e999}\n'
    fixture.observations_path.write_bytes(raw)
    fixture.manifest_path.write_bytes(audit.canonical_json_bytes({
        "schema": audit.LEDGER_SCHEMA,
        "observations_sha256": hashlib.sha256(raw).hexdigest(),
        "counts": {"observations": 1},
    }))
    assert_error("json_nonfinite", fixture.build)

    fixture = AuditFixture(tmp_path / "oversized")
    manifest = {
        "schema": audit.LEDGER_SCHEMA,
        "observations_sha256": hashlib.sha256(fixture.observations_path.read_bytes()).hexdigest(),
        "counts": {"observations": len(fixture.observations)},
        "padding": "x" * 1024,
    }
    fixture.manifest_path.write_bytes(audit.canonical_json_bytes(manifest))
    monkeypatch.setattr(audit, "MAX_JSON_BYTES", 512)
    assert_error("evidence_too_large", fixture.build)


@pytest.mark.parametrize("value", [True, 1.0, [1]])
def test_manifest_count_requires_exact_integer(tmp_path, value):
    fixture = AuditFixture(tmp_path)
    fixture.manifest_path.write_bytes(audit.canonical_json_bytes({
        "schema": audit.LEDGER_SCHEMA,
        "observations_sha256": hashlib.sha256(fixture.observations_path.read_bytes()).hexdigest(),
        "counts": {"observations": value},
    }))
    assert_error("schema_invalid", fixture.build)


def test_malformed_camera_and_state_lists_are_controlled(tmp_path):
    fixture = AuditFixture(tmp_path)
    malformed = dict(fixture.observations[0])
    malformed["camera_id"] = ["ch1"]
    body = audit.canonical_json_bytes(malformed)
    fixture.observations_path.write_bytes(body)
    fixture.manifest_path.write_bytes(audit.canonical_json_bytes({
        "schema": audit.LEDGER_SCHEMA,
        "observations_sha256": hashlib.sha256(body).hexdigest(),
        "counts": {"observations": 1},
    }))
    assert_error("schema_invalid", fixture.build)

    fixture = AuditFixture(tmp_path / "state")
    fixture.rows[0]["state"] = ["accepted"]
    fixture.outcomes_path.write_bytes(
        b"".join(audit.canonical_json_bytes(row) for row in fixture.rows)
    )
    assert_error("schema_invalid", fixture.build)


def test_denominator_counts_occluded_rejections_and_zero_recoverable(tmp_path):
    report = AuditFixture(tmp_path / "occluded", count=5, accepted=4).build()
    assert report["counts"]["recoverable_denominator"] == 5
    assert report["accepted_fraction_of_recoverable"] == 0.8
    assert report["acceptance_eligible"] is True

    report = AuditFixture(
        tmp_path / "mixed", count=5, accepted=3, unavailable=1
    ).build()
    assert report["counts"]["recoverable_denominator"] == 4
    assert report["accepted_fraction_of_recoverable"] == 0.75
    assert report["acceptance_eligible"] is False

    report = AuditFixture(
        tmp_path / "all-unavailable", count=5, accepted=0, unavailable=5
    ).build()
    assert report["counts"]["recoverable_denominator"] == 0
    assert report["accepted_fraction_of_recoverable"] == 0.0
    assert report["acceptance_eligible"] is False


def _race_writer(path, marker, start, queue):
    start.wait()
    try:
        audit.write_report_exclusive(path, {"schema": audit.REPORT_SCHEMA, "marker": marker})
        queue.put(("ok", marker))
    except audit.OutcomeAuditError as exc:
        queue.put((exc.code, marker))


def test_output_publication_never_replaces_existing_or_concurrent_result(tmp_path):
    output = tmp_path / "audit.json"
    output.write_text("existing")
    assert_error("output_exists", lambda: audit.write_report_exclusive(output, {}))
    assert output.read_text() == "existing"

    for index in range(10):
        race_dir = tmp_path / f"race-{index}"
        race_dir.mkdir()
        race_output = race_dir / "audit.json"
        start = mp.Event()
        queue = mp.Queue()
        processes = [
            mp.Process(target=_race_writer, args=(race_output, marker, start, queue))
            for marker in (1, 2)
        ]
        for process in processes:
            process.start()
        start.set()
        results = [queue.get(timeout=5) for _ in processes]
        for process in processes:
            process.join(5)
        assert sorted(result[0] for result in results) == ["ok", "output_exists"]
        assert json.loads(race_output.read_text())["marker"] in {1, 2}
        assert not list(race_dir.glob(".audit.json.tmp-*"))


def test_publication_directory_replacement_cannot_substitute_different_bytes(
    tmp_path, monkeypatch
):
    publication = tmp_path / "publication"
    publication.mkdir()
    displaced = tmp_path / "displaced"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    output = publication / "audit.json"
    substitute = b'{"marker":"attacker-substitute"}\n'
    (replacement / output.name).write_bytes(substitute)
    original_link = audit.os.link
    replaced = False

    def replacing_link(source, destination, *args, **kwargs):
        nonlocal replaced
        result = original_link(source, destination, *args, **kwargs)
        publication.rename(displaced)
        replacement.rename(publication)
        replaced = True
        return result

    monkeypatch.setattr(audit.os, "link", replacing_link)

    assert_error(
        "output_directory_replaced",
        lambda: audit.write_report_exclusive(
            output, {"schema": audit.REPORT_SCHEMA, "marker": "trusted"}
        ),
    )
    assert replaced is True
    assert output.read_bytes() == substitute
    assert not (displaced / output.name).exists()


def test_publication_directory_replacement_after_pre_fsync_check_cannot_substitute_bytes(
    tmp_path, monkeypatch
):
    publication = tmp_path / "publication"
    publication.mkdir()
    displaced = tmp_path / "displaced"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    output = publication / "audit.json"
    substitute = b'{"marker":"attacker-substitute"}\n'
    (replacement / output.name).write_bytes(substitute)
    original_stat = audit.os.stat
    replaced = False

    def replacing_stat(path, *args, **kwargs):
        nonlocal replaced
        result = original_stat(path, *args, **kwargs)
        if (
            not replaced
            and Path(path) == output
            and kwargs.get("dir_fd") is None
        ):
            publication.rename(displaced)
            replacement.rename(publication)
            replaced = True
        return result

    monkeypatch.setattr(audit.os, "stat", replacing_stat)

    assert_error(
        "output_directory_replaced",
        lambda: audit.write_report_exclusive(
            output, {"schema": audit.REPORT_SCHEMA, "marker": "trusted"}
        ),
    )
    assert replaced is True
    assert output.read_bytes() == substitute
    assert not (displaced / output.name).exists()


def test_pinned_key_opened_descriptor_must_remain_a_single_link(tmp_path, monkeypatch):
    fixture = AuditFixture(tmp_path, count=1, accepted=0)
    key_path = fixture.keys_dir / "audit-key.pem"
    raw = key_path.read_bytes()
    fingerprint = hashlib.sha256(raw).hexdigest()
    replacement = fixture.keys_dir / "replacement.pem"
    replacement.write_bytes(raw)
    replacement_alias = fixture.keys_dir / "replacement-alias.pem"
    os.link(replacement, replacement_alias)
    original_open = audit.os.open
    replaced = False

    def replacing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and Path(path) == key_path:
            os.replace(replacement, key_path)
            replaced = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(audit.os, "open", replacing_open)

    assert_error(
        "pinned_key_unsafe",
        lambda: audit._read_pinned_public_key("audit-key", fingerprint, key_path),
    )
    assert replaced is True
