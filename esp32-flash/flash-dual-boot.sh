#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# flash-dual-boot.sh
#
# Flashes both firmware images (classical + PQC) to the XIAO ESP32S3
# in a dual OTA configuration for the PQC workshop.
#
# This script:
#   1. Flashes the custom partition table (dual OTA, 3.5MB per slot)
#   2. Flashes the classical firmware to OTA_0 (boots first)
#   3. Flashes the PQC firmware to OTA_1 (activated by migrate command)
#   4. Sets OTA_0 as the active boot partition
#
# Prerequisites:
#   - esptool.py installed (pip install esptool)
#   - Arduino IDE compiled .bin files for both firmwares
#   - Device connected via USB
#
# Usage:
#   ./flash-dual-boot.sh <port> <classical.bin> <pqc.bin>
#
# Example:
#   ./flash-dual-boot.sh /dev/ttyACM0 \
#     build/classical_firmware.bin \
#     build/pqc_firmware.bin
#
# For batch flashing multiple devices (workshop prep):
#   for port in /dev/ttyACM*; do
#     ./flash-dual-boot.sh "$port" classical.bin pqc.bin
#   done
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────────────────
CHIP="esp32s3"
BAUD="921600"
FLASH_MODE="dio"
FLASH_FREQ="80m"
FLASH_SIZE="8MB"

# Partition offsets (must match partitions_pqc_workshop.csv)
BOOTLOADER_OFFSET="0x0"
PARTITION_TABLE_OFFSET="0x8000"
OTA_DATA_OFFSET="0xe000"
OTA_0_OFFSET="0x10000"
OTA_1_OFFSET="0x390000"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PARTITION_CSV="$SCRIPT_DIR/partitions_pqc_workshop.csv"

# ─── Arguments ───────────────────────────────────────────────────────────────
PORT="${1:-}"
CLASSICAL_BIN="${2:-}"
PQC_BIN="${3:-}"

if [[ -z "$PORT" || -z "$CLASSICAL_BIN" || -z "$PQC_BIN" ]]; then
    echo "Usage: $0 <serial-port> <classical-firmware.bin> <pqc-firmware.bin>"
    echo ""
    echo "Arguments:"
    echo "  serial-port          USB serial port (e.g., /dev/ttyACM0, COM3)"
    echo "  classical-firmware   Compiled .bin for classical TLS (partition 0)"
    echo "  pqc-firmware         Compiled .bin for PQC TLS (partition 1)"
    echo ""
    echo "Example:"
    echo "  $0 /dev/ttyACM0 classical_firmware.bin pqc_firmware.bin"
    echo ""
    echo "Tip: Hold BOOT button on XIAO ESP32S3 while connecting USB to enter download mode."
    exit 1
fi

# ─── Validate inputs ────────────────────────────────────────────────────────
if [[ ! -e "$PORT" && "$PORT" != COM* ]]; then
    echo "[ERROR] Serial port not found: $PORT"
    echo "        Available ports:"
    ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || echo "        (none found)"
    exit 1
fi

if [[ ! -f "$CLASSICAL_BIN" ]]; then
    echo "[ERROR] Classical firmware binary not found: $CLASSICAL_BIN"
    exit 1
fi

if [[ ! -f "$PQC_BIN" ]]; then
    echo "[ERROR] PQC firmware binary not found: $PQC_BIN"
    exit 1
fi

# Check firmware sizes fit in partition (3.5MB = 3670016 bytes)
MAX_SIZE=3670016
CLASSICAL_SIZE=$(stat -f%z "$CLASSICAL_BIN" 2>/dev/null || stat -c%s "$CLASSICAL_BIN")
PQC_SIZE=$(stat -f%z "$PQC_BIN" 2>/dev/null || stat -c%s "$PQC_BIN")

echo "═══════════════════════════════════════════════════════════════"
echo "  PQC Workshop — Dual-Boot Flash Tool"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "Port:              $PORT"
echo "Classical FW:      $CLASSICAL_BIN ($CLASSICAL_SIZE bytes)"
echo "PQC FW:            $PQC_BIN ($PQC_SIZE bytes)"
echo "Max partition:     $MAX_SIZE bytes (3.5 MB)"
echo ""

