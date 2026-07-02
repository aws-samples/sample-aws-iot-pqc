#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# bundle-for-esp32.sh
#
# Creates the esp32-arduino-bundle directory with everything needed to
# compile and flash ESP32 firmware for the PQC workshop. Fetches current
# certs from the Raspberry Pi via SSM and generates secrets.h with the
# correct broker IP and credentials.
#
# Repeatable — safe to run multiple times. Overwrites the bundle each time
# with current certs/config.
#
# Prerequisites:
#   - AWS CLI with credentials
#   - SSM access to the Raspberry Pi (mi-* instance online)
#   - The pqc-workshop CloudFormation stack deployed
#
# Usage:
#   ./bundle-for-esp32.sh [--wifi-ssid SSID] [--wifi-password PASS] [--broker-ip IP]
#
# Examples:
#   ./bundle-for-esp32.sh --wifi-ssid "workshop-net" --wifi-password "pqc2026"
#   WIFI_SSID=mynet WIFI_PASSWORD=mypass ./bundle-for-esp32.sh
#   ./bundle-for-esp32.sh --broker-ip 192.168.1.100
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────────────────────
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
WIFI_SSID="${WIFI_SSID:-YOUR_WIFI_SSID}"
WIFI_PASSWORD="${WIFI_PASSWORD:-YOUR_WIFI_PASSWORD}"
BROKER_IP="${BROKER_IP:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUNDLE_DIR="$PROJECT_ROOT/esp32-arduino-bundle"

# ─── Parse Arguments ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wifi-ssid) WIFI_SSID="$2"; shift 2 ;;
        --wifi-password) WIFI_PASSWORD="$2"; shift 2 ;;
        --broker-ip) BROKER_IP="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--wifi-ssid SSID] [--wifi-password PASS] [--broker-ip IP]"
            echo ""
            echo "Options:"
            echo "  --wifi-ssid      WiFi SSID for the ESP32 (or set WIFI_SSID env var)"
            echo "  --wifi-password  WiFi password (or set WIFI_PASSWORD env var)"
            echo "  --broker-ip      Raspberry Pi IP (auto-detected from SSM if omitted)"
            echo "  --region         AWS region (default: us-east-1)"
            echo ""
            echo "If WiFi credentials are not provided, secrets.h will contain"
            echo "placeholders that you must edit before compiling."
            exit 0
            ;;
        *) echo "[ERROR] Unknown argument: $1"; exit 1 ;;
    esac
done

export AWS_DEFAULT_REGION="$REGION"

echo "═══════════════════════════════════════════════════════════════"
echo "  ESP32 Arduino Bundle Generator"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Region:    $REGION"
echo "  WiFi SSID: $WIFI_SSID"
echo "  Bundle:    $BUNDLE_DIR"
echo ""

# ─── Step 1: Detect Raspberry Pi IP ─────────────────────────────────────────
echo "[1/5] Detecting Raspberry Pi..."

if [[ -z "$BROKER_IP" ]]; then
    # Find the SSM managed instance
    INSTANCE_ID=$(aws ssm describe-instance-information \
        --query 'InstanceInformationList[?PingStatus==`Online`].InstanceId | [0]' \
        --output text 2>/dev/null) || INSTANCE_ID=""

    if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
        echo "       [WARN] No online SSM instance found. Using placeholder IP."
        echo "       Set --broker-ip manually or ensure Pi is online."
        BROKER_IP="RASPBERRY_PI_IP_ADDRESS"
    else
        echo "       SSM Instance: $INSTANCE_ID"
        # Get Pi IP via SSM
        CMD_ID=$(aws ssm send-command \
            --instance-ids "$INSTANCE_ID" \
            --document-name AWS-RunShellScript \
            --parameters '{"commands":["hostname -I | awk \"{print \\$1}\""]}' \
            --query 'Command.CommandId' --output text 2>/dev/null)

        sleep 5
        BROKER_IP=$(aws ssm get-command-invocation \
            --command-id "$CMD_ID" \
            --instance-id "$INSTANCE_ID" \
            --query 'StandardOutputContent' --output text 2>/dev/null | tr -d '[:space:]')

        if [[ -z "$BROKER_IP" ]]; then
            echo "       [WARN] Could not detect Pi IP. Using placeholder."
            BROKER_IP="RASPBERRY_PI_IP_ADDRESS"
        fi
    fi
