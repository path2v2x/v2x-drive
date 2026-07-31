#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'chmod -R u+rwX "$TMP" 2>/dev/null || true; rm -rf "$TMP"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_contains() {
  local file="$1"
  local pattern="$2"
  grep -Fq "$pattern" "$file" || fail "$file does not contain: $pattern"
}

MOCK_BIN="$TMP/bin"
MOCK_STATE="$TMP/aws-state"
MOCK_OBJECTS="$MOCK_STATE/objects"
MOCK_AWS_LOG="$MOCK_STATE/calls.log"
mkdir -p "$MOCK_BIN" "$MOCK_OBJECTS"
: >"$MOCK_AWS_LOG"

cat >"$MOCK_BIN/aws" <<'MOCK_AWS'
#!/usr/bin/env bash
set -euo pipefail

while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --region|--profile) shift 2 ;;
    *) break ;;
  esac
done
service="${1:-}"
operation="${2:-}"
shift 2 || true
printf '%s %s %s\n' "$service" "$operation" "$*" >>"$MOCK_AWS_LOG"

value_after() {
  local wanted="$1"
  shift
  while (( $# )); do
    if [[ "$1" == "$wanted" ]]; then
      printf '%s\n' "${2:-}"
      return 0
    fi
    shift
  done
  return 1
}

object_path() {
  local key="$1"
  printf '%s/%s\n' "$MOCK_OBJECTS" "${key//\//__}"
}

case "$service:$operation" in
  sts:get-caller-identity)
    if [[ " $* " == *" --query Account "* ]]; then
      printf '147229569658\n'
    else
      printf '{"Account":"147229569658","Arn":"arn:aws:iam::147229569658:user/test"}\n'
    fi
    ;;
  lambda:get-function)
    if [[ " $* " == *" --query Configuration.FunctionArn "* ]]; then
      printf 'arn:aws:lambda:us-west-1:147229569658:function:v2x-backend-read\n'
    else
      cat <<'JSON'
{"Configuration":{"FunctionName":"v2x-backend-read","FunctionArn":"arn:aws:lambda:us-west-1:147229569658:function:v2x-backend-read","Role":"arn:aws:iam::147229569658:role/read-role","Runtime":"python3.12","Handler":"index.handler","Timeout":30,"CodeSha256":"oldsha","Environment":{"Variables":{"STATE_BUCKET":"bucket"}}},"Code":{"Location":"https://example.invalid/signed-artifact"}}
JSON
    fi
    ;;
  lambda:get-policy)
    printf '%s\n' '{"Policy":"{\"Version\":\"2012-10-17\",\"Statement\":[]}"}'
    ;;
  apigatewayv2:get-api)
    if [[ " $* " == *" --query ApiEndpoint "* ]]; then
      printf 'https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com\n'
    else
      printf '{"ApiId":"w0j9m7dgpg","Name":"v2x-backend-api","ApiEndpoint":"https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com"}\n'
    fi
    ;;
  apigatewayv2:get-integrations)
    printf '{"Items":[{"IntegrationId":"int-existing","IntegrationType":"AWS_PROXY","IntegrationUri":"arn:aws:lambda:us-west-1:147229569658:function:v2x-backend-read","PayloadFormatVersion":"2.0","Description":"managed-by=v2x-backend/provision-read-api"}]}\n'
    ;;
  apigatewayv2:get-routes)
    printf '{"Items":[{"RouteId":"route-state","RouteKey":"GET /state","Target":"integrations/int-existing"}]}\n'
    ;;
  apigatewayv2:get-stages)
    printf '{"Items":[{"StageName":"$default","AutoDeploy":true}]}\n'
    ;;
  amplify:get-branch)
    printf '{"branch":{"environmentVariables":{}}}\n'
    ;;
  amplify:get-app)
    repository="$(cat "$MOCK_AMPLIFY_REPOSITORY_FILE")"
    if [[ " $* " == *" --query app.repository "* ]]; then
      printf '%s\n' "$repository"
    else
      jq -nc --arg repository "$repository" \
        '{app:{appId:"d1ugco1rmb7yjj",repository:$repository,updateTime:"2026-07-10T00:00:00Z"}}'
    fi
    ;;
  amplify:update-app)
    input_uri="$(value_after --cli-input-json "$@")"
    input_file="${input_uri#file://}"
    jq -e '
      keys == ["accessToken", "appId", "repository"]
      and .appId == "d1ugco1rmb7yjj"
      and .repository == "https://github.com/path2v2x/v2x-backend"
      and .accessToken == "test-repository-token"' "$input_file" >/dev/null
    jq -r '.repository' "$input_file" >"$MOCK_AMPLIFY_REPOSITORY_FILE"
    printf '{}\n'
    ;;
  apigatewayv2:create-route|apigatewayv2:update-route|apigatewayv2:create-integration|apigatewayv2:update-integration|apigatewayv2:create-stage|apigatewayv2:update-stage)
    printf '{}\n'
    ;;
  iam:get-user)
    printf '{"User":{"Path":"/","UserName":"rfs-v2x-service","UserId":"AIDATEST","Arn":"arn:aws:iam::147229569658:user/rfs-v2x-service","CreateDate":"2026-01-01T00:00:00Z"}}\n'
    ;;
  iam:get-user-policy|iam:get-role|iam:get-role-policy)
    echo 'An error occurred (NoSuchEntity) when calling the mock operation' >&2
    exit 254
    ;;
  iam:create-role|iam:put-role-policy|iam:put-user-policy|iam:tag-role)
    printf '{}\n'
    ;;
  s3api:head-object)
    key="$(value_after --key "$@")"
    path="$(object_path "$key")"
    if [[ ! -f "$path" ]]; then
      echo 'An error occurred (404) when calling the HeadObject operation: Not Found' >&2
      exit 254
    fi
    etag="$(sha256sum "$path" | awk '{print $1}')"
    jq -nc --arg etag "\"${etag}\"" '{ETag:$etag}'
    ;;
  s3api:get-object)
    key="$(value_after --key "$@")"
    path="$(object_path "$key")"
    destination="${*: -1}"
    cp "$path" "$destination"
    printf '{"ContentLength":%s}\n' "$(stat -c %s "$path")"
    ;;
  s3api:put-object)
    key="$(value_after --key "$@")"
    body="$(value_after --body "$@")"
    path="$(object_path "$key")"
    if [[ " $* " == *" --if-none-match * "* && -e "$path" ]]; then
      echo 'PreconditionFailed' >&2
      exit 254
    fi
    cp "$body" "$path"
    etag="$(sha256sum "$path" | awk '{print $1}')"
    jq -nc --arg etag "\"${etag}\"" '{ETag:$etag}'
    ;;
  s3api:get-bucket-lifecycle-configuration)
    echo 'An error occurred (NoSuchLifecycleConfiguration) when calling GetBucketLifecycleConfiguration' >&2
    exit 254
    ;;
  s3api:put-bucket-lifecycle-configuration)
    printf '{}\n'
    ;;
  s3api:copy-object)
    key="$(value_after --key "$@")"
    source="$(value_after --copy-source "$@")"
    source_key="${source#*/}"
    cp "$(object_path "$source_key")" "$(object_path "$key")"
    printf '{"CopyObjectResult":{"ETag":"mock"}}\n'
    ;;
  *)
    # Mutation calls used by the IAM apply test are intentionally accepted.
    if [[ "$service" == iam ]]; then
      printf '{}\n'
      exit 0
    fi
    echo "unsupported mock AWS call: $service $operation $*" >&2
    exit 99
    ;;
