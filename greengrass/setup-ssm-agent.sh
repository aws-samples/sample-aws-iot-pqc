#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-ssm-agent.sh
#
# Installs and registers the AWS SSM Agent on the Raspberry Pi as a
# hybrid managed instance. Once registered, you can remotely connect
# via Session Manager without SSH, VPN, or open inbound ports.
#
# This script:
#   1. Installs the SSM agent (ARM64 .deb package)
#   2. Retrieves the hybrid activation credentials from Secrets Manager
#   3. Registers the Pi with Systems Manager
#   4. Starts and enables the agent service
#
# If the Pi was previously registered to a DIFFERENT account/activation,
# use --reset to deregister and re-register (see "Reset" section below).
#
# Prerequisites:
#   - Raspberry Pi with 64-bit Raspberry Pi OS (Bookworm or later)
#   - Internet connectivity (outbound HTTPS on port 443)
#   - AWS CLI configured with credentials that can read Secrets Manager
#     (either env vars or a profile with SecretsManager read access)
#   - The pqc-workshop CloudFormation stack deployed (includes SSM activation)
#
# Usage:
#   sudo ./setup-ssm-agent.sh <aws-region> [stack-name]
#   sudo ./setup-ssm-agent.sh --reset <aws-region> [stack-name]
#
# Examples:
#   sudo ./setup-ssm-agent.sh us-east-1
#   sudo ./setup-ssm-agent.sh us-east-1 pqc-workshop
#   sudo ./setup-ssm-agent.sh --reset us-east-1 pqc-workshop
#
# After setup, connect from your laptop with:
#   aws ssm start-session --target <instance-id> --region <region>
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ─── Parse Arguments ─────────────────────────────────────────────────────────
RESET=false
if [[ "${1:-}" == "--reset" ]]; then
    RESET=true
    shift
fi

AWS_REGION="${1:-}"
STACK_NAME="${2:-pqc-workshop}"

if [[ -z "$AWS_REGION" ]]; then
    echo "Usage: sudo $0 [--reset] <aws-region> [stack-name]"
    echo ""
    echo "Arguments:"
    echo "  --reset        Deregister from previous account before re-registering"
    echo "  aws-region     AWS region where the workshop stack is deployed"
    echo "  stack-name     CloudFormation stack name (default: pqc-workshop)"
    echo ""
    echo "Examples:"
    echo "  sudo $0 us-east-1"
    echo "  sudo $0 --reset us-east-1 pqc-workshop"
    echo ""
    echo "After setup, connect with:"
    echo "  aws ssm start-session --target <instance-id> --region <region>"
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "[ERROR] This script must be run as root: sudo $0 $*"
    exit 1
fi

export AWS_DEFAULT_REGION="$AWS_REGION"

# Derive secret name from stack convention
# The stack stores activation credentials in Secrets Manager
GREENGRASS_THING="pqc-workshop-greengrass-core"
SECRET_ID="iot/${GREENGRASS_THING}/ssm-activation"

echo "═══════════════════════════════════════════════════════════════"
echo "  SSM Agent Setup — Raspberry Pi Hybrid Activation"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Region:     $AWS_REGION"
echo "  Stack:      $STACK_NAME"
echo "  Reset mode: $RESET"
echo ""

# ─── Reset (if requested) ───────────────────────────────────────────────────
if [[ "$RESET" == true ]]; then
    echo "─── Resetting SSM Agent ─────────────────────────────────────"
    echo ""
    echo "  Stopping agent and removing registration..."
    echo ""

    # Stop the agent
    systemctl stop amazon-ssm-agent 2>/dev/null || true

    # Remove the registration (this deregisters from the previous account)
    REGISTRATION_FILE="/var/lib/amazon/ssm/registration"
    if [[ -f "$REGISTRATION_FILE" ]]; then
        echo "  Found existing registration at: $REGISTRATION_FILE"
        echo "  Removing..."
        rm -f "$REGISTRATION_FILE"
        echo "  ✅ Registration removed"
    else
        echo "  No existing registration found (clean state)"
    fi

    # Also clean up the Vault directory which caches credentials
    VAULT_DIR="/var/lib/amazon/ssm/Vault"
    if [[ -d "$VAULT_DIR" ]]; then
        echo "  Clearing credential vault..."
        rm -rf "$VAULT_DIR"
        mkdir -p "$VAULT_DIR"
        echo "  ✅ Vault cleared"
    fi

    # Clean instance fingerprint (forces new identity)
    FINGERPRINT_FILE="/var/lib/amazon/ssm/Vault/Store/RegistrationKey"
    rm -f "$FINGERPRINT_FILE" 2>/dev/null || true

    echo ""
    echo "  Pi is deregistered from the previous account."
    echo "  Proceeding with fresh registration..."
    echo ""
fi

# ─── Step 1: Install SSM Agent ───────────────────────────────────────────────
echo "[1/4] Installing SSM Agent..."

