#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# bundle-for-builder-pi.sh
#
# Creates a MINIMAL zip bundle whose ONLY job is to register the builder
# Raspberry Pi as an SSM managed instance. Everything else (arduino-cli, the
# ESP32 core, the builder GUI app, systemd service, CloudWatch agent) is
# installed AFTER registration by SSM documents run from the deploy host — see
# setup-builder-pi-remote.sh and the install-awscli / install-toolchain /
# deploy-app / configure-service documents created by the SSM activation stack.
#
# WHY SO SMALL: the old bundle carried the whole app + a heavy on-Pi setup
# script that the operator ran under sudo. A mid-script failure under `set -e`
# once died silently (the $HOME-unset abort), leaving the GUI down with no
# captured output. Running the provisioning remotely via SSM (as root, with a
# consistent env, output captured to SSM + CloudWatch, each step re-runnable in
# isolation) fixed that. So this bundle is now just the SSM bootstrap.
#
# The bundle is copied to the Pi (USB stick / SCP / any transfer) and run once.
# It contains only:
#   - setup-ssm.sh   (installs + registers the SSM agent; activation baked in)
#   - README.txt     (the two-step runbook)
#
# Prerequisites:
#   - AWS CLI with credentials for the workshop account
#   - The workshop stack deployed (so the CFN-owned builder-Pi IAM role
#     'pqc-workshop-builder-pi-ssm' and the SSM documents exist)
#
# Usage:
#   ./bundle-for-builder-pi.sh [--region REGION]
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
BUNDLE_DIR="$SCRIPT_DIR/builder-pi-bundle"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

# The builder-Pi IAM role is now created by CloudFormation (the SSM activation
# stack), not by this script. We reference it by name when minting the
# activation, and fail fast if it does not exist.
SSM_ROLE_NAME="pqc-workshop-builder-pi-ssm"

# ─── Parse Arguments ─────────────────────────────────────────────────────────
# --profile is accepted for parity with setup-builder-pi-remote.sh and
# deploy-workshop.sh; when given it is exported as AWS_PROFILE so every aws call
# (and the identity guard below) targets that account.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --region) REGION="$2"; shift 2 ;;
        --profile) export AWS_PROFILE="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--region REGION] [--profile NAME]"
            echo ""
            echo "Options:"
            echo "  --region   AWS region (default: \$AWS_DEFAULT_REGION or us-east-1)"
            echo "  --profile  AWS CLI profile (optional; else ambient/AWS_PROFILE)"
            echo ""
            echo "Creates builder-pi-bundle.zip containing only the SSM agent"
            echo "bootstrap. After registering the Pi, run setup-builder-pi-remote.sh"
            echo "from the deploy host to provision it via SSM documents."
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
echo "  Builder-Pi Bundle Generator (SSM bootstrap only)"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Profile:        ${AWS_PROFILE:-<ambient/default credentials>}"
echo "  Target account: $ACCOUNT_ID"
echo "  Caller:         $CALLER_ARN"
echo "  Region:         $REGION"
echo "  Output:         $BUNDLE_DIR"
echo ""
echo "  The Pi will register as an SSM managed instance in account $ACCOUNT_ID"
echo "  using the CloudFormation-owned role '$SSM_ROLE_NAME'."
echo "  This MUST be the account you deployed the workshop stack into."
echo ""
# Confirm unless CONFIRM_BUNDLE=yes (for automation). Only prompts on a TTY.
if [[ "${CONFIRM_BUNDLE:-}" != "yes" && -t 0 ]]; then
    read -r -p "  Create the activation in account $ACCOUNT_ID? [y/N] " reply
    [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "Aborted."; exit 1; }
    echo ""
fi

# ─── Step 1: Verify the CFN-owned SSM role exists ────────────────────────────
# The role is created by the SSM activation stack (nested in the master). If it
# is missing, the workshop stack has not been deployed (or was deployed without
# the builder-Pi role) — minting an activation against a non-existent role would
# fail later, so stop now with a clear message.
echo "[1/4] Verifying the builder-Pi IAM role ($SSM_ROLE_NAME)..."
if ! aws iam get-role --role-name "$SSM_ROLE_NAME" >/dev/null 2>&1; then
    echo "[ERROR] IAM role '$SSM_ROLE_NAME' not found in account $ACCOUNT_ID."
    echo "        It is created by the workshop's SSM activation stack. Deploy the"
    echo "        workshop first (cloudformation/deploy-workshop.sh), then re-run."
    exit 1
fi
echo "       role present"

# ─── Step 2: Create the SSM hybrid activation ────────────────────────────────
echo "[2/4] Creating SSM hybrid activation..."
ACTIVATION=$(aws ssm create-activation \
    --description "pqc-workshop-builder-pi" \
    --iam-role "$SSM_ROLE_NAME" \
    --registration-limit 1 \
    --expiration-date "$(date -u -v+7d +%Y-%m-%dT%H:%M:%S 2>/dev/null || date -u -d '+7 days' +%Y-%m-%dT%H:%M:%S)" \
    --default-instance-name "pqc-workshop-builder-pi" \
    --region "$REGION" --output json)
ACTIVATION_ID=$(echo "$ACTIVATION" | python3 -c "import sys,json; print(json.load(sys.stdin)['ActivationId'])")
ACTIVATION_CODE=$(echo "$ACTIVATION" | python3 -c "import sys,json; print(json.load(sys.stdin)['ActivationCode'])")
echo "       Activation ID: $ACTIVATION_ID"
echo "       ✅ SSM activation ready"

