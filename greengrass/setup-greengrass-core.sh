#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-greengrass-core.sh
#
# Installs AWS IoT Greengrass v2 on the Raspberry Pi and provisions it
# using credentials from the CloudFormation stack (stored in Secrets Manager).
#
# Run this AFTER deploying the pqc-workshop CloudFormation stack.
#
# Prerequisites:
#   - Raspberry Pi with 64-bit Raspberry Pi OS
#   - AWS CLI configured with credentials that can read Secrets Manager
#   - Java 11+ installed (sudo apt install default-jdk)
#   - Internet connectivity
#
# Usage:
#   sudo ./setup-greengrass-core.sh <aws-region> <greengrass-thing-name>
#
# Example:
#   sudo ./setup-greengrass-core.sh us-east-1 pqc-workshop-greengrass-core
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

AWS_REGION="${1:-}"
THING_NAME="${2:-pqc-workshop-greengrass-core}"
GG_ROOT="/greengrass/v2"

if [[ -z "$AWS_REGION" ]]; then
    echo "Usage: sudo $0 <aws-region> [greengrass-thing-name]"
    echo "Example: sudo $0 us-east-1 pqc-workshop-greengrass-core"
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "[ERROR] Run as root: sudo $0 $*"
    exit 1
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  Greengrass v2 Core Device Setup"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "Region:     $AWS_REGION"
echo "Thing Name: $THING_NAME"
echo "GG Root:    $GG_ROOT"
echo ""

# ─── Step 1: Install Java ───────────────────────────────────────────────────
echo "[1/5] Checking Java..."
if ! command -v java &>/dev/null; then
    echo "       Installing OpenJDK 17..."
    apt-get update -qq
    apt-get install -y -qq default-jdk
fi
JAVA_VERSION=$(java -version 2>&1 | head -1)
echo "       Java: $JAVA_VERSION"

# ─── Step 2: Retrieve credentials from Secrets Manager ───────────────────────
echo "[2/5] Retrieving device credentials from Secrets Manager..."

SECRET_NAME="iot/${THING_NAME}/greengrass-credentials"
SECRET_JSON=$(aws secretsmanager get-secret-value \
    --region "$AWS_REGION" \
    --secret-id "$SECRET_NAME" \
    --query 'SecretString' \
    --output text)

CERT_DIR="/tmp/gg-certs"
mkdir -p "$CERT_DIR"

echo "$SECRET_JSON" | python3 -c "
import json, sys
creds = json.load(sys.stdin)
with open('$CERT_DIR/device.pem.crt', 'w') as f:
    f.write(creds['certificatePem'])
with open('$CERT_DIR/private.pem.key', 'w') as f:
    f.write(creds['privateKey'])
print('       Credentials written to $CERT_DIR/')
"

# Download Amazon Root CA
curl -s -o "$CERT_DIR/AmazonRootCA1.pem" \
    https://www.amazontrust.com/repository/AmazonRootCA1.pem
echo "       Root CA downloaded"

# ─── Step 3: Get IoT endpoint ────────────────────────────────────────────────
echo "[3/5] Getting IoT data endpoint..."
IOT_ENDPOINT=$(aws iot describe-endpoint \
    --region "$AWS_REGION" \
    --endpoint-type iot:Data-ATS \
    --query 'endpointAddress' \
    --output text)
echo "       Endpoint: $IOT_ENDPOINT"

# Get IoT credential endpoint
CRED_ENDPOINT=$(aws iot describe-endpoint \
    --region "$AWS_REGION" \
    --endpoint-type iot:CredentialProvider \
    --query 'endpointAddress' \
    --output text)
echo "       Cred endpoint: $CRED_ENDPOINT"

# ─── Step 4: Download and install Greengrass ─────────────────────────────────
echo "[4/5] Installing Greengrass v2 nucleus..."

INSTALLER_DIR="/tmp/gg-installer"
mkdir -p "$INSTALLER_DIR"
cd "$INSTALLER_DIR"

if [[ ! -f "greengrass-nucleus-latest.zip" ]]; then
    curl -s https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-nucleus-latest.zip \
        -o greengrass-nucleus-latest.zip
fi

rm -rf GreengrassInstaller
unzip -qo greengrass-nucleus-latest.zip -d GreengrassInstaller

# ─── Step 5: Run the installer ───────────────────────────────────────────────
echo "[5/5] Running Greengrass installer..."

java -Droot="$GG_ROOT" -Dlog.store=FILE \
    -jar "$INSTALLER_DIR/GreengrassInstaller/lib/Greengrass.jar" \
    --aws-region "$AWS_REGION" \
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
  certificateFilePath: "$CERT_DIR/device.pem.crt"
  privateKeyPath: "$CERT_DIR/private.pem.key"
  rootCaPath: "$CERT_DIR/AmazonRootCA1.pem"
  rootpath: "$GG_ROOT"
  thingName: "$THING_NAME"
services:
  aws.greengrass.Nucleus:
    componentType: NUCLEUS
    version: ""
    configuration:
      awsRegion: "$AWS_REGION"
      iotRoleAlias: "${THING_NAME}-TES-alias"
      iotDataEndpoint: "$IOT_ENDPOINT"
      iotCredEndpoint: "$CRED_ENDPOINT"
INITCONFIG

# Clean up temp certs
rm -rf "$CERT_DIR"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Greengrass v2 installation complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Service: greengrass.service"
echo "  Status:  sudo systemctl status greengrass"
echo "  Logs:    sudo tail -f $GG_ROOT/logs/greengrass.log"
echo "  CLI:     sudo $GG_ROOT/bin/greengrass-cli component list"
echo ""
echo "  Next: Run setup-mosquitto-pqc.sh to install the PQC broker"
echo ""
