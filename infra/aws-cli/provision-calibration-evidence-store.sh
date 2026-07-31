#!/usr/bin/env bash
set -euo pipefail

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing dependency: $1" >&2
    exit 1
  }
}
need aws
need jq
need sha256sum

AWS_REGION="${AWS_REGION:-us-west-1}"
PLAN_ONLY="${PLAN_ONLY:-true}"
RETENTION_MODE="${RETENTION_MODE:-COMPLIANCE}"
RETENTION_DAYS="${RETENTION_DAYS:-90}"
EXPECTED_CURRENT_STATE_HASH="${EXPECTED_CURRENT_STATE_HASH:-}"
EXPECTED_DESIRED_STATE_HASH="${EXPECTED_DESIRED_STATE_HASH:-}"
CONFIRM_OBJECT_LOCK_IRREVERSIBLE="${CONFIRM_OBJECT_LOCK_IRREVERSIBLE:-}"
CLOUDTRAIL_TRAIL_NAME="${CLOUDTRAIL_TRAIL_NAME:-}"
BACKUP_ROOT="${BACKUP_ROOT:-/home/path/V2XCarla/v2x-backend-backups/calibration-evidence-store}"
export AWS_REGION

case "${PLAN_ONLY}" in true|false) ;; *) echo "PLAN_ONLY must be true or false" >&2; exit 2 ;; esac
case "${RETENTION_MODE}" in COMPLIANCE|GOVERNANCE) ;; *) echo "RETENTION_MODE must be COMPLIANCE or GOVERNANCE" >&2; exit 2 ;; esac
if [[ ! "${RETENTION_DAYS}" =~ ^[1-9][0-9]*$ ]] ||
   (( RETENTION_DAYS < 30 || RETENTION_DAYS > 3650 )); then
  echo "RETENTION_DAYS must be an integer from 30 through 3650" >&2
  exit 2
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${CALIBRATION_EVIDENCE_BUCKET:-v2x-calibration-evidence-${ACCOUNT_ID}-${AWS_REGION}}"
EVIDENCE_WRITER_ROLE_ARN="${EVIDENCE_WRITER_ROLE_ARN:-arn:aws:iam::${ACCOUNT_ID}:role/V2XCalibrationEvidenceWriter}"
if [[ ! "${BUCKET}" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] ||
   [[ "${BUCKET}" == *..* ]] ||
   [[ "${BUCKET}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "CALIBRATION_EVIDENCE_BUCKET is not a valid S3 bucket name" >&2
  exit 2
fi
if [[ ! "${EVIDENCE_WRITER_ROLE_ARN}" =~ ^arn:aws:iam::${ACCOUNT_ID}:role/[A-Za-z0-9+=,.@_-]+$ ]]; then
  echo "EVIDENCE_WRITER_ROLE_ARN must be a same-account pathless IAM role ARN" >&2
  exit 2
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT
CURRENT="${WORKDIR}/current.json"
DESIRED="${WORKDIR}/desired.json"
POLICY_FILE="${WORKDIR}/policy.json"
TAGGING_FILE="${WORKDIR}/tagging.json"
LIFECYCLE_FILE="${WORKDIR}/lifecycle.json"

s3api() {
  aws s3api "$@" --expected-bucket-owner "${ACCOUNT_ID}"
}

read_optional() {
  local output="$1" allowed="$2"
  shift 2
  local error="${output}.err"
  if "$@" >"${output}" 2>"${error}"; then
    rm -f "${error}"
    return 0
  fi
  if grep -Eq "${allowed}" "${error}"; then
    printf '{}\n' >"${output}"
    rm -f "${error}"
    return 0
  fi
  cat "${error}" >&2
  return 1
}

BUCKET_EXISTS=false
head_error="${WORKDIR}/head.err"
if s3api head-bucket --bucket "${BUCKET}" >/dev/null 2>"${head_error}"; then
  BUCKET_EXISTS=true
elif grep -Eq '\(404\)|Not Found|NoSuchBucket' "${head_error}"; then
  BUCKET_EXISTS=false
else
  cat "${head_error}" >&2
  echo "Bucket existence is not readable; refusing to treat it as absent" >&2
  exit 3
fi

CLOUDTRAIL_COVERAGE=false
cloudtrail_selectors_json='{}'
cloudtrail_status_json='{}'
cloudtrail_json='{}'
if [[ -n "${CLOUDTRAIL_TRAIL_NAME}" ]]; then
  cloudtrail_selectors_json="$(aws cloudtrail get-event-selectors --trail-name "${CLOUDTRAIL_TRAIL_NAME}" --output json)"
  cloudtrail_status_json="$(aws cloudtrail get-trail-status --name "${CLOUDTRAIL_TRAIL_NAME}" --output json)"
  cloudtrail_json="$(jq -nS \
    --argjson selectors "${cloudtrail_selectors_json}" \
    --argjson status "${cloudtrail_status_json}" \
    '{selectors:$selectors,status:{
      IsLogging:($status.IsLogging // false),
      LatestDeliveryError:($status.LatestDeliveryError // "")}}')"
  if [[ "$(jq -r '.IsLogging // false' <<<"${cloudtrail_status_json}")" == "true" ]] &&
     [[ -z "$(jq -r '.LatestDeliveryError // ""' <<<"${cloudtrail_status_json}")" ]] &&
     jq -e --arg prefix "arn:aws:s3:::${BUCKET}/" '
       any(.EventSelectors[]?;
         ((.ReadWriteType // "All") == "All" or .ReadWriteType == "WriteOnly")
         and any(.DataResources[]?;
           .Type == "AWS::S3::Object" and any(.Values[]?; . == $prefix)))
       or any(.AdvancedEventSelectors[]?;
         . as $selector
         | ($selector.FieldSelectors | type) == "array"
         and (($selector.FieldSelectors | length) == 3
           or ($selector.FieldSelectors | length) == 4)
         and ([ $selector.FieldSelectors[]?
           | select(. == {Field:"eventCategory",Equals:["Data"]})
         ] | length) == 1
         and ([ $selector.FieldSelectors[]?
           | select(. == {Field:"resources.type",Equals:["AWS::S3::Object"]})
         ] | length) == 1
         and ([ $selector.FieldSelectors[]?
           | select(. == {Field:"resources.ARN",StartsWith:[$prefix]})
         ] | length) == 1
         and ((($selector.FieldSelectors | length) == 3
             and ([ $selector.FieldSelectors[]? | select(.Field == "readOnly") ] | length) == 0)
           or ([ $selector.FieldSelectors[]?
             | select(. == {Field:"readOnly",Equals:["false"]})
           ] | length) == 1))
     ' <<<"${cloudtrail_selectors_json}" >/dev/null; then
    CLOUDTRAIL_COVERAGE=true
  fi
fi

if [[ "${BUCKET_EXISTS}" == "true" ]]; then
  s3api get-bucket-location --bucket "${BUCKET}" --output json >"${WORKDIR}/location.json"
  s3api get-bucket-versioning --bucket "${BUCKET}" --output json >"${WORKDIR}/versioning.json"
  read_optional "${WORKDIR}/public.json" 'NoSuchPublicAccessBlockConfiguration' s3api get-public-access-block --bucket "${BUCKET}" --output json
  read_optional "${WORKDIR}/encryption.json" 'ServerSideEncryptionConfigurationNotFoundError' s3api get-bucket-encryption --bucket "${BUCKET}" --output json
  read_optional "${WORKDIR}/ownership.json" 'OwnershipControlsNotFoundError' s3api get-bucket-ownership-controls --bucket "${BUCKET}" --output json
  read_optional "${WORKDIR}/lock.json" 'ObjectLockConfigurationNotFoundError' s3api get-object-lock-configuration --bucket "${BUCKET}" --output json
  read_optional "${WORKDIR}/policy-response.json" 'NoSuchBucketPolicy' s3api get-bucket-policy --bucket "${BUCKET}" --output json
  read_optional "${WORKDIR}/tags.json" 'NoSuchTagSet' s3api get-bucket-tagging --bucket "${BUCKET}" --output json
  read_optional "${WORKDIR}/lifecycle.json" 'NoSuchLifecycleConfiguration' s3api get-bucket-lifecycle-configuration --bucket "${BUCKET}" --output json
  s3api get-bucket-logging --bucket "${BUCKET}" --output json >"${WORKDIR}/logging.json"
  read_optional "${WORKDIR}/replication.json" 'ReplicationConfigurationNotFoundError' s3api get-bucket-replication --bucket "${BUCKET}" --output json
  s3api get-bucket-acl --bucket "${BUCKET}" --output json >"${WORKDIR}/acl.json"

  actual_region="$(jq -r '.LocationConstraint // "us-east-1"' "${WORKDIR}/location.json")"
  if [[ "${actual_region}" != "${AWS_REGION}" ]]; then
    echo "Existing bucket region ${actual_region} does not match ${AWS_REGION}" >&2
    exit 4
  fi

  jq -nS \
    --arg bucket "${BUCKET}" --arg region "${AWS_REGION}" \
    --argjson location "$(<"${WORKDIR}/location.json")" \
    --argjson versioning "$(<"${WORKDIR}/versioning.json")" \
    --argjson public_access "$(<"${WORKDIR}/public.json")" \
    --argjson encryption "$(<"${WORKDIR}/encryption.json")" \
    --argjson ownership "$(<"${WORKDIR}/ownership.json")" \
    --argjson object_lock "$(<"${WORKDIR}/lock.json")" \
    --argjson policy "$(<"${WORKDIR}/policy-response.json")" \
    --argjson tags "$(<"${WORKDIR}/tags.json")" \
    --argjson lifecycle "$(<"${WORKDIR}/lifecycle.json")" \
    --argjson logging "$(<"${WORKDIR}/logging.json")" \
    --argjson replication "$(<"${WORKDIR}/replication.json")" \
    --argjson acl "$(<"${WORKDIR}/acl.json")" \
    --argjson cloudtrail "${cloudtrail_json}" \
    '{exists:true,bucket:$bucket,requested_region:$region,location:$location,
      versioning:$versioning,public_access:$public_access,encryption:$encryption,
      ownership:$ownership,object_lock:$object_lock,policy:$policy,tags:$tags,
      lifecycle:$lifecycle,logging:$logging,replication:$replication,acl:$acl,
      cloudtrail:$cloudtrail}' >"${CURRENT}"
else
  jq -nS --arg bucket "${BUCKET}" --arg region "${AWS_REGION}" \
    --argjson cloudtrail "${cloudtrail_json}" \
    '{exists:false,bucket:$bucket,requested_region:$region,cloudtrail:$cloudtrail}' >"${CURRENT}"
fi

existing_policy='{"Version":"2012-10-17","Statement":[]}'
if [[ "${BUCKET_EXISTS}" == "true" ]] && jq -e '.Policy | type == "string"' "${WORKDIR}/policy-response.json" >/dev/null 2>&1; then
  existing_policy="$(jq -er '.Policy | fromjson' "${WORKDIR}/policy-response.json")"
fi
jq -nS --arg bucket "${BUCKET}" --arg writer "${EVIDENCE_WRITER_ROLE_ARN}" '{
  Version:"2012-10-17",
  Statement:[
    {Sid:"DenyInsecureTransport",Effect:"Deny",Principal:"*",Action:"s3:*",
     Resource:[("arn:aws:s3:::"+$bucket),("arn:aws:s3:::"+$bucket+"/*")],
     Condition:{Bool:{"aws:SecureTransport":"false"}}},
    {Sid:"DenyEvidenceDeletionOrRetentionBypass",Effect:"Deny",Principal:"*",
     Action:["s3:DeleteObject","s3:DeleteObjectVersion","s3:PutObjectRetention",
       "s3:PutObjectLegalHold","s3:BypassGovernanceRetention"],
     Resource:("arn:aws:s3:::"+$bucket+"/*")},
    {Sid:"DenyUnapprovedEvidenceWriters",Effect:"Deny",Principal:"*",
     Action:["s3:PutObject","s3:AbortMultipartUpload"],
     Resource:("arn:aws:s3:::"+$bucket+"/*"),
     Condition:{ArnNotEquals:{"aws:PrincipalArn":$writer}}}
  ]
}' >"${WORKDIR}/managed-policy.json"
jq -nS --argjson existing "${existing_policy}" --slurpfile managed "${WORKDIR}/managed-policy.json" '
  $existing
  | .Version = "2012-10-17"
  | .Statement = (
      (.Statement
        | if type == "array" then .
          elif type == "object" then [.]
          else error("bucket policy Statement must be an object or array") end)
      | [ .[] | select(.Sid as $sid | [
          "DenyInsecureTransport","DenyEvidenceDeletionOrRetentionBypass",
          "DenyUnapprovedEvidenceWriters"] | index($sid) | not) ]
      ) + $managed[0].Statement
' >"${POLICY_FILE}"

existing_tags='[]'
if [[ "${BUCKET_EXISTS}" == "true" ]]; then
  existing_tags="$(jq -c '.TagSet // []' "${WORKDIR}/tags.json")"
fi
jq -nS --argjson existing "${existing_tags}" '{TagSet:(
  [$existing[] | select(.Key as $key | ["managed-by","purpose","ue-runtime"] | index($key) | not)]
  + [{Key:"managed-by",Value:"v2x-backend"},{Key:"purpose",Value:"calibration-evidence"},{Key:"ue-runtime",Value:"ue5-only"}]
  | sort_by(.Key))}' >"${TAGGING_FILE}"

existing_rules='[]'
LIFECYCLE_TRANSITION_DEFAULT='all_storage_classes_128K'
if [[ "${BUCKET_EXISTS}" == "true" ]]; then
  existing_rules="$(jq -c '.Rules // []' "${WORKDIR}/lifecycle.json")"
  if (( $(jq -r '.Rules // [] | length' "${WORKDIR}/lifecycle.json") > 0 )); then
    LIFECYCLE_TRANSITION_DEFAULT="$(jq -er '
      .TransitionDefaultMinimumObjectSize
      | select(. == "all_storage_classes_128K" or . == "varies_by_storage_class")
    ' "${WORKDIR}/lifecycle.json")" || {
      echo "Existing lifecycle transition default is absent or unreadable; refusing to replace it" >&2
      exit 4
    }
  fi
fi
jq -nS --argjson existing "${existing_rules}" '{Rules:(
  [$existing[] | select(.ID != "AbortIncompleteCalibrationEvidenceUploads")]
  + [{ID:"AbortIncompleteCalibrationEvidenceUploads",Status:"Enabled",
      Filter:{Prefix:""},AbortIncompleteMultipartUpload:{DaysAfterInitiation:7}}])}' >"${LIFECYCLE_FILE}"