esac
MOCK_AWS
chmod 0755 "$MOCK_BIN/aws"

cat >"$MOCK_BIN/curl" <<'MOCK_CURL'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$MOCK_CURL_LOG"
output_file=""
previous=""
for argument in "$@"; do
  if [[ "$previous" == "-o" || "$previous" == "--output" ]]; then
    output_file="$argument"
    break
  fi
  previous="$argument"
done
payload=""
case "$*" in
  *drive-config*)
    payload='{"version":1,"expiresAt":"2099-01-01T00:00:00Z","cloudflareDriveWsUrl":"wss://drive.example.test"}'
    ;;
  *config.json*)
    payload='{"apiBaseUrl":"https://api.example.test","driveConfigPath":"/drive-config","perceptionStreamBaseUrl":"https://perception.example.test"}'
    ;;
  */health*)
    payload='{"status":"ok","ready":true,"cameras":{"ch1":{"fresh":true,"state":"streaming"},"ch2":{"fresh":true,"state":"streaming"},"ch3":{"fresh":true,"state":"streaming"},"ch4":{"fresh":true,"state":"streaming"}}}'
    ;;
esac
if [[ -n "$payload" ]]; then
  if [[ -n "$output_file" ]]; then
    printf '%s\n' "$payload" >"$output_file"
  else
    printf '%s\n' "$payload"
  fi
