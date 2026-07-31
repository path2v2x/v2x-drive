# AWS CLI: `v2x-backend` Provisioning

This folder provisions the `v2x-backend` data plane in **`us-west-1`**:

- MQTT ingest: IoT Core -> `v2x-backend-ingest` -> DynamoDB
- Read API: HTTP API -> `v2x-backend-read`
- Private state bucket for digital twin state + snapshots

## Calibration evidence AWS prerequisites

`provision-calibration-evidence-prerequisites.sh` is the deployment-as-code
gate for the IAM, immutable audit, CloudTrail, and EventBridge resources needed
before the separate calibration evidence bucket can be created. It is fixed to
account `147229569658`, region `us-west-1`, and UE5-only managed tags. It never
creates or changes the evidence bucket itself.

The default is a read-only plan. Supply an explicit same-account IAM user or
role as the future trust principal; the script never infers trust from the
current caller and rejects root, wildcard, STS-session, or cross-account
principals. A plan performs full discovery, fails closed on AccessDenied, and
prints and optionally saves canonical current-state, desired-state, and later
canary-interface JSON with independent SHA-256 hashes.

```bash
# Read-only plan. This makes no AWS changes.
AWS_PROFILE=path AWS_REGION=us-west-1 PLAN_ONLY=true \
TRUST_PRINCIPAL_ARN=arn:aws:iam::147229569658:user/<explicit-user> \
PLAN_OUTPUT_DIR=/tmp/v2x-calibration-prerequisites-plan \
  ./provision-calibration-evidence-prerequisites.sh

# Apply is intentionally shown only as the exact reviewed transaction. Never
# substitute unreviewed hashes, a different trust principal, or weaker strings.
AWS_PROFILE=path AWS_REGION=us-west-1 PLAN_ONLY=false \
TRUST_PRINCIPAL_ARN=arn:aws:iam::147229569658:user/<explicit-user> \
TRUST_PRINCIPAL_ARN_CONFIRM=arn:aws:iam::147229569658:user/<explicit-user> \
EXPECTED_CURRENT_STATE_HASH=<reviewed-current-sha256> \
EXPECTED_DESIRED_STATE_HASH=<reviewed-desired-sha256> \
ACKNOWLEDGED_FOREIGN_POLICY_SHA256S=<exact-comma-separated-reviewed-set> \
CONFIRM_PREREQUISITES=CONFIGURE_CALIBRATION_EVIDENCE_PREREQUISITES \
CONFIRM_COMPLIANCE_AUDIT=CREATE_COMPLIANCE_LOCKED_CALIBRATION_AUDIT_LOG \
  ./provision-calibration-evidence-prerequisites.sh
```

Apply additionally requires an empty blocker list, exact acknowledgment of
every preserved foreign audit-bucket `Allow`, a mode-0700/mode-0600 rollback
bundle, and a conditionally created SSM concurrency lock. An existing lock is
never cleared automatically as stale. After claiming its lock, the transaction
re-reads and hashes the normalized current state before its first non-lock
mutation. A failed, interrupted, or non-convergent apply deliberately retains
its owned lock and prints the rollback bundle plus the manual recovery gate;
only exact successful readback clears it. Never clear a failed lock until a
separate review has compared its token and rollback bundle with a fresh
plan-only state. Successful release itself is fail-closed: the script must read
back the exact owned value, delete it, and then receive exact
`ParameterNotFound`. A read error, ownership mismatch, delete error, surviving
parameter, or ambiguous absence exits nonzero and suppresses the verified
banner. Failure diagnostics distinguish the last confirmed states: `owned`,
`ownership_lost`, and `delete_accepted_unverified`; only `confirmed_absent` is
reported as cleared. A successful read after deletion is separately reported as
`delete_accepted_still_present`. In particular, an accepted delete followed by
AccessDenied does not claim that the lock still exists—it requires a fresh exact
`GetParameter` result, with only `ParameterNotFound` accepted. The transaction
creates the audit bucket
with Object Lock at creation, applies a 365-day COMPLIANCE default, denies
deletion and retention mutation, reconciles the least-privilege writer and
read-only planner roles, configures the fixed single-region write-only trail,
and sends integrity-control mutation events to a dedicated 365-day CloudWatch
log group. Existing stronger COMPLIANCE defaults are preserved rather than
reduced. Existing managed IAM roles with a non-root path or any permissions
boundary fail closed. The monitoring pattern covers the fixed buckets, trail,
IAM roles, EventBridge controls, CloudWatch Logs controls, and SSM apply lock.
CloudTrail remains the durable management-event record: the EventBridge rule or
its log destination cannot guarantee delivery of an event that disables that
same path, so an independent external human notification remains required
before closeout. The CloudWatch Logs resource policy follows the AWS-documented
`events.amazonaws.com` plus `delivery.logs.amazonaws.com` delivery principals
and scopes them to that exact log group; the later gate must prove a fixed-rule
event actually arrives because Logs delivery does not support reliable
per-rule `SourceArn` scoping. It then performs bounded exact readback before
releasing its own lock. The audit bucket and locked objects are retained during
rollback.

