#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${ROOT}/apps/perception"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV="${VENV:-}"

if [[ -n "${VENV}" ]]; then
  # shellcheck disable=SC1090
  source "${VENV}"
fi

cd "${APP_DIR}"
mkdir -p output
export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

exec "${PYTHON_BIN}" process_video.py
