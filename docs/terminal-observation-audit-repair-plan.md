# Terminal observation audit repair plan

Scope: source and synthetic tests only. No live services, deployments, UE
runtimes, production data, or sealed holdout data.

## Acceptance boundary

1. The command receives an evidence root, an audit-authority manifest and its
   detached Ed25519 signature, plus an explicit `key_id=public_key.pem`
   allowlist. The default key allowlist is empty and therefore cannot accept an
   observation. The invocation is the out-of-band trust root: key entries also
   pin the SHA-256 fingerprint of the exact public-key bytes, and the command
   verifies those fingerprints before using a key. It never discovers or trusts
   a key from evidence. Production ownership and rotation of this invocation
   policy is a later deployment gate, outside this source-only change.
2. Strict Draft 2020-12 schemas reject unknown/missing fields, non-finite JSON,
   Boolean/float counts, malformed collection types, and noncanonical ledger
   rows. Every malformed input is surfaced as `OutcomeAuditError`.
3. The independently signed authority manifest binds the exact ledger,
   outcomes, accepted reports, five artifact reports, producer role/key/tool
   commit/tool digest, and the exact in-code schema digest for every output
   role. Audit authority, evidence producer, and retention authority keys and
   roles are distinct. Signatures cover exact file bytes, not reserialized JSON.
   The strict parser rejects duplicate keys before schema validation.
4. Accepted reports and their five artifacts have detached producer signatures.
   They are checked through strict schemas with exact gate sets. Static
   calibration additionally binds camera, observation/event hash, exact camera
   config/map/calibration-manifest hashes, and a signed validity epoch containing
   the observation media timestamp.
5. Unavailability requires a detached retention-authority signature on both an
   authoritative policy snapshot and every exact-stream/time retrieval receipt.
   The report binds their hashes; the policy-derived expiry boundary must match,
   attempts must be ordered, errors must prove the media was unavailable, and
   the final attempt may not exceed the signed trusted audit time by more than a
   fixed 300-second skew. Trusted audit time comes from the audit-authority
   manifest. The manifest also carries a UUID audit-run ID, a bounded validity
   interval containing that time, and the verifier release/schema digest. The
   caller must supply the expected audit-run ID, preventing silent replay of a
   previously valid run.
6. Evidence paths are relative to one configured retained root. Files are opened
   with `O_NOFOLLOW`, required to be regular and single-linked, deduplicated by
   role, canonical path, `(device,inode)`, and hash, read through one descriptor,
   and verified unchanged before/after. The resulting audit includes a canonical
   content-addressed snapshot manifest. Each intermediate path component is
   opened relative to a pinned root directory descriptor with `O_DIRECTORY` and
   `O_NOFOLLOW`; absolute paths, `..`, symlinked components, non-NFC paths, and
   oversized inputs fail closed. Publication uses a fully written and fsynced
   same-directory temporary file plus atomic no-replace hard-link publication,
   followed by parent-directory fsync. Exactly one concurrent writer may win.

## Implementation order

1. Replace permissive loading in
   `apps/perception/tools/audit_vehicle_observation_outcomes.py` with strict,
   duplicate-key rejecting JSON, coded `OutcomeAuditError` failures, exact
   schemas, pinned Ed25519 verification, and safe retained-root descriptors.
2. Add authority, signed acceptance/static, and signed retention validation;
   preserve the existing ledger/outcome bijection and fixed 80% calculation.
3. Replace the focused fixture in
   `apps/perception/tests/test_audit_vehicle_observation_outcomes.py` with three
   generated test authorities and exact signed artifacts, then add one-mutation
   adversarial tests that assert the specific rejection code.
4. Add pinned runtime dependencies to `apps/perception/requirements.txt`, run
   focused and full validation, review the diff, and commit only after the
   implementation review is clean.

## Verification

- Retain the existing bijection, denominator, and publication tests.
- Add adversarial tests for self-authored reports, absent/unpinned/rotated keys,
  role reuse, schema/tool mismatch, cross-event static reuse, invalid epochs,
  future/forged retention claims, outside-root and traversal paths, symlinks,
  hardlinks, duplicate roles, descriptor replacement, NaN, malformed lists,
  Boolean/float counts, zero recoverable, occluded rejection denominators, and
  simultaneous writers.
- Include duplicate JSON keys, exact-byte signature mutation, stale/replayed run
  IDs, Unicode normalization, oversized JSON, intermediate-directory symlinks,
  and verifier/schema digest mismatch. Assert specific failure codes so an
  unrelated path failure cannot satisfy a signature test.
- Run the focused suite and the full perception suite with warnings as errors,
  inspect the final diff and secret scan, obtain a final high-effort Fable
  review, fix any blocking findings, then commit without deploying.
