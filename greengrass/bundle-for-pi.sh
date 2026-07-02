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

cat > "$BUNDLE_DIR/setup-all.sh" << 'SETUP_ALL'
#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-all.sh — Run all Pi setup steps in sequence
#
# Usage:
#   sudo ./setup-all.sh <aws-region>
#
# This is a convenience wrapper that runs all steps in order.
# You can also run each step individually if you prefer.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REGION="${1:-us-east-1}"
THING_NAME="pqc-workshop-greengrass-core"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "[ERROR] Run as root: sudo $0 $*"
    exit 1
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  PQC Workshop — Complete Raspberry Pi Setup"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Region: $REGION"
echo "  Thing:  $THING_NAME"
echo ""
echo "  This will run all setup steps in sequence:"
echo "    1. Install SSM Agent (remote access)"
echo "    2. Install Greengrass v2"
echo "    3. Build PQC Mosquitto (OpenSSL 3.5 + Mosquitto)"
echo "    4. Generate broker certificates"
echo "    5. Configure and start Mosquitto"
echo ""
read -p "  Continue? [Y/n] " -r
echo ""
if [[ ! "$REPLY" =~ ^[Yy]?$ ]]; then
    echo "Aborted."
    exit 0
fi

cd "$SCRIPT_DIR"

# ─── Step 1: SSM Agent ──────────────────────────────────────────────────────
echo ""
echo "━━━ Step 1/5: SSM Agent ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ -f credentials/ssm-activation.json ]]; then
    # Extract creds and register directly (no AWS CLI needed for this step)
    ACTIVATION_ID=$(python3 -c "import json; print(json.load(open('credentials/ssm-activation.json'))['activationId'])")
    ACTIVATION_CODE=$(python3 -c "import json; print(json.load(open('credentials/ssm-activation.json'))['activationCode'])")

    # Install SSM agent
    ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
    case "$ARCH" in
        arm64|aarch64) DEB_ARCH="arm64" ;;
        armhf|armv7l) DEB_ARCH="arm" ;;
        *) echo "[ERROR] Unsupported arch: $ARCH"; exit 1 ;;
    esac

    SSM_DEB="/tmp/amazon-ssm-agent.deb"
    echo "  Downloading SSM Agent..."
    curl -sL -o "$SSM_DEB" \
        "https://s3.${REGION}.amazonaws.com/amazon-ssm-${REGION}/latest/debian_${DEB_ARCH}/amazon-ssm-agent.deb"
    dpkg -i "$SSM_DEB" >/dev/null 2>&1 || apt-get install -f -y -qq >/dev/null 2>&1
    rm -f "$SSM_DEB"

    systemctl stop amazon-ssm-agent 2>/dev/null || true
    amazon-ssm-agent -register -code "$ACTIVATION_CODE" -id "$ACTIVATION_ID" -region "$REGION" -y
    systemctl enable amazon-ssm-agent
    systemctl start amazon-ssm-agent
    echo "  ✅ SSM Agent registered and running"
else
    echo "  [SKIP] No SSM activation credentials in bundle"
fi

# ─── Step 2: Greengrass ─────────────────────────────────────────────────────
echo ""
echo "━━━ Step 2/5: Greengrass v2 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ -f credentials/greengrass-credentials.json ]]; then
    # Install Java if needed
    if ! command -v java &>/dev/null; then
        echo "  Installing Java..."
        apt-get update -qq
        apt-get install -y -qq default-jdk
    fi

    # Extract certs from bundled credentials
    GG_CERT_DIR="/tmp/gg-certs"
    mkdir -p "$GG_CERT_DIR"
    python3 -c "
import json
creds = json.load(open('credentials/greengrass-credentials.json'))
open('$GG_CERT_DIR/device.pem.crt', 'w').write(creds['certificatePem'])
open('$GG_CERT_DIR/private.pem.key', 'w').write(creds['privateKey'])
print('  Greengrass certs extracted')
"
    curl -s -o "$GG_CERT_DIR/AmazonRootCA1.pem" https://www.amazontrust.com/repository/AmazonRootCA1.pem

    # Get IoT endpoints
    IOT_ENDPOINT=$(cat credentials/iot-endpoint.txt)
    # Derive credential endpoint from data endpoint
    CRED_ENDPOINT=$(echo "$IOT_ENDPOINT" | sed 's/-ats\.iot\./.credentials.iot./')

    # Download and install Greengrass
    GG_ROOT="/greengrass/v2"
    INSTALLER_DIR="/tmp/gg-installer"
    mkdir -p "$INSTALLER_DIR"
    if [[ ! -f "$INSTALLER_DIR/greengrass-nucleus-latest.zip" ]]; then
        echo "  Downloading Greengrass nucleus..."
        curl -s https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-nucleus-latest.zip \
            -o "$INSTALLER_DIR/greengrass-nucleus-latest.zip"
    fi
    rm -rf "$INSTALLER_DIR/GreengrassInstaller"
    unzip -qo "$INSTALLER_DIR/greengrass-nucleus-latest.zip" -d "$INSTALLER_DIR/GreengrassInstaller"

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
    echo "  ✅ Greengrass installed and running"
else
    echo "  [SKIP] No Greengrass credentials in bundle"
fi

# ─── Step 3: Build PQC Mosquitto ────────────────────────────────────────────
echo ""
echo "━━━ Step 3/5: PQC Mosquitto (OpenSSL 3.5 + Mosquitto) ━━━━━━"
echo "  This compiles from source and takes 15-20 minutes..."
./setup-mosquitto-pqc.sh
echo "  ✅ PQC Mosquitto installed"