fi
MOCK_CURL
chmod 0755 "$MOCK_BIN/curl"

export MOCK_AWS_LOG MOCK_OBJECTS
export MOCK_CURL_LOG="$TMP/curl-calls.log"
export MOCK_AMPLIFY_REPOSITORY_FILE="$MOCK_STATE/amplify-repository.txt"
: >"$MOCK_CURL_LOG"
printf '%s\n' 'https://github.com/michaelvu1207/v2x-backend' \
  >"$MOCK_AMPLIFY_REPOSITORY_FILE"

# Route-only planning must prove that no Lambda mutation is selected.
PATH="$MOCK_BIN:$PATH" \
API_ID=w0j9m7dgpg RECONCILE_LAMBDA=false ATTACH_DDB_READ_POLICY=false PLAN_ONLY=true \
  "$ROOT/infra/aws-cli/provision-read-api.sh" >"$TMP/read-api-plan.txt"
assert_contains "$TMP/read-api-plan.txt" 'reconcileLambda=false'
assert_contains "$TMP/read-api-plan.txt" 'KEEP existing Lambda code, configuration, role, and policy'
assert_contains "$TMP/read-api-plan.txt" 'CREATE route GET /video/proxy/{token}/{resource_id}'
if grep -Eq 'lambda (update-function|create-function|add-permission|remove-permission)' "$MOCK_AWS_LOG"; then
  fail 'route-only plan attempted a Lambda mutation'
fi
read_api_hash="$(sed -n 's/^[[:space:]]*currentStateHash=//p' "$TMP/read-api-plan.txt" | tail -n 1)"
[[ "$read_api_hash" =~ ^[0-9a-f]{64}$ ]] || fail 'read API plan did not return a state hash'
PATH="$MOCK_BIN:$PATH" \
API_ID=w0j9m7dgpg RECONCILE_LAMBDA=false ATTACH_DDB_READ_POLICY=false \
PLAN_ONLY=false EXPECTED_CURRENT_STATE_HASH="$read_api_hash" BACKUP_ROOT="$TMP/read-api-backup" \
  "$ROOT/infra/aws-cli/provision-read-api.sh" >"$TMP/read-api-apply.txt"
find "$TMP/read-api-backup" -name current-state.json -type f | grep -q . \
  || fail 'route-only apply did not preserve current-state evidence'
if find "$TMP/read-api-backup" -name lambda-before.zip -type f | grep -q .; then
  fail 'route-only apply downloaded a Lambda artifact despite keeping Lambda unchanged'
fi
if grep -Eq 'lambda (update-function|create-function|add-permission|remove-permission)' "$MOCK_AWS_LOG"; then
  fail 'route-only apply attempted a Lambda mutation'
fi

# An explicitly requested read-role policy reconciliation must include the
# observed prior inline-policy state in the reviewed hash before any apply.
: >"$MOCK_AWS_LOG"
PATH="$MOCK_BIN:$PATH" API_ID=w0j9m7dgpg RECONCILE_LAMBDA=true \
ATTACH_DDB_READ_POLICY=true PLAN_ONLY=true \
  "$ROOT/infra/aws-cli/provision-read-api.sh" >"$TMP/read-api-iam-plan.txt"
