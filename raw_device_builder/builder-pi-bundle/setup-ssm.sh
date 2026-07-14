#!/bin/bash
# ─── SSM Agent Setup for Builder-Pi ─────────────────────────────────────────
# This script installs and registers the SSM agent. Run once with sudo.

set -euo pipefail

ACTIVATION_ID="0b2bf478-7104-4cfd-831d-a737ed33c171"
ACTIVATION_CODE="cI7lBYzAKP3JD/uVFt+e"
REGION="us-east-1"

echo "Installing SSM Agent..."

# Check if already registered
if systemctl is-active --quiet amazon-ssm-agent 2>/dev/null; then
    EXISTING=$(cat /var/lib/amazon/ssm/registration 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('ManagedInstanceID',''))" 2>/dev/null) || EXISTING=""
    if [[ -n "$EXISTING" ]]; then
        echo "Already registered as: $EXISTING"
        echo "To re-register, run: sudo $0 --reset"
        exit 0
    fi
fi

# Handle --reset
if [[ "${1:-}" == "--reset" ]]; then
    echo "Deregistering from previous activation..."
    sudo systemctl stop amazon-ssm-agent 2>/dev/null || true
    sudo rm -f /var/lib/amazon/ssm/registration
fi

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