if [[ $CLASSICAL_SIZE -gt $MAX_SIZE ]]; then
    echo "[ERROR] Classical firmware ($CLASSICAL_SIZE bytes) exceeds partition size ($MAX_SIZE bytes)"
    exit 1
fi

if [[ $PQC_SIZE -gt $MAX_SIZE ]]; then
    echo "[ERROR] PQC firmware ($PQC_SIZE bytes) exceeds partition size ($MAX_SIZE bytes)"
    exit 1
fi

# ─── Generate partition table binary ────────────────────────────────────────
echo "[1/4] Generating partition table binary..."

PARTITION_BIN="$SCRIPT_DIR/partitions_pqc_workshop.bin"
if [[ -f "$PARTITION_BIN" ]]; then
    echo "       Using pre-built: $PARTITION_BIN"
elif command -v gen_esp32part.py &>/dev/null; then
    gen_esp32part.py "$PARTITION_CSV" "$PARTITION_BIN"
elif command -v python3 &>/dev/null; then
    PART_TOOL=$(find ~/.arduino15 ~/Library/Arduino15 /usr/local 2>/dev/null -name "gen_esp32part.py" | head -1)
    [[ -z "$PART_TOOL" ]] && PART_TOOL=$(find "$SCRIPT_DIR/../../arduino-cli/data" 2>/dev/null -name "gen_esp32part.py" | head -1)
    if [[ -n "$PART_TOOL" ]]; then
        python3 "$PART_TOOL" "$PARTITION_CSV" "$PARTITION_BIN"
    else
        echo "[ERROR] gen_esp32part.py not found and no pre-built partition binary."
        exit 1
    fi
fi

# ─── Generate OTA data (boot from OTA_0 by default) ─────────────────────────
echo "[2/4] Generating OTA data (initial boot = partition 0)..."

OTA_DATA_BIN="$SCRIPT_DIR/ota_data_initial.bin"
# OTA data partition is 8KB, initial state = all 0xFF (boots ota_0 by default)
python3 -c "
import sys
# OTA data: 8KB of 0xFF means 'boot from ota_0 (first valid partition)'
data = b'\\xff' * 0x2000
with open('$OTA_DATA_BIN', 'wb') as f:
    f.write(data)
print('OTA data generated (boots ota_0)')
"

# ─── Flash everything ────────────────────────────────────────────────────────
echo "[3/4] Flashing device..."
echo "       Partition table → 0x8000"
echo "       OTA data        → 0xe000"
echo "       Classical FW    → 0x10000  (OTA_0)"
echo "       PQC FW          → 0x390000 (OTA_1)"
echo ""

esptool.py \
    --chip "$CHIP" \
    --port "$PORT" \
    --baud "$BAUD" \
    --before default_reset \
    --after hard_reset \
    write_flash \
    --flash_mode "$FLASH_MODE" \
    --flash_freq "$FLASH_FREQ" \
    --flash_size "$FLASH_SIZE" \
    "$PARTITION_TABLE_OFFSET" "$PARTITION_BIN" \
    "$OTA_DATA_OFFSET" "$OTA_DATA_BIN" \
    "$OTA_0_OFFSET" "$CLASSICAL_BIN" \
    "$OTA_1_OFFSET" "$PQC_BIN"

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
echo "[4/4] Flash complete!"
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Device is ready for the workshop"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Boot partition: OTA_0 (Classical TLS 1.2)"
echo "  Migration target: OTA_1 (PQC TLS 1.3 + ML-KEM)"
echo ""
echo "  On power-up, the device will:"
echo "    1. Boot classical firmware from OTA_0"
echo "    2. Connect to Mosquitto broker via TLS 1.2"
echo "    3. Appear on the dashboard"
echo ""
echo "  To migrate to PQC:"
echo "    Click 'Migrate to PQC' on the dashboard"
echo "    (or publish {\"action\":\"migrate-pqc\"} to cmd/<thing>/ota)"
echo ""
