#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-workshop.sh — single-command deploy of the entire PQC workshop.
#
# Deploy order (dependencies flow downward):
#   0. (one-time, manual) build the firmware Docker image — see NOTE below
#   1. broker-container stack      → creates the Private CA + ECS PQC broker
#   2. device-builder stack        → imports the Private CA; builder API,
#                                     CodeBuild, PQC migration state machine
#   3. main (nested) stack         → device provisioning, dashboard backend,
#                                     v2 dashboard hosting, SSM activation
#   4. build + sync the v2 dashboard → build the Vite app, sync to its bucket,
#                                       invalidate CloudFront
#
# Lambda code is packaged from source via `aws cloudformation package` (the
# templates reference Code: <local dir>), so there is no separate
# update-function-code step and no placeholder stubs.
#
# Usage:
#   ./deploy-workshop.sh <aws-region> [device-count]
#
# Example:
#   ./deploy-workshop.sh us-east-1 100
#
# NOTE (one-time, before step 1 on a fresh account): the firmware build image
# must exist in ECR. After the device-builder stack creates the ECR repo +
# image-builder CodeBuild project, run that project once:
#   aws codebuild start-build --project-name pqc-workshop-build-image
# (This bakes the wolfSSL PQC settings into the image the firmware build uses.)
#
# Teardown (reverse order):
#   aws cloudformation delete-stack --stack-name pqc-workshop
#   aws cloudformation delete-stack --stack-name pqc-workshop-device-builder
#   aws cloudformation delete-stack --stack-name pqc-workshop-broker-container
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

AWS_REGION="${1:-us-east-1}"
DEVICE_COUNT="${2:-100}"
TEMPLATE_PREFIX="pqc-workshop"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DASHBOARD_DIR="$PROJECT_ROOT/dashboard/infrastructure"
DEVICE_BUILDER_DIR="$PROJECT_ROOT/raw_device_builder/cloudformation"
BROKER_DIR="$PROJECT_ROOT/broker-container/cloudformation"
V2_FRONTEND="$PROJECT_ROOT/raw_device_builder/dashboard-v2/frontend"

export AWS_DEFAULT_REGION="$AWS_REGION"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ARTIFACTS_BUCKET="pqc-workshop-cfn-${ACCOUNT_ID}-${AWS_REGION}"

echo "═══════════════════════════════════════════════════════════════"
echo "  PQC Workshop — Full Stack Deployment"
echo "═══════════════════════════════════════════════════════════════"
echo "  Region:          $AWS_REGION"
echo "  Devices:         $DEVICE_COUNT"
echo "  Artifacts bucket: $ARTIFACTS_BUCKET"
echo ""

# ─── Artifacts bucket (holds packaged templates + Lambda zips) ───────────────
echo "[setup] Ensuring artifacts bucket exists..."
if ! aws s3 ls "s3://$ARTIFACTS_BUCKET" >/dev/null 2>&1; then
    if [[ "$AWS_REGION" == "us-east-1" ]]; then
        aws s3 mb "s3://$ARTIFACTS_BUCKET"
    else
        aws s3 mb "s3://$ARTIFACTS_BUCKET" --create-bucket-configuration LocationConstraint="$AWS_REGION"
    fi
fi

# package_and_deploy <template> <stack-name> [param-overrides...]
# Runs `cloudformation package` (zips + uploads Code: dirs, rewrites template)
# then `cloudformation deploy`.
package_and_deploy() {
    local template="$1"; shift
    local stack="$1"; shift
    local packaged
    packaged="$(mktemp -t "${stack}.packaged.XXXX.yaml")"
    echo "  packaging $(basename "$template")..."
    aws cloudformation package \
        --template-file "$template" \
        --s3-bucket "$ARTIFACTS_BUCKET" \
        --s3-prefix "$TEMPLATE_PREFIX/packaged" \
        --output-template-file "$packaged" >/dev/null
    echo "  deploying $stack..."
    aws cloudformation deploy \
        --template-file "$packaged" \
        --stack-name "$stack" \
        --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
        --tags Workshop=pqc-reinvent \
        "$@"
    rm -f "$packaged"
}

# ─── Step 1: Broker container (creates the Private CA) ───────────────────────
echo "[1/4] Deploying broker-container stack (Private CA + ECS PQC broker)..."
package_and_deploy "$BROKER_DIR/broker-container-stack.yaml" "pqc-workshop-broker-container" \
    --parameter-overrides IoTEndpoint="${ACCOUNT_ID}-ats.iot.${AWS_REGION}.amazonaws.com"

PRIVATE_CA_ARN=$(aws cloudformation describe-stacks --stack-name pqc-workshop-broker-container \
    --query "Stacks[0].Outputs[?OutputKey=='PrivateCAArn'].OutputValue | [0]" --output text)
echo "       Private CA: $PRIVATE_CA_ARN"

