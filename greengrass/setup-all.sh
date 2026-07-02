#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-all.sh — Idempotent Raspberry Pi provisioning for PQC workshop
#
# This script is IDEMPOTENT — safe to run multiple times. Each step checks
# whether its work is already done and skips if so. Only components that
# are missing or broken will be (re)installed.
#
# PREREQUISITE: Run setup-ssm-agent.sh FIRST and verify it works before
# running this script. SSM provides the IAM credentials the Pi uses for
# all AWS API calls (no separate IAM user needed).
#
# Usage:
#   sudo ./setup-all.sh <aws-region>
#   sudo ./setup-all.sh <aws-region> --force   (re-run all steps regardless)
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REGION="${1:-us-east-1}"
FORCE=false
[[ "${2:-}" == "--force" ]] && FORCE=true

THING_NAME="pqc-workshop-greengrass-core"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "[ERROR] Run as root: sudo $0 $*"
    exit 1
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  PQC Workshop — Raspberry Pi Setup (Idempotent)"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Region: $REGION"
echo "  Thing:  $THING_NAME"
echo "  Force:  $FORCE"
echo ""

cd "$SCRIPT_DIR"

# ─── Prerequisite Check: SSM Agent ──────────────────────────────────────────
echo "─── Prerequisite: SSM Agent ─────────────────────────────────"

SSM_OK=true
if ! command -v amazon-ssm-agent &>/dev/null; then
    SSM_OK=false
    echo "  ❌ SSM Agent not installed"
elif [[ ! -f /var/lib/amazon/ssm/registration ]]; then
    SSM_OK=false
    echo "  ❌ SSM Agent not registered"
elif ! systemctl is-active --quiet amazon-ssm-agent 2>/dev/null; then
    SSM_OK=false
    echo "  ❌ SSM Agent service not running"
