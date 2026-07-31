#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT="${ROOT}/infra/aws-cli/provision-calibration-evidence-prerequisites.sh"
TRUST="arn:aws:iam::147229569658:user/rfs-v2x-service"
TMP="$(mktemp -d)"
trap 'if [[ "${KEEP_TEST_TMP:-false}" == "true" ]]; then echo "Preserved test tmp: ${TMP}" >&2; else rm -rf "${TMP}"; fi' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
assert_eq() { [[ "$1" == "$2" ]] || fail "expected '$1' to equal '$2': $3"; }

bash -n "${SCRIPT}" "${BASH_SOURCE[0]}"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck "${SCRIPT}" "${BASH_SOURCE[0]}"
fi

MOCK_BIN="${TMP}/bin"
mkdir -p "${MOCK_BIN}"

cat >"${MOCK_BIN}/aws" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail

STATE="${MOCK_AWS_STATE:?}"
LOG="${MOCK_AWS_LOG:?}"
SCENARIO="${MOCK_AWS_SCENARIO:-absent}"
mkdir -p "${STATE}"

if [[ "${1:-}" == "--region" ]]; then shift 2; fi
service="${1:?}" operation="${2:?}"
shift 2
printf '%s %s %q\n' "${service}" "${operation}" "$*" >>"${LOG}"

arg() {
  local key="$1" previous="" value
  shift
  for value in "$@"; do
    if [[ "${previous}" == "${key}" ]]; then printf '%s' "${value}"; return 0; fi
    previous="${value}"
  done
  return 1
}
file_arg() { local value; value="$(arg "$1" "${@:2}")"; printf '%s' "${value#file://}"; }
error() { echo "An error occurred ($1) when calling the ${operation} operation: mocked" >&2; exit 254; }
mutate() { printf 'MUTATE %s %s\n' "${service}" "${operation}" >>"${LOG}"; }

ACCOUNT=147229569658
REGION=us-west-1
EVIDENCE="v2x-calibration-evidence-${ACCOUNT}-${REGION}"
AUDIT="v2x-calibration-audit-${ACCOUNT}-${REGION}"
TRAIL=v2x-calibration-evidence-audit
RULE=v2x-calibration-evidence-audit-guard
LOG_GROUP=/aws/events/v2x-calibration-evidence-audit

