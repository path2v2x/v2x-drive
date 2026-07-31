#!/usr/bin/env bash
set -euo pipefail

umask 077

ACTION="${ACTION:-plan}"
LIVE_ROOT="${LIVE_ROOT:-/home/path/V2XCarla/v2x-backend}"
BACKUP_ROOT="${BACKUP_ROOT:-/home/path/V2XCarla/v2x-backend-backups}"
BUNDLE="${BUNDLE:-}"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
CARLA_CONTAINER="${CARLA_CONTAINER:-carla-rr-maps}"
PERCEPTION_PYTHON="${PERCEPTION_PYTHON:-/home/path/V2XCarla/perception-venv/bin/python}"
YOLO_MODEL="${YOLO_MODEL:-${LIVE_ROOT}/apps/perception/yolov8n.pt}"
MOBILENET_MODEL="${MOBILENET_MODEL:-/home/path/.cache/torch/hub/checkpoints/mobilenet_v3_small-047dcff4.pth}"
CONVNEXT_MODEL="${CONVNEXT_MODEL:-/home/path/.cache/torch/hub/checkpoints/convnext_base-6075fbad.pth}"
SYSTEMD_ROOT="${SYSTEMD_ROOT:-/etc/systemd/system}"
CONFIG_ROOT="${CONFIG_ROOT:-/etc}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
DOCKER="${DOCKER:-docker}"
SUDO_CMD="${SUDO_CMD-sudo}"
REQUIRE_TIMERS_STOPPED="${REQUIRE_TIMERS_STOPPED:-true}"
CAPTURE_UE5_BINARY="${CAPTURE_UE5_BINARY:-true}"

TIMERS=(
  v2x-drive-link-health.timer
  v2x-perception-link-health.timer
  v2x-hourly-drive-restart.timer
)

UNITS=(
  v2x-carla-rr.service
  v2x-drive.service
  v2x-web.service
  v2x-perception.service
  v2x-cloudflared-drive.service
  v2x-cloudflared-perception.service
  v2x-drive-link-health.service
  v2x-drive-link-health.timer
  v2x-perception-link-health.service
  v2x-perception-link-health.timer
  v2x-hourly-drive-restart.service
  v2x-hourly-drive-restart.timer
)

CONFIG_FILES=(
  v2x-carla-rr.env
  v2x-perception.env
  v2x-drive-tunnel.env
  v2x-perception-tunnel.env
  v2x-drive-link-health.env
  v2x-perception-link-health.env
  v2x-drive-restart.env
)

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

run_privileged() {
  if [[ -n "$SUDO_CMD" ]]; then
    "$SUDO_CMD" "$@"
  else
    "$@"
  fi
}

copy_privileged_file() {
  local source="$1"
  local destination="$2"
  if [[ -n "$SUDO_CMD" ]]; then
    "$SUDO_CMD" cat -- "$source" >"$destination"
  else
    cat -- "$source" >"$destination"
  fi
  chmod 0600 "$destination"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command is unavailable: $1"
}

require_capture_prerequisites() {
  require_command git
  require_command jq
  require_command tar
  require_command sha256sum
  require_command "$SYSTEMCTL"
  require_command "$DOCKER"
  [[ -d "$LIVE_ROOT/.git" || -f "$LIVE_ROOT/.git" ]] \
    || fail "live root is not a Git checkout: $LIVE_ROOT"
  [[ -x "$PERCEPTION_PYTHON" ]] \
    || fail "perception Python is not executable: $PERCEPTION_PYTHON"
  for asset in "$YOLO_MODEL" "$MOBILENET_MODEL" "$CONVNEXT_MODEL"; do
    run_privileged test -f "$asset" \
      || fail "required runtime asset is missing: $asset"
  done
}

require_quiesced_timers() {
  [[ "$REQUIRE_TIMERS_STOPPED" == true ]] || return 0
  local timer
  for timer in "${TIMERS[@]}"; do
    if "$SYSTEMCTL" is-active --quiet "$timer"; then
      fail "mutation-capable timer is still active: $timer"
    fi
  done
}

capture_asset() {
  local label="$1"
  local source="$2"
  local destination="$3"
  run_privileged sha256sum "$source" >"${destination}/${label}.sha256"
  copy_privileged_file "$source" "${destination}/${label}"
}