# ─── Step 2: Device builder (imports the Private CA) ─────────────────────────
echo "[2/4] Deploying device-builder stack (builder API, CodeBuild, migration)..."
package_and_deploy "$DEVICE_BUILDER_DIR/device-builder-stack.yaml" "pqc-workshop-device-builder" \
    --parameter-overrides \
        PrivateCAArn="$PRIVATE_CA_ARN" \
        IoTEndpoint="${ACCOUNT_ID}-ats.iot.${AWS_REGION}.amazonaws.com"

# ─── Step 3: Main nested stack (device provisioning + dashboard backend + host)
# The parent references child templates by S3 URL; package each child (they have
# Lambda Code: dirs) and upload to templates/, then deploy the parent.
echo "[3/4] Packaging + uploading nested templates, deploying main stack..."
for pair in \
    "$SCRIPT_DIR/device-provisioning-stack.yaml:device-provisioning-stack.yaml" \
    "$SCRIPT_DIR/ssm-hybrid-activation-stack.yaml:ssm-hybrid-activation-stack.yaml" \
    "$DASHBOARD_DIR/dashboard-stack.yaml:dashboard-stack.yaml"; do
    src="${pair%%:*}"; name="${pair##*:}"
    tmp="$(mktemp -t "${name}.XXXX.yaml")"
    aws cloudformation package --template-file "$src" \
        --s3-bucket "$ARTIFACTS_BUCKET" --s3-prefix "$TEMPLATE_PREFIX/packaged" \
        --output-template-file "$tmp" >/dev/null
    aws s3 cp "$tmp" "s3://$ARTIFACTS_BUCKET/$TEMPLATE_PREFIX/templates/$name" >/dev/null
    rm -f "$tmp"
done
aws cloudformation deploy \
    --template-file "$SCRIPT_DIR/pqc-workshop-main.yaml" \
    --stack-name "pqc-workshop" \
    --parameter-overrides \
        DeviceCount="$DEVICE_COUNT" \
        TemplateBucketName="$ARTIFACTS_BUCKET" \
        TemplateBucketPrefix="$TEMPLATE_PREFIX" \
    --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --tags Workshop=pqc-reinvent

# ─── Step 4: Build + sync the v2 dashboard ───────────────────────────────────
echo "[4/4] Building + deploying the v2 dashboard..."
APPSYNC_ENDPOINT=$(aws cloudformation describe-stacks --stack-name pqc-workshop \
    --query "Stacks[0].Outputs[?OutputKey=='AppSyncEndpoint'].OutputValue | [0]" --output text)
IDENTITY_POOL=$(aws cloudformation describe-stacks --stack-name pqc-workshop \
    --query "Stacks[0].Outputs[?OutputKey=='CognitoIdentityPoolId'].OutputValue | [0]" --output text)
API_ENDPOINT=$(aws cloudformation describe-stacks --stack-name pqc-workshop-device-builder \
    --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue | [0]" --output text)
DASH_BUCKET=$(aws cloudformation describe-stacks --stack-name pqc-workshop \
    --query "Stacks[0].Outputs[?OutputKey=='DashboardBucketName'].OutputValue | [0]" --output text)
DASH_DIST=$(aws cloudformation describe-stacks --stack-name pqc-workshop \
    --query "Stacks[0].Outputs[?OutputKey=='DashboardDistributionId'].OutputValue | [0]" --output text)

# Inject the stack outputs into the v2 config at build time.
cat > "$V2_FRONTEND/src/config.js" <<CONFIG
// Generated by deploy-workshop.sh — do not edit by hand.
export default {
  appSyncEndpoint: '${APPSYNC_ENDPOINT}',
  identityPoolId: '${IDENTITY_POOL}',
  apiEndpoint: '${API_ENDPOINT}',
  region: '${AWS_REGION}',
  pollInterval: 5000,
};
CONFIG

( cd "$V2_FRONTEND" && npm ci && npm run build )
aws s3 sync "$V2_FRONTEND/build/" "s3://$DASH_BUCKET/" --delete
aws cloudfront create-invalidation --distribution-id "$DASH_DIST" --paths '/*' >/dev/null

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Deployment complete!"
echo "═══════════════════════════════════════════════════════════════"
DASH_URL=$(aws cloudformation describe-stacks --stack-name pqc-workshop \
    --query "Stacks[0].Outputs[?OutputKey=='DashboardURL'].OutputValue | [0]" --output text)
echo "  Dashboard:  $DASH_URL"
echo ""
echo "Next steps:"
echo "  1. (fresh account, one-time) build the firmware image:"
echo "       aws codebuild start-build --project-name pqc-workshop-build-image"
echo "  2. Set up the Builder-Pi via SSM (see raw_device_builder/bundle-for-builder-pi.sh)"
echo "  3. Register + flash a device:  workshop-builder register --port /dev/ttyACM0"
echo "  4. Validate:  ./check_setup_status.sh --region $AWS_REGION"
echo ""