case "${service}:${operation}" in
  sts:get-caller-identity)
    marker="${MOCK_DRIFT_MARKER:-}"
    jq -n --arg account "${ACCOUNT}" --arg marker "${marker}" \
      '{Account:$account,Arn:("arn:aws:iam::"+$account+":user/mock-planner"+$marker),UserId:("mock"+$marker)}'
    ;;

  s3api:head-bucket)
    bucket="$(arg --bucket "$@")"
    if [[ "${SCENARIO}" == access_denied ]]; then error AccessDenied; fi
    if [[ -f "${STATE}/bucket-${bucket}.exists" ]] ||
       [[ "${SCENARIO}" =~ ^(foreign_allow|foreign_deny_unsafe|no_object_lock|wrong_lifecycle|stronger_lock|governance_lock)$ && "${bucket}" == "${AUDIT}" ]]; then exit 0; fi
    error 404
    ;;
  s3api:create-bucket)
    mutate; bucket="$(arg --bucket "$@")"; : >"${STATE}/bucket-${bucket}.exists"; echo '{}'
    ;;
  s3api:get-bucket-location)
    echo '{"LocationConstraint":"us-west-1"}'
    ;;
  s3api:get-bucket-versioning)
    echo '{"Status":"Enabled"}'
    ;;
  s3api:get-bucket-logging)
    echo '{}'
    ;;
  s3api:get-bucket-acl)
    echo '{"Owner":{"ID":"mock-owner"},"Grants":[]}'
    ;;
  s3api:get-public-access-block)
    echo '{"PublicAccessBlockConfiguration":{"BlockPublicAcls":true,"IgnorePublicAcls":true,"BlockPublicPolicy":true,"RestrictPublicBuckets":true}}'
    ;;
  s3api:get-bucket-encryption)
    echo '{"ServerSideEncryptionConfiguration":{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":false}]}}'
    ;;
  s3api:get-bucket-ownership-controls)
    echo '{"OwnershipControls":{"Rules":[{"ObjectOwnership":"BucketOwnerEnforced"}]}}'
    ;;
  s3api:get-object-lock-configuration)
    if [[ "${SCENARIO}" == no_object_lock ]]; then error ObjectLockConfigurationNotFoundError; fi
    if [[ "${SCENARIO}" == stronger_lock ]]; then
      echo '{"ObjectLockConfiguration":{"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"COMPLIANCE","Days":730}}}}'
      exit 0
    fi
    if [[ "${SCENARIO}" == governance_lock ]]; then
      echo '{"ObjectLockConfiguration":{"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"GOVERNANCE","Days":730}}}}'
      exit 0
    fi
    echo '{"ObjectLockConfiguration":{"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"COMPLIANCE","Days":365}}}}'
    ;;
  s3api:get-bucket-policy)
    bucket="$(arg --bucket "$@")"
    if [[ -f "${STATE}/bucket-${bucket}-policy.json" ]]; then
      jq -Rs '{Policy:.}' <"${STATE}/bucket-${bucket}-policy.json"
    elif [[ "${SCENARIO}" == foreign_allow && "${bucket}" == "${AUDIT}" ]]; then
      jq -nc --arg account "${ACCOUNT}" '{Policy:({Version:"2012-10-17",Statement:[{
        Sid:"ForeignReadGrant",Effect:"Allow",Principal:{AWS:("arn:aws:iam::"+$account+":role/ForeignReader")},
        Action:"s3:GetObject",Resource:("arn:aws:s3:::v2x-calibration-audit-"+$account+"-us-west-1/foreign/*")
      }]} | tojson)}'
    elif [[ "${SCENARIO}" == foreign_deny_unsafe && "${bucket}" == "${AUDIT}" ]]; then
      jq -nc '{Policy:({Version:"2012-10-17",Statement:[{
        Sid:"ForeignNotActionDeny",Effect:"Deny",Principal:"*",NotAction:"s3:GetObject",
        Resource:"arn:aws:s3:::v2x-calibration-audit-147229569658-us-west-1/*"
      }]} | tojson)}'
    else error NoSuchBucketPolicy; fi
    ;;
  s3api:get-bucket-tagging)
    bucket="$(arg --bucket "$@")"
    [[ -f "${STATE}/bucket-${bucket}-tags.json" ]] || error NoSuchTagSet
    cat "${STATE}/bucket-${bucket}-tags.json"
    ;;
  s3api:get-bucket-lifecycle-configuration)
    bucket="$(arg --bucket "$@")"
    if [[ -f "${STATE}/bucket-${bucket}-lifecycle.json" ]]; then
      cat "${STATE}/bucket-${bucket}-lifecycle.json"
    elif [[ "${SCENARIO}" == wrong_lifecycle && "${bucket}" == "${AUDIT}" ]]; then
      echo '{"Rules":[{"ID":"DeleteAuditLogs","Status":"Enabled","Filter":{"Prefix":""},"Expiration":{"Days":1}}]}'
    elif [[ "${SCENARIO}" =~ ^(foreign_allow|foreign_deny_unsafe|no_object_lock|stronger_lock|governance_lock)$ && "${bucket}" == "${AUDIT}" ]]; then
      echo '{"Rules":[{"ID":"AbortIncompleteCalibrationAuditUploads","Status":"Enabled","Filter":{"Prefix":""},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}}]}'
    else error NoSuchLifecycleConfiguration; fi
    ;;
  s3api:get-bucket-replication)
    error ReplicationConfigurationNotFoundError
    ;;
  s3api:put-bucket-policy)
    mutate; bucket="$(arg --bucket "$@")"; cp "$(file_arg --policy "$@")" "${STATE}/bucket-${bucket}-policy.json"; echo '{}'
    ;;
  s3api:put-bucket-tagging)
    mutate; bucket="$(arg --bucket "$@")"; cp "$(file_arg --tagging "$@")" "${STATE}/bucket-${bucket}-tags.json"; echo '{}'
    ;;
  s3api:put-bucket-lifecycle-configuration)
    mutate; bucket="$(arg --bucket "$@")"; cp "$(file_arg --lifecycle-configuration "$@")" "${STATE}/bucket-${bucket}-lifecycle.json"; echo '{}'
    ;;
  s3api:put-public-access-block|s3api:put-bucket-ownership-controls|s3api:put-bucket-encryption|s3api:put-bucket-versioning|s3api:put-object-lock-configuration)
    mutate; echo '{}'
    ;;

  iam:get-role)
    role="$(arg --role-name "$@")"
    if [[ ! -f "${STATE}/role-${role}-trust.json" && "${SCENARIO}" =~ ^(role_attached|role_bad_path|role_boundary)$ && "${role}" == *Writer ]]; then
      jq -n '{Version:"2012-10-17",Statement:[{Effect:"Deny",Principal:"*",Action:"sts:AssumeRole"}]}' \
        >"${STATE}/role-${role}-trust.json"
    fi
    [[ -f "${STATE}/role-${role}-trust.json" ]] || error NoSuchEntity
    role_path=/
    boundary=null
    if [[ "${SCENARIO}" == role_bad_path && "${role}" == *Writer ]]; then role_path=/service-role/; fi
    if [[ "${SCENARIO}" == role_boundary && "${role}" == *Writer ]]; then
      boundary='{"PermissionsBoundaryType":"Policy","PermissionsBoundaryArn":"arn:aws:iam::147229569658:policy/Restricted"}'
    fi
    jq -n --arg role "${role}" --arg account "${ACCOUNT}" --arg path "${role_path}" \
      --argjson boundary "${boundary}" \
      --slurpfile trust "${STATE}/role-${role}-trust.json" \
      '{Role:{Path:$path,RoleName:$role,Arn:("arn:aws:iam::"+$account+":role"+$path+$role),
        Description:"V2X UE5 calibration evidence deployment-as-code role",MaxSessionDuration:3600,
        AssumeRolePolicyDocument:$trust[0],PermissionsBoundary:$boundary}}'
    ;;
  iam:list-role-policies)
    role="$(arg --role-name "$@")"
    if [[ -f "${STATE}/role-${role}-policy.json" ]]; then
      if [[ "${role}" == *Writer ]]; then name=V2XCalibrationEvidenceWriterPolicy; else name=V2XCalibrationEvidencePlannerPolicy; fi
      jq -n --arg name "${name}" '{PolicyNames:[$name]}'
    else echo '{"PolicyNames":[]}' ; fi
    ;;
  iam:list-attached-role-policies)
    role="$(arg --role-name "$@")"
    if [[ "${SCENARIO}" == role_attached && "${role}" == *Writer ]]; then
      echo '{"AttachedPolicies":[{"PolicyName":"AdministratorAccess","PolicyArn":"arn:aws:iam::aws:policy/AdministratorAccess"}]}'
    else echo '{"AttachedPolicies":[]}' ; fi
    ;;
  iam:list-role-tags)
    role="$(arg --role-name "$@")"
    if [[ -f "${STATE}/role-${role}-tags.json" ]]; then jq -n --slurpfile tags "${STATE}/role-${role}-tags.json" '{Tags:$tags[0]}';
    else echo '{"Tags":[]}' ; fi
    ;;
  iam:get-role-policy)
    role="$(arg --role-name "$@")"; policy="$(arg --policy-name "$@")"
    [[ -f "${STATE}/role-${role}-policy.json" ]] || error NoSuchEntity
    jq -n --arg role "${role}" --arg policy "${policy}" --slurpfile doc "${STATE}/role-${role}-policy.json" \
      '{RoleName:$role,PolicyName:$policy,PolicyDocument:$doc[0]}'
    ;;
  iam:create-role)
    mutate; role="$(arg --role-name "$@")"
    cp "$(file_arg --assume-role-policy-document "$@")" "${STATE}/role-${role}-trust.json"
    cp "$(file_arg --tags "$@")" "${STATE}/role-${role}-tags.json"
    echo '{}'
    ;;
  iam:update-assume-role-policy)
    mutate; role="$(arg --role-name "$@")"; cp "$(file_arg --policy-document "$@")" "${STATE}/role-${role}-trust.json"; echo '{}'
    ;;
  iam:tag-role)
    mutate; role="$(arg --role-name "$@")"; cp "$(file_arg --tags "$@")" "${STATE}/role-${role}-tags.json"; echo '{}'
    ;;
  iam:put-role-policy)
    mutate; role="$(arg --role-name "$@")"; cp "$(file_arg --policy-document "$@")" "${STATE}/role-${role}-policy.json"; echo '{}'
    ;;

  cloudtrail:get-trail)
    [[ -f "${STATE}/trail.exists" ]] || error TrailNotFoundException
    jq -n --arg account "${ACCOUNT}" --arg region "${REGION}" --arg audit "${AUDIT}" --arg trail "${TRAIL}" '{Trail:{
      Name:$trail,S3BucketName:$audit,IncludeGlobalServiceEvents:true,IsMultiRegionTrail:false,
      HomeRegion:$region,TrailARN:("arn:aws:cloudtrail:"+$region+":"+$account+":trail/"+$trail),
      LogFileValidationEnabled:true,HasCustomEventSelectors:true,HasInsightSelectors:false,IsOrganizationTrail:false}}'
    ;;
  cloudtrail:get-trail-status)
    if [[ -f "${STATE}/trail.logging" ]]; then echo '{"IsLogging":true,"LatestDeliveryError":"","LatestNotificationError":""}';
    else echo '{"IsLogging":false,"LatestDeliveryError":"","LatestNotificationError":""}'; fi
    ;;
  cloudtrail:get-event-selectors)
    [[ -f "${STATE}/trail-selectors.json" ]] || echo '{"EventSelectors":[]}'
    if [[ -f "${STATE}/trail-selectors.json" ]]; then jq -n --slurpfile selectors "${STATE}/trail-selectors.json" '{TrailARN:"mock",EventSelectors:$selectors[0]}'; fi
    ;;
  cloudtrail:list-tags)
    if [[ -f "${STATE}/trail-tags.json" ]]; then jq -n --slurpfile tags "${STATE}/trail-tags.json" '{ResourceTagList:[{ResourceId:"mock",TagsList:$tags[0]}]}';
    else echo '{"ResourceTagList":[{"ResourceId":"mock","TagsList":[]}]}' ; fi
    ;;
  cloudtrail:create-trail|cloudtrail:update-trail)
    mutate; : >"${STATE}/trail.exists"; echo '{}'
    ;;
  cloudtrail:add-tags)
    mutate; cp "$(file_arg --tags-list "$@")" "${STATE}/trail-tags.json"; echo '{}'
    ;;
  cloudtrail:put-event-selectors)
    selector_file="$(file_arg --event-selectors "$@")"
    jq -e 'all(.[]; ((keys - ["DataResources","ExcludeManagementEventSources","IncludeManagementEvents","ReadWriteType"]) | length)==0)' \
      "${selector_file}" >/dev/null || error InvalidEventSelectorsException
    mutate; cp "${selector_file}" "${STATE}/trail-selectors.json"; echo '{}'
    ;;
  cloudtrail:start-logging)
    mutate; : >"${STATE}/trail.logging"; echo '{}'
    ;;

  events:describe-rule)
    [[ -f "${STATE}/rule-pattern.json" ]] || error ResourceNotFoundException
    pattern="$(jq -c . "${STATE}/rule-pattern.json")"
    jq -n --arg name "${RULE}" --arg account "${ACCOUNT}" --arg region "${REGION}" --arg pattern "${pattern}" \
      '{Name:$name,Arn:("arn:aws:events:"+$region+":"+$account+":rule/"+$name),EventPattern:$pattern,
        State:"ENABLED",Description:"Detect mutation of calibration evidence integrity controls"}'
    ;;
  events:list-targets-by-rule)
    if [[ -f "${STATE}/rule-targets.json" ]]; then jq -n --slurpfile targets "${STATE}/rule-targets.json" '{Targets:$targets[0]}';
    else echo '{"Targets":[]}' ; fi
    ;;
  events:list-tags-for-resource)
    if [[ -f "${STATE}/rule-tags.json" ]]; then jq -n --slurpfile tags "${STATE}/rule-tags.json" '{Tags:$tags[0]}';
    else echo '{"Tags":[]}' ; fi
    ;;
  events:put-rule)
    mutate; cp "$(file_arg --event-pattern "$@")" "${STATE}/rule-pattern.json"; echo '{}'
    ;;
  events:tag-resource)
    mutate; cp "$(file_arg --tags "$@")" "${STATE}/rule-tags.json"; echo '{}'
    ;;
  events:put-targets)
    mutate; cp "$(file_arg --targets "$@")" "${STATE}/rule-targets.json"; echo '{"FailedEntryCount":0,"FailedEntries":[]}'
    ;;

  logs:describe-log-groups)
    if [[ -f "${STATE}/log-group.exists" ]]; then
      jq -n --arg name "${LOG_GROUP}" --arg account "${ACCOUNT}" --arg region "${REGION}" '{logGroups:[{
        logGroupName:$name,arn:("arn:aws:logs:"+$region+":"+$account+":log-group:"+$name+":*"),
        logGroupArn:("arn:aws:logs:"+$region+":"+$account+":log-group:"+$name),
        retentionInDays:365,storedBytes:0}]}'
    else echo '{"logGroups":[]}' ; fi
    ;;
  logs:list-tags-for-resource)
    if [[ -f "${STATE}/log-tags.json" ]]; then jq -n --slurpfile tags "${STATE}/log-tags.json" '{tags:$tags[0]}';
    else echo '{"tags":{}}' ; fi
    ;;
  logs:describe-resource-policies)
    if [[ -f "${STATE}/log-resource-policy.json" ]]; then
      document="$(jq -c . "${STATE}/log-resource-policy.json")"
      jq -n --arg document "${document}" '{resourcePolicies:[{policyName:"v2x-calibration-evidence-audit-events",policyDocument:$document,lastUpdatedTime:1}]}'
    else echo '{"resourcePolicies":[]}' ; fi
    ;;
  logs:create-log-group)
    mutate; : >"${STATE}/log-group.exists"; printf '%s' "$(arg --tags "$@")" >"${STATE}/log-tags.json"; echo '{}'
    ;;
  logs:tag-resource)
    mutate; printf '%s' "$(arg --tags "$@")" >"${STATE}/log-tags.json"; echo '{}'
    ;;
  logs:put-retention-policy)
    mutate; : >"${STATE}/log-group.exists"; echo '{}'
    ;;
  logs:put-resource-policy)
    mutate; printf '%s' "$(arg --policy-document "$@")" >"${STATE}/log-resource-policy.json"; echo '{}'
    ;;

  ssm:get-parameter)
    if [[ "${MOCK_HIDE_LOCK_READS:-false}" == "true" && " $* " != *" --query "* ]]; then error ParameterNotFound; fi
    [[ -f "${STATE}/lock" ]] || error ParameterNotFound
    if [[ -f "${STATE}/lock-delete-attempted" && "${MOCK_RELEASE_VERIFY_FAILURE:-false}" == "true" ]]; then error AccessDenied; fi
    if [[ -f "${STATE}/trail.logging" && "${MOCK_RELEASE_GET_FAILURE:-false}" == "true" &&
          " $* " == *" --query "* ]]; then error AccessDenied; fi
    if [[ " $* " == *" --query "* && " $* " == *" --output text "* ]]; then
      if [[ -f "${STATE}/trail.logging" && "${MOCK_RELEASE_OWNERSHIP_CHANGE:-false}" == "true" ]]; then
        printf '%s' '{"schema":"foreign-lock"}' >"${STATE}/lock"
        cat "${STATE}/lock"
      else
        cat "${STATE}/lock"
      fi
    else jq -n --arg value "$(<"${STATE}/lock")" '{Parameter:{Name:"/v2x/calibration/evidence-prerequisites/apply-lock",Type:"String",Value:$value,Version:1,ARN:"mock"}}'; fi
    ;;
  ssm:put-parameter)
    mutate
    value="$(arg --value "$@")"
    if (set -o noclobber; printf '%s' "${value}" >"${STATE}/lock") 2>/dev/null; then
      cp "${STATE}/lock" "${STATE}/lock-claimed"
      echo 'LOCK-CLAIMED' >>"${LOG}"
      sleep "${MOCK_LOCK_HOLD_SECONDS:-0}"
      if [[ "${MOCK_POST_LOCK_DRIFT:-false}" == "true" ]]; then : >"${STATE}/bucket-${EVIDENCE}.exists"; fi
      echo '{"Version":1}'
    else error ParameterAlreadyExists; fi
    ;;
  ssm:delete-parameter)
    mutate
    if [[ "${MOCK_RELEASE_DELETE_FAILURE:-false}" == "true" ]]; then error AccessDenied; fi
    if [[ "${MOCK_RELEASE_VERIFY_FAILURE:-false}" == "true" ]]; then
      : >"${STATE}/lock-delete-attempted"
      echo '{}'
    elif [[ "${MOCK_RELEASE_STILL_PRESENT:-false}" == "true" ]]; then
      echo '{}'
    else
      rm -f "${STATE}/lock"
      echo '{}'
    fi
    ;;
  *) echo "Unsupported mock call: ${service} ${operation} $*" >&2; exit 97 ;;