# Detect architecture
ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
case "$ARCH" in
    arm64|aarch64)
        DEB_ARCH="arm64"
        ;;
    armhf|armv7l)
        DEB_ARCH="arm"
        ;;
    *)
        echo "[ERROR] Unsupported architecture: $ARCH"
        echo "        Supported: arm64 (64-bit Pi OS), armhf (32-bit Pi OS)"
        exit 1
        ;;
esac

SSM_DEB="/tmp/amazon-ssm-agent.deb"
SSM_URL="https://s3.${AWS_REGION}.amazonaws.com/amazon-ssm-${AWS_REGION}/latest/debian_${DEB_ARCH}/amazon-ssm-agent.deb"

# Check if already installed
if command -v amazon-ssm-agent &>/dev/null; then
    CURRENT_VERSION=$(amazon-ssm-agent --version 2>/dev/null || echo "unknown")
    echo "       SSM Agent already installed: $CURRENT_VERSION"
    echo "       Downloading latest version to update..."
fi

echo "       Downloading from: $SSM_URL"
curl -sL -o "$SSM_DEB" "$SSM_URL"

dpkg -i "$SSM_DEB" >/dev/null 2>&1 || {
    # Fix any dependency issues
    apt-get install -f -y -qq >/dev/null 2>&1
    dpkg -i "$SSM_DEB" >/dev/null 2>&1
}

rm -f "$SSM_DEB"
echo "       ✅ SSM Agent installed"

# ─── Step 2: Stop the agent before registration ─────────────────────────────
echo "[2/4] Stopping agent for registration..."
systemctl stop amazon-ssm-agent 2>/dev/null || true

# ─── Step 3: Retrieve activation credentials from Secrets Manager ────────────
echo "[3/4] Retrieving activation credentials..."

# The user running this script needs AWS creds to read Secrets Manager.
# This could be temporary creds passed via env vars or a configured profile.
ACTIVATION_JSON=$(aws secretsmanager get-secret-value \
    --region "$AWS_REGION" \
    --secret-id "$SECRET_ID" \
    --query 'SecretString' \
    --output text 2>&1) || {
    echo ""
    echo "[ERROR] Failed to retrieve activation credentials from Secrets Manager."
    echo "        Secret: $SECRET_ID"
    echo "        Region: $AWS_REGION"
    echo ""
    echo "        Make sure:"
    echo "          1. The pqc-workshop stack is deployed (includes SSM activation)"
    echo "          2. AWS CLI has credentials with secretsmanager:GetSecretValue"
    echo "          3. The region is correct"
    echo ""
    echo "        To set temporary credentials:"
    echo "          export AWS_ACCESS_KEY_ID=..."
    echo "          export AWS_SECRET_ACCESS_KEY=..."
    echo "          export AWS_SESSION_TOKEN=..."
    exit 1
}

ACTIVATION_ID=$(echo "$ACTIVATION_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['activationId'])")
ACTIVATION_CODE=$(echo "$ACTIVATION_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['activationCode'])")
EXPIRATION=$(echo "$ACTIVATION_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['expirationDate'])")

echo "       Activation ID: $ACTIVATION_ID"
echo "       Expires:       $EXPIRATION"

# ─── Step 4: Register and start ──────────────────────────────────────────────
echo "[4/4] Registering with Systems Manager..."

amazon-ssm-agent -register \
    -code "$ACTIVATION_CODE" \
    -id "$ACTIVATION_ID" \
    -region "$AWS_REGION" \
    -y 2>&1 | grep -v "^$" | sed 's/^/       /'

# Start and enable the service
systemctl enable amazon-ssm-agent
systemctl start amazon-ssm-agent

# Wait a moment for registration to propagate
sleep 5

# Try to get the instance ID
INSTANCE_ID=""
if [[ -f "/var/lib/amazon/ssm/registration" ]]; then
    INSTANCE_ID=$(python3 -c "
import json
with open('/var/lib/amazon/ssm/registration') as f:
    reg = json.load(f)
    print(reg.get('ManagedInstanceID', 'unknown'))
" 2>/dev/null || echo "unknown")
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ SSM Agent registered and running!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Instance ID:  $INSTANCE_ID"
echo "  Region:       $AWS_REGION"
echo "  Service:      amazon-ssm-agent"
echo ""
echo "  Verify status:"
echo "    sudo systemctl status amazon-ssm-agent"
echo ""
echo "  Connect from your laptop:"
echo "    aws ssm start-session --target $INSTANCE_ID --region $AWS_REGION"
echo ""
echo "  Run a remote command:"
echo "    aws ssm send-command \\"
echo "      --instance-ids $INSTANCE_ID \\"
echo "      --document-name AWS-RunShellScript \\"
echo "      --parameters 'commands=[\"hostname && uptime\"]' \\"
echo "      --region $AWS_REGION"
echo ""
echo "  The Pi will appear in the AWS Console:"
echo "    Systems Manager > Fleet Manager > Managed Nodes"
echo ""
echo "  Note: It may take 1-2 minutes to appear in the console."
echo ""
