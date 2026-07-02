#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# arduino-cli/setup.sh
#
# Sets up the arduino-cli environment for compiling ESP32 PQC firmware.
# This is idempotent — safe to run multiple times.
#
# Prerequisites:
#   - arduino-cli installed (brew install arduino-cli)
#
# What this does:
#   1. Updates board/library indexes
#   2. Installs ESP32 board package (if not already installed)
#   3. Installs PubSubClient library
#   4. Installs wolfSSL library
#   5. Applies the PQC user_settings.h to wolfSSL
#
# After running, compile with:
#   arduino-cli --config-file arduino-cli/arduino-cli.yaml compile \
#     -b esp32:esp32:XIAO_ESP32S3 --export-binaries esp32-arduino-bundle/aws_iot_pqc_esp32/
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/arduino-cli.yaml"

echo "═══════════════════════════════════════════════════════════════"
echo "  Arduino CLI — ESP32 PQC Environment Setup"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Config: $CONFIG"
echo ""

CLI="arduino-cli --config-file $CONFIG"

# Check arduino-cli is installed
if ! command -v arduino-cli &>/dev/null; then
    echo "  ❌ arduino-cli not found. Install with:"
    echo "     brew install arduino-cli"
    echo "     # or: curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh"
    exit 1
fi

echo "[1/5] Updating indexes..."
$CLI core update-index 2>&1 | grep -v "^$" | sed 's/^/       /'

echo "[2/5] Installing ESP32 board package..."
if $CLI core list 2>/dev/null | grep -q "esp32:esp32"; then
    echo "       ⏭️  Already installed"
else
    $CLI core install esp32:esp32 2>&1 | tail -3 | sed 's/^/       /'
fi

echo "[3/5] Installing PubSubClient..."
if $CLI lib list 2>/dev/null | grep -q "PubSubClient"; then
    echo "       ⏭️  Already installed"
else
    $CLI lib install "PubSubClient" 2>&1 | sed 's/^/       /'
fi

echo "[4/5] Installing wolfSSL..."
if $CLI lib list 2>/dev/null | grep -q "wolfssl"; then
    echo "       ⏭️  Already installed"
else
    $CLI lib install "wolfssl" 2>&1 | sed 's/^/       /'
fi

echo "[5/5] Applying PQC user_settings.h to wolfSSL..."
WOLF_SETTINGS="$SCRIPT_DIR/user/libraries/wolfssl/src/user_settings.h"
PQC_SETTINGS="$SCRIPT_DIR/wolfssl_user_settings_arduino.h"

if [[ -f "$PQC_SETTINGS" ]]; then
    cp "$PQC_SETTINGS" "$WOLF_SETTINGS"
    echo "       ✅ PQC config applied"
else
    echo "       ❌ Missing: $PQC_SETTINGS"
    exit 1
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ Setup complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Compile PQC firmware:"
echo "    arduino-cli --config-file arduino-cli/arduino-cli.yaml compile \\"
echo "      -b esp32:esp32:XIAO_ESP32S3 --export-binaries \\"
echo "      esp32-arduino-bundle/aws_iot_pqc_esp32/"
echo ""
echo "  Compile classical firmware:"
echo "    arduino-cli --config-file arduino-cli/arduino-cli.yaml compile \\"
echo "      -b esp32:esp32:XIAO_ESP32S3 --export-binaries \\"
echo "      esp32-arduino-bundle/aws_iot_pqc_esp32_v1_classical/"
echo ""
echo "  Upload (with device connected):"
echo "    arduino-cli --config-file arduino-cli/arduino-cli.yaml upload \\"
echo "      -b esp32:esp32:XIAO_ESP32S3 -p /dev/cu.usbmodem* \\"
echo "      esp32-arduino-bundle/aws_iot_pqc_esp32/"
echo ""
