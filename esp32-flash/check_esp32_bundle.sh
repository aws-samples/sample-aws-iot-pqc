#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# check_esp32_bundle.sh
#
# Validates the ESP32 Arduino bundle is correctly generated and ready
# for compilation. Checks certificates, config, and file structure.
#
# Usage:
#   ./check_esp32_bundle.sh [bundle-path]
#
# Default bundle path: ../esp32-arduino-bundle (relative to this script)
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE_DIR="${1:-$(cd "$SCRIPT_DIR/.." && pwd)/esp32-arduino-bundle}"

PASS=0
WARN=0
FAIL=0

pass() { echo "  ✅ $1"; PASS=$((PASS + 1)); }
warn() { echo "  ⚠️  $1"; WARN=$((WARN + 1)); }
fail() { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }

echo "═══════════════════════════════════════════════════════════════"
echo "  ESP32 Bundle Readiness Check"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Bundle: $BUNDLE_DIR"
echo ""

# ─── 1. Directory Structure ──────────────────────────────────────────────────
echo "─── 1. Directory Structure ──────────────────────────────────"

if [[ ! -d "$BUNDLE_DIR" ]]; then
    fail "Bundle directory not found: $BUNDLE_DIR"
    echo ""
    echo "  Run bundle-for-esp32.sh to create it."
    exit 1
fi

for dir in "aws_iot_pqc_esp32" "aws_iot_pqc_esp32_v1_classical" "flash"; do
    if [[ -d "$BUNDLE_DIR/$dir" ]]; then
        pass "Directory exists: $dir/"
    else
        fail "Directory missing: $dir/"
    fi
done

echo ""

# ─── 2. PQC Firmware Files ──────────────────────────────────────────────────
echo "─── 2. PQC Firmware (aws_iot_pqc_esp32/) ────────────────────"

PQC_DIR="$BUNDLE_DIR/aws_iot_pqc_esp32"

if [[ -s "$PQC_DIR/aws_iot_pqc_esp32.ino" ]]; then
    SIZE=$(wc -c < "$PQC_DIR/aws_iot_pqc_esp32.ino" | tr -d ' ')
    pass "PQC sketch: aws_iot_pqc_esp32.ino ($SIZE bytes)"
else
    fail "PQC sketch missing or empty"
fi

if [[ -s "$PQC_DIR/secrets.h" ]]; then
    pass "secrets.h present"
else
    fail "secrets.h missing or empty"
fi

if [[ -s "$PQC_DIR/wolfssl_user_settings.h" ]]; then
    pass "wolfssl_user_settings.h present"
else
    fail "wolfssl_user_settings.h missing"
fi

echo ""

# ─── 3. Classical Firmware Files ─────────────────────────────────────────────
echo "─── 3. Classical Firmware (aws_iot_pqc_esp32_v1_classical/) ─"

CLASSIC_DIR="$BUNDLE_DIR/aws_iot_pqc_esp32_v1_classical"

if [[ -s "$CLASSIC_DIR/aws_iot_pqc_esp32_v1_classical.ino" ]]; then
    SIZE=$(wc -c < "$CLASSIC_DIR/aws_iot_pqc_esp32_v1_classical.ino" | tr -d ' ')
    pass "Classical sketch: aws_iot_pqc_esp32_v1_classical.ino ($SIZE bytes)"
else
    fail "Classical sketch missing or empty"
fi

if [[ -s "$CLASSIC_DIR/secrets.h" ]]; then
    pass "secrets.h present"
else
    fail "secrets.h missing or empty"
fi

echo ""

# ─── 4. Secrets Configuration ───────────────────────────────────────────────
echo "─── 4. Secrets Configuration ────────────────────────────────"

SECRETS="$PQC_DIR/secrets.h"

# WiFi
WIFI_SSID=$(grep '#define WIFI_SSID' "$SECRETS" | sed 's/.*"\(.*\)".*/\1/')
if [[ "$WIFI_SSID" == "YOUR_WIFI_SSID" ]]; then
    warn "WiFi SSID is placeholder — edit before compiling"
else
    pass "WiFi SSID configured: $WIFI_SSID"
fi

# Broker IP
BROKER_IP=$(grep '#define MQTT_BROKER_ENDPOINT' "$SECRETS" | sed 's/.*"\(.*\)".*/\1/')
if [[ "$BROKER_IP" == "RASPBERRY_PI_IP_ADDRESS" ]]; then
    fail "Broker IP is placeholder — run bundle-for-esp32.sh with Pi online"