esac
MOCK
chmod +x "${MOCK_BIN}/aws"

run_plan() {
  local state="$1" output="$2" log="$3" scenario="${4:-absent}" marker="${5:-}"
  mkdir -p "${state}" "${output}"
  PATH="${MOCK_BIN}:${PATH}" MOCK_AWS_STATE="${state}" MOCK_AWS_LOG="${log}" \
    MOCK_AWS_SCENARIO="${scenario}" MOCK_DRIFT_MARKER="${marker}" \
    AWS_BIN=aws AWS_REGION=us-west-1 PLAN_ONLY=true TRUST_PRINCIPAL_ARN="${TRUST}" \
    PLAN_OUTPUT_DIR="${output}" "${SCRIPT}" >"${output}/plan.log" 2>&1
}

hash_file() { sha256sum "$1" | awk '{print $1}'; }

# Stable absent plan and zero mutating AWS calls.
state="${TMP}/stable-state"; log="${TMP}/stable-aws.log"
run_plan "${state}" "${TMP}/plan-a" "${log}"
run_plan "${state}" "${TMP}/plan-b" "${log}"
assert_eq "$(hash_file "${TMP}/plan-a/current.json")" "$(hash_file "${TMP}/plan-b/current.json")" "stable current hash"
assert_eq "$(hash_file "${TMP}/plan-a/desired.json")" "$(hash_file "${TMP}/plan-b/desired.json")" "stable desired hash"
! grep -q '^MUTATE ' "${log}" || fail "plan mode issued a mutating AWS call"

