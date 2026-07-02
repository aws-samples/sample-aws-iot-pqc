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

FQBN="esp32:esp32:XIAO_ESP32S3:USBMode=hwcdc,CDCOnBoot=default,DebugLevel=info,PartitionScheme=default_8MB"
CUSTOM_PARTITIONS="$PROJECT_ROOT/esp32-flash/partitions_pqc_workshop.csv"
CLI="arduino-cli --config-file $CONFIG"

UPLOAD=false
CLASSICAL=false
ALL=false
DUAL_BOOT=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --upload|-u) UPLOAD=true; shift ;;
        --classical|-c) CLASSICAL=true; shift ;;
        --all|-a) ALL=true; shift ;;
        --dual-boot|-d) DUAL_BOOT=true; shift ;;
        --help|-h)
            echo "Usage: $0 [--upload] [--classical] [--all] [--dual-boot]"
            echo ""
            echo "  --upload, -u      Upload single firmware (device must be connected)"
            echo "  --classical, -c   Build/upload classical firmware instead of PQC"
            echo "  --all, -a         Build both PQC and classical firmwares"
            echo "  --dual-boot, -d   Build both and flash dual-boot (classical=OTA_0, PQC=OTA_1)"
            echo ""
            echo "Examples:"
            echo "  $0                 # just compile PQC"
            echo "  $0 --upload        # compile + flash PQC"
            echo "  $0 --classical -u  # compile + flash classical"
            echo "  $0 --dual-boot     # compile both + flash dual-boot partitions"
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

    $CLI compile -b "$FQBN" --build-property "build.partitions=pqc_workshop" --build-property "upload.maximum_size=3670016" --export-binaries "$sketch_dir" 2>&1

    echo ""
    echo "  ✅ Build complete: $sketch_name"
    echo ""

    if [[ "$UPLOAD" == true && "$DUAL_BOOT" == false ]]; then
        local port=$(detect_port)
        echo "  Uploading to $port..."
        $CLI upload -b "$FQBN" -p "$port" "$sketch_dir" 2>&1
        echo "  ✅ Upload complete"
        echo ""
    fi
}

flash_dual_boot() {
    local port=$(detect_port)
    local classical_bin="$BUNDLE/aws_iot_pqc_esp32_v1_classical/build/esp32.esp32.XIAO_ESP32S3/aws_iot_pqc_esp32_v1_classical.ino.bin"
    local pqc_bin="$BUNDLE/aws_iot_pqc_esp32/build/esp32.esp32.XIAO_ESP32S3/aws_iot_pqc_esp32.ino.bin"
    local flash_script="$PROJECT_ROOT/esp32-flash/flash-dual-boot.sh"

    if [[ ! -f "$classical_bin" ]]; then
        echo "[ERROR] Classical binary not found: $classical_bin"
        echo "        Run: $0 --all  first"
        exit 1
    fi
    if [[ ! -f "$pqc_bin" ]]; then
        echo "[ERROR] PQC binary not found: $pqc_bin"
        echo "        Run: $0 --all  first"
        exit 1
    fi

    # Ensure partition table binary exists
    local part_bin="$PROJECT_ROOT/esp32-flash/partitions_pqc_workshop.bin"
    if [[ ! -f "$part_bin" ]]; then
        local gen_tool=$(find "$SCRIPT_DIR/data" -name "gen_esp32part.py" 2>/dev/null | head -1)
        if [[ -n "$gen_tool" ]]; then
            python3 "$gen_tool" "$PROJECT_ROOT/esp32-flash/partitions_pqc_workshop.csv" "$part_bin"
        else
            echo "[ERROR] No partition binary and gen_esp32part.py not found"
            exit 1
        fi
    fi
    # Copy partition bin to flash script directory so it can find it
    cp "$part_bin" "$(dirname "$flash_script")/partitions_pqc_workshop.bin" || true

    echo "═══════════════════════════════════════════════════════════════"
    echo "  Flashing Dual-Boot (OTA_0=Classical, OTA_1=PQC)"
    echo "═══════════════════════════════════════════════════════════════"
    echo ""
    echo "  Port:        $port"
    echo "  Classical:   $(basename $classical_bin)"
    echo "  PQC:         $(basename $pqc_bin)"
    echo ""

    "$flash_script" "$port" "$classical_bin" "$pqc_bin"
}

if [[ "$DUAL_BOOT" == true ]]; then
    # Build both, then flash dual-boot
    build_sketch "$BUNDLE/aws_iot_pqc_esp32"
    build_sketch "$BUNDLE/aws_iot_pqc_esp32_v1_classical"
    flash_dual_boot
elif [[ "$ALL" == true ]]; then
    build_sketch "$BUNDLE/aws_iot_pqc_esp32"
    build_sketch "$BUNDLE/aws_iot_pqc_esp32_v1_classical"
elif [[ "$CLASSICAL" == true ]]; then
    build_sketch "$BUNDLE/aws_iot_pqc_esp32_v1_classical"
else
    build_sketch "$BUNDLE/aws_iot_pqc_esp32"
fi