capture_bundle() {
  require_capture_prerequisites
  require_quiesced_timers

  local final="${BACKUP_ROOT}/v2x-rollback-${STAMP}"
  local partial="${final}.incomplete.$$"
  [[ ! -e "$final" && ! -e "$partial" ]] \
    || fail "rollback destination already exists: $final"
  install -d -m 0700 "$BACKUP_ROOT"
  install -d -m 0700 \
    "$partial" "$partial/repository" "$partial/runtime" \
    "$partial/systemd" "$partial/config"
  trap 'rm -rf -- "${partial:-}"' EXIT

  git -C "$LIVE_ROOT" status --short --branch \
    >"$partial/repository/live-status.txt"
  git -C "$LIVE_ROOT" status --porcelain=v1 -z --untracked-files=all \
    >"$partial/repository/live-status-porcelain.zlist"
  git -C "$LIVE_ROOT" rev-parse HEAD \
    >"$partial/repository/live-head.txt"
  git -C "$LIVE_ROOT" diff --binary \
    >"$partial/repository/live-unstaged.patch"
  git -C "$LIVE_ROOT" diff --cached --binary \
    >"$partial/repository/live-staged.patch"
  git -C "$LIVE_ROOT" ls-files --others --exclude-standard -z \
    >"$partial/repository/untracked-files.zlist"
  tar -C "$LIVE_ROOT" --null --verbatim-files-from --no-recursion \
    --files-from="$partial/repository/untracked-files.zlist" \
    -cpf "$partial/repository/untracked-files.tar"

  "$DOCKER" inspect "$CARLA_CONTAINER" \
    >"$partial/runtime/carla-container.inspect.json"
  "$DOCKER" inspect -f '{{.Image}}' "$CARLA_CONTAINER" \
    >"$partial/runtime/carla-image-id.txt"
  "$DOCKER" inspect -f '{{.State.Pid}}' "$CARLA_CONTAINER" \
    >"$partial/runtime/carla-container-pid.txt"

  if [[ "$CAPTURE_UE5_BINARY" == true ]]; then
    local container_pid ue5_binary
    container_pid="$(<"$partial/runtime/carla-container-pid.txt")"
    [[ "$container_pid" =~ ^[1-9][0-9]*$ ]] \
      || fail "CARLA container has no live PID"
    ue5_binary="/proc/${container_pid}/root/home/carla/CarlaUnreal/Binaries/Linux/CarlaUnreal-Linux-Shipping"
    run_privileged sha256sum "$ue5_binary" \
      >"$partial/runtime/ue5-binary.sha256"
    run_privileged strings -a "$ue5_binary" \
      | awk 'index($0, "/UnrealEngine5/") {found=1} END {exit !found}' \
      || fail "CARLA binary does not contain the UnrealEngine5 marker"
  fi

  capture_asset yolov8n.pt "$YOLO_MODEL" "$partial/runtime"
  capture_asset mobilenet_v3_small.pth "$MOBILENET_MODEL" "$partial/runtime"
  capture_asset convnext_base.pth "$CONVNEXT_MODEL" "$partial/runtime"
  "$PERCEPTION_PYTHON" --version \
    >"$partial/runtime/perception-python-version.txt" 2>&1
  "$PERCEPTION_PYTHON" -m pip freeze \
    >"$partial/runtime/perception-pip-freeze.txt"
  "$PERCEPTION_PYTHON" -m pip check \
    >"$partial/runtime/perception-pip-check.txt"

  "$SYSTEMCTL" cat "${UNITS[@]}" \
    >"$partial/systemd/installed-units.txt" 2>&1
  "$SYSTEMCTL" show "${UNITS[@]}" \
    --property=Id,ActiveState,SubState,UnitFileState,FragmentPath,MainPID,ExecMainStartTimestamp,NextElapseUSecRealtime \
    >"$partial/systemd/unit-state.txt"

  local unit_file config_file
  while IFS= read -r -d '' unit_file; do
    run_privileged sha256sum "$unit_file"
  done < <(run_privileged find "$SYSTEMD_ROOT" -maxdepth 1 -type f -name 'v2x-*' -print0) \
    >"$partial/systemd/installed-unit-files.sha256"

  for config_file in "${CONFIG_FILES[@]}"; do
    if run_privileged test -f "$CONFIG_ROOT/$config_file"; then
      copy_privileged_file \
        "$CONFIG_ROOT/$config_file" "$partial/config/$config_file"
    fi
  done

  jq -n \
    --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg live_root "$LIVE_ROOT" \
    --arg head "$(<"$partial/repository/live-head.txt")" \
    --arg container "$CARLA_CONTAINER" \
    --argjson timers_required_stopped "$REQUIRE_TIMERS_STOPPED" \
    '{schema_version:1,created_at:$created_at,live_root:$live_root,head:$head,carla_container:$container,ue_runtime:"5.5.0-0+UE5",timers_required_stopped:$timers_required_stopped}' \
    >"$partial/metadata.json"

  local manifest_files="$partial/.manifest-files.zlist"
  (
    cd "$partial"
    find . -type f ! -name MANIFEST.sha256 \
      ! -name .manifest-files.zlist -print0 | sort -z \
      >"$manifest_files"
    xargs -0 sha256sum <"$manifest_files" >MANIFEST.sha256
  )
  rm -f -- "$manifest_files"
  find "$partial" -type d -exec chmod 0700 {} +
  find "$partial" -type f -exec chmod 0600 {} +
  mv -- "$partial" "$final"
  trap - EXIT
  printf 'ROLLBACK_BUNDLE=%s\n' "$final"
}