# Exact immutable controls, least privilege, monitoring, tags, and canary interface.
jq -e '
  .resources.audit_bucket.object_lock.Rule.DefaultRetention == {Mode:"COMPLIANCE",Days:365} and
  .resources.audit_bucket.lifecycle.Rules == [{ID:"AbortIncompleteCalibrationAuditUploads",Status:"Enabled",Filter:{Prefix:""},AbortIncompleteMultipartUpload:{DaysAfterInitiation:7}}] and
  (.resources.writer_role.trust.Statement[0].Principal.AWS | endswith(":user/rfs-v2x-service")) and
  ([.resources.writer_role.inline_policy.Statement[] | select(.Sid=="DenyEvidenceDeletionAndLockMutation")] | length)==1 and
  ([.resources.writer_role.inline_policy.Statement[] | select(.Sid=="AllowComplianceWritesAndMultipartCompletion")
    | .Condition.NumericGreaterThanEqualsIfExists["s3:object-lock-remaining-retention-days"]] == ["90"]) and
  .resources.trail.event_selectors[0].ReadWriteType=="WriteOnly" and
  (.resources.trail.event_selectors[0] | has("Name") | not) and
  .resources.trail.event_selectors[0].IncludeManagementEvents==true and
  .resources.trail.event_selectors[0].DataResources==[{Type:"AWS::S3::Object",Values:["arn:aws:s3:::v2x-calibration-evidence-147229569658-us-west-1/"]}] and
  (.resources.monitoring.rule.event_pattern["$or"][0].detail.requestParameters.name | length)==2 and
  (.resources.monitoring.rule.event_pattern["$or"][1].detail.requestParameters.trailName | length)==2 and
  any(.resources.monitoring.rule.event_pattern["$or"][];
    .detail.eventSource==["iam.amazonaws.com"] and (.detail.requestParameters.roleName | length)==2) and
  any(.resources.monitoring.rule.event_pattern["$or"][];
    .detail.eventSource==["events.amazonaws.com"] and .detail.eventName==["PutTargets","RemoveTargets"]) and
  any(.resources.monitoring.rule.event_pattern["$or"][];
    .detail.eventSource==["logs.amazonaws.com"] and ((.detail.eventName | index("DeleteLogGroup")) != null)) and
  any(.resources.monitoring.rule.event_pattern["$or"][];
    .detail.eventSource==["ssm.amazonaws.com"] and .detail.eventName==["PutParameter","DeleteParameter"]) and
  (.resources.monitoring.log_resource_policy.document.Statement[0].Principal.Service | sort)==["delivery.logs.amazonaws.com","events.amazonaws.com"] and
  (.resources.monitoring.log_resource_policy.document.Statement[0] | has("Condition") | not) and
  .resources.monitoring.log_group.retention_days==365 and
  .resources.monitoring.external_human_notification.status=="yellow-pending-product-decision" and
  .resources.monitoring.integrity_monitoring.durable_management_events_recorded_by_cloudtrail and
  (.resources.monitoring.integrity_monitoring.self_monitoring_limitation | contains("can be disabled")) and
  ([.resources.audit_bucket.tags[],.resources.writer_role.tags[],.resources.planner_role.tags[],
    .resources.trail.tags[],.resources.monitoring.rule.tags[]] | map(select(.Key=="ue-runtime" and .Value=="ue5-only")) | length)==5
' "${TMP}/plan-a/desired.json" >/dev/null || fail "desired policy/lifecycle/tag/trail shape"
jq -e '
  .retention_mode=="COMPLIANCE" and .minimum_retention_days==90 and
  .object_key_prefix=="canary/provisioning/" and
  .audit.require_exact_writer_session_put_object and .audit.require_digest_validation and
  .audit.require_eventbridge_rule_fire_log_readback and
  .audit.verifier_principal.must_be_explicit_before_canary and
  .requires_separate_locked_reviewed_gate
' "${TMP}/plan-a/later-canary-interface.json" >/dev/null || fail "later canary interface shape"

# Drift changes current hash but not desired hash when desired inputs did not change.
run_plan "${TMP}/drift-state" "${TMP}/drift-a" "${TMP}/drift.log" absent a
run_plan "${TMP}/drift-state" "${TMP}/drift-b" "${TMP}/drift.log" absent b
[[ "$(hash_file "${TMP}/drift-a/current.json")" != "$(hash_file "${TMP}/drift-b/current.json")" ]] || fail "current hash ignored drift"
assert_eq "$(hash_file "${TMP}/drift-a/desired.json")" "$(hash_file "${TMP}/drift-b/desired.json")" "desired hash independent of caller drift"

# AccessDenied is never mapped to absence.
if run_plan "${TMP}/denied-state" "${TMP}/denied" "${TMP}/denied.log" access_denied; then
  fail "AccessDenied plan unexpectedly succeeded"
fi
grep -q 'refusing to treat it as absent' "${TMP}/denied/plan.log" || fail "AccessDenied diagnostic missing"

