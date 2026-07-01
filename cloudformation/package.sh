#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# package.sh
#
# Packages Lambda functions and uploads all deployment artifacts to S3.
#
# This script prepares the assets bucket that CloudFormation references.
# It can target either:
#   - A self-created bucket (for local/dev testing)
#   - The Workshop Studio assets bucket (for re:Invent deployment)
#
# After running this script, the CloudFormation template can be deployed
# referencing the bucket and prefix as parameters.
#
# S3 layout after packaging:
#   s3://<bucket>/<prefix>/
#     templates/             ← Nested CloudFormation YAML files
#       greengrass-core-stack.yaml
#       device-provisioning-stack.yaml
#       dashboard-stack.yaml
#     lambda/                ← Zipped Lambda function packages
#       auto-register.zip
#       cert-provisioner.zip
#       command-publisher.zip
#       telemetry-processor.zip
#     policies/              ← IAM policy JSON files (reference)
#       *.json
#
# Usage:
#   ./package.sh <aws-region> [s3-bucket] [s3-prefix]
#
# Examples:
#   # Auto-create a bucket (dev/testing)
#   ./package.sh us-east-1
#
#   # Target a specific bucket (Workshop Studio assets bucket)
#   ./package.sh us-east-1 ws-assets-prod-iad-r-iad-xxxxx pqc-workshop
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

AWS_REGION="${1:-us-east-1}"
BUCKET_NAME="${2:-}"
S3_PREFIX="${3:-pqc-workshop}"
DEPLOY="${DEPLOY:-true}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LAMBDA_DIR="$PROJECT_ROOT/lambda"
POLICIES_DIR="$PROJECT_ROOT/policies"
DASHBOARD_INFRA="$PROJECT_ROOT/dashboard/infrastructure"
BUILD_DIR="$PROJECT_ROOT/.build"

STACK_NAME="pqc-workshop"

echo "═══════════════════════════════════════════════════════════════"
echo "  PQC Workshop — Package & Deploy"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ─── Step 1: Determine or create S3 bucket ──────────────────────────────────
if [[ -z "$BUCKET_NAME" ]]; then
    ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
    BUCKET_NAME="pqc-workshop-assets-${ACCOUNT_ID}-${AWS_REGION}"
    echo "[1/5] Creating artifacts bucket: $BUCKET_NAME"
    if ! aws s3 ls "s3://$BUCKET_NAME" --region "$AWS_REGION" 2>/dev/null; then
        if [[ "$AWS_REGION" == "us-east-1" ]]; then
            aws s3 mb "s3://$BUCKET_NAME" --region "$AWS_REGION"
        else
            aws s3 mb "s3://$BUCKET_NAME" --region "$AWS_REGION" \
                --create-bucket-configuration LocationConstraint="$AWS_REGION"
        fi
    fi
else
    echo "[1/5] Using provided bucket: $BUCKET_NAME"
fi

echo "       Bucket: $BUCKET_NAME"
echo "       Prefix: $S3_PREFIX"
echo "       Region: $AWS_REGION"
echo ""

# ─── Step 2: Package Lambda functions ────────────────────────────────────────
echo "[2/5] Packaging Lambda functions..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

for func_dir in "$LAMBDA_DIR"/*/; do
    func_name=$(basename "$func_dir")
    zip_file="$BUILD_DIR/${func_name}.zip"
    echo "       Zipping $func_name → ${func_name}.zip"
    (cd "$func_dir" && zip -qr "$zip_file" .)
done

# ─── Step 3: Upload Lambda zips ──────────────────────────────────────────────
echo "[3/5] Uploading Lambda packages..."
aws s3 sync "$BUILD_DIR/" "s3://$BUCKET_NAME/$S3_PREFIX/lambda/" \
    --region "$AWS_REGION" --exclude "*" --include "*.zip" --delete

# ─── Step 4: Upload nested templates and policies ────────────────────────────
echo "[4/5] Uploading templates and policies..."

# Upload nested CloudFormation templates
aws s3 cp "$SCRIPT_DIR/greengrass-core-stack.yaml" \
    "s3://$BUCKET_NAME/$S3_PREFIX/templates/greengrass-core-stack.yaml" \
    --region "$AWS_REGION"
aws s3 cp "$SCRIPT_DIR/device-provisioning-stack.yaml" \
    "s3://$BUCKET_NAME/$S3_PREFIX/templates/device-provisioning-stack.yaml" \
    --region "$AWS_REGION"
aws s3 cp "$DASHBOARD_INFRA/dashboard-stack.yaml" \
    "s3://$BUCKET_NAME/$S3_PREFIX/templates/dashboard-stack.yaml" \
    --region "$AWS_REGION"

# Upload policies (for reference/audit — not directly consumed by CFn)
aws s3 sync "$POLICIES_DIR/" "s3://$BUCKET_NAME/$S3_PREFIX/policies/" \
    --region "$AWS_REGION" --include "*.json" --delete

echo "       Done."
echo ""

# ─── Step 5: Deploy (optional) ───────────────────────────────────────────────
if [[ "$DEPLOY" == "true" ]]; then
    echo "[5/5] Deploying CloudFormation stack: $STACK_NAME"
    aws cloudformation deploy \
        --template-file "$SCRIPT_DIR/pqc-workshop-main.yaml" \
        --stack-name "$STACK_NAME" \
        --region "$AWS_REGION" \
        --parameter-overrides \
            TemplateBucketName="$BUCKET_NAME" \
            TemplateBucketPrefix="$S3_PREFIX" \
        --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
        --tags Key=Workshop,Value=pqc-reinvent

    echo ""
    echo "Stack outputs:"
    aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --region "$AWS_REGION" \
        --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
        --output table
else
    echo "[5/5] Skipping deploy (DEPLOY=false)."
    echo ""
    echo "Assets uploaded to:"
    echo "  s3://$BUCKET_NAME/$S3_PREFIX/templates/"
    echo "  s3://$BUCKET_NAME/$S3_PREFIX/lambda/"
    echo "  s3://$BUCKET_NAME/$S3_PREFIX/policies/"
    echo ""
    echo "To deploy manually:"
    echo "  aws cloudformation deploy \\"
    echo "    --template-file $SCRIPT_DIR/pqc-workshop-main.yaml \\"
    echo "    --stack-name $STACK_NAME \\"
    echo "    --parameter-overrides \\"
    echo "      TemplateBucketName=$BUCKET_NAME \\"
    echo "      TemplateBucketPrefix=$S3_PREFIX \\"
    echo "    --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Packaging complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "Workshop Studio usage:"
echo "  1. Upload assets to the Workshop Studio assets bucket:"
echo "     DEPLOY=false ./package.sh $AWS_REGION <ws-assets-bucket> pqc-workshop"
echo ""
echo "  2. Set the provisioning template to:"
echo "     cloudformation/pqc-workshop-main.yaml"
echo ""
echo "  3. Set template parameters:"
echo "     TemplateBucketName = <ws-assets-bucket>"
echo "     TemplateBucketPrefix = pqc-workshop"
echo ""
