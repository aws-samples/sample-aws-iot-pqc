#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-workshop.sh — single-command deploy of the PQC workshop.
#
# The master template (pqc-workshop-main.yaml) nests all five stacks:
#   BrokerStack → DeviceBuilderStack → DeviceStack, SSMActivationStack,
#   DashboardStack. CloudFormation owns the ordering (DeviceBuilderStack
#   consumes BrokerStack's Private CA + NLB endpoint via cross-stack refs), so
#   this script just:
#     1. packages each child template's Lambda code (aws cloudformation package)
#     2. uploads the packaged children to the templates bucket
#     3. deploys the single master stack
#     4. builds + syncs the v2 dashboard (a build step CFN can't do)
#
# Usage:
#   ./deploy-workshop.sh <aws-region> --vpc-id <vpc-xxxx> --subnets <subnet-a,subnet-b> [--device-count N]
#
# Example:
#   ./deploy-workshop.sh us-east-1 --vpc-id vpc-0abc --subnets subnet-01,subnet-02
#
# REMAINING MANUAL STEPS (cannot be done by CloudFormation — see README):
#   - One-time firmware Docker image build (ImageBuilderProject output)
#   - Builder-Pi SSM agent registration on the physical Raspberry Pi
#
# Teardown:  aws cloudformation delete-stack --stack-name pqc-workshop
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

AWS_REGION="${1:-us-east-1}"; shift || true
DEVICE_COUNT="100"
VPC_ID=""
SUBNETS=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --vpc-id) VPC_ID="$2"; shift 2 ;;
        --subnets) SUBNETS="$2"; shift 2 ;;
        --device-count) DEVICE_COUNT="$2"; shift 2 ;;
        *) echo "[ERROR] Unknown argument: $1"; exit 1 ;;
    esac
done

if [[ -z "$VPC_ID" || -z "$SUBNETS" ]]; then
    echo "[ERROR] --vpc-id and --subnets are required (the broker NLB/ECS need them)."
    echo "  Example: $0 $AWS_REGION --vpc-id vpc-0abc --subnets subnet-01,subnet-02"
    exit 1
fi

TEMPLATE_PREFIX="pqc-workshop"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
V2_FRONTEND="$PROJECT_ROOT/raw_device_builder/dashboard-v2/frontend"

export AWS_DEFAULT_REGION="$AWS_REGION"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ARTIFACTS_BUCKET="pqc-workshop-cfn-${ACCOUNT_ID}-${AWS_REGION}"

echo "═══════════════════════════════════════════════════════════════"
echo "  PQC Workshop — Full Stack Deployment (single master)"
echo "═══════════════════════════════════════════════════════════════"
echo "  Region:           $AWS_REGION"
echo "  VPC / subnets:    $VPC_ID / $SUBNETS"
echo "  Devices:          $DEVICE_COUNT"
echo "  Artifacts bucket: $ARTIFACTS_BUCKET"
echo ""

# ─── Ensure the artifacts bucket exists ──────────────────────────────────────
if ! aws s3 ls "s3://$ARTIFACTS_BUCKET" >/dev/null 2>&1; then
    if [[ "$AWS_REGION" == "us-east-1" ]]; then
        aws s3 mb "s3://$ARTIFACTS_BUCKET"
    else
        aws s3 mb "s3://$ARTIFACTS_BUCKET" --create-bucket-configuration LocationConstraint="$AWS_REGION"
    fi
fi

# ─── Step 1+2: package each child template + upload to templates/ ────────────
# `cloudformation package` zips each Lambda Code: dir, uploads it, and rewrites
# the template. The packaged child is placed where the master's TemplateURL
# expects it. All five children are nested by the master.
echo "[1/3] Packaging + uploading nested child templates..."
for pair in \
    "$PROJECT_ROOT/broker-container/cloudformation/broker-container-stack.yaml:broker-container-stack.yaml" \
    "$PROJECT_ROOT/raw_device_builder/cloudformation/device-builder-stack.yaml:device-builder-stack.yaml" \
    "$SCRIPT_DIR/device-provisioning-stack.yaml:device-provisioning-stack.yaml" \
    "$SCRIPT_DIR/ssm-hybrid-activation-stack.yaml:ssm-hybrid-activation-stack.yaml" \
    "$PROJECT_ROOT/dashboard/infrastructure/dashboard-stack.yaml:dashboard-stack.yaml"; do
    src="${pair%%:*}"; name="${pair##*:}"
    tmp="$(mktemp -t "${name}.XXXX.yaml")"
    echo "        - $name"
    aws cloudformation package --template-file "$src" \
        --s3-bucket "$ARTIFACTS_BUCKET" --s3-prefix "$TEMPLATE_PREFIX/packaged" \
        --output-template-file "$tmp" >/dev/null
    aws s3 cp "$tmp" "s3://$ARTIFACTS_BUCKET/$TEMPLATE_PREFIX/templates/$name" >/dev/null
    rm -f "$tmp"
done

# ─── Step 2: deploy the single master stack ──────────────────────────────────
echo "[2/3] Deploying the master stack (nests all five children)..."
aws cloudformation deploy \
    --template-file "$SCRIPT_DIR/pqc-workshop-main.yaml" \
    --stack-name "pqc-workshop" \
    --parameter-overrides \
        DeviceCount="$DEVICE_COUNT" \
        VpcId="$VPC_ID" \
        SubnetIds="$SUBNETS" \
        TemplateBucketName="$ARTIFACTS_BUCKET" \
        TemplateBucketPrefix="$TEMPLATE_PREFIX" \
    --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --tags Workshop=pqc-reinvent

# ─── Step 3: build + sync the v2 dashboard (post-deploy build step) ──────────
echo "[3/3] Building + deploying the v2 dashboard..."
get_out() { aws cloudformation describe-stacks --stack-name pqc-workshop \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text; }
APPSYNC_ENDPOINT=$(get_out AppSyncEndpoint)
IDENTITY_POOL=$(get_out CognitoIdentityPoolId)
API_ENDPOINT=$(get_out BuilderApiEndpoint)
DASH_BUCKET=$(get_out DashboardBucketName)
DASH_DIST=$(get_out DashboardDistributionId)

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
echo "  Dashboard:  $(get_out DashboardURL)"
echo "  Broker:     $(get_out BrokerEndpoint):8883"
echo ""
echo "REMAINING MANUAL STEPS:"
echo "  1. Build the firmware image once (needed before any device build):"
echo "       aws codebuild start-build --project-name $(get_out FirmwareImageBuilderProject)"
echo "  2. Register the Builder-Pi via SSM (see raw_device_builder/bundle-for-builder-pi.sh)"
echo "  3. Validate:  ./check_setup_status.sh --region $AWS_REGION"
echo ""
