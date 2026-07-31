#!/usr/bin/env bash
set -euo pipefail

# Plan or reconcile the immutable audit, IAM, CloudTrail, and EventBridge
# prerequisites for the separately gated calibration evidence store. Plan mode
# is the default and performs AWS reads only.

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing dependency: $1" >&2
    exit 1
  }
}

AWS_BIN="${AWS_BIN:-aws}"
need "${AWS_BIN}"
need jq
need sha256sum

readonly EXPECTED_ACCOUNT_ID="147229569658"
readonly FIXED_REGION="us-west-1"
readonly EVIDENCE_BUCKET="v2x-calibration-evidence-147229569658-us-west-1"
readonly AUDIT_BUCKET="v2x-calibration-audit-147229569658-us-west-1"
readonly TRAIL_NAME="v2x-calibration-evidence-audit"
readonly WRITER_ROLE_NAME="V2XCalibrationEvidenceWriter"
readonly PLANNER_ROLE_NAME="V2XCalibrationEvidencePlanner"
readonly WRITER_POLICY_NAME="V2XCalibrationEvidenceWriterPolicy"
readonly PLANNER_POLICY_NAME="V2XCalibrationEvidencePlannerPolicy"
readonly RULE_NAME="v2x-calibration-evidence-audit-guard"
readonly RULE_TARGET_ID="CalibrationEvidenceAuditLog"
readonly LOG_GROUP_NAME="/aws/events/v2x-calibration-evidence-audit"
readonly LOG_RESOURCE_POLICY_NAME="v2x-calibration-evidence-audit-events"
readonly APPLY_LOCK_NAME="/v2x/calibration/evidence-prerequisites/apply-lock"
readonly SCHEMA_VERSION="v2x-calibration-evidence-prerequisites/v1"

AWS_REGION="${AWS_REGION:-${FIXED_REGION}}"
PLAN_ONLY="${PLAN_ONLY:-true}"
TRUST_PRINCIPAL_ARN="${TRUST_PRINCIPAL_ARN:-}"
TRUST_PRINCIPAL_ARN_CONFIRM="${TRUST_PRINCIPAL_ARN_CONFIRM:-}"
EXPECTED_CURRENT_STATE_HASH="${EXPECTED_CURRENT_STATE_HASH:-}"
EXPECTED_DESIRED_STATE_HASH="${EXPECTED_DESIRED_STATE_HASH:-}"
CONFIRM_PREREQUISITES="${CONFIRM_PREREQUISITES:-}"
CONFIRM_COMPLIANCE_AUDIT="${CONFIRM_COMPLIANCE_AUDIT:-}"
ACKNOWLEDGED_FOREIGN_POLICY_SHA256S="${ACKNOWLEDGED_FOREIGN_POLICY_SHA256S:-}"
PLAN_OUTPUT_DIR="${PLAN_OUTPUT_DIR:-}"
BACKUP_ROOT="${BACKUP_ROOT:-/home/path/V2XCarla/v2x-backend-backups/calibration-evidence-prerequisites}"
VERIFY_ATTEMPTS="${VERIFY_ATTEMPTS:-5}"
VERIFY_DELAY_SECONDS="${VERIFY_DELAY_SECONDS:-2}"
export AWS_REGION

case "${PLAN_ONLY}" in true|false) ;; *) echo "PLAN_ONLY must be true or false" >&2; exit 2 ;; esac
if [[ "${AWS_REGION}" != "${FIXED_REGION}" ]]; then
  echo "AWS_REGION must remain ${FIXED_REGION}" >&2
  exit 2
fi
if [[ ! "${VERIFY_ATTEMPTS}" =~ ^[1-9][0-9]*$ ]] ||
   [[ ! "${VERIFY_DELAY_SECONDS}" =~ ^[0-9]+$ ]]; then
  echo "VERIFY_ATTEMPTS must be positive and VERIFY_DELAY_SECONDS non-negative" >&2
  exit 2
fi
if [[ -z "${TRUST_PRINCIPAL_ARN}" ]]; then
  echo "TRUST_PRINCIPAL_ARN must be an explicit same-account IAM user or role ARN" >&2
  exit 2
fi
if [[ ! "${TRUST_PRINCIPAL_ARN}" =~ ^arn:aws:iam::${EXPECTED_ACCOUNT_ID}:(user|role)/[A-Za-z0-9+=,.@_/-]+$ ]] ||
   [[ "${TRUST_PRINCIPAL_ARN}" == *"*"* ]] ||
   [[ "${TRUST_PRINCIPAL_ARN}" == "arn:aws:iam::${EXPECTED_ACCOUNT_ID}:root" ]]; then
  echo "TRUST_PRINCIPAL_ARN must be a non-root, non-wildcard user/role in account ${EXPECTED_ACCOUNT_ID}" >&2
  exit 2
fi

WORKDIR="$(mktemp -d)"
DISCOVERY_DIR="${WORKDIR}/discovery"
GENERATED_DIR="${WORKDIR}/generated"
mkdir -p "${DISCOVERY_DIR}" "${GENERATED_DIR}"
CURRENT="${WORKDIR}/current.json"
DESIRED="${WORKDIR}/desired.json"
CANARY_INTERFACE="${WORKDIR}/later-canary-interface.json"
LOCK_CLAIMED=false
LOCK_TOKEN=""
LOCK_RELEASE_STATE="not_claimed"
PRELOCK_CURRENT="${WORKDIR}/current-prelock.json"

aws_cli() {
  "${AWS_BIN}" --region "${AWS_REGION}" "$@"
}

aws_s3api() {
  aws_cli s3api "$@" --expected-bucket-owner "${EXPECTED_ACCOUNT_ID}"
}

error_has_exact_code() {
  local file="$1" codes="$2"
  grep -Eq "\((${codes})\)" "${file}"
}

capture_optional() {
  local output="$1" codes="$2"
  shift 2
  local error="${output}.err"
  if "$@" >"${output}" 2>"${error}"; then
    rm -f "${error}"
    return 0
  fi
  if error_has_exact_code "${error}" "${codes}"; then
    printf '{"_absent":true}\n' >"${output}"
    rm -f "${error}"
    return 10
  fi
  cat "${error}" >&2
  echo "AWS discovery failed; refusing to map an ambiguous error to absence" >&2
  return 1
}

canonical_file() {
  local source="$1" destination="$2"
  jq -S . "${source}" >"${destination}"
}

release_lock() {
  if [[ "${LOCK_CLAIMED}" != "true" ]]; then
    return 0
  fi
  local current_value error="${WORKDIR}/lock-release.err"
  if ! current_value="$(aws_cli ssm get-parameter --name "${APPLY_LOCK_NAME}" \
      --query 'Parameter.Value' --output text 2>"${error}")"; then
    cat "${error}" >&2
    echo "Unable to read the owned apply lock before release; refusing to report success" >&2
    return 1
  fi
  rm -f "${error}"
  if [[ "${current_value}" != "${LOCK_TOKEN}" ]]; then
    LOCK_RELEASE_STATE="ownership_lost"
    echo "Apply lock ownership changed; refusing to clear it or report success" >&2
    return 1
  fi
  if ! aws_cli ssm delete-parameter --name "${APPLY_LOCK_NAME}" \
      >"${WORKDIR}/lock-delete.json" 2>"${error}"; then
    cat "${error}" >&2
    echo "Unable to delete the owned apply lock; refusing to report success" >&2
    return 1
  fi
  LOCK_RELEASE_STATE="delete_accepted_unverified"
  rm -f "${error}"
  if aws_cli ssm get-parameter --name "${APPLY_LOCK_NAME}" --output json \
      >"${WORKDIR}/lock-after-delete.json" 2>"${error}"; then
    LOCK_RELEASE_STATE="delete_accepted_still_present"
    echo "Apply lock still exists after delete; refusing to report success" >&2
    return 1
  fi
  if ! error_has_exact_code "${error}" 'ParameterNotFound'; then
    cat "${error}" >&2
    echo "Unable to verify exact ParameterNotFound after apply-lock deletion; refusing to report success" >&2
    return 1
  fi
  rm -f "${error}"
  LOCK_CLAIMED=false
  LOCK_RELEASE_STATE="confirmed_absent"
  echo "Apply lock release state: confirmed_absent"
}

cleanup() {
  local status=$?
  case "${LOCK_RELEASE_STATE}" in
    owned)
      echo "ERROR: apply did not complete. Release state: owned (last exact read matched this process; deletion was not confirmed accepted or was not attempted)." >&2
      echo "Rollback bundle: ${ROLLBACK_BUNDLE:-not-created}" >&2
      echo "Recovery gate: read the exact Parameter.Value, compare its token and rollback bundle with fresh plan-only state, then clear only in a separately reviewed manual action." >&2
      ;;
    ownership_lost)
      echo "ERROR: apply did not complete. Release state: ownership_lost; the parameter belongs to a different value and this process does not claim it is owned or retained." >&2
      echo "Rollback bundle: ${ROLLBACK_BUNDLE:-not-created}" >&2
      echo "Recovery gate: read and review the current Parameter.Value and its owning workflow; do not delete it as this process's lock." >&2
      ;;
    delete_accepted_unverified)
      echo "ERROR: apply did not complete. Release state: delete_accepted_unverified; AWS accepted deletion but current parameter existence is unknown." >&2
      echo "Rollback bundle: ${ROLLBACK_BUNDLE:-not-created}" >&2
      echo "Recovery gate: obtain an exact ssm:GetParameter result; accept only ParameterNotFound as cleared, otherwise review the returned value before any manual action." >&2
      ;;
    delete_accepted_still_present)
      echo "ERROR: apply did not complete. Release state: delete_accepted_still_present; AWS accepted deletion but an exact read proved the parameter still exists." >&2
      echo "Rollback bundle: ${ROLLBACK_BUNDLE:-not-created}" >&2
      echo "Recovery gate: review the current Parameter.Value and rollback bundle before any separately approved retry or manual deletion." >&2
      ;;
    not_claimed|confirmed_absent) ;;
    *) echo "ERROR: unknown apply-lock release state ${LOCK_RELEASE_STATE}" >&2 ;;
  esac
  rm -rf "${WORKDIR}"
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

