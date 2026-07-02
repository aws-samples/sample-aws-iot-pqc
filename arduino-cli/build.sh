#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# arduino-cli/build.sh
#
# Compiles and optionally uploads ESP32 PQC firmware using arduino-cli.
#
# Usage:
#   ./build.sh                    # Compile PQC firmware only
#   ./build.sh --upload           # Compile and upload PQC firmware
#   ./build.sh --classical        # Compile classical firmware only
#   ./build.sh --classical --upload  # Compile and upload classical firmware
#   ./build.sh --all              # Compile both firmwares
#
# Prerequisites:
#   - Run setup.sh first (installs ESP32 core, libraries, wolfSSL config)
#   - For upload: ESP32-S3 connected via USB
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="$SCRIPT_DIR/arduino-cli.yaml"
BUNDLE="$PROJECT_ROOT/esp32-arduino-bundle"

FQBN="esp32:esp32:XIAO_ESP32S3:USBMode=hwcdc,CDCOnBoot=default,DebugLevel=info"
CLI="arduino-cli --config-file $CONFIG"

UPLOAD=false
CLASSICAL=false
ALL=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --upload|-u) UPLOAD=true; shift ;;
        --classical|-c) CLASSICAL=true; shift ;;
        --all|-a) ALL=true; shift ;;
        --help|-h)
            echo "Usage: $0 [--upload] [--classical] [--all]"
            echo ""
            echo "  --upload, -u      Upload after compiling (device must be connected)"
            echo "  --classical, -c   Build classical firmware instead of PQC"
            echo "  --all, -a         Build both PQC and classical firmwares"
            exit 0
            ;;
        *) echo "[ERROR] Unknown: $1"; exit 1 ;;
    esac
done

# Detect port for upload
detect_port() {
    local port=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
    if [[ -z "$port" ]]; then
        echo "[ERROR] No ESP32 USB port found. Is the device connected?"
        exit 1
    fi
    echo "$port"
}

build_sketch() {
    local sketch_dir="$1"
    local sketch_name=$(basename "$sketch_dir")

    echo "═══════════════════════════════════════════════════════════════"
    echo "  Compiling: $sketch_name"
    echo "═══════════════════════════════════════════════════════════════"
    echo ""

    $CLI compile -b "$FQBN" --export-binaries "$sketch_dir" 2>&1

    echo ""
    echo "  ✅ Build complete: $sketch_name"
    echo ""

    if [[ "$UPLOAD" == true ]]; then
        local port=$(detect_port)
        echo "  Uploading to $port..."
        $CLI upload -b "$FQBN" -p "$port" "$sketch_dir" 2>&1
        echo "  ✅ Upload complete"
        echo ""
    fi
}

if [[ "$ALL" == true ]]; then
    build_sketch "$BUNDLE/aws_iot_pqc_esp32"
    build_sketch "$BUNDLE/aws_iot_pqc_esp32_v1_classical"
elif [[ "$CLASSICAL" == true ]]; then
    build_sketch "$BUNDLE/aws_iot_pqc_esp32_v1_classical"
else
    build_sketch "$BUNDLE/aws_iot_pqc_esp32"
fi