fi

echo "       Broker IP: $BROKER_IP"

# ─── Step 2: Fetch certificates ─────────────────────────────────────────────
echo "[2/5] Fetching fleet certificates..."

CA_CERT=""
DEVICE_CERT=""
DEVICE_KEY=""

# Find the SSM instance if not already found
if [[ -z "${INSTANCE_ID:-}" ]]; then
    INSTANCE_ID=$(aws ssm describe-instance-information \
        --query 'InstanceInformationList[?PingStatus==`Online`].InstanceId | [0]' \
        --output text 2>/dev/null) || INSTANCE_ID=""
    [[ "$INSTANCE_ID" == "None" ]] && INSTANCE_ID=""
fi

# Try fetching from Pi via SSM
if [[ -n "${INSTANCE_ID:-}" ]]; then
    CMD_ID=$(aws ssm send-command \
        --instance-ids "$INSTANCE_ID" \
        --document-name AWS-RunShellScript \
        --parameters '{"commands":["cat /opt/mosquitto-pqc/etc/mosquitto/certs/ca.crt && echo ===SEPARATOR=== && find / -name fleet-device.crt -exec cat {} \\; 2>/dev/null | head -30 && echo ===SEPARATOR=== && find / -name fleet-device.key -exec cat {} \\; 2>/dev/null | head -30"]}' \
        --query 'Command.CommandId' --output text 2>/dev/null)

    sleep 8
    CERT_OUTPUT=$(aws ssm get-command-invocation \
        --command-id "$CMD_ID" \
        --instance-id "$INSTANCE_ID" \
        --query 'StandardOutputContent' --output text 2>/dev/null) || CERT_OUTPUT=""

    if [[ -n "$CERT_OUTPUT" ]]; then
        CA_CERT=$(echo "$CERT_OUTPUT" | sed -n '1,/===SEPARATOR===/p' | grep -v "===SEPARATOR===")
        DEVICE_CERT=$(echo "$CERT_OUTPUT" | sed -n '/===SEPARATOR===/,/===SEPARATOR===/p' | grep -v "===SEPARATOR===" | head -25)
        DEVICE_KEY=$(echo "$CERT_OUTPUT" | awk '/===SEPARATOR===$/{n++} n==2' | grep -v "===SEPARATOR===")
    fi
fi

# Fallback: try local bundle certs
if [[ -z "$CA_CERT" ]]; then
    LOCAL_CERTS="$PROJECT_ROOT/greengrass/rpi-pqc-workshop-bundle"
    if [[ -f "$LOCAL_CERTS/certs/output/ca.crt" ]]; then
        echo "       Using certs from local Pi bundle"
        CA_CERT=$(cat "$LOCAL_CERTS/certs/output/ca.crt")
        DEVICE_CERT=$(cat "$LOCAL_CERTS/certs/output/fleet-device.crt")
        DEVICE_KEY=$(cat "$LOCAL_CERTS/certs/output/fleet-device.key")
    fi
fi

# Fallback: Secrets Manager
if [[ -z "$CA_CERT" ]]; then
    echo "       [WARN] Could not fetch certs from Pi. Checking Secrets Manager..."
    CRED_JSON=$(aws secretsmanager get-secret-value \
        --secret-id "iot/pqc-workshop-greengrass-core/greengrass-credentials" \
        --query 'SecretString' --output text 2>/dev/null) || CRED_JSON=""
    if [[ -n "$CRED_JSON" ]]; then
        echo "       Found Greengrass credentials (note: this is the Greengrass cert, not fleet cert)"
    fi
fi

if [[ -z "$CA_CERT" ]]; then
    echo "       [ERROR] Could not obtain certificates from any source."
    echo "       Ensure the Pi is online or provide certs manually."
    exit 1