# Existing-resource safety blockers are explicit and hash-visible.
run_plan "${TMP}/nolock-state" "${TMP}/nolock-plan" "${TMP}/nolock.log" no_object_lock
jq -e '.apply_blockers | index("existing audit bucket was not created with Object Lock") != null' \
  "${TMP}/nolock-plan/desired.json" >/dev/null || fail "missing non-Object-Lock blocker"
run_plan "${TMP}/lifecycle-state" "${TMP}/lifecycle-plan" "${TMP}/lifecycle.log" wrong_lifecycle
jq -e '.apply_blockers | index("existing audit lifecycle is not the exact abort-only managed rule") != null' \
  "${TMP}/lifecycle-plan/desired.json" >/dev/null || fail "missing lifecycle blocker"
run_plan "${TMP}/attached-state" "${TMP}/attached-plan" "${TMP}/attached.log" role_attached
jq -e '.apply_blockers | index("writer role has foreign inline or attached policies") != null' \
  "${TMP}/attached-plan/desired.json" >/dev/null || fail "missing foreign role-policy blocker"
run_plan "${TMP}/path-state" "${TMP}/path-plan" "${TMP}/path.log" role_bad_path
jq -e '.apply_blockers | index("writer role has a non-root IAM path") != null' \
  "${TMP}/path-plan/desired.json" >/dev/null || fail "missing role-path blocker"
run_plan "${TMP}/boundary-state" "${TMP}/boundary-plan" "${TMP}/boundary.log" role_boundary
jq -e '.apply_blockers | index("writer role has a permissions boundary") != null' \
  "${TMP}/boundary-plan/desired.json" >/dev/null || fail "missing role-boundary blocker"
run_plan "${TMP}/strong-lock-state" "${TMP}/strong-lock-plan" "${TMP}/strong-lock.log" stronger_lock
jq -e '
  .apply_blockers == [] and
  .resources.audit_bucket.object_lock.Rule.DefaultRetention == {Mode:"COMPLIANCE",Days:730}
' "${TMP}/strong-lock-plan/desired.json" >/dev/null || fail "stronger COMPLIANCE retention was not preserved"
run_plan "${TMP}/governance-lock-state" "${TMP}/governance-lock-plan" "${TMP}/governance-lock.log" governance_lock
jq -e '.apply_blockers | index("existing audit bucket default retention is not COMPLIANCE") != null' \
  "${TMP}/governance-lock-plan/desired.json" >/dev/null || fail "missing GOVERNANCE retention blocker"

# A foreign Allow is preserved, hash-bound, and requires the exact acknowledgment.
run_plan "${TMP}/foreign-state" "${TMP}/foreign-plan" "${TMP}/foreign.log" foreign_allow
foreign_sha="$(jq -r '.required_foreign_policy_acknowledgments[0]' "${TMP}/foreign-plan/desired.json")"
[[ "${foreign_sha}" =~ ^[0-9a-f]{64}$ ]] || fail "foreign Allow SHA-256 missing"
jq -e --arg sha "${foreign_sha}" '
  .foreign_audit_policy_statements[] | select(.sha256==$sha and .effect=="Allow" and .statement.Sid=="ForeignReadGrant")
' "${TMP}/foreign-plan/desired.json" >/dev/null || fail "foreign Allow not preserved in desired hash"
run_plan "${TMP}/unsafe-deny-state" "${TMP}/unsafe-deny-plan" "${TMP}/unsafe-deny.log" foreign_deny_unsafe
jq -e '
  (.required_foreign_policy_acknowledgments[0] | test("^[0-9a-f]{64}$")) and
  any(.apply_blockers[]; startswith("foreign audit-bucket Deny may block CloudTrail delivery:"))
' "${TMP}/unsafe-deny-plan/desired.json" >/dev/null || fail "unsafe foreign Deny was not hash-bound and blocked"

# A complete mocked apply proves gates, conditional lock, rollback evidence, and bounded readback.
apply_state="${TMP}/apply-state"; apply_plan="${TMP}/apply-plan"; apply_log="${TMP}/apply.log"
run_plan "${apply_state}" "${apply_plan}" "${apply_log}"
current_hash="$(hash_file "${apply_plan}/current.json")"
desired_hash="$(hash_file "${apply_plan}/desired.json")"
backup_root="${TMP}/backups"
PATH="${MOCK_BIN}:${PATH}" MOCK_AWS_STATE="${apply_state}" MOCK_AWS_LOG="${apply_log}" \
  AWS_BIN=aws AWS_REGION=us-west-1 PLAN_ONLY=false TRUST_PRINCIPAL_ARN="${TRUST}" \
  TRUST_PRINCIPAL_ARN_CONFIRM="${TRUST}" EXPECTED_CURRENT_STATE_HASH="${current_hash}" \
  EXPECTED_DESIRED_STATE_HASH="${desired_hash}" \
  CONFIRM_PREREQUISITES=CONFIGURE_CALIBRATION_EVIDENCE_PREREQUISITES \
  CONFIRM_COMPLIANCE_AUDIT=CREATE_COMPLIANCE_LOCKED_CALIBRATION_AUDIT_LOG \
  BACKUP_ROOT="${backup_root}" VERIFY_ATTEMPTS=1 VERIFY_DELAY_SECONDS=0 \
  "${SCRIPT}" >"${TMP}/apply-output.log" 2>&1 || { cat "${TMP}/apply-output.log" >&2; fail "mocked apply"; }
grep -q 'Calibration evidence AWS prerequisites verified' "${TMP}/apply-output.log" || fail "apply verification message"
grep -q '^Apply lock release state: confirmed_absent$' "${TMP}/apply-output.log" ||
  fail "apply confirmed-absent release state missing"
confirmed_line="$(grep -n '^Apply lock release state: confirmed_absent$' "${TMP}/apply-output.log" | cut -d: -f1)"
verified_line="$(grep -n '^Calibration evidence AWS prerequisites verified\.$' "${TMP}/apply-output.log" | cut -d: -f1)"
(( confirmed_line < verified_line )) || fail "confirmed-absent state was not emitted before verified banner"
[[ ! -e "${apply_state}/lock" ]] || fail "owned lock was not released"
! grep -Eq '^MUTATE s3api .*v2x-calibration-evidence-147229569658-us-west-1' "${apply_log}" || fail "prerequisite apply mutated evidence bucket"
bundle="$(find "${backup_root}" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[[ -n "${bundle}" ]] || fail "rollback bundle missing"
assert_eq "$(stat -c '%a' "${bundle}")" 700 "rollback directory mode"
find "${bundle}" -type f ! -perm 0600 -print | grep -q . && fail "rollback file mode is not 0600"
(cd "${bundle}" && sha256sum -c SHA256SUMS >/dev/null) || fail "rollback checksums"
jq -e '
  [.restore_order[].resource] == ["eventbridge-rule-and-target","cloudwatch-log-group-and-policy",
    "cloudtrail-and-selector","planner-role","writer-role","audit-bucket"] and
  .safety.destructive_rollback_forbidden and .evidence_store_was_not_mutated_by_this_transaction
' "${bundle}/rollback-manifest.json" >/dev/null || fail "rollback manifest completeness"

