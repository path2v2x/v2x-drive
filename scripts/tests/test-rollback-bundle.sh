#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'chmod -R u+rwX "$TMP" 2>/dev/null || true; rm -rf "$TMP"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

mkdir -p \
  "$TMP/live" "$TMP/bin" "$TMP/assets" "$TMP/systemd" \
  "$TMP/config" "$TMP/backups"

git -C "$TMP/live" init -q
git -C "$TMP/live" config user.name test
git -C "$TMP/live" config user.email test@example.invalid
printf 'base\n' >"$TMP/live/tracked.txt"
printf 'staged-base\n' >"$TMP/live/staged.txt"
git -C "$TMP/live" add tracked.txt staged.txt
git -C "$TMP/live" commit -qm base
printf 'staged-change\n' >"$TMP/live/staged.txt"
git -C "$TMP/live" add staged.txt
printf 'unstaged-change\n' >"$TMP/live/tracked.txt"
printf 'untracked\n' >"$TMP/live/untracked.txt"

cat >"$TMP/bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  is-active) exit 1 ;;
  cat) printf '# mocked unit definitions\n' ;;
  show) printf 'Id=v2x-mocked.service\nActiveState=active\n' ;;
  *) exit 2 ;;
esac
MOCK

cat >"$TMP/bin/docker" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *" {{.Image}} "* ]]; then
  printf 'sha256:test-image\n'
elif [[ " $* " == *" {{.State.Pid}} "* ]]; then
  printf '12345\n'
else
  printf '[{"Name":"/carla-rr-maps","Image":"sha256:test-image"}]\n'
fi
MOCK

cat >"$TMP/bin/perception-python" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == --version ]]; then
  printf 'Python 3.12.test\n'
elif [[ " $* " == *" pip freeze "* ]]; then
  printf 'example==1.0\n'
elif [[ " $* " == *" pip check "* ]]; then
  printf 'No broken requirements found.\n'
else
  exit 2
fi
MOCK
chmod +x "$TMP/bin/systemctl" "$TMP/bin/docker" "$TMP/bin/perception-python"

for asset in yolo.pt mobilenet.pth convnext.pth; do
  printf '%s\n' "$asset" >"$TMP/assets/$asset"
done
printf '[Unit]\nDescription=test\n' >"$TMP/systemd/v2x-test.service"
printf 'SAFE_TEST_VALUE=true\n' >"$TMP/config/v2x-perception.env"

common_env=(
  PATH="$TMP/bin:$PATH"
  LIVE_ROOT="$TMP/live"
  BACKUP_ROOT="$TMP/backups"
  STAMP=20260711T010000Z
  SYSTEMCTL=systemctl
  DOCKER=docker
  SUDO_CMD=
  PERCEPTION_PYTHON="$TMP/bin/perception-python"
  YOLO_MODEL="$TMP/assets/yolo.pt"
  MOBILENET_MODEL="$TMP/assets/mobilenet.pth"
  CONVNEXT_MODEL="$TMP/assets/convnext.pth"
  SYSTEMD_ROOT="$TMP/systemd"
  CONFIG_ROOT="$TMP/config"
  CAPTURE_UE5_BINARY=false
)

before="$(find "$TMP/backups" -mindepth 1 -maxdepth 1 | wc -l)"
env "${common_env[@]}" ACTION=plan \
  "$ROOT/scripts/capture-v2x-rollback.sh" >"$TMP/plan.txt"
after="$(find "$TMP/backups" -mindepth 1 -maxdepth 1 | wc -l)"
[[ "$before" == "$after" ]] || fail "plan mode created a backup"
grep -Fq 'MUTATES=none' "$TMP/plan.txt" || fail "plan is not explicit"

capture_output="$(env "${common_env[@]}" ACTION=capture \
  "$ROOT/scripts/capture-v2x-rollback.sh")"
bundle="${capture_output#ROLLBACK_BUNDLE=}"
[[ -d "$bundle" && "$(stat -c '%a' "$bundle")" == 700 ]] \
  || fail "capture did not create a mode-0700 bundle"
[[ "$(<"$TMP/live/tracked.txt")" == unstaged-change ]] \
  || fail "capture changed the live checkout"

env "${common_env[@]}" ACTION=verify BUNDLE="$bundle" \
  "$ROOT/scripts/capture-v2x-rollback.sh" >"$TMP/verify.txt"
grep -Fq "VERIFIED_BUNDLE=$bundle" "$TMP/verify.txt" \
  || fail "restore rehearsal did not verify"

printf 'tampered\n' >>"$bundle/repository/live-head.txt"
if env "${common_env[@]}" ACTION=verify BUNDLE="$bundle" \
  "$ROOT/scripts/capture-v2x-rollback.sh" >/dev/null 2>&1; then
  fail "tampered bundle passed verification"
fi

echo "rollback bundle tests passed"