assert_contains "$TMP/read-api-iam-plan.txt" 'observedInlinePolicyExists=false (included in currentStateHash)'
assert_contains "$MOCK_AWS_LOG" 'iam get-role-policy --role-name read-role --policy-name v2x-backend-detections-ddb-read'
PATH="$MOCK_BIN:$PATH" API_ID=w0j9m7dgpg RECONCILE_LAMBDA=true \
ATTACH_DDB_READ_POLICY=false PLAN_ONLY=true \
  "$ROOT/infra/aws-cli/provision-read-api.sh" >"$TMP/read-api-unprivileged-plan.txt"
assert_contains "$TMP/read-api-unprivileged-plan.txt" \
  'BLOCKED FOR APPLY: HLS proxy state access has not been reconciled'

# IAM apply must be preceded by a real-state review hash.
: >"$MOCK_AWS_LOG"
PATH="$MOCK_BIN:$PATH" ACTION=review \
  "$ROOT/infra/aws-cli/bootstrap-v2x-deploy-role.sh" >"$TMP/iam-review.txt"
iam_hash="$(sed -n 's/^[[:space:]]*currentStateHash=//p' "$TMP/iam-review.txt" | tail -n 1)"
[[ "$iam_hash" =~ ^[0-9a-f]{64}$ ]] || fail 'IAM review did not return a state hash'
if PATH="$MOCK_BIN:$PATH" ACTION=apply BACKUP_ROOT="$TMP/iam-backup-missing" \
    "$ROOT/infra/aws-cli/bootstrap-v2x-deploy-role.sh" >"$TMP/iam-apply-missing.txt" 2>&1; then
  fail 'IAM apply succeeded without EXPECTED_CURRENT_STATE_HASH'
fi
PATH="$MOCK_BIN:$PATH" ACTION=apply EXPECTED_CURRENT_STATE_HASH="$iam_hash" \
BACKUP_ROOT="$TMP/iam-backup" \
  "$ROOT/infra/aws-cli/bootstrap-v2x-deploy-role.sh" >"$TMP/iam-apply.txt"
find "$TMP/iam-backup" -name iam-current-state.json -type f | grep -q . \
  || fail 'IAM apply did not preserve current-state evidence'

# First Drive publication must record absence; rollback must create an expired,
# forward-versioned tombstone rather than deleting the audited object.
: >"$MOCK_AWS_LOG"
PATH="$MOCK_BIN:$PATH" ACTION=publish STATE_BUCKET=test-state \
EXPECTED_CURRENT_VERSION=0 DRIVE_WS_URL=wss://drive.example.test \
TAILSCALE_DRIVE_WS_URL=wss://tailscale.example.test \
  "$ROOT/scripts/publish-drive-tunnel-config.sh" >"$TMP/drive-publish.txt"
config_path="$MOCK_OBJECTS/api__drive-config.json"
jq -e '.version == 1 and (.tombstone // false) == false' "$config_path" >/dev/null
absence_path="$(find "$MOCK_OBJECTS" -name '*drive-config-prior-absence-*' -type f | head -n 1)"
[[ -n "$absence_path" ]] || fail 'first publication did not record prior absence'
jq -e '.kind == "drive-config-prior-absence" and .absent == true' "$absence_path" >/dev/null
absence_key="${absence_path##*/}"
absence_key="${absence_key//__/\/}"
PATH="$MOCK_BIN:$PATH" ACTION=rollback STATE_BUCKET=test-state \
EXPECTED_CURRENT_VERSION=1 ROLLBACK_BACKUP_KEY="$absence_key" \
  "$ROOT/scripts/publish-drive-tunnel-config.sh" >"$TMP/drive-rollback.txt"
jq -e '
  .version == 2
  and .tombstone == true
  and .restoresPriorAbsence == true
  and (.expiresAt < .updatedAt)' "$config_path" >/dev/null