# Re-plan and re-apply the converged fixture to exercise every update/idempotent path.
run_plan "${apply_state}" "${TMP}/converged-plan" "${apply_log}"
converged_current="$(hash_file "${TMP}/converged-plan/current.json")"
converged_desired="$(hash_file "${TMP}/converged-plan/desired.json")"
PATH="${MOCK_BIN}:${PATH}" MOCK_AWS_STATE="${apply_state}" MOCK_AWS_LOG="${apply_log}" \
  AWS_BIN=aws AWS_REGION=us-west-1 PLAN_ONLY=false TRUST_PRINCIPAL_ARN="${TRUST}" \
  TRUST_PRINCIPAL_ARN_CONFIRM="${TRUST}" EXPECTED_CURRENT_STATE_HASH="${converged_current}" \
  EXPECTED_DESIRED_STATE_HASH="${converged_desired}" \
  CONFIRM_PREREQUISITES=CONFIGURE_CALIBRATION_EVIDENCE_PREREQUISITES \
  CONFIRM_COMPLIANCE_AUDIT=CREATE_COMPLIANCE_LOCKED_CALIBRATION_AUDIT_LOG \
  BACKUP_ROOT="${TMP}/idempotent-backups" VERIFY_ATTEMPTS=1 VERIFY_DELAY_SECONDS=0 \
  "${SCRIPT}" >"${TMP}/idempotent-apply.log" 2>&1 || { cat "${TMP}/idempotent-apply.log" >&2; fail "idempotent apply"; }
grep -q '^MUTATE iam update-assume-role-policy' "${apply_log}" || fail "idempotent role update path not exercised"
grep -q '^MUTATE cloudtrail update-trail' "${apply_log}" || fail "idempotent trail update path not exercised"
grep -q '^Apply lock release state: confirmed_absent$' "${TMP}/idempotent-apply.log" ||
  fail "idempotent apply confirmed-absent state missing"

# Restore fixture: drift every captured mutable document, then reconcile it from
# the rollback bundle using update/put/tag operations only (never deletion).
restore_bundle="$(find "${TMP}/idempotent-backups" -mindepth 1 -maxdepth 1 -type d | head -n1)"
restore_current="${restore_bundle}/current.json"
[[ -f "${restore_current}" ]] || fail "idempotent rollback current state missing"
restore_docs="${TMP}/restore-docs"; mkdir -p "${restore_docs}"
jq -S '.resources.audit_bucket.policy.Policy | fromjson' "${restore_current}" >"${restore_docs}/audit-policy.json"
jq -S '{TagSet:.resources.audit_bucket.tags}' "${restore_current}" >"${restore_docs}/audit-tags.json"
jq -S '{Rules:.resources.audit_bucket.lifecycle.Rules}' "${restore_current}" >"${restore_docs}/audit-lifecycle.json"
for role in writer planner; do
  jq -S ".resources.${role}_role.role.AssumeRolePolicyDocument" "${restore_current}" >"${restore_docs}/${role}-trust.json"
  jq -S ".resources.${role}_role.managed_inline_policy.PolicyDocument" "${restore_current}" >"${restore_docs}/${role}-policy.json"
  jq -S ".resources.${role}_role.tags" "${restore_current}" >"${restore_docs}/${role}-tags.json"
done
jq -S '.resources.trail.selectors.EventSelectors' "${restore_current}" >"${restore_docs}/selectors.json"
jq -S '.resources.trail.tags' "${restore_current}" >"${restore_docs}/trail-tags.json"
jq -S '.resources.event_rule.rule.EventPattern | fromjson' "${restore_current}" >"${restore_docs}/rule-pattern.json"
jq -S '.resources.event_rule.tags' "${restore_current}" >"${restore_docs}/rule-tags.json"
jq -S '.resources.event_rule.targets' "${restore_current}" >"${restore_docs}/rule-targets.json"
jq -c '.resources.log_group.tags | from_entries' "${restore_current}" >"${restore_docs}/log-tags.json"
jq -S '.resources.log_resource_policy.policy.policyDocument | fromjson' "${restore_current}" >"${restore_docs}/log-policy.json"

printf '{"drift":true}\n' >"${apply_state}/bucket-v2x-calibration-audit-147229569658-us-west-1-policy.json"
printf '[]\n' >"${apply_state}/role-V2XCalibrationEvidenceWriter-tags.json"
printf '{"drift":true}\n' >"${apply_state}/trail-selectors.json"
printf '{"drift":true}\n' >"${apply_state}/rule-pattern.json"
printf '{}\n' >"${apply_state}/log-resource-policy.json"
restore_log="${TMP}/restore.log"; : >"${restore_log}"
restore_env=(PATH="${MOCK_BIN}:${PATH}" MOCK_AWS_STATE="${apply_state}" MOCK_AWS_LOG="${restore_log}" AWS_BIN=aws)
env "${restore_env[@]}" aws --region us-west-1 s3api put-bucket-policy --bucket v2x-calibration-audit-147229569658-us-west-1 --policy "file://${restore_docs}/audit-policy.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 s3api put-bucket-tagging --bucket v2x-calibration-audit-147229569658-us-west-1 --tagging "file://${restore_docs}/audit-tags.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 s3api put-bucket-lifecycle-configuration --bucket v2x-calibration-audit-147229569658-us-west-1 --lifecycle-configuration "file://${restore_docs}/audit-lifecycle.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 iam update-assume-role-policy --role-name V2XCalibrationEvidenceWriter --policy-document "file://${restore_docs}/writer-trust.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 iam tag-role --role-name V2XCalibrationEvidenceWriter --tags "file://${restore_docs}/writer-tags.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 iam put-role-policy --role-name V2XCalibrationEvidenceWriter --policy-name V2XCalibrationEvidenceWriterPolicy --policy-document "file://${restore_docs}/writer-policy.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 iam update-assume-role-policy --role-name V2XCalibrationEvidencePlanner --policy-document "file://${restore_docs}/planner-trust.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 iam tag-role --role-name V2XCalibrationEvidencePlanner --tags "file://${restore_docs}/planner-tags.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 iam put-role-policy --role-name V2XCalibrationEvidencePlanner --policy-name V2XCalibrationEvidencePlannerPolicy --policy-document "file://${restore_docs}/planner-policy.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 cloudtrail update-trail --name v2x-calibration-evidence-audit --s3-bucket-name v2x-calibration-audit-147229569658-us-west-1 >/dev/null
env "${restore_env[@]}" aws --region us-west-1 cloudtrail add-tags --resource-id mock --tags-list "file://${restore_docs}/trail-tags.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 cloudtrail put-event-selectors --trail-name v2x-calibration-evidence-audit --event-selectors "file://${restore_docs}/selectors.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 cloudtrail start-logging --name v2x-calibration-evidence-audit >/dev/null
env "${restore_env[@]}" aws --region us-west-1 events put-rule --name v2x-calibration-evidence-audit-guard --event-pattern "file://${restore_docs}/rule-pattern.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 events tag-resource --resource-arn mock --tags "file://${restore_docs}/rule-tags.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 events put-targets --rule v2x-calibration-evidence-audit-guard --targets "file://${restore_docs}/rule-targets.json" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 logs tag-resource --resource-arn mock --tags "$(<"${restore_docs}/log-tags.json")" >/dev/null
env "${restore_env[@]}" aws --region us-west-1 logs put-retention-policy --log-group-name /aws/events/v2x-calibration-evidence-audit --retention-in-days 365 >/dev/null
env "${restore_env[@]}" aws --region us-west-1 logs put-resource-policy --policy-name v2x-calibration-evidence-audit-events --policy-document "$(jq -c . "${restore_docs}/log-policy.json")" >/dev/null
! grep -Eq '^MUTATE .* (delete|remove|stop)' "${restore_log}" || fail "rollback restore used a destructive API"
run_plan "${apply_state}" "${TMP}/restored-plan" "${restore_log}"
assert_eq "$(hash_file "${restore_current}")" "$(hash_file "${TMP}/restored-plan/current.json")" "rollback fixture restored exact captured current state"