After bootstrap, assume `V2XCalibrationEvidencePlanner` and require two stable
plans through that role. Then run `provision-calibration-evidence-store.sh` in
plan mode as a separate reviewed gate. The generated
`later-canary-interface.json` defines—but does not execute—the subsequent
locked canary proof: an explicit 90-day-or-longer COMPLIANCE write, exact
content/version/retention readback, matching writer-session `PutObject` data
event, EventBridge rule-fire log readback, and CloudTrail digest validation.
The writer policy rejects explicit non-COMPLIANCE or sub-90-day headers; its
`IfExists` form is required so multipart part/completion requests—which do not
carry Object Lock headers—can finish. Therefore explicit headers plus final
retention readback remain a hard canary acceptance gate. The canary interface
also requires a separately approved read-only audit verifier with exact access
to `AWSLogs/147229569658/*`; neither the writer nor planner is silently
broadened for that later proof. Real holdouts remain out of scope.

Deterministic mocked safety tests are available at:

```bash
./tests/test-calibration-evidence-prerequisites.sh
```

## Calibration evidence store

`provision-calibration-evidence-store.sh` plans or creates the separate
versioned, encrypted, public-blocked S3 bucket for write-once calibration
manifests and holdout evidence. It defaults to read-only plan mode and prints a
hash of the exact current state and a separate hash of the desired state.
Applying requires both hashes plus the explicit irreversible Object Lock
confirmation; the script refuses to retrofit the normal mutable state bucket
or silently rewrite an existing Years-based retention default as Days. Default
retention is 90-day COMPLIANCE mode. Do not upload a holdout until
split/model/config choices are frozen and the authority manifest has passed
review.

The writer role and a named, actively logging CloudTrail trail with write-capable
S3 object data events for the exact bucket prefix must already exist. Advanced
selectors are accepted only when their complete field set proves unfiltered S3
object writes for that prefix; read-only or event-name-filtered selectors are
rejected. The bucket policy restricts writes to that role and denies object
deletion, retention changes, and governance bypass. Existing lifecycle rules
and their transition-size compatibility mode are preserved and verified.
Per-object COMPLIANCE retention is the write-once control; administrators can
still change bucket policy/defaults for future objects, so organization SCPs
and CloudTrail monitoring should alert on bucket policy, lifecycle, and Object
Lock configuration changes. Retention extension and legal holds are
intentionally blocked for ordinary writers; changing that policy is a separate
reviewed operation.