# ─── Step 2b: Prune stale managed instances ──────────────────────────────────
# Each new bundle mints a new activation, and the Pi re-registers under a new
# mi-... id; the old ids linger as ConnectionLost. Auto-prune them so
# `describe-instance-information` (and the orchestrator's Online check) stays
# clean. Best-effort — never fail the bundle over this.
echo "       Pruning stale (ConnectionLost) builder-Pi managed instances..."
STALE=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=pqc-workshop-builder-pi" \
    --query "InstanceInformationList[?PingStatus=='ConnectionLost'].InstanceId" \
    --output text --region "$REGION" 2>/dev/null || true)
# Fall back to the DefaultInstanceName-based lookup if the tag filter finds none
# (hybrid instances may not carry the Name tag depending on registration).
if [[ -z "$STALE" ]]; then
    STALE=$(aws ssm describe-instance-information \
        --query "InstanceInformationList[?PingStatus=='ConnectionLost' && Name=='pqc-workshop-builder-pi'].InstanceId" \
        --output text --region "$REGION" 2>/dev/null || true)
fi
for mi in $STALE; do
    [[ "$mi" == mi-* ]] || continue
    echo "         - deregistering $mi"
    aws ssm deregister-managed-instance --instance-id "$mi" --region "$REGION" >/dev/null 2>&1 \
        || echo "           (could not deregister $mi; ignoring)"
done

# ─── Step 3: Create the bundle directory (SSM bootstrap only) ────────────────
echo "[3/4] Creating bundle..."
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR"

# The SSM agent installer/register script, with the activation baked in. This is
# the ONLY executable in the bundle now.
cat > "$BUNDLE_DIR/setup-ssm.sh" << 'SSMEOF'
#!/bin/bash
# ─── SSM Agent Setup for Builder-Pi ─────────────────────────────────────────
# Installs and registers the SSM agent so the Pi becomes a managed instance.
# MUST be run with sudo (as root). After this, provision the Pi from the deploy
# host with setup-builder-pi-remote.sh (which runs the SSM documents remotely).

set -euo pipefail

# ─── Require root ────────────────────────────────────────────────────────────
# Installing the SSM agent, writing /var/lib/amazon/ssm/registration, and
# managing the systemd service all need root. If this is run as a normal user it
# would fail part-way and leave the Pi in a broken, half-registered state — so
# refuse to run at all unless invoked with sudo/root, with a clear message.
if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: setup-ssm.sh must be run as root. Re-run it with sudo:" >&2
    echo "         sudo $0${1:+ $1}" >&2
    exit 1
fi

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
echo ""
echo "   NEXT: from the deploy host, provision this Pi via SSM:"
echo "     ./raw_device_builder/setup-builder-pi-remote.sh --region $REGION"
SSMEOF

# Inject activation values.
sed -i.bak "s|__ACTIVATION_ID__|$ACTIVATION_ID|g" "$BUNDLE_DIR/setup-ssm.sh"
sed -i.bak "s|__ACTIVATION_CODE__|$ACTIVATION_CODE|g" "$BUNDLE_DIR/setup-ssm.sh"
sed -i.bak "s|__REGION__|$REGION|g" "$BUNDLE_DIR/setup-ssm.sh"
rm -f "$BUNDLE_DIR/setup-ssm.sh.bak"
chmod +x "$BUNDLE_DIR/setup-ssm.sh"

# A short README with the two-step runbook.
cat > "$BUNDLE_DIR/README.txt" << README
PQC Workshop — Builder-Pi bundle (SSM bootstrap only)
=====================================================

This bundle only registers the Pi as an SSM managed instance. All software
(arduino-cli, ESP32 core, the builder GUI, systemd service) is installed
afterwards from the deploy host via SSM documents — nothing else runs here.

On the Pi:
  unzip builder-pi-bundle.zip
  cd builder-pi-bundle
  sudo ./setup-ssm.sh

Then, from the deploy host (the machine with AWS credentials for this account):
  ./raw_device_builder/setup-builder-pi-remote.sh --region $REGION

That orchestrator waits for the Pi to come Online, then runs, in order:
  1. install-awscli       (the AWS CLI — prerequisite for the S3/SSM steps)
  2. install-toolchain    (arduino-cli, ESP32 core — slow, ~5GB)
  3. deploy-app           (GUI app + assets from S3, config.json)
  4. configure-service    (udev, systemd; the GUI auto-starts)

When it finishes, the GUI is running at http://<pi-ip>:8080

Activation ID: $ACTIVATION_ID   (expires in 7 days — re-run this bundler if needed)
README

# ─── Step 4: Create zip ─────────────────────────────────────────────────────
echo "[4/4] Creating zip..."
cd "$SCRIPT_DIR"
rm -f builder-pi-bundle.zip
zip -r builder-pi-bundle.zip builder-pi-bundle/ >/dev/null

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ Builder-Pi bundle created (SSM bootstrap only)!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Directory: $BUNDLE_DIR"
echo "  Zip:       $SCRIPT_DIR/builder-pi-bundle.zip"
echo "  Activation ID: $ACTIVATION_ID"
echo "  Region: $REGION"
echo ""
echo "  On the Pi:"
echo "    unzip builder-pi-bundle.zip"
echo "    cd builder-pi-bundle"
echo "    sudo ./setup-ssm.sh"
echo ""
echo "  Then from the deploy host:"
echo "    ./raw_device_builder/setup-builder-pi-remote.sh --region $REGION"
echo ""
echo "  ⚠️  Activation expires in 7 days. Run this script again if needed."
echo ""
