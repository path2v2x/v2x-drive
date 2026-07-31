#!/usr/bin/env bash
set -euo pipefail

# Sends a bounded, debounced operations alert when a supervised V2X unit
# fails, and a single recovery notice when it next succeeds. Every alert
# lands in the journal and a per-unit state file; an optional webhook
# (Slack-compatible JSON, configured in /etc/v2x-alerts.env) is used when
# present. Webhook delivery failure never propagates: alerting must not
# create new failures.

MODE="${1:?usage: notify-alert.sh failure|recovered <unit-name>}"
UNIT="${2:?usage: notify-alert.sh failure|recovered <unit-name>}"

STATE_DIR="${STATE_DIR:-/var/lib/v2x-alerts}"
DEBOUNCE_SECONDS="${V2X_ALERT_DEBOUNCE_SECONDS:-3600}"
WEBHOOK_URL="${V2X_ALERT_WEBHOOK_URL:-}"
HOST="$(hostname)"
NOW_EPOCH="$(date +%s)"
NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$STATE_DIR"
SAFE_UNIT="$(printf '%s' "$UNIT" | tr -c 'A-Za-z0-9_.-' '_')"
STATE_FILE="$STATE_DIR/${SAFE_UNIT}.alerted"

post_webhook() {
  local text="$1"
  [[ -z "$WEBHOOK_URL" ]] && return 0
  local payload
  payload="$(jq -cn --arg text "$text" '{text: $text}')"
  curl -fsS -m 10 -X POST -H 'Content-Type: application/json' \
    --data "$payload" "$WEBHOOK_URL" >/dev/null 2>&1 \
    || echo "alert webhook delivery failed (alert still recorded locally)" >&2
}

case "$MODE" in
  failure)
    if [[ -f "$STATE_FILE" ]]; then
      last="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
      if (( NOW_EPOCH - last < DEBOUNCE_SECONDS )); then
        echo "ALERT (debounced): ${UNIT} still failing on ${HOST} at ${NOW_ISO}"
        exit 0
      fi
    fi
    printf '%s' "$NOW_EPOCH" >"$STATE_FILE"
    msg="ALERT: ${UNIT} failed on ${HOST} at ${NOW_ISO}. Inspect with: journalctl -u ${UNIT}"
    echo "$msg"
    post_webhook "$msg"
    ;;
  recovered)
    # Only speak if a failure alert was previously sent for this unit.
    [[ -f "$STATE_FILE" ]] || exit 0
    rm -f "$STATE_FILE"
    msg="RECOVERED: ${UNIT} healthy again on ${HOST} at ${NOW_ISO}."
    echo "$msg"
    post_webhook "$msg"
    ;;
  *)
    echo "unknown mode: ${MODE} (expected failure|recovered)" >&2
    exit 2
    ;;
esac
