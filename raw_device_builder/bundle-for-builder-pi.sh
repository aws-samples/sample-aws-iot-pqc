#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# bundle-for-builder-pi.sh
#
# Creates a self-contained zip bundle with everything needed to set up
# the builder Raspberry Pi:
#   - SSM Agent setup script (registers Pi as managed instance)
#   - Builder GUI application (Flask web server)
#   - Device manager (USB detection, esptool flash)
#   - Configuration defaults
#
# The bundle is designed to be copied to the Pi (via USB stick, SCP, or
# any file transfer method) and executed without needing AWS credentials
# on the Pi itself (SSM activation code is pre-fetched and included).
#
# Prerequisites:
#   - AWS CLI with credentials (to fetch SSM activation)
#   - The device-builder CloudFormation stack deployed
#     OR the SSM hybrid activation stack deployed
#
# Usage:
#   ./bundle-for-builder-pi.sh [--api-endpoint URL]
#
# Output:
#   ./builder-pi-bundle/           (directory ready to copy)
#   ./builder-pi-bundle.zip        (zip for USB transfer)
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUNDLE_DIR="$SCRIPT_DIR/builder-pi-bundle"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
API_ENDPOINT=""

# ─── Parse Arguments ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --api-endpoint) API_ENDPOINT="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--api-endpoint URL] [--region REGION]"
            echo ""
            echo "Options:"
            echo "  --api-endpoint   API Gateway URL for device registration"
            echo "  --region         AWS region (default: us-east-1)"
            exit 0
            ;;
        *) echo "[ERROR] Unknown argument: $1"; exit 1 ;;
    esac
done

# ─── Identity guard ──────────────────────────────────────────────────────────
# The SSM activation is created in whatever account the current credentials
# resolve to, and the Pi will register into THAT account. Getting this wrong
# (e.g. leaving AWS_PROFILE unset so it targets a default/live account) silently
# registers the Pi into the wrong account. Show the target and confirm.
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null) || {
    echo "[ERROR] No usable AWS credentials. Set AWS_PROFILE (or credentials) for the"
    echo "        account you deployed the workshop into, then re-run."
    exit 1
}
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "═══════════════════════════════════════════════════════════════"
echo "  Builder-Pi Bundle Generator"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Profile:        ${AWS_PROFILE:-<ambient/default credentials>}"
echo "  Target account: $ACCOUNT_ID"
echo "  Caller:         $CALLER_ARN"
echo "  Region:         $REGION"
echo "  Output:         $BUNDLE_DIR"
echo ""
echo "  The Pi will register as an SSM managed instance in account $ACCOUNT_ID."
echo "  This MUST be the account you deployed the workshop stack into."
echo ""
# Confirm unless CONFIRM_BUNDLE=yes (for automation). Only prompts on a TTY.
if [[ "${CONFIRM_BUNDLE:-}" != "yes" && -t 0 ]]; then
    read -r -p "  Create the activation in account $ACCOUNT_ID? [y/N] " reply
    [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "Aborted."; exit 1; }
    echo ""
fi

# ─── Step 1: Create SSM Hybrid Activation ────────────────────────────────────
echo "[1/4] Creating SSM hybrid activation..."

# Check if an activation already exists
EXISTING_ACTIVATION=$(aws ssm describe-activations \
    --query "ActivationList[?Description=='pqc-workshop-builder-pi' && ExpirationDate>='$(date -u +%Y-%m-%dT%H:%M:%S)'].{Id:ActivationId,Code:DefaultInstanceName}" \
    --output text --region "$REGION" 2>/dev/null | head -1) || EXISTING_ACTIVATION=""

if [[ -n "$EXISTING_ACTIVATION" ]]; then
    echo "       Using existing activation"
    ACTIVATION_ID=$(echo "$EXISTING_ACTIVATION" | awk '{print $1}')