jq -nS \
  --arg bucket "${BUCKET}" --arg region "${AWS_REGION}" \
  --arg writer "${EVIDENCE_WRITER_ROLE_ARN}" \
  --arg mode "${RETENTION_MODE}" --argjson days "${RETENTION_DAYS}" \
  --arg lifecycle_transition_default "${LIFECYCLE_TRANSITION_DEFAULT}" \
  --argjson policy "$(<"${POLICY_FILE}")" \
  --argjson tags "$(<"${TAGGING_FILE}")" \
  --argjson lifecycle "$(<"${LIFECYCLE_FILE}")" \
  --argjson cloudtrail_covered "${CLOUDTRAIL_COVERAGE}" \
  '{bucket:$bucket,region:$region,writer_role_arn:$writer,
    versioning:"Enabled",object_ownership:"BucketOwnerEnforced",
    encryption:"AES256",public_access_block_all:true,
    object_lock:{enabled:true,mode:$mode,days:$days},
    policy:$policy,tags:$tags,lifecycle:$lifecycle,
    lifecycle_transition_default_minimum_object_size:$lifecycle_transition_default,
    cloudtrail_data_events_covered:$cloudtrail_covered}' >"${DESIRED}"

CURRENT_STATE_HASH="$(sha256sum "${CURRENT}" | awk '{print $1}')"
DESIRED_STATE_HASH="$(sha256sum "${DESIRED}" | awk '{print $1}')"
echo "Account: ${ACCOUNT_ID}"
echo "Region: ${AWS_REGION}"
echo "Bucket: ${BUCKET}"
echo "Current state hash: ${CURRENT_STATE_HASH}"
echo "Desired state hash: ${DESIRED_STATE_HASH}"
echo "Exists: ${BUCKET_EXISTS}"
echo "Plan only: ${PLAN_ONLY}"
echo "Current state:"
jq . "${CURRENT}"
echo "Desired state:"
jq . "${DESIRED}"

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
if [[ "${CONFIRM_OBJECT_LOCK_IRREVERSIBLE}" != "CONFIGURE_OBJECT_LOCKED_EVIDENCE_BUCKET" ]]; then
  echo "Set CONFIRM_OBJECT_LOCK_IRREVERSIBLE=CONFIGURE_OBJECT_LOCKED_EVIDENCE_BUCKET" >&2
  exit 5