# The launcher must not parse a root-owned EnvironmentFile in the service user.
cat >"$MOCK_BIN/cloudflared" <<'MOCK_CLOUDFLARED'
#!/usr/bin/env bash
printf '%s\n' "$*" >"$MOCK_CLOUDFLARED_ARGS"
MOCK_CLOUDFLARED
chmod 0755 "$MOCK_BIN/cloudflared"
printf 'this is deliberately not shell syntax (\n' >"$TMP/root-owned.env"
chmod 000 "$TMP/root-owned.env"
export MOCK_CLOUDFLARED_ARGS="$TMP/cloudflared-args.txt"
CLOUDFLARED_BIN="$MOCK_BIN/cloudflared" ENV_FILE="$TMP/root-owned.env" \
DRIVE_TUNNEL_MODE=quick ORIGIN_SERVICE=http://localhost:8765 \
  "$ROOT/scripts/launch-cloudflared-drive-tunnel.sh" >/dev/null
assert_contains "$MOCK_CLOUDFLARED_ARGS" 'tunnel --url http://localhost:8765'

# Static recovery guards that complement the executable mocks.
assert_contains "$ROOT/scripts/systemd/v2x-drive-link-health.service" 'EnvironmentFile=-/etc/v2x-drive-tunnel.env'
assert_contains "$ROOT/infra/amplify/deploy.sh" 'RECOVERY_CONNECTED_DEPLOY_GATE'
assert_contains "$ROOT/scripts/systemd/README.md" 'git -C /home/path/V2XCarla/v2x-backend stash push --include-untracked'
assert_contains "$ROOT/scripts/v2x-calibration-worker.sh" '(($root.Mounts // []) | length == 0)'
assert_contains "$ROOT/scripts/v2x-calibration-worker.sh" 'V2X_CALIBRATION_EXPECTED_IMAGE_ID'
assert_contains "$ROOT/scripts/v2x-calibration-worker.sh" 'V2X_CALIBRATION_MAP_READY_TIMEOUT_SECONDS:-180'
assert_contains "$ROOT/scripts/v2x-calibration-worker.sh" 'MAP_READY_TIMEOUT_SECONDS < 90 || MAP_READY_TIMEOUT_SECONDS > 300'
assert_contains "$ROOT/scripts/v2x-calibration-worker.sh" 'timeout --signal=TERM --kill-after=5s'
assert_contains "$ROOT/scripts/v2x-calibration-worker.sh" 'now - last_load_request >= 120.0'
if grep -A20 'if path == "/detections/recent"' \
    "$ROOT/infra/aws-cli/provision-read-api.sh" | grep -Fq 'table.scan'; then
  fail '/detections/recent still uses unordered DynamoDB Scan'
fi
python3 "$ROOT/infra/aws-cli/tests/test_generated_read_api.py"

# Literal braces in the default perception path must survive Bash parsing, and
# both publication gates must probe four distinct, exact camera URLs.
: >"$MOCK_CURL_LOG"
env -u PERCEPTION_STREAM_PATH_TEMPLATE PATH="$MOCK_BIN:$PATH" \
ACTION=plan UPDATE_DRIVE=false UPDATE_PERCEPTION=true \
PERCEPTION_STREAM_BASE_URL=https://perception.example.test \
  "$ROOT/scripts/publish-amplify-runtime-config.sh" >"$TMP/amplify-runtime-plan.txt"
for camera_id in ch1 ch2 ch3 ch4; do
  assert_contains "$MOCK_CURL_LOG" \
    "https://perception.example.test/streams/${camera_id}.mjpg"
done
if grep -Fq '{camera_id' "$MOCK_CURL_LOG"; then
  fail 'Amplify runtime publisher emitted an unrendered camera path marker'
fi

: >"$MOCK_CURL_LOG"
env -u PERCEPTION_STREAM_PATH_TEMPLATE PATH="$MOCK_BIN:$PATH" \
FRONTEND_CONFIG_URL=https://frontend.example.test/config.json \
PERCEPTION_PUBLIC_URL=https://perception.example.test \
  "$ROOT/scripts/check-perception-frontend-link.sh" \
  >"$TMP/perception-link-check.txt"
