#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# bundle-for-pi.sh
#
# Creates a self-contained zip file with everything needed to provision
# the Raspberry Pi for the PQC workshop. Copy this single zip to the Pi
# and run the setup scripts inside.
#
# The bundle includes:
#   - SSM agent setup script + activation credentials
#   - Greengrass setup script
#   - Mosquitto PQC build script
#   - Certificate generation script
#   - Mosquitto configuration files
#   - Greengrass component recipe
#   - Pi readiness check script
#   - README with step-by-step instructions
#
# Usage:
#   ./bundle-for-pi.sh [--region REGION] [--stack-name STACK_NAME]
#
# Example:
#   ./bundle-for-pi.sh --region us-east-1
#   # Then: scp rpi-pqc-workshop-bundle.zip pi@<pi-ip>:~/
#
# Prerequisites:
#   - AWS CLI with credentials that can read Secrets Manager
#   - The pqc-workshop CloudFormation stack deployed
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
STACK_NAME="pqc-workshop"
GREENGRASS_THING="pqc-workshop-greengrass-core"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --region) REGION="$2"; shift 2 ;;
        --stack-name) STACK_NAME="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--region REGION] [--stack-name STACK_NAME]"
            exit 0
            ;;
        *) echo "[ERROR] Unknown argument: $1"; exit 1 ;;
    esac
done

export AWS_DEFAULT_REGION="$REGION"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE_DIR="/tmp/rpi-pqc-workshop-bundle"
OUTPUT_ZIP="$SCRIPT_DIR/rpi-pqc-workshop-bundle.zip"

echo "═══════════════════════════════════════════════════════════════"
echo "  Building Raspberry Pi Bundle"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Region: $REGION"
echo "  Stack:  $STACK_NAME"
echo ""

# Clean up any previous bundle
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR/credentials"
mkdir -p "$BUNDLE_DIR/mosquitto"
mkdir -p "$BUNDLE_DIR/certs"
mkdir -p "$BUNDLE_DIR/greengrass-component/artifacts"

# ─── Fetch secrets from AWS ──────────────────────────────────────────────────
echo "[1/5] Fetching credentials from Secrets Manager..."

# SSM activation
aws secretsmanager get-secret-value \
    --secret-id "iot/${GREENGRASS_THING}/ssm-activation" \
    --query 'SecretString' --output text \
    > "$BUNDLE_DIR/credentials/ssm-activation.json" 2>/dev/null || {
    echo "       [WARN] SSM activation secret not found — SSM setup will need manual creds"
}

# Greengrass credentials
aws secretsmanager get-secret-value \
    --secret-id "iot/${GREENGRASS_THING}/greengrass-credentials" \
    --query 'SecretString' --output text \
    > "$BUNDLE_DIR/credentials/greengrass-credentials.json" 2>/dev/null || {
    echo "       [WARN] Greengrass credentials not found — Greengrass setup will need manual creds"
}

# IoT endpoint
IOT_ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS \
    --query 'endpointAddress' --output text 2>/dev/null || echo "UNKNOWN")
echo "$IOT_ENDPOINT" > "$BUNDLE_DIR/credentials/iot-endpoint.txt"

echo "       IoT Endpoint: $IOT_ENDPOINT"
echo "       SSM + Greengrass credentials saved"

# ─── Copy scripts ───────────────────────────────────────────────────────────
echo "[2/5] Copying setup scripts..."

