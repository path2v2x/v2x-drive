#!/usr/bin/env bash
set -euo pipefail

# Daily green check: archives a dated evidence directory with the
# dependency-light four-feed gate and an observational phase-4 probe.
# Failure fails the unit and escalates through the v2x-alert@ drop-in.
# This produces the per-day evidence stream for the roadmap's 7-day soak
# (M12) without any mutating operations.

LIVE="${LIVE:-/home/path/V2XCarla/v2x-backend}"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-/home/path/V2XCarla/v2x-evidence/perception}"
PERCEPTION_PY=/home/path/V2XCarla/perception-venv/bin/python
BRIDGE_PY=/home/path/V2XCarla/carla-venv-310/bin/python

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
E="$EVIDENCE_ROOT/${STAMP}-daily-verify"
mkdir -p "$E"

fail() {
  echo "DAILY_VERIFY_FAIL stage=$1 evidence=$E"
  exit 1
}

"$PERCEPTION_PY" "$LIVE/apps/perception/tools/verify_live_feeds.py" \
  http://127.0.0.1:8090 >"$E/feeds.json" 2>"$E/feeds.stderr" || fail feeds

PYTHONPATH="$LIVE/apps/bridge" "$BRIDGE_PY" \
  "$LIVE/apps/bridge/tools/verify_phase4_live.py" \
  >"$E/phase4.json" 2>"$E/phase4.stderr" || fail phase4
jq -e '.ok==true' "$E/phase4.json" >/dev/null || fail phase4_ok

echo "DAILY_VERIFY_PASS evidence=$E"