for camera_id in ch1 ch2 ch3 ch4; do
  assert_contains "$MOCK_CURL_LOG" \
    "https://perception.example.test/streams/${camera_id}.mjpg"
done
if grep -Fq '{camera_id' "$MOCK_CURL_LOG"; then
  fail 'Perception link checker emitted an unrendered camera path marker'
fi

# Secure wss:// checks must let websockets create its default TLS context.
# Passing ssl=None explicitly is rejected by newer releases before a handshake.
FAKE_PYTHON_MODULES="$TMP/fake-python-modules"
mkdir -p "$FAKE_PYTHON_MODULES"
cat >"$FAKE_PYTHON_MODULES/websockets.py" <<'MOCK_WEBSOCKETS'
class _Connection:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def connect(url, **kwargs):
    if url.startswith("wss://") and kwargs.get("ssl", "omitted") is None:
        raise ValueError("ssl=None is incompatible with a wss:// URI")
    return _Connection()
MOCK_WEBSOCKETS

: >"$MOCK_CURL_LOG"
PYTHONPATH="$FAKE_PYTHON_MODULES" PATH="$MOCK_BIN:$PATH" \
PYTHON_BIN="$(command -v python3)" \
FRONTEND_CONFIG_URL=https://frontend.example.test/config.json \
DRIVE_CONFIG_URL=https://api.example.test/drive-config \
DRIVE_LINK_HEALTH_REPAIR=false DRIVE_CONFIG_REQUIRED=true \
DRIVE_WS_INSECURE_SSL=false \
  "$ROOT/scripts/check-drive-frontend-link.sh" \
  >"$TMP/drive-link-check.txt"
assert_contains "$TMP/drive-link-check.txt" 'Drive frontend link is healthy.'

# Repository reconnection must use the lowercase AWS request schema while the
# token stays in a mode-0600 file rather than process arguments or logs.
PATH="$MOCK_BIN:$PATH" ACTION=plan \
  "$ROOT/infra/amplify/reconcile-repository.sh" \
  >"$TMP/amplify-repository-plan.txt"
repository_hash="$(sed -n 's/^[[:space:]]*currentMetadataHash=//p' \
  "$TMP/amplify-repository-plan.txt" | head -n 1)"
[[ "$repository_hash" =~ ^[0-9a-f]{64}$ ]] \
  || fail 'Amplify repository plan did not return a metadata hash'
printf '%s\n' 'test-repository-token' | PATH="$MOCK_BIN:$PATH" \
ACTION=apply TOKEN_FROM_STDIN=true EXPECTED_CURRENT_HASH="$repository_hash" \
BACKUP_DIR="$TMP/amplify-repository-backup" \
  "$ROOT/infra/amplify/reconcile-repository.sh" \
  >"$TMP/amplify-repository-apply.txt"
assert_contains "$TMP/amplify-repository-apply.txt" \
  'Saved repository rollback metadata:'
assert_contains "$MOCK_AMPLIFY_REPOSITORY_FILE" \
  'https://github.com/path2v2x/v2x-backend'
if grep -Fq 'test-repository-token' "$MOCK_AWS_LOG"; then
  fail 'Amplify repository token appeared in AWS process arguments/logs'
fi

if command -v systemd-analyze >/dev/null 2>&1; then
  unit_dir="$TMP/units"
  mkdir -p "$unit_dir"
  for unit in "$ROOT"/scripts/systemd/*.service "$ROOT"/scripts/systemd/*.timer; do
    sed "s#/home/path/V2XCarla/v2x-backend#${ROOT}#g" "$unit" \
      >"$unit_dir/$(basename "$unit")"
  done
  SYSTEMD_UNIT_PATH="$unit_dir:/usr/lib/systemd/system" \
    systemd-analyze verify "$unit_dir"/*.service "$unit_dir"/*.timer
fi

echo 'recovery infra tests: PASS'