# Missing foreign-policy acknowledgment fails before any mutation; exact acknowledgment succeeds.
foreign_current="$(hash_file "${TMP}/foreign-plan/current.json")"
foreign_desired="$(hash_file "${TMP}/foreign-plan/desired.json")"
before_mutations="$(grep -c '^MUTATE ' "${TMP}/foreign.log" || true)"
if PATH="${MOCK_BIN}:${PATH}" MOCK_AWS_STATE="${TMP}/foreign-state" MOCK_AWS_LOG="${TMP}/foreign.log" \
  MOCK_AWS_SCENARIO=foreign_allow AWS_BIN=aws AWS_REGION=us-west-1 PLAN_ONLY=false \
  TRUST_PRINCIPAL_ARN="${TRUST}" TRUST_PRINCIPAL_ARN_CONFIRM="${TRUST}" \
  EXPECTED_CURRENT_STATE_HASH="${foreign_current}" EXPECTED_DESIRED_STATE_HASH="${foreign_desired}" \
  CONFIRM_PREREQUISITES=CONFIGURE_CALIBRATION_EVIDENCE_PREREQUISITES \
  CONFIRM_COMPLIANCE_AUDIT=CREATE_COMPLIANCE_LOCKED_CALIBRATION_AUDIT_LOG \
  BACKUP_ROOT="${TMP}/foreign-backups" "${SCRIPT}" >"${TMP}/foreign-no-ack.log" 2>&1; then
  fail "foreign Allow apply succeeded without acknowledgment"
fi
assert_eq "$(grep -c '^MUTATE ' "${TMP}/foreign.log" || true)" "${before_mutations}" "foreign ack failure pre-mutation"
grep -q 'ACKNOWLEDGED_FOREIGN_POLICY_SHA256S' "${TMP}/foreign-no-ack.log" || fail "foreign ack diagnostic"
PATH="${MOCK_BIN}:${PATH}" MOCK_AWS_STATE="${TMP}/foreign-state" MOCK_AWS_LOG="${TMP}/foreign.log" \
  MOCK_AWS_SCENARIO=foreign_allow AWS_BIN=aws AWS_REGION=us-west-1 PLAN_ONLY=false \
  TRUST_PRINCIPAL_ARN="${TRUST}" TRUST_PRINCIPAL_ARN_CONFIRM="${TRUST}" \
  EXPECTED_CURRENT_STATE_HASH="${foreign_current}" EXPECTED_DESIRED_STATE_HASH="${foreign_desired}" \
  ACKNOWLEDGED_FOREIGN_POLICY_SHA256S="${foreign_sha}" \
  CONFIRM_PREREQUISITES=CONFIGURE_CALIBRATION_EVIDENCE_PREREQUISITES \
  CONFIRM_COMPLIANCE_AUDIT=CREATE_COMPLIANCE_LOCKED_CALIBRATION_AUDIT_LOG \
  BACKUP_ROOT="${TMP}/foreign-backups" VERIFY_ATTEMPTS=1 VERIFY_DELAY_SECONDS=0 \
  "${SCRIPT}" >"${TMP}/foreign-ack.log" 2>&1 || { cat "${TMP}/foreign-ack.log" >&2; fail "acknowledged foreign Allow apply"; }
grep -q 'Calibration evidence AWS prerequisites verified' "${TMP}/foreign-ack.log" || fail "acknowledged foreign apply verification"
grep -q '^Apply lock release state: confirmed_absent$' "${TMP}/foreign-ack.log" ||
  fail "acknowledged foreign apply confirmed-absent state missing"

# Concurrency fixture: one apply claims the conditional lock; the second cannot mutate.
con_state="${TMP}/con-state"; con_plan="${TMP}/con-plan"; con_log="${TMP}/con.log"
run_plan "${con_state}" "${con_plan}" "${con_log}"
con_current="$(hash_file "${con_plan}/current.json")"; con_desired="$(hash_file "${con_plan}/desired.json")"
apply_env=(PATH="${MOCK_BIN}:${PATH}" MOCK_AWS_STATE="${con_state}" MOCK_AWS_LOG="${con_log}"
  MOCK_HIDE_LOCK_READS=true
  AWS_BIN=aws AWS_REGION=us-west-1 PLAN_ONLY=false TRUST_PRINCIPAL_ARN="${TRUST}"
  TRUST_PRINCIPAL_ARN_CONFIRM="${TRUST}" EXPECTED_CURRENT_STATE_HASH="${con_current}"
  EXPECTED_DESIRED_STATE_HASH="${con_desired}"
  CONFIRM_PREREQUISITES=CONFIGURE_CALIBRATION_EVIDENCE_PREREQUISITES
  CONFIRM_COMPLIANCE_AUDIT=CREATE_COMPLIANCE_LOCKED_CALIBRATION_AUDIT_LOG
  BACKUP_ROOT="${TMP}/con-backups" VERIFY_ATTEMPTS=1 VERIFY_DELAY_SECONDS=0)
env "${apply_env[@]}" MOCK_LOCK_HOLD_SECONDS=2 "${SCRIPT}" >"${TMP}/con-first.log" 2>&1 &
first_pid=$!
for _ in {1..100}; do [[ -f "${con_state}/lock" ]] && break; sleep 0.02; done
[[ -f "${con_state}/lock" ]] || fail "first apply never claimed lock"
if env "${apply_env[@]}" "${SCRIPT}" >"${TMP}/con-second.log" 2>&1; then
  fail "second concurrent apply unexpectedly succeeded"
fi
wait "${first_pid}" || { cat "${TMP}/con-first.log" >&2; fail "first concurrent apply"; }
grep -q '^Apply lock release state: confirmed_absent$' "${TMP}/con-first.log" ||
  fail "concurrent winner confirmed-absent state missing"
put_locks="$(grep -c '^MUTATE ssm put-parameter' "${con_log}" || true)"
claimed_locks="$(grep -c '^LOCK-CLAIMED$' "${con_log}" || true)"
assert_eq "${put_locks}" 2 "both concurrent claimants reached the conditional SSM API"
assert_eq "${claimed_locks}" 1 "only one conditional lock claimant succeeded"
grep -q 'Conditional SSM apply-lock claim failed' "${TMP}/con-second.log" || fail "concurrent blocker diagnostic"