fi
if [[ "${CLOUDTRAIL_COVERAGE}" != "true" ]]; then
  echo "A named CloudTrail with S3 object data events for this bucket is required" >&2
  exit 5
fi
writer_role_name="${EVIDENCE_WRITER_ROLE_ARN##*/}"
aws iam get-role --role-name "${writer_role_name}" --query 'Role.Arn' --output text | grep -Fx "${EVIDENCE_WRITER_ROLE_ARN}" >/dev/null

if [[ "${BUCKET_EXISTS}" == "true" ]]; then
  existing_lock="$(jq -r '.object_lock.ObjectLockConfiguration.ObjectLockEnabled // ""' "${CURRENT}")"
  existing_mode="$(jq -r '.object_lock.ObjectLockConfiguration.Rule.DefaultRetention.Mode // ""' "${CURRENT}")"
  existing_days="$(jq -r '.object_lock.ObjectLockConfiguration.Rule.DefaultRetention.Days // 0' "${CURRENT}")"
  existing_years="$(jq -r '.object_lock.ObjectLockConfiguration.Rule.DefaultRetention.Years // empty' "${CURRENT}")"
  if [[ "${existing_lock}" != "Enabled" ]]; then
    echo "Refusing to retrofit an existing non-Object-Lock bucket" >&2
    exit 6
  fi
  if [[ -n "${existing_years}" ]]; then
    echo "Refusing to rewrite Years-based default retention as Days; use a separately reviewed migration" >&2
    exit 6
  fi
  if (( RETENTION_DAYS < existing_days )) ||
     [[ "${existing_mode}" == "COMPLIANCE" && "${RETENTION_MODE}" != "COMPLIANCE" ]]; then
    echo "Refusing to weaken existing default retention" >&2
    exit 6
  fi