cp "$SCRIPT_DIR/setup-ssm-agent.sh" "$BUNDLE_DIR/"
cp "$SCRIPT_DIR/setup-greengrass-core.sh" "$BUNDLE_DIR/"
cp "$SCRIPT_DIR/setup-mosquitto-pqc.sh" "$BUNDLE_DIR/"
cp "$SCRIPT_DIR/check_rpi_setup_status.sh" "$BUNDLE_DIR/"
chmod +x "$BUNDLE_DIR"/*.sh

# ─── Copy configs ───────────────────────────────────────────────────────────
echo "[3/5] Copying configuration files..."

cp "$SCRIPT_DIR/mosquitto/mosquitto-pqc.conf" "$BUNDLE_DIR/mosquitto/"
cp "$SCRIPT_DIR/mosquitto/bridge-iotcore.conf" "$BUNDLE_DIR/mosquitto/"
cp "$SCRIPT_DIR/mosquitto/acl.conf" "$BUNDLE_DIR/mosquitto/"
cp "$SCRIPT_DIR/certs/generate-broker-certs.sh" "$BUNDLE_DIR/certs/"
chmod +x "$BUNDLE_DIR/certs/generate-broker-certs.sh"

# Greengrass component
cp "$SCRIPT_DIR/greengrass-component/recipe.yaml" "$BUNDLE_DIR/greengrass-component/"
cp "$SCRIPT_DIR/greengrass-component/deploy.sh" "$BUNDLE_DIR/greengrass-component/"
chmod +x "$BUNDLE_DIR/greengrass-component/deploy.sh"

# ─── Create setup-all.sh ────────────────────────────────────────────────────
echo "[4/5] Creating master setup script..."

cp "$SCRIPT_DIR/setup-all.sh" "$BUNDLE_DIR/"
chmod +x "$BUNDLE_DIR/setup-all.sh"
# ─── Create README ──────────────────────────────────────────────────────────
cat > "$BUNDLE_DIR/README.md" << 'README'
# PQC Workshop — Raspberry Pi Bundle

Everything needed to provision the Raspberry Pi for the PQC workshop.

## Quick Start

```bash
# On the Pi:
unzip rpi-pqc-workshop-bundle.zip
cd rpi-pqc-workshop-bundle

# Step 1: Set up SSM agent (requires one-time AWS creds or physical access)
sudo ./setup-ssm-agent.sh us-east-1

# Verify SSM is working
sudo systemctl status amazon-ssm-agent

# Step 2: Run the main setup (idempotent — safe to re-run)
sudo ./setup-all.sh us-east-1

# Step 3: Verify everything
./check_rpi_setup_status.sh
```

## Two-Phase Design

**Phase 1: SSM Agent** (`setup-ssm-agent.sh`)
- Install and register SSM agent using bundled activation credentials
- Once registered, the Pi has IAM credentials via the SSM role
- No separate IAM user needed on the Pi
- Verify this works before proceeding

**Phase 2: Everything Else** (`setup-all.sh`)
- Requires SSM to be running (checks and exits if not)
- Installs Greengrass, builds Mosquitto, generates certs, configures broker
- Fully idempotent — skips steps already completed
- Use `--force` to redo everything: `sudo ./setup-all.sh us-east-1 --force`

## What's Included

| File/Folder | Purpose |
|-------------|---------|
| `setup-ssm-agent.sh` | Phase 1: Installs + registers SSM agent |
| `setup-all.sh` | Phase 2: Installs Greengrass, Mosquitto, certs (idempotent) |
| `setup-greengrass-core.sh` | Standalone Greengrass installer (used by setup-all) |
| `setup-mosquitto-pqc.sh` | Compiles OpenSSL 3.5 + Mosquitto from source |
| `check_rpi_setup_status.sh` | Validates all components are working |
| `credentials/` | Pre-fetched secrets (SSM activation, Greengrass certs, IoT endpoint) |
| `mosquitto/` | Broker configuration files |
| `certs/` | Certificate generation script |
| `greengrass-component/` | Greengrass component recipe for Mosquitto |

## IAM Credentials on the Pi

Once SSM is registered, the agent provides IAM credentials automatically via
the `pqc-workshop-greengrass-core-ssm-role`. This role has permissions to:

- Read Secrets Manager (Greengrass certs)
- Query IoT Core (endpoint, things, policies)
- Query Greengrass (core device status)
- Call STS (identity verification)

No `aws configure` or IAM user needed. The AWS CLI picks up SSM instance
credentials automatically.

## Resetting a Previously Provisioned Pi

```bash
# Reset SSM (if registered to a different account)
sudo ./setup-ssm-agent.sh --reset us-east-1

# Full reset (nuclear option)
sudo systemctl stop greengrass mosquitto-pqc
sudo rm -rf /greengrass/v2 /opt/mosquitto-pqc /opt/openssl-pqc
sudo ./setup-all.sh us-east-1
```
README

# ─── Create zip ─────────────────────────────────────────────────────────────
echo "[5/5] Creating zip bundle..."

cd /tmp
rm -f "$OUTPUT_ZIP"
zip -r "$OUTPUT_ZIP" "$(basename "$BUNDLE_DIR")" -x "*.DS_Store" >/dev/null

BUNDLE_SIZE=$(du -h "$OUTPUT_ZIP" | awk '{print $1}')
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ Bundle created!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  File: $OUTPUT_ZIP"
echo "  Size: $BUNDLE_SIZE"
echo ""
echo "  Copy to Pi:"
echo "    scp $OUTPUT_ZIP pi@<raspberry-pi-ip>:~/"
echo ""
echo "  Then on the Pi:"
echo "    unzip rpi-pqc-workshop-bundle.zip"
echo "    cd rpi-pqc-workshop-bundle"
echo "    sudo ./setup-all.sh us-east-1"
echo ""

# Clean up temp
rm -rf "$BUNDLE_DIR"
