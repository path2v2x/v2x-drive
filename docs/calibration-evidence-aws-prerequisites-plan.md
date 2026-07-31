# Calibration evidence AWS prerequisite gate

Status: draft, no AWS mutation authorized by this document alone.

## Fixed scope

- Account: `147229569658` (revalidated from STS on every run).
- Region: `us-west-1`.
- Evidence bucket: `v2x-calibration-evidence-147229569658-us-west-1`.
- Audit-log bucket: `v2x-calibration-audit-147229569658-us-west-1`.
- Trail: `v2x-calibration-evidence-audit`.
- Writer role: `V2XCalibrationEvidenceWriter`.
- Planner role: `V2XCalibrationEvidencePlanner` (read-only state discovery).
- Apply lock: `/v2x/calibration/evidence-prerequisites/apply-lock` in SSM.
- Trust principal: explicit same-account ARN supplied at apply; no implicit
  current-caller trust and no wildcard principal.
- Required tags on every supported resource: `managed-by=v2x-backend`,
  `purpose=calibration-evidence` (or `calibration-evidence-audit` for audit-only
  resources), and `ue-runtime=ue5-only`. This infrastructure must never carry
  UE6 experimental evidence. Bucket, role, trail, EventBridge rule, and log-group
  tag readback is part of both hashes and the applied exit gate.

## Desired controls

### Audit-log bucket

Create only if absent and enable Object Lock at creation, with expected-owner
checks, fixed region, versioning, BucketOwnerEnforced ownership, AES256 default
encryption, all four public-access blocks, and TLS-only policy. Configure a
365-day COMPLIANCE default and deny object/version deletion, retention changes,
legal-hold changes, and governance bypass. The audit bucket is deliberately as
immutable as the evidence bucket; this is a second explicit irreversibility
boundary. Add only the exact CloudTrail ACL-check/write grants for the fixed
trail ARN, restricted by `aws:SourceArn`, account log prefix, and
`bucket-owner-full-control`. Apply an explicit lifecycle rule to abort incomplete
multipart uploads; do not delete delivered audit logs automatically.

Do not silently preserve foreign allow statements. Any unrecognized statement
that grants access is a hard stop unless apply includes a separate acknowledgment
of that statement's canonical SHA-256. Foreign deny statements may be preserved
only after they are included in both reviewed state hashes and shown not to block
CloudTrail delivery/readback.

### Writer role

Create or reconcile one role with an explicit same-account user/role trust
principal, rejecting account-root and wildcard principals. Its managed inline
policy is restricted to the fixed evidence bucket. Allow bounded multipart
upload initiation/completion, multipart listing/abort, explicit COMPLIANCE-locked
object writes, and exact readback via `s3:GetObject`, `s3:GetObjectVersion`,
`s3:GetObjectAttributes`, `s3:GetObjectRetention`, `s3:GetObjectTagging`, and
the fixed bucket's version/list calls. Add explicit role-policy denies for object/
version deletion, retention/legal-hold change, governance bypass, and access
outside the fixed bucket so later attached policies cannot silently broaden the
writer. The uploader must send `COMPLIANCE` plus a retain-until time at least 90
days in the future; relying only on the bucket default is not an acceptance path.
Preserve unrelated role tags but replace only the managed trust document,
inline policy name, and managed tag keys after exact current-state review.

The planner role trusts the same explicit non-root principal and grants only the
STS/IAM/S3/CloudTrail/SSM/EventBridge/CloudWatch read calls required to render
the plan. After bootstrap, two consecutive plans must run through this role; a
stray write in plan mode then fails at IAM rather than relying only on review.

### CloudTrail

Create or reconcile a single-region trail using the fixed audit-log bucket and
log-file validation. Record write-only management events as well as write-only
S3 object data events, so changes to the trail, selectors, bucket policy,
retention, or writer role are themselves auditable. Use a classic event selector
with `ReadWriteType=WriteOnly`, `IncludeManagementEvents=true`, and exactly one
`AWS::S3::Object` data resource value equal to the evidence bucket object ARN
prefix. Start logging, then require `IsLogging=true`, empty
`LatestDeliveryError`, exact selector readback, fixed home region, fixed log
bucket, and log-file validation enabled.