elif [[ "$BROKER_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    pass "Broker IP: $BROKER_IP"
else
    warn "Broker IP looks unusual: $BROKER_IP"
fi

# CA Certificate
if grep -q "BEGIN CERTIFICATE" "$SECRETS"; then
    CA_LINES=$(grep -c "BEGIN CERTIFICATE\|END CERTIFICATE" "$SECRETS")
    if [[ $CA_LINES -ge 4 ]]; then
        pass "Certificates present (CA + device cert)"
    else
        warn "Found certificates but expected at least 2 (CA + device)"
    fi
else
    fail "No certificates found in secrets.h"
fi

# Private Key
if grep -q "BEGIN PRIVATE KEY\|BEGIN RSA PRIVATE KEY" "$SECRETS"; then
    pass "Private key present"
else
    fail "Private key missing from secrets.h"
fi

# Check both secrets.h match
if diff -q "$PQC_DIR/secrets.h" "$CLASSIC_DIR/secrets.h" &>/dev/null; then
    pass "Both secrets.h files are identical"
else
    warn "PQC and classical secrets.h differ (should be the same)"
fi

echo ""

# ─── 5. Flash Tools ─────────────────────────────────────────────────────────
echo "─── 5. Flash Tools ──────────────────────────────────────────"

FLASH_DIR="$BUNDLE_DIR/flash"

if [[ -x "$FLASH_DIR/flash-dual-boot.sh" ]]; then
    pass "flash-dual-boot.sh (executable)"
else
    fail "flash-dual-boot.sh missing or not executable"
fi

if [[ -s "$FLASH_DIR/partitions_pqc_workshop.csv" ]]; then
    pass "partitions_pqc_workshop.csv present"
else
    fail "partitions_pqc_workshop.csv missing"
fi

echo ""

# ─── 6. wolfSSL PQC Config Validation ───────────────────────────────────────
echo "─── 6. wolfSSL PQC Configuration ────────────────────────────"

WOLF_CFG="$PQC_DIR/wolfssl_user_settings.h"

REQUIRED_DEFINES=(
    "WOLFSSL_TLS13"
    "WOLFSSL_HAVE_MLKEM"
    "WOLFSSL_WC_KYBER"
    "WOLFSSL_SHA3"
    "HAVE_ECC"
    "HAVE_SNI"
    "HAVE_AESGCM"
)

for define in "${REQUIRED_DEFINES[@]}"; do
    if grep -q "#define $define" "$WOLF_CFG"; then
        pass "$define"
    else
        fail "Missing #define $define in wolfssl_user_settings.h"
    fi
done

echo ""

# ─── 7. Certificate Validity ────────────────────────────────────────────────
echo "─── 7. Certificate Validity ─────────────────────────────────"

# Extract CA cert to temp file and validate
CA_TEMP=$(mktemp)
sed -n '/AWS_CERT_CA.*PROGMEM/,/^)EOF/p' "$SECRETS" | grep -v "PROGMEM\|EOF\|static\|R\"" > "$CA_TEMP"

if command -v openssl &>/dev/null && [[ -s "$CA_TEMP" ]]; then
    if openssl x509 -in "$CA_TEMP" -checkend 0 -noout 2>/dev/null; then
        EXPIRY=$(openssl x509 -in "$CA_TEMP" -enddate -noout 2>/dev/null | cut -d= -f2)
        pass "CA certificate valid (expires: $EXPIRY)"
    else
        fail "CA certificate is EXPIRED"
    fi

    CA_SUBJECT=$(openssl x509 -in "$CA_TEMP" -subject -noout 2>/dev/null)
    if echo "$CA_SUBJECT" | grep -qi "PQC Workshop"; then
        pass "CA subject: $CA_SUBJECT"
    else
        warn "CA subject doesn't mention 'PQC Workshop': $CA_SUBJECT"
    fi
else
    warn "Cannot validate certificate (openssl not available or cert extraction failed)"
fi
rm -f "$CA_TEMP"

echo ""

# ─── Summary ─────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  RESULTS"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  ✅ Passed:   $PASS"
echo "  ⚠️  Warnings: $WARN"
echo "  ❌ Failed:   $FAIL"
echo ""

if [[ $FAIL -eq 0 && $WARN -eq 0 ]]; then
    echo "  🎉 Bundle is READY to compile and flash!"
elif [[ $FAIL -eq 0 ]]; then
    echo "  👍 Bundle is MOSTLY READY (review warnings — WiFi may need editing)"
else
    echo "  🚨 Bundle has issues. Fix them and re-run bundle-for-esp32.sh"
fi
echo ""