else
    # Find the SSM role (either from device-builder stack or create inline)
    SSM_ROLE_NAME="pqc-workshop-builder-pi-ssm"
    SSM_ROLE_ARN=$(aws iam get-role --role-name "$SSM_ROLE_NAME" \
        --query 'Role.Arn' --output text --region "$REGION" 2>/dev/null) || SSM_ROLE_ARN=""

    if [[ -z "$SSM_ROLE_ARN" ]]; then
        echo "       Creating SSM role: $SSM_ROLE_NAME"
        aws iam create-role \
            --role-name "$SSM_ROLE_NAME" \
            --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ssm.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
            --region "$REGION" > /dev/null
        aws iam attach-role-policy \
            --role-name "$SSM_ROLE_NAME" \
            --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore \
            --region "$REGION"
        # Allow reading firmware from S3
        aws iam put-role-policy \
            --role-name "$SSM_ROLE_NAME" \
            --policy-name S3FirmwareAccess \
            --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject","s3:ListBucket"],"Resource":["arn:aws:s3:::pqc-workshop-firmware-*","arn:aws:s3:::pqc-workshop-firmware-*/*"]}]}' \
            --region "$REGION"
        # Allow calling API Gateway
        aws iam put-role-policy \
            --role-name "$SSM_ROLE_NAME" \
            --policy-name ApiGatewayAccess \
            --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["execute-api:Invoke"],"Resource":["*"]}]}' \
            --region "$REGION"
        SSM_ROLE_ARN=$(aws iam get-role --role-name "$SSM_ROLE_NAME" \
            --query 'Role.Arn' --output text --region "$REGION")
        echo "       Waiting for role propagation..."
        sleep 10
    fi

    # Create activation (valid for 7 days, 1 instance)
    ACTIVATION=$(aws ssm create-activation \
        --description "pqc-workshop-builder-pi" \
        --iam-role "$SSM_ROLE_NAME" \
        --registration-limit 1 \
        --expiration-date "$(date -u -v+7d +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -u -d '+7 days' +%Y-%m-%dT%H:%M:%S)" \
        --default-instance-name "pqc-workshop-builder-pi" \
        --region "$REGION" --output json)

    ACTIVATION_ID=$(echo "$ACTIVATION" | python3 -c "import sys,json; print(json.load(sys.stdin)['ActivationId'])")
    ACTIVATION_CODE=$(echo "$ACTIVATION" | python3 -c "import sys,json; print(json.load(sys.stdin)['ActivationCode'])")
fi

# If we don't have the code yet, we can't proceed (existing activation doesn't expose the code)
if [[ -z "${ACTIVATION_CODE:-}" ]]; then
    echo "       [WARN] Could not retrieve activation code. Creating new activation..."
    SSM_ROLE_NAME="pqc-workshop-builder-pi-ssm"
    ACTIVATION=$(aws ssm create-activation \
        --description "pqc-workshop-builder-pi-$(date +%s)" \
        --iam-role "$SSM_ROLE_NAME" \
        --registration-limit 1 \
        --expiration-date "$(date -u -v+7d +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -u -d '+7 days' +%Y-%m-%dT%H:%M:%S)" \
        --default-instance-name "pqc-workshop-builder-pi" \
        --region "$REGION" --output json)
    ACTIVATION_ID=$(echo "$ACTIVATION" | python3 -c "import sys,json; print(json.load(sys.stdin)['ActivationId'])")
    ACTIVATION_CODE=$(echo "$ACTIVATION" | python3 -c "import sys,json; print(json.load(sys.stdin)['ActivationCode'])")
fi

echo "       Activation ID: $ACTIVATION_ID"
echo "       ✅ SSM activation ready"

# ─── Step 2: Try to get API endpoint ────────────────────────────────────────
echo "[2/4] Detecting API endpoint..."

if [[ -z "$API_ENDPOINT" ]]; then
    # Prefer the master stack's BuilderApiEndpoint output (single-master deploy).
    API_ENDPOINT=$(aws cloudformation describe-stacks \
        --stack-name pqc-workshop \
        --query "Stacks[0].Outputs[?OutputKey=='BuilderApiEndpoint'].OutputValue | [0]" \
        --output text --region "$REGION" 2>/dev/null) || API_ENDPOINT=""

    # Fall back to the standalone/nested device-builder stack's ApiEndpoint
    # output (the nested stack name is pqc-workshop-DeviceBuilderStack-<hash>).
    if [[ -z "$API_ENDPOINT" || "$API_ENDPOINT" == "None" ]]; then
        DB_STACK=$(aws cloudformation list-stacks \
            --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
            --query "StackSummaries[?contains(StackName,'DeviceBuilder') || StackName=='pqc-workshop-device-builder'].StackName | [0]" \
            --output text --region "$REGION" 2>/dev/null) || DB_STACK=""
        if [[ -n "$DB_STACK" && "$DB_STACK" != "None" ]]; then
            API_ENDPOINT=$(aws cloudformation describe-stacks \
                --stack-name "$DB_STACK" \
                --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue | [0]" \
                --output text --region "$REGION" 2>/dev/null) || API_ENDPOINT=""
        fi
    fi

    if [[ -z "$API_ENDPOINT" || "$API_ENDPOINT" == "None" ]]; then
        API_ENDPOINT="https://REPLACE_WITH_API_GATEWAY_URL/prod"
        echo "       [WARN] Could not resolve the builder API endpoint from the"
        echo "       'pqc-workshop' master stack or a DeviceBuilder stack. Using a"
        echo "       placeholder — update config.json on the Pi after deployment."
    fi
fi
echo "       API: $API_ENDPOINT"

# ─── Step 3: Create bundle directory ────────────────────────────────────────
echo "[3/4] Creating bundle..."

rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR"

# Copy builder-pi application
cp -r "$SCRIPT_DIR/builder-pi/"* "$BUNDLE_DIR/"

# Bundle the wolfSSL PQC user_settings header so setup-builder-pi.sh can apply
# it to the Arduino wolfSSL library (step 5). The header lives with the
# firmware-builder pipeline sources (codebuild/), not in builder-pi/, so copy
# it in explicitly. Without this, step 5 emits a "not found" warning and the
# Pi's wolfSSL library keeps its default (non-PQC) settings.
WOLFSSL_HEADER="$SCRIPT_DIR/codebuild/wolfssl_user_settings_arduino.h"
if [[ -f "$WOLFSSL_HEADER" ]]; then
    cp "$WOLFSSL_HEADER" "$BUNDLE_DIR/wolfssl_user_settings_arduino.h"
    echo "       wolfSSL PQC header bundled"
else
    echo "       [WARN] wolfSSL header not found at $WOLFSSL_HEADER;"
    echo "              step 5 on the Pi will warn and skip the PQC config."
fi

# Update config with actual, per-deployment values. Only these keys are
# deployment-specific; everything else in config.json is a static default or a
# field the operator sets in the GUI (wifi_ssid/wifi_password/broker_endpoint).
# The firmware bucket is named pqc-workshop-firmware-<accountId> by the stack,
# so derive it from the account the activation was created in.
FIRMWARE_BUCKET="pqc-workshop-firmware-${ACCOUNT_ID}"
python3 -c "
import json
with open('$BUNDLE_DIR/config.json', 'r') as f:
    config = json.load(f)
config['api_endpoint'] = '$API_ENDPOINT'
config['region'] = '$REGION'
config['firmware_bucket'] = '$FIRMWARE_BUCKET'
with open('$BUNDLE_DIR/config.json', 'w') as f:
    json.dump(config, f, indent=2)
print('       config.json updated (api_endpoint, region, firmware_bucket)')
"
echo "       firmware_bucket: $FIRMWARE_BUCKET"

# Create SSM setup script with embedded activation credentials
cat > "$BUNDLE_DIR/setup-ssm.sh" << 'SSMEOF'
#!/bin/bash
# ─── SSM Agent Setup for Builder-Pi ─────────────────────────────────────────
# This script installs and registers the SSM agent. Run once with sudo.

set -euo pipefail

ACTIVATION_ID="__ACTIVATION_ID__"
ACTIVATION_CODE="__ACTIVATION_CODE__"
REGION="__REGION__"

# Handle --reset FIRST, before the already-registered check below. Otherwise a
# Pi that is already registered exits early and --reset can never run.
if [[ "${1:-}" == "--reset" ]]; then
    echo "Deregistering from previous activation..."
    systemctl stop amazon-ssm-agent 2>/dev/null || true
    rm -f /var/lib/amazon/ssm/registration
    echo "Previous registration cleared."
fi

# If still registered (and not resetting), stop — nothing to do.
if systemctl is-active --quiet amazon-ssm-agent 2>/dev/null; then
    EXISTING=$(cat /var/lib/amazon/ssm/registration 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('ManagedInstanceID',''))" 2>/dev/null) || EXISTING=""
    if [[ -n "$EXISTING" ]]; then
        echo "Already registered as: $EXISTING"
        echo "To re-register against a new activation, run: sudo $0 --reset"
        exit 0
    fi
fi

echo "Installing SSM Agent..."

# Install SSM agent (ARM64 for Raspberry Pi)
ARCH=$(dpkg --print-architecture)
if [[ "$ARCH" == "arm64" ]]; then
    DEB_URL="https://s3.$REGION.amazonaws.com/amazon-ssm-$REGION/latest/debian_arm64/amazon-ssm-agent.deb"
elif [[ "$ARCH" == "armhf" ]]; then
    DEB_URL="https://s3.$REGION.amazonaws.com/amazon-ssm-$REGION/latest/debian_arm/amazon-ssm-agent.deb"
else
    echo "Unsupported architecture: $ARCH"
    exit 1
fi

TMP_DEB="/tmp/amazon-ssm-agent.deb"
curl -fsSL "$DEB_URL" -o "$TMP_DEB"
dpkg -i "$TMP_DEB"

# Register with activation
systemctl stop amazon-ssm-agent
amazon-ssm-agent -register \
    -code "$ACTIVATION_CODE" \
    -id "$ACTIVATION_ID" \
    -region "$REGION"

systemctl enable amazon-ssm-agent
systemctl start amazon-ssm-agent

echo ""
echo "✅ SSM Agent registered!"
INSTANCE_ID=$(cat /var/lib/amazon/ssm/registration | python3 -c "import sys,json; print(json.load(sys.stdin)['ManagedInstanceID'])")
echo "   Instance ID: $INSTANCE_ID"
echo "   Connect with: aws ssm start-session --target $INSTANCE_ID --region $REGION"
SSMEOF

# Inject activation values
sed -i.bak "s|__ACTIVATION_ID__|$ACTIVATION_ID|g" "$BUNDLE_DIR/setup-ssm.sh"
sed -i.bak "s|__ACTIVATION_CODE__|$ACTIVATION_CODE|g" "$BUNDLE_DIR/setup-ssm.sh"
sed -i.bak "s|__REGION__|$REGION|g" "$BUNDLE_DIR/setup-ssm.sh"
rm -f "$BUNDLE_DIR/setup-ssm.sh.bak"
chmod +x "$BUNDLE_DIR/setup-ssm.sh"
chmod +x "$BUNDLE_DIR/setup-builder-pi.sh"

# Create a master setup script
cat > "$BUNDLE_DIR/setup-all.sh" << 'EOF'
#!/bin/bash
# ─── Builder-Pi Complete Setup ───────────────────────────────────────────────
# Runs all setup steps in order. Execute with sudo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "═══════════════════════════════════════════════════════════════"
echo "  Builder-Pi — Complete Setup"
echo "═══════════════════════════════════════════════════════════════"
echo ""

echo "Step 1: SSM Agent..."
"$SCRIPT_DIR/setup-ssm.sh"
echo ""

echo "Step 2: Builder Application..."
"$SCRIPT_DIR/setup-builder-pi.sh"
echo ""

echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ Setup complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  GUI: http://$(hostname -I | awk '{print $1}'):8080"
echo ""
EOF
chmod +x "$BUNDLE_DIR/setup-all.sh"

# ─── Step 4: Create zip ─────────────────────────────────────────────────────
echo "[4/4] Creating zip..."
cd "$SCRIPT_DIR"
rm -f builder-pi-bundle.zip
zip -r builder-pi-bundle.zip builder-pi-bundle/ -x "*.pyc" "*__pycache__*"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ Builder-Pi bundle created!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Directory: $BUNDLE_DIR"
echo "  Zip:       $SCRIPT_DIR/builder-pi-bundle.zip"
echo "  Activation ID: $ACTIVATION_ID"
echo "  Region: $REGION"
echo "  API: $API_ENDPOINT"
echo ""
echo "  Transfer to Pi and run:"
echo "    unzip builder-pi-bundle.zip"
echo "    cd builder-pi-bundle"
echo "    sudo ./setup-all.sh"
echo ""
echo "  ⚠️  Activation expires in 7 days. Run this script again if needed."
echo ""
