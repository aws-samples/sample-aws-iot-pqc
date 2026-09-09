#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run-tests.sh — Fast, mocked unit-test suite for the workshop's Python code.
#
# Runs Lambda handler tests, Step Functions definition tests, and the
# Builder-Pi CLI tests with all AWS services mocked (moto). No live AWS calls.
#
# Usage:
#   ./run-tests.sh                 # fast unit suite (default)
#   ./run-tests.sh -m integration  # opt-in integration tests (real AWS)
#   ./run-tests.sh -k publish_ota   # pass through any pytest args
#
# Sets up/uses a local .venv automatically.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
if [[ ! -d "$VENV" ]]; then
    echo "[run-tests] creating virtualenv at $VENV"
    python3 -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

# Install dev deps if pytest/moto are missing.
if ! python -c "import pytest, moto" >/dev/null 2>&1; then
    echo "[run-tests] installing dev dependencies"
    pip install -q --disable-pip-version-check -r requirements-dev.txt
fi

echo "[run-tests] running unit suite"
exec pytest "$@"