# Drift after the conditional claim fails before the first non-lock resource
# mutation and deliberately retains the owned lock for reviewed recovery.
drift_state="${TMP}/postlock-drift-state"; drift_plan="${TMP}/postlock-drift-plan"; drift_log="${TMP}/postlock-drift.log"
run_plan "${drift_state}" "${drift_plan}" "${drift_log}"
drift_current="$(hash_file "${drift_plan}/current.json")"
drift_desired="$(hash_file "${drift_plan}/desired.json")"
if PATH="${MOCK_BIN}:${PATH}" MOCK_AWS_STATE="${drift_state}" MOCK_AWS_LOG="${drift_log}" \
  MOCK_POST_LOCK_DRIFT=true AWS_BIN=aws AWS_REGION=us-west-1 PLAN_ONLY=false \
  TRUST_PRINCIPAL_ARN="${TRUST}" TRUST_PRINCIPAL_ARN_CONFIRM="${TRUST}" \
  EXPECTED_CURRENT_STATE_HASH="${drift_current}" EXPECTED_DESIRED_STATE_HASH="${drift_desired}" \
  CONFIRM_PREREQUISITES=CONFIGURE_CALIBRATION_EVIDENCE_PREREQUISITES \
  CONFIRM_COMPLIANCE_AUDIT=CREATE_COMPLIANCE_LOCKED_CALIBRATION_AUDIT_LOG \
  BACKUP_ROOT="${TMP}/postlock-drift-backups" VERIFY_ATTEMPTS=1 VERIFY_DELAY_SECONDS=0 \
  "${SCRIPT}" >"${TMP}/postlock-drift-output.log" 2>&1; then
  fail "post-lock drift apply unexpectedly succeeded"
fi
[[ -f "${drift_state}/lock" ]] || fail "failed apply did not retain its owned SSM lock"
grep -q 'AWS state changed after planning and before the first resource mutation' \
  "${TMP}/postlock-drift-output.log" || fail "post-lock drift diagnostic missing"
grep -q 'Release state: owned' \
  "${TMP}/postlock-drift-output.log" || fail "failed-lock owned-state diagnostic missing"
assert_eq "$(grep -c '^MUTATE ' "${drift_log}" || true)" 1 "post-lock drift mutated more than its SSM lock"
grep -q '^MUTATE ssm put-parameter$' "${drift_log}" || fail "post-lock drift did not claim only the SSM lock"

# Lock release is part of acceptance: every ambiguous read/delete/absence or
# ownership change fails nonzero, suppresses success, and retains safety state.
for release_case in get_failure ownership_change delete_failure verify_failure still_present; do
  release_state="${TMP}/release-${release_case}-state"
  release_plan="${TMP}/release-${release_case}-plan"
  release_log="${TMP}/release-${release_case}.log"
  run_plan "${release_state}" "${release_plan}" "${release_log}"
  release_current="$(hash_file "${release_plan}/current.json")"
  release_desired="$(hash_file "${release_plan}/desired.json")"
  release_flag=()
  case "${release_case}" in
    get_failure) release_flag=(MOCK_RELEASE_GET_FAILURE=true) ;;
    ownership_change) release_flag=(MOCK_RELEASE_OWNERSHIP_CHANGE=true) ;;
    delete_failure) release_flag=(MOCK_RELEASE_DELETE_FAILURE=true) ;;
    verify_failure) release_flag=(MOCK_RELEASE_VERIFY_FAILURE=true) ;;
    still_present) release_flag=(MOCK_RELEASE_STILL_PRESENT=true) ;;
  esac
  if env PATH="${MOCK_BIN}:${PATH}" MOCK_AWS_STATE="${release_state}" MOCK_AWS_LOG="${release_log}" \
    "${release_flag[@]}" AWS_BIN=aws AWS_REGION=us-west-1 PLAN_ONLY=false \
    TRUST_PRINCIPAL_ARN="${TRUST}" TRUST_PRINCIPAL_ARN_CONFIRM="${TRUST}" \
    EXPECTED_CURRENT_STATE_HASH="${release_current}" EXPECTED_DESIRED_STATE_HASH="${release_desired}" \
    CONFIRM_PREREQUISITES=CONFIGURE_CALIBRATION_EVIDENCE_PREREQUISITES \
    CONFIRM_COMPLIANCE_AUDIT=CREATE_COMPLIANCE_LOCKED_CALIBRATION_AUDIT_LOG \
    BACKUP_ROOT="${TMP}/release-${release_case}-backups" VERIFY_ATTEMPTS=1 VERIFY_DELAY_SECONDS=0 \
    "${SCRIPT}" >"${TMP}/release-${release_case}-output.log" 2>&1; then
    fail "${release_case} lock release unexpectedly succeeded"
  fi
  [[ -f "${release_state}/lock" ]] || fail "${release_case} did not retain lock safety state"
  ! grep -q 'Calibration evidence AWS prerequisites verified' \
    "${TMP}/release-${release_case}-output.log" || fail "${release_case} printed success banner"
  case "${release_case}" in
    get_failure|delete_failure)
      grep -q 'Release state: owned' "${TMP}/release-${release_case}-output.log" ||
        fail "${release_case} omitted owned-state diagnostic"
      ;;
    ownership_change)
      grep -q 'Release state: ownership_lost' "${TMP}/release-${release_case}-output.log" ||
        fail "ownership change omitted ownership_lost diagnostic"
      grep -q 'does not claim it is owned or retained' "${TMP}/release-${release_case}-output.log" ||
        fail "ownership change made an owned/retained claim"
      assert_eq "$(<"${release_state}/lock")" '{"schema":"foreign-lock"}' \
        "ownership change mock state"
      ;;
    verify_failure)
      grep -q 'Release state: delete_accepted_unverified' \
        "${TMP}/release-${release_case}-output.log" || fail "verification failure omitted delete-accepted state"
      grep -q 'AWS accepted deletion but current parameter existence is unknown' \
        "${TMP}/release-${release_case}-output.log" || fail "verification failure misstated existence"
      grep -q 'accept only ParameterNotFound as cleared' \
        "${TMP}/release-${release_case}-output.log" || fail "verification failure omitted exact read gate"
      ;;
    still_present)
      grep -q 'Release state: delete_accepted_still_present' \
        "${TMP}/release-${release_case}-output.log" || fail "surviving lock omitted still-present state"
      grep -q 'an exact read proved the parameter still exists' \
        "${TMP}/release-${release_case}-output.log" || fail "surviving lock omitted exact existence proof"
      grep -q 'review the current Parameter.Value and rollback bundle' \
        "${TMP}/release-${release_case}-output.log" || fail "surviving lock omitted recovery gate"
      cmp -s "${release_state}/lock" "${release_state}/lock-claimed" ||
        fail "surviving lock did not retain the exact originally claimed value"
      ;;
  esac
done

echo "PASS: calibration evidence prerequisite provisioner mocked tests"