```bash
# Read-only plan: review current + desired state and retain both printed hashes.
AWS_PROFILE=path AWS_REGION=us-west-1 \
CLOUDTRAIL_TRAIL_NAME=<trail-with-this-bucket-data-events> \
  ./provision-calibration-evidence-store.sh

# Apply only the exact reviewed state.
AWS_PROFILE=path AWS_REGION=us-west-1 PLAN_ONLY=false \
CLOUDTRAIL_TRAIL_NAME=<trail-with-this-bucket-data-events> \
EVIDENCE_WRITER_ROLE_ARN=arn:aws:iam::<account>:role/V2XCalibrationEvidenceWriter \
EXPECTED_CURRENT_STATE_HASH=<reviewed-hash> \
EXPECTED_DESIRED_STATE_HASH=<reviewed-desired-hash> \
CONFIRM_OBJECT_LOCK_IRREVERSIBLE=CONFIGURE_OBJECT_LOCKED_EVIDENCE_BUCKET \
  ./provision-calibration-evidence-store.sh
```

Before the first mutation the script stores mode-0700 rollback evidence. A new
empty bucket can be removed if provisioning fails. After any COMPLIANCE-locked
object is uploaded, deletion is impossible until retention expires; this is the
intended property, not a reversible deployment. A partial apply is recovered by
running a fresh plan, reviewing its new hash, and reconciling again. The script
does not upload a canary or holdout object.

## Security note

Do **not** paste AWS access keys into chat or commit them to git.

If you already shared credentials, treat them as compromised:
- If they are temporary STS creds, let them expire and issue new ones.
- If they are long-lived, **deactivate/rotate immediately** in IAM.

## Prereqs

- AWS CLI v2 authenticated (recommended: SSO or a named profile)
- `jq`, `sha256sum`, and `zip` available

## Provision

```bash
cd infra/aws-cli

# Optional: use a specific profile
export AWS_PROFILE="your-profile"

# Region is fixed by default to us-west-1, override if needed:
export AWS_REGION="us-west-1"

./provision.sh
```

Default resource names:

- DynamoDB table: `v2x-backend-detections`
- Ingest Lambda: `v2x-backend-ingest`
- Read Lambda: `v2x-backend-read`
- HTTP API: `v2x-backend-api`
- IoT rule: `v2x_backend_detections_to_ddb`
- IoT policy: `v2x-backend-edge-publish`

### If your role can’t call `iam:*` (common with SSO)

If you see `AccessDenied` for `iam:GetRole` / `iam:CreateRole`, don’t create a new role:

- Create/choose an existing IAM role for Lambda in the AWS console (or via your platform team).
- Ensure it trusts `lambda.amazonaws.com` and has `dynamodb:PutItem` on the table plus basic CloudWatch Logs permissions.
- Run:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 SKIP_IAM=true \
  LAMBDA_ROLE_ARN="arn:aws:iam::<account-id>:role/<existing-role>" \
  ./provision.sh
```

If your principal can’t edit IAM roles but you can `iam:PassRole`, you still need the role to already include DynamoDB permissions.

If you *can* add an inline policy to that existing role (still no new role), you can have the script add `dynamodb:PutItem` for you:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 SKIP_IAM=true \
  LAMBDA_ROLE_ARN="arn:aws:iam::<account-id>:role/<existing-role>" \
  ATTACH_DDB_PUT_POLICY=true \
  ./provision.sh
```

Artifacts:
- Device cert/key written under `./.secrets/iot/<thingName>/` (ignored by git)

## Create another device (Thing + cert)

```bash
cd infra/aws-cli
./create-device.sh edge-device-002
```

Note: `./provision.sh` will not generate new certificates for `THING_NAME` if one is already attached.

## Publish a test event (IAM creds, not device cert)

```bash
cd infra/aws-cli
./publish-test.sh
```

## Read and write API

Provision the write route:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 ./provision-write-api.sh
```

Provision the read routes:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 \
RECONCILE_LAMBDA=true ATTACH_DDB_READ_POLICY=true PLAN_ONLY=true \
  ./provision-read-api.sh
# Review the exact Lambda/integration/route/stage plan and copy its state hash,
# then apply the same inputs explicitly:
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 \
RECONCILE_LAMBDA=true PLAN_ONLY=false \
ATTACH_DDB_READ_POLICY=true \
EXPECTED_CURRENT_STATE_HASH=<reviewed-hash> ./provision-read-api.sh
```