fi

backup="${BACKUP_ROOT}/$(date -u +%Y%m%dT%H%M%SZ)-${CURRENT_STATE_HASH}"
install -d -m 0700 "${backup}"
install -m 0600 "${CURRENT}" "${backup}/current.json"
install -m 0600 "${DESIRED}" "${backup}/desired.json"
printf '%s\n' "${CURRENT_STATE_HASH}" >"${backup}/current-state.sha256"
chmod 0600 "${backup}/current-state.sha256"

if [[ "${BUCKET_EXISTS}" == "false" ]]; then
  create_args=(create-bucket --bucket "${BUCKET}" --object-lock-enabled-for-bucket --object-ownership BucketOwnerEnforced)
  if [[ "${AWS_REGION}" != "us-east-1" ]]; then
    create_args+=(--create-bucket-configuration "LocationConstraint=${AWS_REGION}")
  fi
  aws s3api "${create_args[@]}" >/dev/null
fi

s3api put-public-access-block --bucket "${BUCKET}" --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true >/dev/null
s3api put-bucket-ownership-controls --bucket "${BUCKET}" --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]' >/dev/null
s3api put-bucket-encryption --bucket "${BUCKET}" --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":false}]}' >/dev/null
s3api put-bucket-versioning --bucket "${BUCKET}" --versioning-configuration Status=Enabled >/dev/null
s3api put-object-lock-configuration --bucket "${BUCKET}" --object-lock-configuration "{\"ObjectLockEnabled\":\"Enabled\",\"Rule\":{\"DefaultRetention\":{\"Mode\":\"${RETENTION_MODE}\",\"Days\":${RETENTION_DAYS}}}}" >/dev/null
s3api put-bucket-policy --bucket "${BUCKET}" --policy "file://${POLICY_FILE}" >/dev/null
s3api put-bucket-tagging --bucket "${BUCKET}" --tagging "file://${TAGGING_FILE}" >/dev/null
s3api put-bucket-lifecycle-configuration --bucket "${BUCKET}" \
  --lifecycle-configuration "file://${LIFECYCLE_FILE}" \
  --transition-default-minimum-object-size "${LIFECYCLE_TRANSITION_DEFAULT}" >/dev/null