fi

echo "       ✅ Certificates obtained"

# ─── Step 3: Get IoT endpoint ────────────────────────────────────────────────
echo "[3/5] Getting IoT Core endpoint..."

IOT_ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS \
    --query 'endpointAddress' --output text 2>/dev/null) || IOT_ENDPOINT="unknown"
echo "       IoT Endpoint: $IOT_ENDPOINT"

# ─── Step 4: Create bundle directory ────────────────────────────────────────
echo "[4/5] Creating bundle..."

rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR/aws_iot_pqc_esp32"
mkdir -p "$BUNDLE_DIR/aws_iot_pqc_esp32_v1_classical"
mkdir -p "$BUNDLE_DIR/flash"

# Copy firmware source
cp "$PROJECT_ROOT/aws_iot_pqc_esp32/aws_iot_pqc_esp32.ino" "$BUNDLE_DIR/aws_iot_pqc_esp32/"
cp "$PROJECT_ROOT/aws_iot_pqc_esp32/wolfssl_user_settings.h" "$BUNDLE_DIR/aws_iot_pqc_esp32/"
cp "$PROJECT_ROOT/aws_iot_pqc_esp32_v1_classical/aws_iot_pqc_esp32_v1_classical.ino" "$BUNDLE_DIR/aws_iot_pqc_esp32_v1_classical/"

# Copy flash tools
cp "$SCRIPT_DIR/flash-dual-boot.sh" "$BUNDLE_DIR/flash/"
cp "$SCRIPT_DIR/partitions_pqc_workshop.csv" "$BUNDLE_DIR/flash/"
chmod +x "$BUNDLE_DIR/flash/flash-dual-boot.sh"

# Generate secrets.h
SECRETS_CONTENT="/*
 * secrets.h — PQC Workshop (shared fleet configuration)
 *
 * Generated by bundle-for-esp32.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")
 * This single file works for ALL devices (identity from MAC address).
 *
 * Broker: Raspberry Pi Mosquitto with PQC TLS ($BROKER_IP)
 */

#ifndef SECRETS_H
#define SECRETS_H

// ─── WiFi Configuration ─────────────────────────────────────────────────────
#define WIFI_SSID     \"$WIFI_SSID\"
#define WIFI_PASSWORD \"$WIFI_PASSWORD\"

// ─── Logging Configuration ──────────────────────────────────────────────────
#define LOG_LEVEL  3  // INFO

// ─── MQTT Broker (Mosquitto on Raspberry Pi) ────────────────────────────────
#define MQTT_BROKER_ENDPOINT \"$BROKER_IP\"
#define MQTT_BROKER_PORT     8883

// Device identity derived at runtime from ESP32 MAC address.
// Format: \"esp32-XXYYZZ\" (last 3 bytes of MAC). Unique per device.

// AWS IoT Core endpoint (reference — device connects via Mosquitto bridge)
#define AWS_IOT_ENDPOINT \"$IOT_ENDPOINT\"
#define AWS_IOT_PORT     8883

// Legacy topics
#define AWS_IOT_PUBLISH_TOPIC   \"esp32/pqc/pub\"
#define AWS_IOT_SUBSCRIBE_TOPIC \"esp32/pqc/sub\"

// ─── Broker CA Certificate ──────────────────────────────────────────────────
static const char AWS_CERT_CA[] PROGMEM = R\"EOF(
$CA_CERT
)EOF\";

// ─── Fleet Device Certificate (shared by all workshop devices) ──────────────
static const char AWS_CERT_CRT[] PROGMEM = R\"KEY(
$DEVICE_CERT
)KEY\";

// ─── Fleet Device Private Key ───────────────────────────────────────────────
static const char AWS_CERT_PRIVATE[] PROGMEM = R\"KEY(
$DEVICE_KEY
)KEY\";

#endif // SECRETS_H"