The read provisioner reconciles one managed Lambda proxy integration and each
tracked route in place. It updates an existing route whose target drifted and
does not hide API Gateway failures behind `|| true`. `PLAN_ONLY=true` performs
AWS reads only. Running it repeatedly must report `KEEP`/`REUSE` for converged
routes rather than creating duplicate integrations.

IAM is unchanged by default (`ATTACH_DDB_READ_POLICY=false`). An existing read
Lambda retains its execution role. Creating the read Lambda requires an
explicit, pre-provisioned `READ_LAMBDA_ROLE_ARN`; the script will not infer the
ingest role or create a role. Set `ATTACH_DDB_READ_POLICY=true` only in a
separately approved IAM gate after reviewing the generated least-privilege
inline policy. The same reviewed gate is mandatory when deploying the HLS proxy
Lambda because its opaque session state requires prefix-scoped S3
get/put/delete access. A code apply with `RECONCILE_LAMBDA=true` fails closed
unless that policy reconciliation is explicit. The prior named inline policy
is included in the reviewed state hash and rollback evidence.

For the current recovery, the surgically deployed read Lambda is already the
accepted code/configuration. Reconcile only HTTP API integrations, routes, and
stages:

```bash
AWS_PROFILE=v2x-backend-deploy API_ID=w0j9m7dgpg \
RECONCILE_LAMBDA=false ATTACH_DDB_READ_POLICY=false PLAN_ONLY=true \
  ./provision-read-api.sh
# Copy currentStateHash from the reviewed plan.
AWS_PROFILE=v2x-backend-deploy API_ID=w0j9m7dgpg \
RECONCILE_LAMBDA=false ATTACH_DDB_READ_POLICY=false \
PLAN_ONLY=false EXPECTED_CURRENT_STATE_HASH=<reviewed-hash> ./provision-read-api.sh
```

Every apply captures a mode-0700 rollback directory before its first AWS
mutation. It contains redacted Lambda metadata/configuration/policy, complete
API/integration/route/stage JSON, inputs, and SHA-256 evidence. When
`RECONCILE_LAMBDA=true`, it also immediately downloads the expiring prior
Lambda artifact as `lambda-before.zip`; `Code.Location` is never persisted.
When execution-role policy reconciliation is requested, the prior named inline
policy (or its proven absence) is also retained.
Route-only recovery does not alter Lambda code, configuration, role, or
resource policy.

The dedicated deploy role intentionally has no `apigateway:DELETE`. Existing
route/integration targets can be restored with `PATCH`, while newly created
children remain as additive infrastructure during rollback. Exact deletion
requires a separately reviewed break-glass principal and is not part of this
recovery execution.

Optional env vars for `provision-read-api.sh` include:

- `STATE_BUCKET` (defaults to `v2x-backend-state-<account-id>-us-west-1`)
- `SNAPSHOT_URL_EXPIRES_SECONDS` (defaults to `300`)
- `READ_LAMBDA_ROLE_ARN` (required only when the read Lambda does not exist)
- `ATTACH_DDB_READ_POLICY` (defaults to `false`; explicit IAM mutation)
- `RECONCILE_LAMBDA` (defaults to `false`; set `true` only for a reviewed Lambda deployment)
- `EXPECTED_CURRENT_STATE_HASH` (required for apply; copied from the reviewed plan)
- `PLAN_ONLY` (defaults to `true`; apply requires an explicit `false`)

Tracked read routes include `/drive-config`, `/detections/timeline`,
`/video/coverage/{camera_id}`, and the opaque
`/video/proxy/{token}/{resource_id}` route in addition to the state, snapshot,
detection, demo-video, and HLS-session routes. After applying, capture
route-to-integration parity with:

