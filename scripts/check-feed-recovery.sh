#!/usr/bin/env bash
set -euo pipefail

# Detects perception feed recovery after an upstream outage and runs the
# post-recovery validation pipeline exactly once per recovery: the
# dependency-light live-feed gate, an observational phase-4 twin probe, and
# a bounded latency-baseline capture. Evidence is archived under
# v2x-evidence/perception/. While feeds are down this exits 0 quietly
# (link-health already alerts on the outage); a failed validation fails the
# unit, which the v2x-alert@ drop-in escalates. Mutating gates (the PR 54
# parity canary, the long upload watch) stay operator-initiated per the
# controlled deployment doctrine.

LIVE="${LIVE:-/home/path/V2XCarla/v2x-backend}"
STATE_DIR="${STATE_DIR:-/var/lib/v2x-feed-recovery}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8090/health}"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-/home/path/V2XCarla/v2x-evidence/perception}"
LATENCY_CAPTURE_SECONDS="${LATENCY_CAPTURE_SECONDS:-600}"
PERCEPTION_PY=/home/path/V2XCarla/perception-venv/bin/python
BRIDGE_PY=/home/path/V2XCarla/carla-venv-310/bin/python

mkdir -p "$STATE_DIR"

health="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null || true)"
ready="$(jq -r '.ready' <<<"$health" 2>/dev/null || echo false)"

if [[ "$ready" != "true" ]]; then
  # Outage (or perception restart) in progress: re-arm so the next
  # transition back to ready triggers a fresh validation.
  rm -f "$STATE_DIR/validated"
  exit 0
fi

if [[ -f "$STATE_DIR/validated" ]]; then
  exit 0
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
E="$EVIDENCE_ROOT/${STAMP}-post-recovery-validation"
mkdir -p "$E"

fail() {
  echo "POST_RECOVERY_VALIDATION_FAIL stage=$1 evidence=$E"
  exit 1
}

"$PERCEPTION_PY" "$LIVE/apps/perception/tools/verify_live_feeds.py" \
  http://127.0.0.1:8090 >"$E/feeds.json" 2>"$E/feeds.stderr" || fail feeds

PYTHONPATH="$LIVE/apps/bridge" "$BRIDGE_PY" \
  "$LIVE/apps/bridge/tools/verify_phase4_live.py" \
  >"$E/phase4.json" 2>"$E/phase4.stderr" || fail phase4
jq -e '.ok==true' "$E/phase4.json" >/dev/null || fail phase4_ok

"$PERCEPTION_PY" "$LIVE/apps/perception/tools/latency_baseline.py" \
  --duration "$LATENCY_CAPTURE_SECONDS" \
  --output "$E/latency-baseline.json" \
  >"$E/latency-baseline.stdout" 2>"$E/latency-baseline.stderr" \
  || fail latency

touch "$STATE_DIR/validated"
echo "POST_RECOVERY_VALIDATION_PASS evidence=$E"
