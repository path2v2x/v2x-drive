#!/usr/bin/env bash
set -euo pipefail

# Installs the V2X feed-loss alerting layer: the v2x-alert@ notifier units
# and OnFailure/OnSuccess drop-ins for the supervised perception units.
# Existing installed units are never overwritten — only drop-ins are added.
# Backups of any file this script replaces are kept under
# v2x-backend-backups/ so the change is reversible.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO_DIR/scripts/systemd"
BACKUP_ROOT="${BACKUP_ROOT:-/home/path/V2XCarla/v2x-backend-backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$BACKUP_ROOT/feed-alerts-$STAMP"

SUDO="sudo"
if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=""
fi

install_file() {
  local src="$1" dst="$2"
  if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
    echo "unchanged: $dst"
    return 0
  fi
  if [[ -f "$dst" ]]; then
    mkdir -p "$BACKUP_DIR"
    cp -a "$dst" "$BACKUP_DIR/"
    echo "backed up: $dst -> $BACKUP_DIR/"
  fi
  $SUDO install -D -m 0644 "$src" "$dst"
  echo "installed: $dst"
}

install_file "$UNIT_SRC/v2x-alert@.service" \
  /etc/systemd/system/v2x-alert@.service
install_file "$UNIT_SRC/v2x-alert-recovered@.service" \
  /etc/systemd/system/v2x-alert-recovered@.service
install_file "$UNIT_SRC/dropins/v2x-perception-link-health.service.d/v2x-alerts.conf" \
  /etc/systemd/system/v2x-perception-link-health.service.d/v2x-alerts.conf
install_file "$UNIT_SRC/dropins/v2x-perception.service.d/v2x-alerts.conf" \
  /etc/systemd/system/v2x-perception.service.d/v2x-alerts.conf
install_file "$UNIT_SRC/v2x-feed-recovery.service" \
  /etc/systemd/system/v2x-feed-recovery.service
install_file "$UNIT_SRC/v2x-feed-recovery.timer" \
  /etc/systemd/system/v2x-feed-recovery.timer
install_file "$UNIT_SRC/v2x-daily-verify.service" \
  /etc/systemd/system/v2x-daily-verify.service
install_file "$UNIT_SRC/v2x-daily-verify.timer" \
  /etc/systemd/system/v2x-daily-verify.timer

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now v2x-feed-recovery.timer v2x-daily-verify.timer
echo "daemon-reload complete; v2x-feed-recovery.timer and v2x-daily-verify.timer enabled."

echo
echo "Optional webhook: put V2X_ALERT_WEBHOOK_URL=https://... in /etc/v2x-alerts.env"
echo "Verify the notifier: systemctl start v2x-alert@install-test.service && journalctl -u v2x-alert@install-test.service -n 3"