```bash
api_id="$(aws apigatewayv2 get-apis \
  --query 'Items[?Name==`v2x-backend-api`].ApiId | [0]' --output text)"
aws apigatewayv2 get-routes --api-id "$api_id" \
  --query 'Items[].{route:RouteKey,target:Target}' --output table
aws apigatewayv2 get-integrations --api-id "$api_id" \
  --query 'Items[].{id:IntegrationId,uri:IntegrationUri,description:Description}' --output table
```

### Dedicated API/Lambda deploy role

The recovered IAM user `arn:aws:iam::147229569658:user/rfs-v2x-service`
cannot read API Gateway. Do not add
V2X deployment permissions to `AmplifyServiceRole-OPTIMAT-FRONT-SK`; Amplify's
service role has a separate trust boundary and is not a general deployment
role.

The plan-first bootstrap is deliberately separate from
`provision-read-api.sh`:

```bash
# Local policy rendering/validation only; makes no AWS calls.
ACTION=plan ./bootstrap-v2x-deploy-role.sh

# Read the actual user/role/policy/tag/attachment/profile state without writes.
ACTION=review ./bootstrap-v2x-deploy-role.sh

# Requires the exact state hash from review and a separately authorized IAM
# bootstrap principal.
ACTION=apply EXPECTED_CURRENT_STATE_HASH=<reviewed-hash> \
  ./bootstrap-v2x-deploy-role.sh
```

It creates/reconciles `V2XBackendDeployRole`, whose trust policy names only
that exact IAM user, plus a narrowly scoped inline user policy allowing
`rfs-v2x-service` to assume only that new role. The deploy role can read and
reconcile routes, integrations, and stages under HTTP API `w0j9m7dgpg`, and
update/inspect the existing `v2x-backend-read` Lambda and its API Gateway
invoke permission. It cannot create a Lambda, delete API Gateway resources,
modify IAM, or pass a role. `iam:PassRole` is unnecessary because the existing
read Lambda keeps its current execution role.

The role also has read-only CloudWatch metric access and read-only access to
the exact `/aws/lambda/v2x-backend-read` log group so a release gate can retain
error/throttle and failure-signature evidence. It has no CloudWatch or Logs
write actions and no access to other log groups.

Configure an AWS CLI role profile whose source profile is the existing
`rfs-v2x-service` credential chain, then force the known API ID so the read
provisioner does not need account-wide `GET /apis`:

```ini
[profile v2x-backend-deploy]
role_arn = arn:aws:iam::147229569658:role/V2XBackendDeployRole
source_profile = <rfs-v2x-service-source-profile>
role_session_name = v2x-backend-reconcile
region = us-west-1
```

```bash
AWS_PROFILE=v2x-backend-deploy API_ID=w0j9m7dgpg \
RECONCILE_LAMBDA=false ATTACH_DDB_READ_POLICY=false PLAN_ONLY=true \
  ./provision-read-api.sh
```

`ACTION=review` requires read access to the trusted user, its named inline
policy, the named role and inline policy (when present), role tags, attached and
inline policy inventories, and instance profiles. It prints a canonical
`currentStateHash` but writes nothing persistently. `ACTION=apply` re-reads that
state and refuses to mutate IAM without the matching
`EXPECTED_CURRENT_STATE_HASH`.

`ACTION=apply` writes a mode-0700 rollback directory under
`/home/path/V2XCarla/v2x-backend-backups/iam-bootstrap/` before changing an
existing role or inline policy. To restore an existing role, extract
`.Role.AssumeRolePolicyDocument` from `role.json` and `.PolicyDocument` from
`inline-policy.json`, then use `iam update-assume-role-policy` and
`iam put-role-policy`. Restore or remove the source-user assume policy with
`iam put-user-policy`/`iam delete-user-policy` according
to `source-assume-policy-existed.txt`. If both the deploy role and source-user
assume policy were new, deletion is explicitly gated:

```bash
ACTION=delete CONFIRM_DELETE=V2XBackendDeployRole \
  ./bootstrap-v2x-deploy-role.sh
```

