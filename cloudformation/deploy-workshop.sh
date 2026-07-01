#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-workshop.sh
#
# Deploys the entire PQC workshop infrastructure with a single command.
#
# This script:
#   1. Creates an S3 bucket for nested CloudFormation templates
#   2. Uploads all nested templates to S3
#   3. Deploys the main (parent) stack which creates everything
#
# Usage:
#   ./deploy-workshop.sh <aws-region> [device-count]
#
# Example:
#   ./deploy-workshop.sh us-east-1 100
#
# To tear down:
#   aws cloudformation delete-stack --stack-name pqc-workshop
#   aws s3 rb s3://<bucket-name> --force
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

AWS_REGION="${1:-us-east-1}"
DEVICE_COUNT="${2:-100}"
STACK_NAME="pqc-workshop"
TEMPLATE_PREFIX="pqc-workshop"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD_DIR="$SCRIPT_DIR/../dashboard/infrastructure"

# Get account ID for unique bucket name
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET_NAME="pqc-workshop-cfn-${ACCOUNT_ID}-${AWS_REGION}"

echo "═══════════════════════════════════════════════════════════════"
echo "  PQC Workshop — Full Stack Deployment"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "Region:        $AWS_REGION"
echo "Devices:       $DEVICE_COUNT"
echo "Stack:         $STACK_NAME"
echo "Template bucket: $BUCKET_NAME"
echo ""

# ─── Step 1: Create S3 bucket for templates ──────────────────────────────────
echo "[1/3] Creating template bucket..."
if aws s3 ls "s3://$BUCKET_NAME" --region "$AWS_REGION" 2>/dev/null; then
    echo "       Bucket already exists"
else
    if [[ "$AWS_REGION" == "us-east-1" ]]; then
        aws s3 mb "s3://$BUCKET_NAME" --region "$AWS_REGION"
    else
        aws s3 mb "s3://$BUCKET_NAME" --region "$AWS_REGION" \
            --create-bucket-configuration LocationConstraint="$AWS_REGION"
    fi
    echo "       Bucket created"
fi

# ─── Step 2: Upload nested templates ────────────────────────────────────────
echo "[2/3] Uploading nested templates to S3..."

# Upload all nested stacks
aws s3 cp "$SCRIPT_DIR/greengrass-core-stack.yaml" \
    "s3://$BUCKET_NAME/$TEMPLATE_PREFIX/templates/greengrass-core-stack.yaml" \
    --region "$AWS_REGION"

aws s3 cp "$SCRIPT_DIR/device-provisioning-stack.yaml" \
    "s3://$BUCKET_NAME/$TEMPLATE_PREFIX/templates/device-provisioning-stack.yaml" \
    --region "$AWS_REGION"

aws s3 cp "$DASHBOARD_DIR/dashboard-stack.yaml" \
    "s3://$BUCKET_NAME/$TEMPLATE_PREFIX/templates/dashboard-stack.yaml" \
    --region "$AWS_REGION"

echo "       Templates uploaded"

# ─── Step 3: Deploy main stack ───────────────────────────────────────────────
echo "[3/3] Deploying main CloudFormation stack..."
echo "       (This may take 5-10 minutes)"
echo ""

aws cloudformation deploy \
    --template-file "$SCRIPT_DIR/pqc-workshop-main.yaml" \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --parameter-overrides \
        DeviceCount="$DEVICE_COUNT" \
        TemplateBucketName="$BUCKET_NAME" \
        TemplateBucketPrefix="$TEMPLATE_PREFIX" \
    --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --tags Workshop=pqc-reinvent

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Deployment complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Print outputs
echo "Stack outputs:"
aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
    --output table

echo ""
echo "Next steps:"
echo "  1. Update dashboard/frontend/src/config.js with AppSync endpoint + Cognito pool ID"
echo "  2. Build and deploy frontend: cd dashboard/frontend && npm install && npm run build"
echo "  3. Upload frontend to S3 (bucket name shown in outputs above)"
echo "  4. Set up Raspberry Pi: cd greengrass && sudo ./setup-greengrass-core.sh $AWS_REGION"
echo "  5. Install PQC Mosquitto: sudo ./setup-mosquitto-pqc.sh"
echo "  6. Flash ESP32 devices: cd esp32-flash && ./flash-dual-boot.sh ..."
echo ""
