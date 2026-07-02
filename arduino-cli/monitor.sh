#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# arduino-cli/monitor.sh
#
# Opens the serial monitor for the connected ESP32-S3 device.
# Press Ctrl+C to exit.
#
# Usage:
#   ./monitor.sh              # Auto-detect port
#   ./monitor.sh /dev/cu.usbmodem3101   # Specify port
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/arduino-cli.yaml"
FQBN="esp32:esp32:XIAO_ESP32S3"

# Detect or use provided port
PORT="${1:-}"
if [[ -z "$PORT" ]]; then
    PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
    if [[ -z "$PORT" ]]; then
        echo "[ERROR] No ESP32 USB port found. Is the device connected?"
        echo "        Or specify the port: $0 /dev/cu.usbmodemXXXX"
        exit 1
    fi
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  Serial Monitor — $PORT @ 115200 baud"
echo "  Press Ctrl+C to exit"
echo "═══════════════════════════════════════════════════════════════"
echo ""

arduino-cli --config-file "$CONFIG" monitor \
    -p "$PORT" \
    -b "$FQBN" \
    --config 115200
