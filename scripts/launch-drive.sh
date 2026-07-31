#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# Digital Twin Platform — Drive Server Launcher
#
# Starts the Drive Server: a WebSocket service that lets a client
# drive a vehicle in CARLA, records sessions, and reconstructs
# scenes from V2X detection history.
#
# CARLA must already be running on port 2000 before invoking this
# script.
#
# NOTE: Mutually exclusive with the observation bridge — do not
# run both against the same CARLA instance.
# ─────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_DIR="${REPO_ROOT}/apps/bridge"

# ── Configurable ──
VENV="${VENV:-/home/path/V2XCarla/carla-venv-310/bin/activate}"
AWS_PROFILE="${AWS_PROFILE:-Path-Emerging-Dev-147229569658}"
CARLA_PORT="${CARLA_PORT:-2000}"
WS_PORT="${WS_PORT:-8765}"

DRIVE_PID=""
SHUTTING_DOWN=0

cleanup() {
    local status=$?
    trap - INT TERM EXIT
    echo ""
    echo "Shutting down..."
    if [ -n "$DRIVE_PID" ]; then
        echo "  Stopping drive server (PID $DRIVE_PID)..."
        kill "$DRIVE_PID" 2>/dev/null
        wait "$DRIVE_PID" 2>/dev/null || true
    fi
    echo "Done."
    if [ "$SHUTTING_DOWN" = "1" ]; then
        exit 0
    fi
    exit "$status"
}

request_shutdown() {
    SHUTTING_DOWN=1
    cleanup
}

trap request_shutdown INT TERM
trap cleanup EXIT

# ─────────────────────────────────────────────────────────────
# 1. Check AWS credentials
# ─────────────────────────────────────────────────────────────
echo "Checking AWS credentials..."
if ! aws sts get-caller-identity --profile "$AWS_PROFILE" &>/dev/null; then
    echo "  ERROR: AWS credentials for profile '$AWS_PROFILE' are not valid."
    exit 1
fi
echo "  AWS OK"

# ─────────────────────────────────────────────────────────────
# 2. Activate venv
# ─────────────────────────────────────────────────────────────
echo "Activating Python venv..."
if [ ! -f "$VENV" ]; then
    echo "  ERROR: venv not found at $VENV"
    echo "  Set VENV env var to point to your carla venv activate script."
    exit 1
fi
# Deployment selects the absolute VENV path.
# shellcheck disable=SC1090
source "$VENV"

# ─────────────────────────────────────────────────────────────
# 3. Verify CARLA is already running
# ─────────────────────────────────────────────────────────────
echo "Checking CARLA on port $CARLA_PORT..."
if python3 -c "
import carla
c = carla.Client('localhost', $CARLA_PORT)
c.set_timeout(3.0)
w = c.get_world()
w.get_map()
w.get_blueprint_library()
" &>/dev/null; then
    echo "  CARLA OK"
else
    echo "  ERROR: CARLA is not running on port $CARLA_PORT."
    echo "  Start the UE4 Editor manually, then re-run this script."
    exit 1
fi

# ─────────────────────────────────────────────────────────────
# 4. Start Drive Server
# ─────────────────────────────────────────────────────────────
echo "Starting Drive Server..."
cd "$BRIDGE_DIR"
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
export AWS_PROFILE
export DTB_CARLA_HOST="${CARLA_HOST:-localhost}"
export DTB_CARLA_PORT="$CARLA_PORT"
export DTB_WS_PORT="$WS_PORT"
python -m digital_twin_bridge.drive_main &
DRIVE_PID=$!
echo "  Drive server PID: $DRIVE_PID"

# ─────────────────────────────────────────────────────────────
# Ready
# ─────────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  Drive Server is running"
echo "============================================"
echo ""
echo "  Repo     : $REPO_ROOT"
echo "  Bridge   : $BRIDGE_DIR"
echo "  CARLA    : localhost:$CARLA_PORT"
echo "  Drive WS : ws://0.0.0.0:$WS_PORT"
echo "  PID      : $DRIVE_PID"
echo ""
echo "  Press Ctrl+C to stop."
echo ""

wait "$DRIVE_PID"