else
    INSTANCE_ID=$(python3 -c "
import json
with open('/var/lib/amazon/ssm/registration') as f:
    print(json.load(f).get('ManagedInstanceID', 'unknown'))
" 2>/dev/null || echo "unknown")
    echo "  ✅ SSM Agent running (Instance: $INSTANCE_ID)"
fi

if [[ "$SSM_OK" == false ]]; then
    echo ""
    echo "  SSM Agent must be set up first. Run:"
    echo "    sudo ./setup-ssm-agent.sh $REGION"
    echo ""
    echo "  Then verify it works:"
    echo "    sudo systemctl status amazon-ssm-agent"
    echo ""
    echo "  SSM provides the IAM credentials for all AWS API calls."
    echo "  No separate IAM user is needed on the Pi."
    echo ""
    echo "  After SSM is working, re-run this script."
    exit 1
fi

echo ""

STEPS_SKIPPED=0
STEPS_RUN=0

step_skipped() {
    echo "  ⏭️  SKIPPED: $1"
    STEPS_SKIPPED=$((STEPS_SKIPPED + 1))
}

step_done() {
    echo "  ✅ $1"
    STEPS_RUN=$((STEPS_RUN + 1))
}

# ─── Step 1: Greengrass ─────────────────────────────────────────────────────
echo "━━━ Step 1/4: Greengrass v2 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

GG_ROOT="/greengrass/v2"
GG_NEEDS_INSTALL=false

if [[ "$FORCE" == true ]]; then
    GG_NEEDS_INSTALL=true
elif [[ ! -d "$GG_ROOT" ]]; then
    echo "  Greengrass not installed"
    GG_NEEDS_INSTALL=true
elif ! systemctl is-active --quiet greengrass 2>/dev/null; then
    echo "  Greengrass installed but service not running — starting..."
    systemctl start greengrass 2>/dev/null && step_done "Greengrass service started" || {
        echo "  Service failed to start — reinstalling"
        GG_NEEDS_INSTALL=true
    }
else
    step_skipped "Greengrass running ($GG_ROOT exists, service active)"
fi

if [[ "$GG_NEEDS_INSTALL" == true ]]; then
    if [[ ! -f credentials/greengrass-credentials.json ]]; then
        echo "  [SKIP] No Greengrass credentials in bundle"
    else
        # Install Java if needed
        if ! command -v java &>/dev/null; then
            echo "  Installing Java..."
            apt-get update -qq
            apt-get install -y -qq default-jdk
        fi

        GG_CERT_DIR="/tmp/gg-certs"
        mkdir -p "$GG_CERT_DIR"
        python3 -c "
import json
creds = json.load(open('credentials/greengrass-credentials.json'))
open('$GG_CERT_DIR/device.pem.crt', 'w').write(creds['certificatePem'])
open('$GG_CERT_DIR/private.pem.key', 'w').write(creds['privateKey'])
"
        curl -s -o "$GG_CERT_DIR/AmazonRootCA1.pem" \
            https://www.amazontrust.com/repository/AmazonRootCA1.pem

        IOT_ENDPOINT=$(cat credentials/iot-endpoint.txt)
        CRED_ENDPOINT=$(echo "$IOT_ENDPOINT" | sed 's/-ats\.iot\./.credentials.iot./')

        INSTALLER_DIR="/tmp/gg-installer"
        mkdir -p "$INSTALLER_DIR"
        if [[ ! -f "$INSTALLER_DIR/greengrass-nucleus-latest.zip" ]]; then
            echo "  Downloading Greengrass nucleus..."
            curl -s https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-nucleus-latest.zip \
                -o "$INSTALLER_DIR/greengrass-nucleus-latest.zip"
        fi
        rm -rf "$INSTALLER_DIR/GreengrassInstaller"
        unzip -qo "$INSTALLER_DIR/greengrass-nucleus-latest.zip" \
            -d "$INSTALLER_DIR/GreengrassInstaller"

        echo "  Running Greengrass installer..."
        java -Droot="$GG_ROOT" -Dlog.store=FILE \
            -jar "$INSTALLER_DIR/GreengrassInstaller/lib/Greengrass.jar" \
            --aws-region "$REGION" \
            --thing-name "$THING_NAME" \
            --thing-policy-name "${THING_NAME}-policy" \
            --tes-role-name "${THING_NAME}-TES-role" \
            --tes-role-alias-name "${THING_NAME}-TES-alias" \
            --component-default-user ggc_user:ggc_group \
            --provision false \
            --setup-system-service true \
            --init-config - <<INITCONFIG
---
system:
  certificateFilePath: "$GG_CERT_DIR/device.pem.crt"
  privateKeyPath: "$GG_CERT_DIR/private.pem.key"
  rootCaPath: "$GG_CERT_DIR/AmazonRootCA1.pem"
  rootpath: "$GG_ROOT"
  thingName: "$THING_NAME"
services:
  aws.greengrass.Nucleus:
    componentType: NUCLEUS
    version: ""
    configuration:
      awsRegion: "$REGION"
      iotRoleAlias: "${THING_NAME}-TES-alias"
      iotDataEndpoint: "$IOT_ENDPOINT"
      iotCredEndpoint: "$CRED_ENDPOINT"
INITCONFIG

        rm -rf "$GG_CERT_DIR"
        step_done "Greengrass installed and running"
    fi
fi

# ─── Step 2: Build PQC Mosquitto ────────────────────────────────────────────
echo ""
echo "━━━ Step 2/4: PQC Mosquitto (OpenSSL 3.5 + Mosquitto) ━━━━━━"

MOSQUITTO_BIN="/opt/mosquitto-pqc/sbin/mosquitto"
[[ ! -x "$MOSQUITTO_BIN" ]] && MOSQUITTO_BIN="/opt/mosquitto-pqc/bin/mosquitto"
OPENSSL_PQC="/opt/openssl-pqc/bin/openssl"

if [[ "$FORCE" == true ]]; then
    echo "  Force mode — rebuilding..."
    ./setup-mosquitto-pqc.sh
    step_done "PQC Mosquitto rebuilt"
elif [[ ! -x "$OPENSSL_PQC" ]]; then
    echo "  OpenSSL PQC not found — building from source (~15-20 min)..."
    ./setup-mosquitto-pqc.sh
    step_done "PQC Mosquitto installed (OpenSSL + Mosquitto)"
elif [[ ! -x "$MOSQUITTO_BIN" ]]; then
    echo "  OpenSSL PQC present but Mosquitto binary missing — building..."
    ./setup-mosquitto-pqc.sh
    step_done "PQC Mosquitto installed"
else
    step_skipped "OpenSSL PQC + Mosquitto already installed"
fi

# ─── Step 3: Generate broker certs ──────────────────────────────────────────
echo ""
echo "━━━ Step 3/4: Generate Broker Certificates ━━━━━━━━━━━━━━━━━━"

CERT_OUTPUT_DIR="$SCRIPT_DIR/certs/output"

if [[ "$FORCE" == true ]]; then
    echo "  Force mode — regenerating certs..."
    cd certs
    LD_LIBRARY_PATH=/opt/openssl-pqc/lib ./generate-broker-certs.sh 100
    cd "$SCRIPT_DIR"
    step_done "Broker + device certificates regenerated"
elif [[ -f "$CERT_OUTPUT_DIR/ca.crt" && -f "$CERT_OUTPUT_DIR/broker.crt" \
        && -d "$CERT_OUTPUT_DIR/devices" ]]; then
    DEVICE_COUNT=$(find "$CERT_OUTPUT_DIR/devices" -name "device.crt" \
        2>/dev/null | wc -l)
    step_skipped "Certificates already generated ($DEVICE_COUNT device certs)"
else
    echo "  Certificates not found — generating..."
    cd certs
    LD_LIBRARY_PATH=/opt/openssl-pqc/lib ./generate-broker-certs.sh 100
    cd "$SCRIPT_DIR"
    step_done "Broker + device certificates generated"
fi

# ─── Step 4: Configure and start Mosquitto ──────────────────────────────────
echo ""
echo "━━━ Step 4/4: Configure and Start Mosquitto ━━━━━━━━━━━━━━━━━"

MOSQ_CERT_DIR="/opt/mosquitto-pqc/etc/mosquitto/certs"
BRIDGE_CONF="/opt/mosquitto-pqc/etc/mosquitto/conf.d/bridge-iotcore.conf"
NEEDS_CONFIGURE=false

if [[ "$FORCE" == true ]]; then
    NEEDS_CONFIGURE=true
elif [[ ! -f "$MOSQ_CERT_DIR/ca.crt" ]]; then
    echo "  Broker certs not installed"
    NEEDS_CONFIGURE=true
elif [[ ! -f "$BRIDGE_CONF" ]]; then
    echo "  Bridge config not installed"
    NEEDS_CONFIGURE=true
elif grep -q "REPLACE_WITH" "$BRIDGE_CONF" 2>/dev/null; then
    echo "  Bridge config has placeholders"
    NEEDS_CONFIGURE=true
elif [[ ! -f "$MOSQ_CERT_DIR/greengrass-device.crt" ]]; then
    echo "  Greengrass bridge cert missing"
    NEEDS_CONFIGURE=true
fi

if [[ "$NEEDS_CONFIGURE" == true ]]; then
    mkdir -p "$MOSQ_CERT_DIR"
    mkdir -p /opt/mosquitto-pqc/etc/mosquitto/conf.d

    # Copy broker certs (use generated output if available)
    if [[ -f "$CERT_OUTPUT_DIR/ca.crt" ]]; then
        cp "$CERT_OUTPUT_DIR/ca.crt" "$MOSQ_CERT_DIR/"
        cp "$CERT_OUTPUT_DIR/broker.crt" "$MOSQ_CERT_DIR/"
        cp "$CERT_OUTPUT_DIR/broker.key" "$MOSQ_CERT_DIR/"
    fi

    # Copy config
    cp mosquitto/mosquitto-pqc.conf /opt/mosquitto-pqc/etc/mosquitto/
    sed "s|REPLACE_WITH_GREENGRASS_THING_NAME|$THING_NAME|g" \
        mosquitto/acl.conf > /opt/mosquitto-pqc/etc/mosquitto/acl.conf

    # Configure bridge
    IOT_ENDPOINT=$(cat credentials/iot-endpoint.txt)
    sed "s|REPLACE_WITH_IOT_ENDPOINT|$IOT_ENDPOINT|g; \
         s|REPLACE_WITH_GREENGRASS_THING_NAME|$THING_NAME|g" \
        mosquitto/bridge-iotcore.conf > "$BRIDGE_CONF"

    # Greengrass device cert for bridge
    if [[ -f credentials/greengrass-credentials.json ]]; then
        python3 -c "
import json
creds = json.load(open('credentials/greengrass-credentials.json'))
open('$MOSQ_CERT_DIR/greengrass-device.crt', 'w').write(creds['certificatePem'])
open('$MOSQ_CERT_DIR/greengrass-device.key', 'w').write(creds['privateKey'])
"
    fi

    # Amazon Root CA for bridge
    if [[ ! -f "$MOSQ_CERT_DIR/AmazonRootCA1.pem" ]]; then
        curl -so "$MOSQ_CERT_DIR/AmazonRootCA1.pem" \
            https://www.amazontrust.com/repository/AmazonRootCA1.pem
    fi

    # (Re)start Mosquitto
    systemctl restart mosquitto-pqc 2>/dev/null || systemctl start mosquitto-pqc
    systemctl enable mosquitto-pqc
    step_done "Mosquitto configured and started"
else
    # Just ensure the service is running
    if ! systemctl is-active --quiet mosquitto-pqc 2>/dev/null; then
        echo "  Configuration present but service stopped — starting..."
        systemctl start mosquitto-pqc
        step_done "Mosquitto service started"
    else
        step_skipped "Mosquitto configured, certs installed, service running"
    fi
fi

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Setup Complete"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Steps run:     $STEPS_RUN"
echo "  Steps skipped: $STEPS_SKIPPED (already done)"
echo ""

if [[ $STEPS_SKIPPED -gt 0 && $STEPS_RUN -eq 0 ]]; then
    echo "  Everything was already set up — nothing to do! 🎉"
else
    echo "  Run the readiness check to verify:"
    echo "    ./check_rpi_setup_status.sh"
fi
echo ""