verify_bundle() {
  [[ -n "$BUNDLE" ]] || fail "ACTION=verify requires BUNDLE"
  [[ -d "$BUNDLE" && ! -L "$BUNDLE" ]] \
    || fail "rollback bundle is not a real directory: $BUNDLE"
  [[ "$(stat -c '%a' "$BUNDLE")" == 700 ]] \
    || fail "rollback bundle must have mode 0700"
  [[ -f "$BUNDLE/MANIFEST.sha256" && -f "$BUNDLE/metadata.json" ]] \
    || fail "rollback bundle manifest or metadata is missing"
  (cd "$BUNDLE" && sha256sum -c MANIFEST.sha256 >/dev/null)
  jq -e '.schema_version == 1 and .ue_runtime == "5.5.0-0+UE5"' \
    "$BUNDLE/metadata.json" >/dev/null

  local archive="$BUNDLE/repository/untracked-files.tar"
  [[ -f "$archive" ]] || fail "untracked-file archive is missing"
  if tar -tf "$archive" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
    fail "untracked-file archive contains an unsafe path"
  fi

  local source_root captured_head rehearsal restored_status
  source_root="$(jq -er '.live_root' "$BUNDLE/metadata.json")"
  captured_head="$(jq -er '.head' "$BUNDLE/metadata.json")"
  [[ -d "$source_root/.git" || -f "$source_root/.git" ]] \
    || fail "captured live root is no longer a Git checkout"
  rehearsal="$(mktemp -d)"
  trap 'rm -rf -- "${rehearsal:-}"' EXIT
  git clone --no-hardlinks --quiet "$source_root" "$rehearsal/repository"
  git -C "$rehearsal/repository" -c advice.detachedHead=false \
    checkout --detach --quiet "$captured_head"
  if [[ -s "$BUNDLE/repository/live-staged.patch" ]]; then
    git -C "$rehearsal/repository" apply --index \
      "$BUNDLE/repository/live-staged.patch"
  fi
  if [[ -s "$BUNDLE/repository/live-unstaged.patch" ]]; then
    git -C "$rehearsal/repository" apply \
      "$BUNDLE/repository/live-unstaged.patch"
  fi
  tar -C "$rehearsal/repository" --no-same-owner --no-same-permissions \
    -xpf "$archive"
  restored_status="$rehearsal/restored-status.zlist"
  git -C "$rehearsal/repository" status --porcelain=v1 -z --untracked-files=all \
    >"$restored_status"
  cmp -s "$BUNDLE/repository/live-status-porcelain.zlist" "$restored_status" \
    || fail "isolated repository restore does not reproduce captured Git state"
  rm -rf -- "$rehearsal"
  trap - EXIT
  printf 'VERIFIED_BUNDLE=%s\n' "$BUNDLE"
}

show_plan() {
  printf 'ACTION=plan\n'
  printf 'LIVE_ROOT=%s\n' "$LIVE_ROOT"
  printf 'BACKUP_ROOT=%s\n' "$BACKUP_ROOT"
  printf 'REQUIRES_STOPPED_TIMERS=%s\n' "${TIMERS[*]}"
  printf 'CAPTURES=git-patches,untracked-files,UE5-container-and-binary,model-assets,python-environment,systemd-units,private-runtime-config\n'
  printf 'MUTATES=none\n'
}

case "$ACTION" in
  plan) show_plan ;;
  capture) capture_bundle ;;
  verify) verify_bundle ;;
  *) fail "ACTION must be plan, capture, or verify" ;;
esac