verify_error=''
for attempt in 1 2 3 4 5; do
  actual_location="$(s3api get-bucket-location --bucket "${BUCKET}" --output json)"
  actual_versioning="$(s3api get-bucket-versioning --bucket "${BUCKET}" --query Status --output text)"
  actual_lock="$(s3api get-object-lock-configuration --bucket "${BUCKET}" --output json)"
  actual_public="$(s3api get-public-access-block --bucket "${BUCKET}" --query PublicAccessBlockConfiguration --output json)"
  actual_ownership="$(s3api get-bucket-ownership-controls --bucket "${BUCKET}" --query 'OwnershipControls.Rules[0].ObjectOwnership' --output text)"
  actual_encryption="$(s3api get-bucket-encryption --bucket "${BUCKET}" --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' --output text)"
  actual_policy="$(s3api get-bucket-policy --bucket "${BUCKET}" --query Policy --output text | jq -S .)"
  actual_tags="$(s3api get-bucket-tagging --bucket "${BUCKET}" --output json | jq -S '.TagSet|sort_by(.Key)')"
  actual_lifecycle_response="$(s3api get-bucket-lifecycle-configuration --bucket "${BUCKET}" --output json)"
  actual_lifecycle="$(jq -S '.Rules|sort_by(.ID)' <<<"${actual_lifecycle_response}")"
  actual_lifecycle_transition_default="$(jq -r '.TransitionDefaultMinimumObjectSize // ""' <<<"${actual_lifecycle_response}")"
  expected_policy="$(jq -S . "${POLICY_FILE}")"
  expected_tags="$(jq -S '.TagSet|sort_by(.Key)' "${TAGGING_FILE}")"
  expected_lifecycle="$(jq -S '.Rules|sort_by(.ID)' "${LIFECYCLE_FILE}")"
  actual_region="$(jq -r '.LocationConstraint // "us-east-1"' <<<"${actual_location}")"
  if [[ "${actual_region}" == "${AWS_REGION}" && "${actual_versioning}" == "Enabled" &&
        "$(jq -r '.ObjectLockConfiguration.ObjectLockEnabled' <<<"${actual_lock}")" == "Enabled" &&
        "$(jq -r '.ObjectLockConfiguration.Rule.DefaultRetention.Mode' <<<"${actual_lock}")" == "${RETENTION_MODE}" &&
        "$(jq -r '.ObjectLockConfiguration.Rule.DefaultRetention.Days' <<<"${actual_lock}")" == "${RETENTION_DAYS}" &&
        "$(jq -r '[.BlockPublicAcls,.IgnorePublicAcls,.BlockPublicPolicy,.RestrictPublicBuckets]|all' <<<"${actual_public}")" == "true" &&
        "${actual_ownership}" == "BucketOwnerEnforced" && "${actual_encryption}" == "AES256" &&
        "${actual_policy}" == "${expected_policy}" && "${actual_tags}" == "${expected_tags}" &&
        "${actual_lifecycle}" == "${expected_lifecycle}" &&
        "${actual_lifecycle_transition_default}" == "${LIFECYCLE_TRANSITION_DEFAULT}" ]]; then
    verify_error=''
    break
  fi
  verify_error="verification did not converge on attempt ${attempt}"
  sleep 2
done
if [[ -n "${verify_error}" ]]; then
  echo "${verify_error}; inspect rollback bundle ${backup}" >&2
  exit 7
fi

echo "Object-locked calibration evidence store verified."
echo "Rollback evidence: ${backup}"
echo "No evidence object was uploaded; an empty newly created bucket remains removable."