discover_identity() {
  aws_cli sts get-caller-identity --output json >"${DISCOVERY_DIR}/identity.json"
  local account
  account="$(jq -r '.Account' "${DISCOVERY_DIR}/identity.json")"
  if [[ "${account}" != "${EXPECTED_ACCOUNT_ID}" ]]; then
    echo "Authenticated account ${account} does not match fixed account ${EXPECTED_ACCOUNT_ID}" >&2
    exit 3
  fi
}

discover_role() {
  local role_name="$1" policy_name="$2" output="$3" prefix="$4"
  local role="${DISCOVERY_DIR}/${prefix}-role.json"
  if capture_optional "${role}" 'NoSuchEntity' aws_cli iam get-role --role-name "${role_name}" --output json; then
    aws_cli iam list-role-policies --role-name "${role_name}" --output json >"${DISCOVERY_DIR}/${prefix}-inline-names.json"
    aws_cli iam list-attached-role-policies --role-name "${role_name}" --output json >"${DISCOVERY_DIR}/${prefix}-attached.json"
    aws_cli iam list-role-tags --role-name "${role_name}" --output json >"${DISCOVERY_DIR}/${prefix}-tags.json"
    capture_optional "${DISCOVERY_DIR}/${prefix}-managed-policy.json" 'NoSuchEntity' \
      aws_cli iam get-role-policy --role-name "${role_name}" --policy-name "${policy_name}" --output json || {
      local status=$?
      if [[ ${status} -ne 10 ]]; then return "${status}"; fi
    }
    jq -nS \
      --slurpfile role "${role}" \
      --slurpfile names "${DISCOVERY_DIR}/${prefix}-inline-names.json" \
      --slurpfile attached "${DISCOVERY_DIR}/${prefix}-attached.json" \
      --slurpfile tags "${DISCOVERY_DIR}/${prefix}-tags.json" \
      --slurpfile policy "${DISCOVERY_DIR}/${prefix}-managed-policy.json" '
      {exists:true,
       role:($role[0].Role | {Arn,RoleName,Path,Description,MaxSessionDuration,
         AssumeRolePolicyDocument,PermissionsBoundary}),
       inline_policy_names:($names[0].PolicyNames // [] | sort),
       attached_managed_policies:($attached[0].AttachedPolicies // [] | sort_by(.PolicyArn)),
       tags:($tags[0].Tags // [] | sort_by(.Key)),
       managed_inline_policy:(if ($policy[0]._absent // false) then null
         else ($policy[0] | {PolicyName,PolicyDocument}) end)}' >"${output}"
  else
    local status=$?
    if [[ ${status} -ne 10 ]]; then return "${status}"; fi
    jq -nS --arg name "${role_name}" '{exists:false,role_name:$name}' >"${output}"
  fi
}

discover_bucket() {
  local bucket="$1" output="$2" prefix="$3"
  local head_error="${DISCOVERY_DIR}/${prefix}-head.err"
  if aws_s3api head-bucket --bucket "${bucket}" >/dev/null 2>"${head_error}"; then
    rm -f "${head_error}"
  elif error_has_exact_code "${head_error}" '404|NoSuchBucket'; then
    rm -f "${head_error}"
    jq -nS --arg bucket "${bucket}" '{exists:false,bucket:$bucket}' >"${output}"
    return 0
  else
    cat "${head_error}" >&2
    echo "Bucket ${bucket} existence is not readable; refusing to treat it as absent" >&2
    return 1
  fi

  aws_s3api get-bucket-location --bucket "${bucket}" --output json >"${DISCOVERY_DIR}/${prefix}-location.json"
  local actual_region
  actual_region="$(jq -r '.LocationConstraint // "us-east-1"' "${DISCOVERY_DIR}/${prefix}-location.json")"
  if [[ "${actual_region}" != "${AWS_REGION}" ]]; then
    echo "Bucket ${bucket} is in ${actual_region}, expected ${AWS_REGION}" >&2
    return 1
  fi
  aws_s3api get-bucket-versioning --bucket "${bucket}" --output json >"${DISCOVERY_DIR}/${prefix}-versioning.json"
  aws_s3api get-bucket-logging --bucket "${bucket}" --output json >"${DISCOVERY_DIR}/${prefix}-logging.json"
  aws_s3api get-bucket-acl --bucket "${bucket}" --output json >"${DISCOVERY_DIR}/${prefix}-acl.json"

  local name codes
  while IFS='|' read -r name codes; do
    capture_optional "${DISCOVERY_DIR}/${prefix}-${name}.json" "${codes}" \
      aws_s3api "get-bucket-${name}" --bucket "${bucket}" --output json || {
      local status=$?
      if [[ ${status} -ne 10 ]]; then return "${status}"; fi
    }
  done <<'EOF'
policy|NoSuchBucketPolicy
tagging|NoSuchTagSet
lifecycle-configuration|NoSuchLifecycleConfiguration
encryption|ServerSideEncryptionConfigurationNotFoundError
ownership-controls|OwnershipControlsNotFoundError
replication|ReplicationConfigurationNotFoundError
EOF
  capture_optional "${DISCOVERY_DIR}/${prefix}-public-access-block.json" 'NoSuchPublicAccessBlockConfiguration' \
    aws_s3api get-public-access-block --bucket "${bucket}" --output json || {
    local status=$?
    if [[ ${status} -ne 10 ]]; then return "${status}"; fi
  }
  capture_optional "${DISCOVERY_DIR}/${prefix}-object-lock.json" 'ObjectLockConfigurationNotFoundError' \
    aws_s3api get-object-lock-configuration --bucket "${bucket}" --output json || {
    local status=$?
    if [[ ${status} -ne 10 ]]; then return "${status}"; fi
  }

  jq -nS --arg bucket "${bucket}" \
    --slurpfile location "${DISCOVERY_DIR}/${prefix}-location.json" \
    --slurpfile versioning "${DISCOVERY_DIR}/${prefix}-versioning.json" \
    --slurpfile logging "${DISCOVERY_DIR}/${prefix}-logging.json" \
    --slurpfile acl "${DISCOVERY_DIR}/${prefix}-acl.json" \
    --slurpfile policy "${DISCOVERY_DIR}/${prefix}-policy.json" \
    --slurpfile tags "${DISCOVERY_DIR}/${prefix}-tagging.json" \
    --slurpfile lifecycle "${DISCOVERY_DIR}/${prefix}-lifecycle-configuration.json" \
    --slurpfile encryption "${DISCOVERY_DIR}/${prefix}-encryption.json" \
    --slurpfile ownership "${DISCOVERY_DIR}/${prefix}-ownership-controls.json" \
    --slurpfile replication "${DISCOVERY_DIR}/${prefix}-replication.json" \
    --slurpfile public_access "${DISCOVERY_DIR}/${prefix}-public-access-block.json" \
    --slurpfile object_lock "${DISCOVERY_DIR}/${prefix}-object-lock.json" '
    {exists:true,bucket:$bucket,location:$location[0],versioning:$versioning[0],
     public_access:$public_access[0],encryption:$encryption[0],ownership:$ownership[0],
     object_lock:$object_lock[0],policy:$policy[0],
     tags:(($tags[0].TagSet // []) | sort_by(.Key)),
     lifecycle:$lifecycle[0],logging:$logging[0],replication:$replication[0],acl:$acl[0]}' >"${output}"
}

discover_trail() {
  local output="$1"
  if capture_optional "${DISCOVERY_DIR}/trail-get.json" 'TrailNotFoundException' \
      aws_cli cloudtrail get-trail --name "${TRAIL_NAME}" --output json; then
    :
  else
    local status=$?
    if [[ ${status} -eq 10 ]]; then
      jq -nS --arg name "${TRAIL_NAME}" '{exists:false,name:$name}' >"${output}"
      return 0
    fi
    return "${status}"
  fi
  if [[ "$(jq -r '.Trail // empty' "${DISCOVERY_DIR}/trail-get.json")" == "" ]]; then
    jq -nS --arg name "${TRAIL_NAME}" '{exists:false,name:$name}' >"${output}"
    return 0
  fi
  aws_cli cloudtrail get-trail-status --name "${TRAIL_NAME}" --output json >"${DISCOVERY_DIR}/trail-status.json"
  aws_cli cloudtrail get-event-selectors --trail-name "${TRAIL_NAME}" --output json >"${DISCOVERY_DIR}/trail-selectors.json"
  local arn
  arn="$(jq -r '.Trail.TrailARN' "${DISCOVERY_DIR}/trail-get.json")"
  aws_cli cloudtrail list-tags --resource-id-list "${arn}" --output json >"${DISCOVERY_DIR}/trail-tags.json"
  jq -nS --slurpfile trail "${DISCOVERY_DIR}/trail-get.json" \
    --slurpfile status "${DISCOVERY_DIR}/trail-status.json" \
    --slurpfile selectors "${DISCOVERY_DIR}/trail-selectors.json" \
    --slurpfile tags "${DISCOVERY_DIR}/trail-tags.json" '
    {exists:true,trail:$trail[0].Trail,
     status:($status[0] | {IsLogging,LatestDeliveryError,LatestNotificationError}),
     selectors:($selectors[0] | {EventSelectors,AdvancedEventSelectors}),
     tags:(($tags[0].ResourceTagList[0].TagsList // []) | sort_by(.Key))}' >"${output}"
}

discover_monitoring() {
  local rule="${DISCOVERY_DIR}/rule.json"
  if capture_optional "${rule}" 'ResourceNotFoundException' aws_cli events describe-rule --name "${RULE_NAME}" --output json; then
    aws_cli events list-targets-by-rule --rule "${RULE_NAME}" --output json >"${DISCOVERY_DIR}/rule-targets.json"
    aws_cli events list-tags-for-resource --resource-arn "$(jq -r '.Arn' "${rule}")" --output json >"${DISCOVERY_DIR}/rule-tags.json"
    jq -nS --slurpfile rule "${rule}" --slurpfile targets "${DISCOVERY_DIR}/rule-targets.json" \
      --slurpfile tags "${DISCOVERY_DIR}/rule-tags.json" '
      {exists:true,rule:($rule[0] | {Name,Arn,EventPattern,State,Description}),
       targets:(($targets[0].Targets // []) | sort_by(.Id)),
       tags:(($tags[0].Tags // []) | sort_by(.Key))}' >"${DISCOVERY_DIR}/monitor-rule-state.json"
  else
    local status=$?
    if [[ ${status} -ne 10 ]]; then return "${status}"; fi
    jq -nS --arg name "${RULE_NAME}" '{exists:false,name:$name}' >"${DISCOVERY_DIR}/monitor-rule-state.json"
  fi

  aws_cli logs describe-log-groups --log-group-name-prefix "${LOG_GROUP_NAME}" --output json >"${DISCOVERY_DIR}/log-groups.json"
  local log_arn
  log_arn="$(jq -r --arg name "${LOG_GROUP_NAME}" '
    .logGroups[]? | select(.logGroupName==$name) | (.logGroupArn // (.arn | sub(":\\*$";"")))
  ' "${DISCOVERY_DIR}/log-groups.json" | head -n1)"
  if [[ -n "${log_arn}" ]]; then
    aws_cli logs list-tags-for-resource --resource-arn "${log_arn}" --output json >"${DISCOVERY_DIR}/log-tags.json"
    jq -nS --arg name "${LOG_GROUP_NAME}" --arg arn "${log_arn}" \
      --slurpfile groups "${DISCOVERY_DIR}/log-groups.json" --slurpfile tags "${DISCOVERY_DIR}/log-tags.json" '
      {exists:true,log_group:($groups[0].logGroups[] | select(.logGroupName==$name)
         | {logGroupName,arn,retentionInDays,kmsKeyId}),
       tags:($tags[0].tags // {} | to_entries | sort_by(.key))}' >"${DISCOVERY_DIR}/monitor-log-state.json"
  else
    jq -nS --arg name "${LOG_GROUP_NAME}" '{exists:false,name:$name}' >"${DISCOVERY_DIR}/monitor-log-state.json"
  fi
  aws_cli logs describe-resource-policies --output json >"${DISCOVERY_DIR}/log-resource-policies.json"
  jq -nS --arg name "${LOG_RESOURCE_POLICY_NAME}" --slurpfile policies "${DISCOVERY_DIR}/log-resource-policies.json" '
    (($policies[0].resourcePolicies // [] | map(select(.policyName==$name))) as $matches
     | if ($matches|length)==0 then {exists:false,name:$name}
       elif ($matches|length)==1 then {exists:true,policy:$matches[0]}
       else error("duplicate CloudWatch Logs resource policy names") end)' >"${DISCOVERY_DIR}/monitor-log-policy-state.json"
}

discover_lock() {
  if capture_optional "${DISCOVERY_DIR}/apply-lock.json" 'ParameterNotFound' \
    aws_cli ssm get-parameter --name "${APPLY_LOCK_NAME}" --output json; then
    jq -nS --slurpfile value "${DISCOVERY_DIR}/apply-lock.json" \
      '{exists:true,parameter:($value[0].Parameter | {Name,Type,Value,Version,LastModifiedDate,ARN})}' \
      >"${DISCOVERY_DIR}/apply-lock-state.json"
  else
    local status=$?
    if [[ ${status} -ne 10 ]]; then return "${status}"; fi
    jq -nS --arg name "${APPLY_LOCK_NAME}" '{exists:false,name:$name}' >"${DISCOVERY_DIR}/apply-lock-state.json"
  fi
}

discover_all() {
  discover_identity
  discover_bucket "${EVIDENCE_BUCKET}" "${DISCOVERY_DIR}/evidence-bucket-state.json" evidence
  discover_bucket "${AUDIT_BUCKET}" "${DISCOVERY_DIR}/audit-bucket-state.json" audit
  discover_role "${WRITER_ROLE_NAME}" "${WRITER_POLICY_NAME}" "${DISCOVERY_DIR}/writer-role-state.json" writer
  discover_role "${PLANNER_ROLE_NAME}" "${PLANNER_POLICY_NAME}" "${DISCOVERY_DIR}/planner-role-state.json" planner
  discover_trail "${DISCOVERY_DIR}/trail-state.json"
  discover_monitoring
  discover_lock
  jq -nS --arg schema "${SCHEMA_VERSION}" --arg region "${AWS_REGION}" \
    --slurpfile identity "${DISCOVERY_DIR}/identity.json" \
    --slurpfile evidence "${DISCOVERY_DIR}/evidence-bucket-state.json" \
    --slurpfile audit "${DISCOVERY_DIR}/audit-bucket-state.json" \
    --slurpfile writer "${DISCOVERY_DIR}/writer-role-state.json" \
    --slurpfile planner "${DISCOVERY_DIR}/planner-role-state.json" \
    --slurpfile trail "${DISCOVERY_DIR}/trail-state.json" \
    --slurpfile rule "${DISCOVERY_DIR}/monitor-rule-state.json" \
    --slurpfile log "${DISCOVERY_DIR}/monitor-log-state.json" \
    --slurpfile log_policy "${DISCOVERY_DIR}/monitor-log-policy-state.json" \
    --slurpfile lock "${DISCOVERY_DIR}/apply-lock-state.json" '
    {schema:$schema,account:$identity[0].Account,region:$region,caller_arn:$identity[0].Arn,
     resources:{evidence_bucket:$evidence[0],audit_bucket:$audit[0],
       writer_role:$writer[0],planner_role:$planner[0],trail:$trail[0],
       event_rule:$rule[0],log_group:$log[0],log_resource_policy:$log_policy[0],
       apply_lock:$lock[0]}}' >"${CURRENT}"
}

managed_tags_array() {
  local current_tags_json="$1" purpose="$2" output="$3"
  jq -nS --argjson current "${current_tags_json}" --arg purpose "${purpose}" '
    ([$current[]? | select(.Key as $key
       | ["managed-by","purpose","ue-runtime"] | index($key) | not)]
     + [{Key:"managed-by",Value:"v2x-backend"},
        {Key:"purpose",Value:$purpose},{Key:"ue-runtime",Value:"ue5-only"}]
     | sort_by(.Key))' >"${output}"
}

generate_policy_documents() {
  local existing_policy='{"Version":"2012-10-17","Statement":[]}'
  if jq -e '.resources.audit_bucket.exists and (.resources.audit_bucket.policy.Policy | type == "string")' \
      "${CURRENT}" >/dev/null 2>&1; then
    existing_policy="$(jq -er '.resources.audit_bucket.policy.Policy | fromjson' "${CURRENT}")"
  fi
  jq -nS --argjson policy "${existing_policy}" '$policy' >"${GENERATED_DIR}/audit-existing-policy.json"

  local managed_sids
  managed_sids='["DenyInsecureTransport","DenyAuditDeletionRetentionMutation","AllowCloudTrailAclCheck","AllowCloudTrailWrite"]'
  : >"${GENERATED_DIR}/foreign-policy.jsonl"
  while IFS= read -r statement; do
    local digest effect
    digest="$(printf '%s\n' "${statement}" | sha256sum | awk '{print $1}')"
    effect="$(jq -r '.Effect // ""' <<<"${statement}")"
    jq -ncS --argjson statement "${statement}" --arg sha "${digest}" --arg effect "${effect}" \
      '{sha256:$sha,effect:$effect,statement:$statement}' >>"${GENERATED_DIR}/foreign-policy.jsonl"
  done < <(jq -cS --argjson managed "${managed_sids}" '
    (.Statement // [] | if type=="array" then . elif type=="object" then [.] else [] end)
    | .[] | select((.Sid // "") as $sid | $managed | index($sid) | not)' \
    "${GENERATED_DIR}/audit-existing-policy.json")
  if [[ -s "${GENERATED_DIR}/foreign-policy.jsonl" ]]; then
    jq -sS 'sort_by(.sha256)' "${GENERATED_DIR}/foreign-policy.jsonl" >"${GENERATED_DIR}/foreign-policy.json"
  else
    printf '[]\n' >"${GENERATED_DIR}/foreign-policy.json"
  fi

  jq -nS --arg account "${EXPECTED_ACCOUNT_ID}" --arg bucket "${AUDIT_BUCKET}" \
    --arg trail "arn:aws:cloudtrail:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:trail/${TRAIL_NAME}" '{
    Version:"2012-10-17",
    Statement:[
      {Sid:"DenyInsecureTransport",Effect:"Deny",Principal:"*",Action:"s3:*",
       Resource:[("arn:aws:s3:::"+$bucket),("arn:aws:s3:::"+$bucket+"/*")],
       Condition:{Bool:{"aws:SecureTransport":"false"}}},
      {Sid:"DenyAuditDeletionRetentionMutation",Effect:"Deny",Principal:"*",
       Action:["s3:DeleteObject","s3:DeleteObjectVersion","s3:PutObjectRetention",
         "s3:PutObjectLegalHold","s3:BypassGovernanceRetention"],
       Resource:("arn:aws:s3:::"+$bucket+"/*")},
      {Sid:"AllowCloudTrailAclCheck",Effect:"Allow",
       Principal:{Service:"cloudtrail.amazonaws.com"},Action:"s3:GetBucketAcl",
       Resource:("arn:aws:s3:::"+$bucket),
       Condition:{StringEquals:{"AWS:SourceArn":$trail}}},
      {Sid:"AllowCloudTrailWrite",Effect:"Allow",
       Principal:{Service:"cloudtrail.amazonaws.com"},Action:"s3:PutObject",
       Resource:("arn:aws:s3:::"+$bucket+"/AWSLogs/"+$account+"/*"),
       Condition:{StringEquals:{"s3:x-amz-acl":"bucket-owner-full-control","AWS:SourceArn":$trail}}}
    ]}' >"${GENERATED_DIR}/audit-managed-policy.json"
  jq -nS --slurpfile existing "${GENERATED_DIR}/foreign-policy.json" \
    --slurpfile managed "${GENERATED_DIR}/audit-managed-policy.json" '
    {Version:"2012-10-17",
     Statement:(($existing[0] | map(.statement)) + $managed[0].Statement)}' \
    >"${GENERATED_DIR}/audit-policy.json"

  jq -nS --arg principal "${TRUST_PRINCIPAL_ARN}" '{Version:"2012-10-17",Statement:[{
    Sid:"TrustExplicitCalibrationOperator",Effect:"Allow",Principal:{AWS:$principal},
    Action:"sts:AssumeRole"}]}' >"${GENERATED_DIR}/trust-policy.json"

  jq -nS --arg bucket "${EVIDENCE_BUCKET}" '{Version:"2012-10-17",Statement:[
    {Sid:"AllowEvidenceBucketReadback",Effect:"Allow",
     Action:["s3:GetBucketLocation","s3:GetBucketVersioning","s3:GetBucketObjectLockConfiguration",
       "s3:ListBucket","s3:ListBucketVersions","s3:ListBucketMultipartUploads"],
     Resource:("arn:aws:s3:::"+$bucket)},
    {Sid:"AllowComplianceWritesAndMultipartCompletion",Effect:"Allow",Action:"s3:PutObject",
     Resource:("arn:aws:s3:::"+$bucket+"/*"),
     Condition:{StringEqualsIfExists:{"s3:object-lock-mode":"COMPLIANCE"},
       NumericGreaterThanEqualsIfExists:{"s3:object-lock-remaining-retention-days":"90"}}},
    {Sid:"DenyNonComplianceLockHeader",Effect:"Deny",Action:"s3:PutObject",
     Resource:("arn:aws:s3:::"+$bucket+"/*"),
     Condition:{Null:{"s3:object-lock-mode":"false"},
       StringNotEquals:{"s3:object-lock-mode":"COMPLIANCE"}}},
    {Sid:"DenyRetentionHeaderBelowNinetyDays",Effect:"Deny",Action:"s3:PutObject",
     Resource:("arn:aws:s3:::"+$bucket+"/*"),
     Condition:{Null:{"s3:object-lock-retain-until-date":"false"},
       NumericLessThan:{"s3:object-lock-remaining-retention-days":"90"}}},
    {Sid:"AllowEvidenceObjectReadbackAndMultipartCleanup",Effect:"Allow",
     Action:["s3:GetObject","s3:GetObjectVersion","s3:GetObjectAttributes",
       "s3:GetObjectRetention","s3:GetObjectTagging","s3:ListMultipartUploadParts",
       "s3:AbortMultipartUpload"],Resource:("arn:aws:s3:::"+$bucket+"/*")},
    {Sid:"DenyEvidenceDeletionAndLockMutation",Effect:"Deny",
     Action:["s3:DeleteObject","s3:DeleteObjectVersion","s3:PutObjectRetention",
       "s3:PutObjectLegalHold","s3:BypassGovernanceRetention"],
     Resource:("arn:aws:s3:::"+$bucket+"/*")},
    {Sid:"DenyS3OutsideEvidenceBucket",Effect:"Deny",Action:"s3:*",
     NotResource:[("arn:aws:s3:::"+$bucket),("arn:aws:s3:::"+$bucket+"/*")]},
    {Sid:"DenyUnexpectedAwsActions",Effect:"Deny",
     NotAction:["s3:GetBucketLocation","s3:GetBucketVersioning","s3:GetBucketObjectLockConfiguration",
       "s3:ListBucket","s3:ListBucketVersions","s3:ListBucketMultipartUploads","s3:PutObject",
       "s3:GetObject","s3:GetObjectVersion","s3:GetObjectAttributes","s3:GetObjectRetention",
       "s3:GetObjectTagging","s3:ListMultipartUploadParts","s3:AbortMultipartUpload"],
     Resource:"*"}
  ]}' >"${GENERATED_DIR}/writer-policy.json"

  jq -nS --arg account "${EXPECTED_ACCOUNT_ID}" --arg region "${AWS_REGION}" \
    --arg evidence "${EVIDENCE_BUCKET}" --arg audit "${AUDIT_BUCKET}" \
    --arg writer "${WRITER_ROLE_NAME}" --arg planner "${PLANNER_ROLE_NAME}" \
    --arg trail "arn:aws:cloudtrail:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:trail/${TRAIL_NAME}" \
    --arg rule "arn:aws:events:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:rule/${RULE_NAME}" \
    --arg log "arn:aws:logs:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:log-group:${LOG_GROUP_NAME}" \
    --arg lock "arn:aws:ssm:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:parameter/v2x/calibration/evidence-prerequisites/apply-lock" '{
    Version:"2012-10-17",Statement:[
      {Sid:"ReadIdentity",Effect:"Allow",Action:"sts:GetCallerIdentity",Resource:"*"},
      {Sid:"ReadManagedRoles",Effect:"Allow",
       Action:["iam:GetRole","iam:GetRolePolicy","iam:ListRolePolicies",
         "iam:ListAttachedRolePolicies","iam:ListRoleTags"],
       Resource:[("arn:aws:iam::"+$account+":role/"+$writer),("arn:aws:iam::"+$account+":role/"+$planner)]},
      {Sid:"ReadFixedBuckets",Effect:"Allow",
       Action:["s3:GetBucketLocation","s3:GetBucketVersioning","s3:GetBucketPublicAccessBlock",
         "s3:GetEncryptionConfiguration","s3:GetBucketOwnershipControls","s3:GetBucketObjectLockConfiguration",
         "s3:GetBucketPolicy","s3:GetBucketTagging","s3:GetLifecycleConfiguration",
         "s3:GetBucketLogging","s3:GetReplicationConfiguration","s3:GetBucketAcl","s3:ListBucket"],
       Resource:[("arn:aws:s3:::"+$evidence),("arn:aws:s3:::"+$audit)]},
      {Sid:"ReadFixedTrail",Effect:"Allow",
       Action:["cloudtrail:GetTrail","cloudtrail:GetTrailStatus","cloudtrail:GetEventSelectors","cloudtrail:ListTags"],
       Resource:$trail},
      {Sid:"ReadFixedRule",Effect:"Allow",
       Action:["events:DescribeRule","events:ListTargetsByRule","events:ListTagsForResource"],Resource:$rule},
      {Sid:"ReadMonitoringLogs",Effect:"Allow",
       Action:["logs:DescribeLogGroups","logs:DescribeResourcePolicies"],Resource:"*"},
      {Sid:"ReadMonitoringLogTags",Effect:"Allow",Action:"logs:ListTagsForResource",Resource:$log},
      {Sid:"ReadApplyLock",Effect:"Allow",Action:"ssm:GetParameter",Resource:$lock}
    ]}' >"${GENERATED_DIR}/planner-policy.json"
}

generate_monitoring_documents() {
  jq -nS --arg evidence "${EVIDENCE_BUCKET}" --arg audit "${AUDIT_BUCKET}" \
    --arg trail "${TRAIL_NAME}" --arg writer "${WRITER_ROLE_NAME}" \
    --arg planner "${PLANNER_ROLE_NAME}" --arg rule "${RULE_NAME}" \
    --arg log_group "${LOG_GROUP_NAME}" --arg log_policy "${LOG_RESOURCE_POLICY_NAME}" \
    --arg lock_name "${APPLY_LOCK_NAME}" \
    --arg trail_arn "arn:aws:cloudtrail:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:trail/${TRAIL_NAME}" '{
    source:["aws.cloudtrail","aws.s3","aws.iam","aws.events","aws.logs","aws.ssm"],
    "$or":[
      {detail:{eventSource:["cloudtrail.amazonaws.com"],
        eventName:["StopLogging","DeleteTrail","UpdateTrail"],
        requestParameters:{name:[$trail,$trail_arn]}}},
      {detail:{eventSource:["cloudtrail.amazonaws.com"],
        eventName:["PutEventSelectors"],requestParameters:{trailName:[$trail,$trail_arn]}}},
      {detail:{eventSource:["s3.amazonaws.com"],
        eventName:["PutBucketPolicy","DeleteBucketPolicy","PutObjectLockConfiguration",
          "PutBucketLifecycle","DeleteBucketLifecycle","PutBucketVersioning","PutBucketEncryption",
          "DeleteBucketEncryption","PutPublicAccessBlock","DeletePublicAccessBlock",
          "PutBucketOwnershipControls","DeleteBucketOwnershipControls","PutBucketTagging","DeleteBucketTagging",
          "DeleteBucket","PutBucketAcl","PutBucketReplication","DeleteBucketReplication"],
        requestParameters:{bucketName:[$evidence,$audit]}}},
      {detail:{eventSource:["iam.amazonaws.com"],
        eventName:["UpdateAssumeRolePolicy","PutRolePolicy","DeleteRolePolicy","AttachRolePolicy",
          "DetachRolePolicy","DeleteRole","TagRole","UntagRole","PutRolePermissionsBoundary",
          "DeleteRolePermissionsBoundary"],requestParameters:{roleName:[$writer,$planner]}}},
      {detail:{eventSource:["events.amazonaws.com"],
        eventName:["PutRule","DisableRule","EnableRule","DeleteRule","TagResource","UntagResource"],
        requestParameters:{name:[$rule]}}},
      {detail:{eventSource:["events.amazonaws.com"],
        eventName:["PutTargets","RemoveTargets"],requestParameters:{rule:[$rule]}}},
      {detail:{eventSource:["logs.amazonaws.com"],
        eventName:["DeleteLogGroup","PutRetentionPolicy","DeleteRetentionPolicy","TagResource","UntagResource"],
        requestParameters:{logGroupName:[$log_group]}}},
      {detail:{eventSource:["logs.amazonaws.com"],
        eventName:["PutResourcePolicy","DeleteResourcePolicy"],requestParameters:{policyName:[$log_policy]}}},
      {detail:{eventSource:["ssm.amazonaws.com"],eventName:["PutParameter","DeleteParameter"],
        requestParameters:{name:[$lock_name]}}}
    ]}' >"${GENERATED_DIR}/event-pattern.json"

  jq -nS --arg prefix "arn:aws:s3:::${EVIDENCE_BUCKET}/" '[{
    IncludeManagementEvents:true,ReadWriteType:"WriteOnly",
    ExcludeManagementEventSources:[],
    DataResources:[{Type:"AWS::S3::Object",Values:[$prefix]}]}]' \
    >"${GENERATED_DIR}/event-selectors.json"

  jq -nS --arg log "arn:aws:logs:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:log-group:${LOG_GROUP_NAME}:*" '{
    Version:"2012-10-17",Statement:[{Sid:"AllowEventBridgeLogDelivery",
      Effect:"Allow",Principal:{Service:["events.amazonaws.com","delivery.logs.amazonaws.com"]},
      Action:["logs:CreateLogStream","logs:PutLogEvents"],Resource:$log}]}' \
    >"${GENERATED_DIR}/log-resource-policy.json"
}

generate_desired() {
  generate_policy_documents
  generate_monitoring_documents

  jq -nS --slurpfile current "${CURRENT}" '
    ($current[0].resources.audit_bucket.object_lock.ObjectLockConfiguration.Rule.DefaultRetention // null) as $r |
    {ObjectLockEnabled:"Enabled",Rule:{DefaultRetention:
      (if (($r.Mode // "") == "COMPLIANCE") and
          (((($r.Days // 0) | type) == "number" and ($r.Days // 0) >= 365) or
           ((($r.Years // 0) | type) == "number" and ($r.Years // 0) >= 1))
       then $r else {Mode:"COMPLIANCE",Days:365} end)}}' \
    >"${GENERATED_DIR}/audit-object-lock.json"

  managed_tags_array "$(jq -c '.resources.audit_bucket.tags // []' "${CURRENT}")" \
    calibration-evidence-audit "${GENERATED_DIR}/audit-tags.json"
  managed_tags_array "$(jq -c '.resources.writer_role.tags // []' "${CURRENT}")" \
    calibration-evidence "${GENERATED_DIR}/writer-tags.json"
  managed_tags_array "$(jq -c '.resources.planner_role.tags // []' "${CURRENT}")" \
    calibration-evidence "${GENERATED_DIR}/planner-tags.json"
  managed_tags_array "$(jq -c '.resources.trail.tags // []' "${CURRENT}")" \
    calibration-evidence-audit "${GENERATED_DIR}/trail-tags.json"
  managed_tags_array "$(jq -c '.resources.event_rule.tags // []' "${CURRENT}")" \
    calibration-evidence-audit "${GENERATED_DIR}/rule-tags.json"

  jq -nS --arg schema "${SCHEMA_VERSION}" --arg account "${EXPECTED_ACCOUNT_ID}" \
    --arg region "${AWS_REGION}" --arg trust "${TRUST_PRINCIPAL_ARN}" \
    --arg evidence "${EVIDENCE_BUCKET}" --arg audit "${AUDIT_BUCKET}" \
    --arg trail_name "${TRAIL_NAME}" --arg writer_name "${WRITER_ROLE_NAME}" \
    --arg planner_name "${PLANNER_ROLE_NAME}" --arg writer_policy_name "${WRITER_POLICY_NAME}" \
    --arg planner_policy_name "${PLANNER_POLICY_NAME}" --arg rule_name "${RULE_NAME}" \
    --arg target_id "${RULE_TARGET_ID}" --arg log_group "${LOG_GROUP_NAME}" \
    --arg log_policy_name "${LOG_RESOURCE_POLICY_NAME}" --arg lock_name "${APPLY_LOCK_NAME}" \
    --slurpfile current "${CURRENT}" --slurpfile audit_policy "${GENERATED_DIR}/audit-policy.json" \
    --slurpfile foreign "${GENERATED_DIR}/foreign-policy.json" \
    --slurpfile trust_policy "${GENERATED_DIR}/trust-policy.json" \
    --slurpfile writer_policy "${GENERATED_DIR}/writer-policy.json" \
    --slurpfile planner_policy "${GENERATED_DIR}/planner-policy.json" \
    --slurpfile selectors "${GENERATED_DIR}/event-selectors.json" \
    --slurpfile pattern "${GENERATED_DIR}/event-pattern.json" \
    --slurpfile log_policy "${GENERATED_DIR}/log-resource-policy.json" \
    --slurpfile audit_object_lock "${GENERATED_DIR}/audit-object-lock.json" \
    --slurpfile audit_tags "${GENERATED_DIR}/audit-tags.json" \
    --slurpfile writer_tags "${GENERATED_DIR}/writer-tags.json" \
    --slurpfile planner_tags "${GENERATED_DIR}/planner-tags.json" \
    --slurpfile trail_tags "${GENERATED_DIR}/trail-tags.json" \
    --slurpfile rule_tags "${GENERATED_DIR}/rule-tags.json" '
    def actions: (.Action // [] | if type=="array" then . else [.] end);
    def managed_rule: {ID:"AbortIncompleteCalibrationAuditUploads",Status:"Enabled",
      Filter:{Prefix:""},AbortIncompleteMultipartUpload:{DaysAfterInitiation:7}};
    ($current[0]) as $c |
    ($foreign[0] | map(.sha256)) as $foreignAcknowledgments |
    ($foreign[0] | map(select(.effect=="Deny" and (
      (.statement | has("NotAction") or has("NotResource") or has("NotPrincipal") or has("Condition")) or
      (.statement | actions | any(.=="*" or .=="s3:*" or .=="s3:GetBucketAcl" or .=="s3:PutObject"))))
      | .sha256)) as $blockingDenies |
    ([
      (if ($c.resources.apply_lock.exists // false) then "an SSM apply lock already exists" else empty end),
      (if ($c.resources.audit_bucket.exists and
          (($c.resources.audit_bucket.object_lock.ObjectLockConfiguration.ObjectLockEnabled // "") != "Enabled"))
        then "existing audit bucket was not created with Object Lock" else empty end),
      (if ($c.resources.audit_bucket.exists and
          (($c.resources.audit_bucket.object_lock.ObjectLockConfiguration.Rule.DefaultRetention.Mode // "COMPLIANCE") != "COMPLIANCE"))
        then "existing audit bucket default retention is not COMPLIANCE" else empty end),
      (if (($c.resources.audit_bucket.lifecycle.Rules // []) | length) > 0 and
          (($c.resources.audit_bucket.lifecycle.Rules // []) != [managed_rule])
        then "existing audit lifecycle is not the exact abort-only managed rule" else empty end),
      (if ($c.resources.writer_role.exists and
          (($c.resources.writer_role.inline_policy_names - [$writer_policy_name] | length) > 0 or
           ($c.resources.writer_role.attached_managed_policies | length) > 0))
        then "writer role has foreign inline or attached policies" else empty end),
      (if ($c.resources.writer_role.exists and (($c.resources.writer_role.role.Path // "") != "/"))
        then "writer role has a non-root IAM path" else empty end),
      (if ($c.resources.writer_role.exists and (($c.resources.writer_role.role.PermissionsBoundary // null) != null))
        then "writer role has a permissions boundary" else empty end),
      (if ($c.resources.planner_role.exists and
          (($c.resources.planner_role.inline_policy_names - [$planner_policy_name] | length) > 0 or
           ($c.resources.planner_role.attached_managed_policies | length) > 0))
        then "planner role has foreign inline or attached policies" else empty end),
      (if ($c.resources.planner_role.exists and (($c.resources.planner_role.role.Path // "") != "/"))
        then "planner role has a non-root IAM path" else empty end),
      (if ($c.resources.planner_role.exists and (($c.resources.planner_role.role.PermissionsBoundary // null) != null))
        then "planner role has a permissions boundary" else empty end),
      (if ($c.resources.trail.exists and (($c.resources.trail.trail.HomeRegion // $region) != $region))
        then "existing trail has a foreign home region" else empty end),
      (if (($c.resources.event_rule.targets // []) | map(select(.Id != $target_id)) | length) > 0
        then "EventBridge rule has foreign targets" else empty end),
      ($blockingDenies[] | "foreign audit-bucket Deny may block CloudTrail delivery: \(.)")
    ]) as $blockers |
    {schema:$schema,account:$account,region:$region,trust_principal_arn:$trust,
     fixed_names:{evidence_bucket:$evidence,audit_bucket:$audit,trail:$trail_name,
       writer_role:$writer_name,planner_role:$planner_name,event_rule:$rule_name,
       log_group:$log_group,apply_lock:$lock_name},
     apply_blockers:$blockers,
     required_foreign_policy_acknowledgments:$foreignAcknowledgments,
     foreign_audit_policy_statements:$foreign[0],
     resources:{
       audit_bucket:{name:$audit,create_with_object_lock:true,region:$region,
         versioning:{Status:"Enabled"},ownership:"BucketOwnerEnforced",
         encryption:{Rules:[{ApplyServerSideEncryptionByDefault:{SSEAlgorithm:"AES256"},BucketKeyEnabled:false}]},
         public_access:{BlockPublicAcls:true,IgnorePublicAcls:true,BlockPublicPolicy:true,RestrictPublicBuckets:true},
         object_lock:$audit_object_lock[0],
         policy:$audit_policy[0],tags:$audit_tags[0],lifecycle:{Rules:[managed_rule]}},
       writer_role:{name:$writer_name,arn:("arn:aws:iam::"+$account+":role/"+$writer_name),
         trust:$trust_policy[0],inline_policy_name:$writer_policy_name,
         inline_policy:$writer_policy[0],tags:$writer_tags[0],
         multipart_header_context:{policy_uses_if_exists_for_put_object:true,
           rationale:"multipart part/completion requests do not carry Object Lock headers",
           acceptance_requires_explicit_canary_headers_and_lock_readback:true}},
       planner_role:{name:$planner_name,arn:("arn:aws:iam::"+$account+":role/"+$planner_name),
         trust:$trust_policy[0],inline_policy_name:$planner_policy_name,
         inline_policy:$planner_policy[0],tags:$planner_tags[0]},
       trail:{name:$trail_name,arn:("arn:aws:cloudtrail:"+$region+":"+$account+":trail/"+$trail_name),
         home_region:$region,s3_bucket_name:$audit,include_global_service_events:true,
         is_multi_region_trail:false,enable_log_file_validation:true,
         event_selectors:$selectors[0],tags:$trail_tags[0],logging_required:true,
         latest_delivery_error_required:""},
       monitoring:{rule:{name:$rule_name,arn:("arn:aws:events:"+$region+":"+$account+":rule/"+$rule_name),
           state:"ENABLED",event_pattern:$pattern[0],tags:$rule_tags[0]},
         target:{id:$target_id,arn:("arn:aws:logs:"+$region+":"+$account+":log-group:"+$log_group)},
         log_group:{name:$log_group,arn:("arn:aws:logs:"+$region+":"+$account+":log-group:"+$log_group),
           retention_days:365,tags:{"managed-by":"v2x-backend","purpose":"calibration-evidence-audit","ue-runtime":"ue5-only"}},
         log_resource_policy:{name:$log_policy_name,document:$log_policy[0],
           scope:"exact-log-group",documented_delivery_principals:true,
           acceptance_requires_rule_fire_readback:true},
         integrity_monitoring:{durable_management_events_recorded_by_cloudtrail:true,
           covered_controls:["audit-and-evidence-buckets","cloudtrail","writer-and-planner-iam-roles",
             "eventbridge-rule-and-targets","cloudwatch-log-group-and-resource-policy","ssm-apply-lock"],
           self_monitoring_limitation:"The guarded EventBridge rule or its CloudWatch destination can be disabled before it delivers its own mutation event; CloudTrail audit-bucket history remains the durable record, and independent external notification is required before closeout."},
         external_human_notification:{required_before_closeout:true,status:"yellow-pending-product-decision"}}
     },
     next_gate:{script:"provision-calibration-evidence-store.sh",mode:"plan-only",
       evidence_bucket_mutation_in_this_transaction:false}}' >"${DESIRED}"

  jq -nS --arg account "${EXPECTED_ACCOUNT_ID}" --arg region "${AWS_REGION}" \
    --arg bucket "${EVIDENCE_BUCKET}" \
    --arg writer "arn:aws:iam::${EXPECTED_ACCOUNT_ID}:role/${WRITER_ROLE_NAME}" \
    --arg trail "${TRAIL_NAME}" --arg audit "${AUDIT_BUCKET}" '{
    schema:"v2x-calibration-evidence-canary/v1",account:$account,region:$region,
    ue_runtime:"ue5-only",evidence_bucket:$bucket,writer_role_arn:$writer,
    object_key_prefix:"canary/provisioning/",retention_mode:"COMPLIANCE",
    minimum_retention_days:90,require_explicit_object_lock_headers:true,
    readback:["content-sha256","version-id","object-lock-mode","retain-until","object-tags"],
    audit:{trail_name:$trail,audit_bucket:$audit,
      exact_data_resource_prefix:("arn:aws:s3:::"+$bucket+"/"),
      require_exact_writer_session_put_object:true,require_digest_validation:true,
      require_eventbridge_rule_fire_log_readback:true,
      verifier_principal:{must_be_explicit_before_canary:true,not_provisioned_by_prerequisite_gate:true,
        required_permissions:[
          {actions:["s3:GetBucketLocation","s3:ListBucket"],resource:("arn:aws:s3:::"+$audit),
           prefix_condition:("AWSLogs/"+$account+"/*")},
          {actions:["s3:GetObject","s3:GetObjectVersion","s3:GetObjectAttributes"],
           resource:("arn:aws:s3:::"+$audit+"/AWSLogs/"+$account+"/*")}] }},
    bounded_lookup:{max_minutes:30,poll_seconds:30},
    mutates_evidence_store:true,requires_separate_locked_reviewed_gate:true}' >"${CANARY_INTERFACE}"
}

emit_plan_artifacts() {
  if [[ -z "${PLAN_OUTPUT_DIR}" ]]; then
    return 0
  fi
  install -d -m 0700 "${PLAN_OUTPUT_DIR}"
  install -m 0600 "${CURRENT}" "${PLAN_OUTPUT_DIR}/current.json"
  install -m 0600 "${DESIRED}" "${PLAN_OUTPUT_DIR}/desired.json"
  install -m 0600 "${CANARY_INTERFACE}" "${PLAN_OUTPUT_DIR}/later-canary-interface.json"
  printf '%s  %s\n' \
    "$(sha256sum "${CURRENT}" | awk '{print $1}')" current.json \
    "$(sha256sum "${DESIRED}" | awk '{print $1}')" desired.json \
    "$(sha256sum "${CANARY_INTERFACE}" | awk '{print $1}')" later-canary-interface.json \
    >"${PLAN_OUTPUT_DIR}/SHA256SUMS"
  chmod 0600 "${PLAN_OUTPUT_DIR}/SHA256SUMS"
}

validate_foreign_policy_acknowledgments() {
  local required provided
  required="$(jq -c '.required_foreign_policy_acknowledgments | sort' "${DESIRED}")"
  provided="$(jq -nRc --arg csv "${ACKNOWLEDGED_FOREIGN_POLICY_SHA256S}" '
    if ($csv|length)==0 then []
    else ($csv | split(",") | map(gsub("^[[:space:]]+|[[:space:]]+$";"")) | sort) end')"
  if [[ "${required}" != "${provided}" ]]; then
    echo "ACKNOWLEDGED_FOREIGN_POLICY_SHA256S must exactly equal the reviewed foreign statement SHA-256 set" >&2
    echo "Required canonical acknowledgments: ${required}" >&2
    return 1
  fi
}

refresh_rollback_checksums() {
  local checksum_tmp="${WORKDIR}/rollback-SHA256SUMS"
  (
    cd "${ROLLBACK_BUNDLE}"
    find . -type f ! -name SHA256SUMS -print0 \
      | sort -z | xargs -0 sha256sum >"${checksum_tmp}"
  )
  install -m 0600 "${checksum_tmp}" "${ROLLBACK_BUNDLE}/SHA256SUMS"
}

create_rollback_bundle() {
  local current_hash="$1" desired_hash="$2"
  ROLLBACK_BUNDLE="${BACKUP_ROOT}/$(date -u +%Y%m%dT%H%M%SZ)-${current_hash}"
  export ROLLBACK_BUNDLE
  install -d -m 0700 "${ROLLBACK_BUNDLE}" "${ROLLBACK_BUNDLE}/discovery"
  install -m 0600 "${CURRENT}" "${ROLLBACK_BUNDLE}/current.json"
  install -m 0600 "${DESIRED}" "${ROLLBACK_BUNDLE}/desired.json"
  install -m 0600 "${CANARY_INTERFACE}" "${ROLLBACK_BUNDLE}/later-canary-interface.json"
  local file
  for file in "${DISCOVERY_DIR}"/*.json; do
    [[ -f "${file}" ]] || continue
    install -m 0600 "${file}" "${ROLLBACK_BUNDLE}/discovery/$(basename "${file}")"
  done
  jq -nS --arg current_hash "${current_hash}" --arg desired_hash "${desired_hash}" \
    --arg account "${EXPECTED_ACCOUNT_ID}" --arg region "${AWS_REGION}" \
    --arg audit "${AUDIT_BUCKET}" --arg writer "${WRITER_ROLE_NAME}" \
    --arg planner "${PLANNER_ROLE_NAME}" --arg trail "${TRAIL_NAME}" \
    --arg rule "${RULE_NAME}" --arg log "${LOG_GROUP_NAME}" '{
    schema:"v2x-calibration-evidence-prerequisites-rollback/v1",
    current_state_sha256:$current_hash,desired_state_sha256:$desired_hash,
    account:$account,region:$region,
    restore_order:[
      {resource:"eventbridge-rule-and-target",name:$rule,source:"current.json.resources.event_rule"},
      {resource:"cloudwatch-log-group-and-policy",name:$log,
       source:["current.json.resources.log_group","current.json.resources.log_resource_policy"]},
      {resource:"cloudtrail-and-selector",name:$trail,source:"current.json.resources.trail"},
      {resource:"planner-role",name:$planner,source:"current.json.resources.planner_role"},
      {resource:"writer-role",name:$writer,source:"current.json.resources.writer_role"},
      {resource:"audit-bucket",name:$audit,source:"current.json.resources.audit_bucket"}],
    safety:{destructive_rollback_forbidden:true,buckets_retained_by_default:true,
      compliance_objects_cannot_be_deleted_before_retention:true,
      stale_ssm_lock_requires_separate_reviewed_clear:true},
    evidence_store_was_not_mutated_by_this_transaction:true}' >"${ROLLBACK_BUNDLE}/rollback-manifest.json"
  chmod 0600 "${ROLLBACK_BUNDLE}/rollback-manifest.json"
  refresh_rollback_checksums
}

claim_apply_lock() {
  local current_hash="$1" desired_hash="$2"
  LOCK_TOKEN="$(jq -ncS --arg current "${current_hash}" --arg desired "${desired_hash}" \
    --arg caller "$(jq -r '.caller_arn' "${CURRENT}")" --arg created "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg pid "$$" '{schema:"v2x-calibration-evidence-prerequisites-lock/v1",
      current_state_sha256:$current,desired_state_sha256:$desired,caller_arn:$caller,
      created_at:$created,process_id:$pid}')"
  local error="${WORKDIR}/lock-claim.err"
  if ! aws_cli ssm put-parameter --name "${APPLY_LOCK_NAME}" --type String \
      --value "${LOCK_TOKEN}" --no-overwrite >"${WORKDIR}/lock-claim.json" 2>"${error}"; then
    cat "${error}" >&2
    echo "Conditional SSM apply-lock claim failed; no infrastructure mutation was attempted" >&2
    return 1
  fi
  rm -f "${error}"
  LOCK_CLAIMED=true
  LOCK_RELEASE_STATE="owned"
}

verify_preapply_state_unchanged() {
  local expected_hash="$1" owned_value postlock_hash
  if ! owned_value="$(aws_cli ssm get-parameter --name "${APPLY_LOCK_NAME}" \
      --query 'Parameter.Value' --output text 2>/dev/null)" || [[ "${owned_value}" != "${LOCK_TOKEN}" ]]; then
    echo "Owned SSM apply lock could not be read back exactly; refusing all resource mutation" >&2
    return 1
  fi
  rm -rf "${DISCOVERY_DIR}"
  mkdir -p "${DISCOVERY_DIR}"
  discover_all
  jq -S --slurpfile prelock "${PRELOCK_CURRENT}" \
    '.resources.apply_lock = $prelock[0].resources.apply_lock' "${CURRENT}" \
    >"${WORKDIR}/current-postlock-normalized.json"
  postlock_hash="$(sha256sum "${WORKDIR}/current-postlock-normalized.json" | awk '{print $1}')"
  install -m 0600 "${PRELOCK_CURRENT}" "${CURRENT}"
  if [[ "${postlock_hash}" != "${expected_hash}" ]]; then
    echo "AWS state changed after planning and before the first resource mutation" >&2
    echo "Expected pre-lock current state hash: ${expected_hash}" >&2
    echo "Observed normalized post-lock state hash: ${postlock_hash}" >&2
    return 1
  fi
}

apply_audit_bucket() {
  if [[ "$(jq -r '.resources.audit_bucket.exists' "${CURRENT}")" == "false" ]]; then
    aws_cli s3api create-bucket --bucket "${AUDIT_BUCKET}" --object-lock-enabled-for-bucket \
      --object-ownership BucketOwnerEnforced \
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
  fi
  aws_s3api put-public-access-block --bucket "${AUDIT_BUCKET}" \
    --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true >/dev/null
  aws_s3api put-bucket-ownership-controls --bucket "${AUDIT_BUCKET}" \
    --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]' >/dev/null
  aws_s3api put-bucket-encryption --bucket "${AUDIT_BUCKET}" \
    --server-side-encryption-configuration \
      '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":false}]}' >/dev/null
  aws_s3api put-bucket-versioning --bucket "${AUDIT_BUCKET}" --versioning-configuration Status=Enabled >/dev/null
  aws_s3api put-object-lock-configuration --bucket "${AUDIT_BUCKET}" \
    --object-lock-configuration "file://${GENERATED_DIR}/audit-object-lock.json" >/dev/null
  aws_s3api put-bucket-policy --bucket "${AUDIT_BUCKET}" \
    --policy "file://${GENERATED_DIR}/audit-policy.json" >/dev/null
  jq -nS --slurpfile tags "${GENERATED_DIR}/audit-tags.json" '{TagSet:$tags[0]}' \
    >"${GENERATED_DIR}/audit-tagging.json"
  aws_s3api put-bucket-tagging --bucket "${AUDIT_BUCKET}" \
    --tagging "file://${GENERATED_DIR}/audit-tagging.json" >/dev/null
  jq -nS '{Rules:[{ID:"AbortIncompleteCalibrationAuditUploads",Status:"Enabled",
    Filter:{Prefix:""},AbortIncompleteMultipartUpload:{DaysAfterInitiation:7}}]}' \
    >"${GENERATED_DIR}/audit-lifecycle.json"
  aws_s3api put-bucket-lifecycle-configuration --bucket "${AUDIT_BUCKET}" \
    --lifecycle-configuration "file://${GENERATED_DIR}/audit-lifecycle.json" >/dev/null
}

apply_role() {
  local role_name="$1" policy_name="$2" current_key="$3" tags_file="$4" policy_file="$5"
  if [[ "$(jq -r ".resources.${current_key}.exists" "${CURRENT}")" == "false" ]]; then
    aws_cli iam create-role --role-name "${role_name}" \
      --description "V2X UE5 calibration evidence deployment-as-code role" \
      --assume-role-policy-document "file://${GENERATED_DIR}/trust-policy.json" \
      --tags "file://${tags_file}" >/dev/null
  else
    aws_cli iam update-assume-role-policy --role-name "${role_name}" \
      --policy-document "file://${GENERATED_DIR}/trust-policy.json" >/dev/null
    aws_cli iam tag-role --role-name "${role_name}" --tags "file://${tags_file}" >/dev/null
  fi
  aws_cli iam put-role-policy --role-name "${role_name}" --policy-name "${policy_name}" \
    --policy-document "file://${policy_file}" >/dev/null
}

apply_monitoring() {
  local log_tags
  log_tags='{"managed-by":"v2x-backend","purpose":"calibration-evidence-audit","ue-runtime":"ue5-only"}'
  if [[ "$(jq -r '.resources.log_group.exists' "${CURRENT}")" == "false" ]]; then
    aws_cli logs create-log-group --log-group-name "${LOG_GROUP_NAME}" --tags "${log_tags}" >/dev/null
  else
    aws_cli logs tag-resource --resource-arn \
      "arn:aws:logs:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:log-group:${LOG_GROUP_NAME}" \
      --tags "${log_tags}" >/dev/null
  fi
  aws_cli logs put-retention-policy --log-group-name "${LOG_GROUP_NAME}" --retention-in-days 365 >/dev/null
  aws_cli logs put-resource-policy --policy-name "${LOG_RESOURCE_POLICY_NAME}" \
    --policy-document "$(jq -c . "${GENERATED_DIR}/log-resource-policy.json")" >/dev/null

  aws_cli events put-rule --name "${RULE_NAME}" \
    --description "Detect mutation of calibration evidence integrity controls" \
    --event-pattern "file://${GENERATED_DIR}/event-pattern.json" --state ENABLED >/dev/null
  aws_cli events tag-resource --resource-arn \
    "arn:aws:events:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:rule/${RULE_NAME}" \
    --tags "file://${GENERATED_DIR}/rule-tags.json" >/dev/null
  jq -nS --arg id "${RULE_TARGET_ID}" \
    --arg arn "arn:aws:logs:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:log-group:${LOG_GROUP_NAME}" \
    '[{Id:$id,Arn:$arn}]' >"${GENERATED_DIR}/rule-targets.json"
  aws_cli events put-targets --rule "${RULE_NAME}" --targets "file://${GENERATED_DIR}/rule-targets.json" \
    >"${GENERATED_DIR}/put-targets-result.json"
  if [[ "$(jq -r '.FailedEntryCount // 0' "${GENERATED_DIR}/put-targets-result.json")" != "0" ]]; then
    echo "EventBridge target reconciliation failed" >&2
    jq . "${GENERATED_DIR}/put-targets-result.json" >&2
    return 1
  fi
}

apply_trail() {
  if [[ "$(jq -r '.resources.trail.exists' "${CURRENT}")" == "false" ]]; then
    aws_cli cloudtrail create-trail --name "${TRAIL_NAME}" --s3-bucket-name "${AUDIT_BUCKET}" \
      --include-global-service-events --no-is-multi-region-trail --enable-log-file-validation >/dev/null
  else
    aws_cli cloudtrail update-trail --name "${TRAIL_NAME}" --s3-bucket-name "${AUDIT_BUCKET}" \
      --include-global-service-events --no-is-multi-region-trail --enable-log-file-validation >/dev/null
  fi
  aws_cli cloudtrail add-tags --resource-id \
    "arn:aws:cloudtrail:${AWS_REGION}:${EXPECTED_ACCOUNT_ID}:trail/${TRAIL_NAME}" \
    --tags-list "file://${GENERATED_DIR}/trail-tags.json" >/dev/null
  aws_cli cloudtrail put-event-selectors --trail-name "${TRAIL_NAME}" \
    --event-selectors "file://${GENERATED_DIR}/event-selectors.json" >/dev/null
  aws_cli cloudtrail start-logging --name "${TRAIL_NAME}" >/dev/null
}

verify_actual_state() {
  local actual="${WORKDIR}/current-after.json"
  rm -rf "${DISCOVERY_DIR}"
  mkdir -p "${DISCOVERY_DIR}"
  discover_all
  install -m 0600 "${CURRENT}" "${actual}"
  if [[ -n "${ROLLBACK_BUNDLE:-}" && -d "${ROLLBACK_BUNDLE}" ]]; then
    install -m 0600 "${actual}" "${ROLLBACK_BUNDLE}/current-after-last-readback.json"
    refresh_rollback_checksums
  fi
  jq -ne --slurpfile actual "${actual}" --slurpfile desired "${DESIRED}" '
    ($actual[0]) as $a | ($desired[0]) as $d |
    ($a.resources.audit_bucket.exists == true) and
    ($a.resources.audit_bucket.versioning.Status == "Enabled") and
    ($a.resources.audit_bucket.public_access.PublicAccessBlockConfiguration == $d.resources.audit_bucket.public_access) and
    ($a.resources.audit_bucket.encryption.ServerSideEncryptionConfiguration == $d.resources.audit_bucket.encryption) and
    ($a.resources.audit_bucket.ownership.OwnershipControls.Rules[0].ObjectOwnership == "BucketOwnerEnforced") and
    ($a.resources.audit_bucket.object_lock.ObjectLockConfiguration == $d.resources.audit_bucket.object_lock) and
    (($a.resources.audit_bucket.policy.Policy | fromjson) == $d.resources.audit_bucket.policy) and
    ($a.resources.audit_bucket.tags == $d.resources.audit_bucket.tags) and
    ($a.resources.audit_bucket.lifecycle.Rules == $d.resources.audit_bucket.lifecycle.Rules) and
    ($a.resources.writer_role.exists == true) and
    ($a.resources.writer_role.role.Arn == $d.resources.writer_role.arn) and
    ($a.resources.writer_role.role.Path == "/") and
    (($a.resources.writer_role.role.PermissionsBoundary // null) == null) and
    ($a.resources.writer_role.role.AssumeRolePolicyDocument == $d.resources.writer_role.trust) and
    ($a.resources.writer_role.managed_inline_policy.PolicyDocument == $d.resources.writer_role.inline_policy) and
    ($a.resources.writer_role.tags == $d.resources.writer_role.tags) and
    ($a.resources.planner_role.exists == true) and
    ($a.resources.planner_role.role.Arn == $d.resources.planner_role.arn) and
    ($a.resources.planner_role.role.Path == "/") and
    (($a.resources.planner_role.role.PermissionsBoundary // null) == null) and
    ($a.resources.planner_role.role.AssumeRolePolicyDocument == $d.resources.planner_role.trust) and
    ($a.resources.planner_role.managed_inline_policy.PolicyDocument == $d.resources.planner_role.inline_policy) and
    ($a.resources.planner_role.tags == $d.resources.planner_role.tags) and
    ($a.resources.trail.exists == true) and
    ($a.resources.trail.trail.HomeRegion == $d.resources.trail.home_region) and
    ($a.resources.trail.trail.S3BucketName == $d.resources.trail.s3_bucket_name) and
    ($a.resources.trail.trail.IncludeGlobalServiceEvents == true) and
    ($a.resources.trail.trail.IsMultiRegionTrail == false) and
    ($a.resources.trail.trail.LogFileValidationEnabled == true) and
    ($a.resources.trail.status.IsLogging == true) and
    (($a.resources.trail.status.LatestDeliveryError // "") == "") and
    (($a.resources.trail.selectors.EventSelectors // []) == $d.resources.trail.event_selectors) and
    ($a.resources.trail.tags == $d.resources.trail.tags) and
    ($a.resources.event_rule.exists == true) and
    ($a.resources.event_rule.rule.State == "ENABLED") and
    (($a.resources.event_rule.rule.EventPattern | fromjson) == $d.resources.monitoring.rule.event_pattern) and
    ($a.resources.event_rule.tags == $d.resources.monitoring.rule.tags) and
    (($a.resources.event_rule.targets | length) == 1) and
    ($a.resources.event_rule.targets[0].Id == $d.resources.monitoring.target.id) and
    ($a.resources.event_rule.targets[0].Arn == $d.resources.monitoring.target.arn) and
    ($a.resources.log_group.exists == true) and
    ($a.resources.log_group.log_group.retentionInDays == 365) and
    (($a.resources.log_group.tags | from_entries) == $d.resources.monitoring.log_group.tags) and
    ($a.resources.log_resource_policy.exists == true) and
    (($a.resources.log_resource_policy.policy.policyDocument | fromjson) == $d.resources.monitoring.log_resource_policy.document)
  ' >/dev/null
}

bounded_readback() {
  local attempt
  for ((attempt=1; attempt<=VERIFY_ATTEMPTS; attempt++)); do
    if verify_actual_state; then
      return 0
    fi
    if (( attempt < VERIFY_ATTEMPTS )); then
      sleep "${VERIFY_DELAY_SECONDS}"
    fi
  done
  echo "Bounded readback did not converge after ${VERIFY_ATTEMPTS} attempts" >&2
  echo "Rollback bundle: ${ROLLBACK_BUNDLE}" >&2
  return 1
}

discover_all
generate_desired
CURRENT_STATE_HASH="$(sha256sum "${CURRENT}" | awk '{print $1}')"
DESIRED_STATE_HASH="$(sha256sum "${DESIRED}" | awk '{print $1}')"
CANARY_INTERFACE_HASH="$(sha256sum "${CANARY_INTERFACE}" | awk '{print $1}')"
emit_plan_artifacts

echo "Account: ${EXPECTED_ACCOUNT_ID}"
echo "Region: ${AWS_REGION}"
echo "Trust principal: ${TRUST_PRINCIPAL_ARN}"
echo "Current state hash: ${CURRENT_STATE_HASH}"
echo "Desired state hash: ${DESIRED_STATE_HASH}"
echo "Later canary interface hash: ${CANARY_INTERFACE_HASH}"
echo "Plan only: ${PLAN_ONLY}"
echo "Current state:"
jq . "${CURRENT}"
echo "Desired state:"
jq . "${DESIRED}"
echo "Later locked canary interface:"
jq . "${CANARY_INTERFACE}"

if [[ "${PLAN_ONLY}" == "true" ]]; then
  echo "No AWS state changed."
  exit 0
fi

if [[ "${EXPECTED_CURRENT_STATE_HASH}" != "${CURRENT_STATE_HASH}" ]]; then
  echo "EXPECTED_CURRENT_STATE_HASH does not match current state" >&2
  exit 5
fi
if [[ "${EXPECTED_DESIRED_STATE_HASH}" != "${DESIRED_STATE_HASH}" ]]; then
  echo "EXPECTED_DESIRED_STATE_HASH does not match desired state" >&2
  exit 5
fi
if [[ "${TRUST_PRINCIPAL_ARN_CONFIRM}" != "${TRUST_PRINCIPAL_ARN}" ]]; then
  echo "TRUST_PRINCIPAL_ARN_CONFIRM must repeat the exact reviewed trust principal" >&2
  exit 5
fi
if [[ "${CONFIRM_PREREQUISITES}" != "CONFIGURE_CALIBRATION_EVIDENCE_PREREQUISITES" ]]; then
  echo "Set CONFIRM_PREREQUISITES=CONFIGURE_CALIBRATION_EVIDENCE_PREREQUISITES" >&2
  exit 5
fi
if [[ "${CONFIRM_COMPLIANCE_AUDIT}" != "CREATE_COMPLIANCE_LOCKED_CALIBRATION_AUDIT_LOG" ]]; then
  echo "Set CONFIRM_COMPLIANCE_AUDIT=CREATE_COMPLIANCE_LOCKED_CALIBRATION_AUDIT_LOG" >&2
  exit 5
fi
if [[ "$(jq -r '.apply_blockers | length' "${DESIRED}")" != "0" ]]; then
  echo "Apply blockers are present:" >&2
  jq -r '.apply_blockers[] | "- \(.)"' "${DESIRED}" >&2
  exit 5
fi
validate_foreign_policy_acknowledgments || exit 5

create_rollback_bundle "${CURRENT_STATE_HASH}" "${DESIRED_STATE_HASH}"
install -m 0600 "${CURRENT}" "${PRELOCK_CURRENT}"
claim_apply_lock "${CURRENT_STATE_HASH}" "${DESIRED_STATE_HASH}"
verify_preapply_state_unchanged "${CURRENT_STATE_HASH}"
apply_audit_bucket
apply_role "${WRITER_ROLE_NAME}" "${WRITER_POLICY_NAME}" writer_role \
  "${GENERATED_DIR}/writer-tags.json" "${GENERATED_DIR}/writer-policy.json"
apply_role "${PLANNER_ROLE_NAME}" "${PLANNER_POLICY_NAME}" planner_role \
  "${GENERATED_DIR}/planner-tags.json" "${GENERATED_DIR}/planner-policy.json"
apply_monitoring
apply_trail
bounded_readback
release_lock

echo "Calibration evidence AWS prerequisites verified."
echo "Rollback bundle: ${ROLLBACK_BUNDLE}"
echo "The evidence bucket, canary, holdouts, live services, and UE6 were not changed."
echo "Next executable gate: run provision-calibration-evidence-store.sh in plan mode through ${PLANNER_ROLE_NAME}."