# ─── Step 4: Generate broker certs ──────────────────────────────────────────
echo ""
echo "━━━ Step 4/5: Generate Broker Certificates ━━━━━━━━━━━━━━━━━━"
cd certs
LD_LIBRARY_PATH=/opt/openssl-pqc/lib ./generate-broker-certs.sh 100
cd "$SCRIPT_DIR"
echo "  ✅ Broker + device certificates generated"

# ─── Step 5: Configure and start Mosquitto ──────────────────────────────────
echo ""
echo "━━━ Step 5/5: Configure and Start Mosquitto ━━━━━━━━━━━━━━━━━"

CERT_DIR="/opt/mosquitto-pqc/etc/mosquitto/certs"
mkdir -p "$CERT_DIR"
mkdir -p /opt/mosquitto-pqc/etc/mosquitto/conf.d

# Copy broker certs
cp certs/output/ca.crt "$CERT_DIR/"
cp certs/output/broker.crt "$CERT_DIR/"
cp certs/output/broker.key "$CERT_DIR/"

# Copy config
cp mosquitto/mosquitto-pqc.conf /opt/mosquitto-pqc/etc/mosquitto/
cp mosquitto/acl.conf /opt/mosquitto-pqc/etc/mosquitto/

# Configure bridge
IOT_ENDPOINT=$(cat credentials/iot-endpoint.txt)
sed "s|REPLACE_WITH_IOT_ENDPOINT|$IOT_ENDPOINT|g; s|REPLACE_WITH_GREENGRASS_THING_NAME|$THING_NAME|g" \
    mosquitto/bridge-iotcore.conf > /opt/mosquitto-pqc/etc/mosquitto/conf.d/bridge-iotcore.conf

# Greengrass device cert for bridge
python3 -c "
import json
creds = json.load(open('credentials/greengrass-credentials.json'))
open('$CERT_DIR/greengrass-device.crt', 'w').write(creds['certificatePem'])
open('$CERT_DIR/greengrass-device.key', 'w').write(creds['privateKey'])
print('  Greengrass bridge certs installed')
"

# Amazon Root CA for bridge → IoT Core
curl -so "$CERT_DIR/AmazonRootCA1.pem" https://www.amazontrust.com/repository/AmazonRootCA1.pem

# Start Mosquitto
systemctl start mosquitto-pqc 2>/dev/null || systemctl restart mosquitto-pqc
systemctl enable mosquitto-pqc
echo "  ✅ Mosquitto running with PQC TLS"

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ Raspberry Pi setup complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Run the readiness check to verify:"
echo "    ./check_rpi_setup_status.sh"
echo ""
SETUP_ALL

chmod +x "$BUNDLE_DIR/setup-all.sh"

# ─── Create README ──────────────────────────────────────────────────────────
cat > "$BUNDLE_DIR/README.md" << 'README'
# PQC Workshop — Raspberry Pi Bundle

Everything needed to provision the Raspberry Pi for the PQC workshop.

## Quick Start (automated)

```bash
# On the Pi:
unzip rpi-pqc-workshop-bundle.zip
cd rpi-pqc-workshop-bundle
sudo ./setup-all.sh us-east-1
```

This runs all steps automatically (~30 minutes total).

## Manual Steps (if you prefer)

```bash
# 1. SSM Agent (enables remote access from your laptop)
sudo ./setup-ssm-agent.sh us-east-1

# 2. Greengrass v2 nucleus
sudo -E ./setup-greengrass-core.sh us-east-1

# 3. Build PQC Mosquitto (compiles OpenSSL 3.5 + Mosquitto from source)
sudo ./setup-mosquitto-pqc.sh

# 4. Generate broker + device certs
cd certs && LD_LIBRARY_PATH=/opt/openssl-pqc/lib ./generate-broker-certs.sh 100 && cd ..

# 5. Install certs/config and start Mosquitto
# (see greengrass/README.md steps 6-8 for details)

# 6. Verify everything
./check_rpi_setup_status.sh
```

## What's Included

| File/Folder | Purpose |
|-------------|---------|
| `setup-all.sh` | Master script (runs everything in sequence) |
| `setup-ssm-agent.sh` | Installs + registers SSM agent |
| `setup-greengrass-core.sh` | Installs Greengrass v2 nucleus |
| `setup-mosquitto-pqc.sh` | Compiles OpenSSL 3.5 + Mosquitto from source |
| `check_rpi_setup_status.sh` | Validates all components are working |
| `credentials/` | Pre-fetched secrets (SSM activation, Greengrass certs, IoT endpoint) |
| `mosquitto/` | Broker configuration files |
| `certs/` | Certificate generation script |
| `greengrass-component/` | Greengrass component recipe for Mosquitto |

## Credentials

The `credentials/` folder contains pre-fetched secrets so the Pi doesn't
need AWS CLI credentials for most setup steps:

- `ssm-activation.json` — SSM hybrid activation ID + code
- `greengrass-credentials.json` — Device cert + private key
- `iot-endpoint.txt` — IoT Core data endpoint

These were fetched from Secrets Manager at bundle creation time.

## Resetting a Previously Provisioned Pi

If the Pi was set up for a different workshop/account:

```bash
# Reset SSM
sudo ./setup-ssm-agent.sh --reset us-east-1

# Reset Greengrass (stop + remove)
sudo systemctl stop greengrass
sudo rm -rf /greengrass/v2

# Reset Mosquitto
sudo systemctl stop mosquitto-pqc
sudo rm -rf /opt/mosquitto-pqc

# Then run setup-all.sh fresh
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