Create an EventBridge rule matching `StopLogging`, `DeleteTrail`, `UpdateTrail`,
and `PutEventSelectors` for the fixed trail plus evidence/audit bucket policy or
Object-Lock mutation calls. Its first target is a dedicated CloudWatch log group
with 365-day retention and a resource policy limited to the rule. An external
human-notification target is a separate required production decision before
final closeout; absence of that target keeps the monitoring gate yellow, not the
integrity gate green.

## Plan/apply transaction

1. Read STS identity, role/trust/policies/tags, both bucket existence/region and
   full mutable configuration, trail definition/status/selectors, and current
   audit-bucket policy/tags/lifecycle. Any AccessDenied or ambiguous bucket
   existence aborts; only exact not-found codes map to absence.
2. Canonicalize and print `current.json` and `desired.json` plus independent
   SHA-256 hashes. Plan mode exits before IAM, S3, or CloudTrail mutation.
3. Apply requires both reviewed hashes, the exact trust principal ARN repeated
   separately, `CONFIGURE_CALIBRATION_EVIDENCE_PREREQUISITES`, and
   `CREATE_COMPLIANCE_LOCKED_CALIBRATION_AUDIT_LOG`. Re-read all state so drift
   invalidates the hashes.
4. Store a mode-0700/mode-0600 rollback bundle, then claim the SSM apply lock
   with a conditional create before any infrastructure mutation. A live lock
   blocks concurrent apply; a stale lock requires a separately reviewed clear.
5. Reconcile in dependency order: audit bucket controls/policy, writer role and
   inline policy, trail, event selector, start logging.
6. Retry bounded readback of every desired field. On failure, print the rollback
   bundle and stop; never continue into evidence-bucket creation.
7. Run `provision-calibration-evidence-store.sh` in plan mode. Only after its
   separate current/desired hashes are reviewed may its irreversible apply run.
8. Assume the writer role and upload a hash-bound canary under
   `canary/provisioning/<uuid>.json` with explicit COMPLIANCE mode and at least
   90 days retention. Prove readback mode/retain-until/hash/version, locate the
   exact writer-session `PutObject` data event in the immutable audit bucket,
   and, after the digest arrives, run CloudTrail log validation over the canary
   interval. The canary remains locked; the evidence bucket is intentionally no
   longer empty.
9. Do not upload a real holdout object until calibration/model/config choices
   and the authority manifest are frozen.

## Rollback and irreversibility boundary

- Before any evidence object exists, the evidence bucket can be removed after
  its trail selector is detached. Object Lock itself is not retrofitted or
  disabled.
- Stop and delete the dedicated trail only after confirming no evidence upload
  is in flight. Audit objects and the canary remain undeletable until their
  COMPLIANCE retention expires. The buckets are retained by default.
- The writer role can be disabled by replacing its trust policy with a deny-only
  trust or deleted after all sessions expire. Never broaden the trust principal
  as a rollback shortcut.
- After a COMPLIANCE-locked evidence object exists, it cannot be deleted before
  retention expiry. Rollback means disabling future writes and preserving the
  object, not attempting deletion.

## Exit gates

PASS requires all of the following:

- plan-only command proves zero AWS mutation and stable hashes on two consecutive
  reads, and post-bootstrap plans pass through the read-only planner role;
- shell syntax/static checks and deterministic mocked absent/existing/drift/
  AccessDenied/object-policy-shape/lifecycle-shape fixtures pass;
- independent code review and Fable high-effort review both pass;
- a mocked rollback-restore fixture proves the captured role, policy, bucket,
  trail, selector, rule, and log-group state can be reconciled without deletion;
- concurrent-apply fixtures prove only one SSM lock claimant can mutate;
- applied audit bucket, writer role, trail, selector, and logging status match
  exact desired state;
- applied EventBridge rule pattern/state/target/tags and CloudWatch log-group
  retention/resource-policy/tags match exact desired state;
- every supported bucket, role, trail, rule, and log group returns the exact
  three required managed tags, and no desired-state resource is left untagged;
- the separate evidence-store plan sees active healthy write auditing and the
  exact writer role;
- the immutable canary's object lock, content/version readback, matching data
  event, and CloudTrail digest validation all pass;
- rollback bundle hashes and permissions verify.

Any failed item is a hard stop. Thresholds and evidence requirements are not
reduced to make provisioning pass.