Deletion refuses a deploy role that has an unmanaged inline policy, attached
managed policy, or instance profile. It never deletes the
`rfs-v2x-service` user or touches the Amplify service role; it manages only the
named, single-resource assume policy on that user.

Example write request:

```bash
curl -X POST -H 'content-type: application/json' \
  -d '{"object_id":"traffic_cone_001","timestamp_utc":"2026-02-05T00:00:00Z"}' \
  'https://<api-id>.execute-api.us-west-1.amazonaws.com/detections'
```

Example read request:

```bash
curl 'https://<api-id>.execute-api.us-west-1.amazonaws.com/detections/recent?limit=5'
```

## State bucket

Provision the dedicated private bucket for digital twin state assets:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 ./provision-state-bucket.sh
```

This creates a bucket named `v2x-backend-state-<account-id>-us-west-1` by default and seeds:

- `api/state.json`
- `api/map-data.json`
- S3 Public Access Block enabled for the bucket
- no public bucket policy or public ACLs

The read API serves the browser-facing state assets from this private bucket:

- `GET /state`
- `GET /map-data`
- `GET /snapshots/{object_id}/latest`

Harden an existing state bucket:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-1 ./harden-state-bucket.sh
```

## Video streams

Provision the Kinesis Video Streams in `us-west-2`:

```bash
cd infra/aws-cli
AWS_PROFILE="your-profile" AWS_REGION=us-west-2 ./provision-video-streams.sh
```

Defaults:

- Stream prefix: `v2x-backend-cam-`
- Camera IDs: `ch1 ch2 ch3 ch4`
- Retention: `24` hours requested; existing streams are left at their current retention if already higher

The read API also exposes:

- `GET /video/session/{camera_id}`
- `GET /video/proxy/{token}/{resource_id}` (opaque URLs returned by the session endpoint)
- `GET /video/coverage/{camera_id}?start=<ISO-8601>&end=<ISO-8601>`

The session endpoint returns a short-lived same-origin proxy URL for `ch1`
through `ch4`; it never returns the signed Kinesis URL. The Lambda stores the
Kinesis session token in a private, encrypted `hls-proxy/v1/` state object,
rewrites master and media playlists recursively, and authenticates each opaque
child descriptor with a key derived from that unexposed session token. Proxy
requests accept only the exact Kinesis Video origin and HLS resource allowlist,
reject redirects, and cap playlists at 1 MiB and binary fragments at 4 MiB.
Live sessions default to five minutes; archived sessions require both `start`
and `end` and are limited to a 24-hour window. Expired proxy state is rejected
and deleted when accessed. A bucket lifecycle rule for the dedicated
`hls-proxy/v1/` prefix remains recommended as defense-in-depth cleanup for
expired sessions that are never requested again.

The 4 MiB raw-fragment limit is below Lambda's synchronous base64 response
ceiling. Treat an observed larger fragment as a failed release gate requiring
lower producer fragment size/bitrate or a streaming proxy service; do not raise
the limit past the Lambda/API Gateway transport bound.

An HTTP 200 or advancing MJPEG byte count does not prove a live source. HLS
acceptance requires all four camera producer timestamps to remain recent and
advance across two samples, real frame content to change, and the perception
service to recover after a forced session expiry without accumulating
`CLOSE_WAIT` sockets. The returned `hlsUrl` is safe to identify as a proxy URL,
but do not retain the opaque token path in durable evidence:

```bash
for camera in ch1 ch2 ch3 ch4; do
  curl -fsS "https://<api-id>.execute-api.us-west-1.amazonaws.com/video/session/${camera}" \
    | jq '{cameraId,playbackMode,delivery,expiresIn,hlsUrlPresent:(.hlsUrl|type == "string" and length > 0)}'
done
```

## Cleanup

```bash
cd infra/aws-cli
./cleanup.sh
```

## Legacy decommission

After the new stack is verified, remove the old `v2x-detections-*` and `v2x-viewer` resources:

```bash
cd infra/aws-cli
./decommission-legacy-v2x.sh
```