echo "$SECRETS_CONTENT" > "$BUNDLE_DIR/aws_iot_pqc_esp32/secrets.h"
echo "$SECRETS_CONTENT" > "$BUNDLE_DIR/aws_iot_pqc_esp32_v1_classical/secrets.h"

# Copy the README
cp "$SCRIPT_DIR/../esp32-arduino-bundle/README.md" "$BUNDLE_DIR/README.md" 2>/dev/null || \
cat > "$BUNDLE_DIR/README.md" << 'READMEEOF'
# ESP32 Arduino Bundle — PQC Workshop

See the main project README for full instructions.

Quick start:
1. Edit WiFi in secrets.h (both folders)
2. Install ESP32 board package + wolfSSL + PubSubClient in Arduino IDE
3. Copy wolfssl_user_settings.h into wolfSSL library's user_settings.h
4. Open .ino file, select XIAO_ESP32S3, Upload
READMEEOF

# ─── Step 5: Verify ─────────────────────────────────────────────────────────
echo "[5/5] Verifying bundle..."

ERRORS=0

# Check secrets.h contains real certs (not placeholders)
if grep -q "REPLACE_WITH" "$BUNDLE_DIR/aws_iot_pqc_esp32/secrets.h"; then
    echo "       ❌ secrets.h still has REPLACE_WITH placeholders"
    ERRORS=$((ERRORS + 1))
fi

if ! grep -q "BEGIN CERTIFICATE" "$BUNDLE_DIR/aws_iot_pqc_esp32/secrets.h"; then
    echo "       ❌ secrets.h missing certificates"
    ERRORS=$((ERRORS + 1))
fi

if ! grep -q "BEGIN PRIVATE KEY\|BEGIN RSA PRIVATE KEY" "$BUNDLE_DIR/aws_iot_pqc_esp32/secrets.h"; then
    echo "       ❌ secrets.h missing private key"
    ERRORS=$((ERRORS + 1))
fi

# Check .ino files exist and are non-empty
for ino in "$BUNDLE_DIR/aws_iot_pqc_esp32/aws_iot_pqc_esp32.ino" \
           "$BUNDLE_DIR/aws_iot_pqc_esp32_v1_classical/aws_iot_pqc_esp32_v1_classical.ino"; do
    if [[ ! -s "$ino" ]]; then
        echo "       ❌ Missing or empty: $ino"
        ERRORS=$((ERRORS + 1))
    fi
done

# Check wolfssl_user_settings.h
if [[ ! -s "$BUNDLE_DIR/aws_iot_pqc_esp32/wolfssl_user_settings.h" ]]; then
    echo "       ❌ Missing wolfssl_user_settings.h"
    ERRORS=$((ERRORS + 1))
fi

# Check flash tools
if [[ ! -x "$BUNDLE_DIR/flash/flash-dual-boot.sh" ]]; then
    echo "       ❌ Missing or not executable: flash/flash-dual-boot.sh"
    ERRORS=$((ERRORS + 1))
fi

if [[ $ERRORS -eq 0 ]]; then
    echo "       ✅ All checks passed"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
if [[ $ERRORS -eq 0 ]]; then
    echo "  ✅ ESP32 bundle created successfully!"
else
    echo "  ⚠️  Bundle created with $ERRORS issue(s) — review above"
fi
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Location:  $BUNDLE_DIR"
echo "  Broker IP: $BROKER_IP"
echo "  WiFi SSID: $WIFI_SSID"
echo "  IoT Endpoint: $IOT_ENDPOINT"
echo ""
echo "  Next steps:"
echo "    1. Open Arduino IDE"
if [[ "$WIFI_SSID" == "YOUR_WIFI_SSID" ]]; then
    echo "    2. ⚠️  Edit WiFi credentials in secrets.h (both folders)"
else
    echo "    2. WiFi already configured ✅"
fi
echo "    3. Install wolfSSL + PubSubClient libraries"
echo "    4. Copy wolfssl_user_settings.h → wolfSSL's user_settings.h"
echo "    5. Select board: XIAO_ESP32S3"
echo "    6. Upload or Export Binary + flash"
echo ""
