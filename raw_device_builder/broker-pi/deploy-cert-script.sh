#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-cert-script.sh
#
# Deploys the per-device cert generation script to the broker-pi via SSM.
# Run this once after the broker-pi is set up.
#
# Usage:
#   ./deploy-cert-script.sh [broker-pi-instance-id]
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
# Pass the broker-Pi's SSM managed-instance ID as $1 (mi-...). No real default —
# it is account/deploy specific.
INSTANCE_ID="${1:?Usage: $0 <mi-XXXXXXXX>  (broker-Pi SSM managed-instance ID)}"

echo "Deploying cert generation script to broker-pi ($INSTANCE_ID)..."

# Read the script content
SCRIPT_CONTENT=$(cat "$SCRIPT_DIR/generate-device-cert.sh")

# Use SSM to write the script to the broker-pi
COMMAND_ID=$(aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --document-name AWS-RunShellScript \
    --parameters commands=["cat > /opt/mosquitto-pqc/etc/mosquitto/certs/generate-device-cert.sh << 'SCRIPTEOF'
${SCRIPT_CONTENT}
SCRIPTEOF
chmod +x /opt/mosquitto-pqc/etc/mosquitto/certs/generate-device-cert.sh
echo 'Script deployed successfully'"] \
    --region "$REGION" \
    --query 'Command.CommandId' --output text)

echo "Command sent: $COMMAND_ID"
echo "Waiting for completion..."

sleep 5

RESULT=$(aws ssm get-command-invocation \
    --command-id "$COMMAND_ID" \
    --instance-id "$INSTANCE_ID" \
    --region "$REGION" \
    --query 'Status' --output text)

echo "Status: $RESULT"

if [[ "$RESULT" == "Success" ]]; then
    echo "✅ Cert generation script deployed to broker-pi"
else
    echo "❌ Deployment failed. Check SSM command output."
    aws ssm get-command-invocation \
        --command-id "$COMMAND_ID" \
        --instance-id "$INSTANCE_ID" \
        --region "$REGION" \
        --query 'StandardErrorContent' --output text
fi
