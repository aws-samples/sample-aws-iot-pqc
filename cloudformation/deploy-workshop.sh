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
#        and uploads the packaged children to the templates bucket
#     2. deploys the single master stack (broker image build deferred)
#     3. stages the Docker build contexts to S3, builds the broker container
#        image, and scales the broker ECS service up
#     4. builds + syncs the v2 dashboard (a build step CFN can't do)
#     5. prints remaining manual steps
#
# Usage:
#   ./deploy-workshop.sh <aws-region> [--vpc-id <vpc-xxxx> --subnets <a,b>] [--profile NAME] [--device-count N]
#
# VPC: pass BOTH --vpc-id and --subnets to deploy into an existing VPC, or pass
# NEITHER to have the stack create its own VPC (2 public subnets across 2 AZs +
# IGW). Creating a VPC is the right choice for Workshop Studio / Control Tower
# accounts that have no usable default VPC.
#
#   Create a VPC:   ./deploy-workshop.sh us-east-1 --profile pqc-b
#   Existing VPC:   ./deploy-workshop.sh us-east-1 --profile pqc-b --vpc-id vpc-0abc --subnets subnet-01,subnet-02
#
# --profile is optional: when given it targets that named AWS CLI profile for
# every aws call; when omitted the ambient/default credentials are used. Set up
# profiles with `ada credentials update --profile <name> --account <id> --role Admin`.
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
PROFILE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --vpc-id) VPC_ID="$2"; shift 2 ;;
        --subnets) SUBNETS="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --device-count) DEVICE_COUNT="$2"; shift 2 ;;
        *) echo "[ERROR] Unknown argument: $1"; exit 1 ;;
    esac
done

# VPC mode: if neither --vpc-id nor --subnets is given, the stack creates its
# own VPC (CreateVpc=true). If both are given, we use that existing VPC. Giving
# only one is an error.
if [[ -z "$VPC_ID" && -z "$SUBNETS" ]]; then
    CREATE_VPC="true"
elif [[ -n "$VPC_ID" && -n "$SUBNETS" ]]; then
    CREATE_VPC="false"
else
    echo "[ERROR] Pass BOTH --vpc-id and --subnets to use an existing VPC, or"
    echo "        NEITHER to have the stack create its own VPC."
    echo "  Existing VPC: $0 $AWS_REGION --profile pqc-b --vpc-id vpc-0abc --subnets subnet-01,subnet-02"
    echo "  Create a VPC: $0 $AWS_REGION --profile pqc-b"
    exit 1
fi

# When --profile is given, export AWS_PROFILE so every aws call in this script
# targets that account. Exported (not per-command) so nested tools inherit it.
if [[ -n "$PROFILE" ]]; then
    export AWS_PROFILE="$PROFILE"
fi

TEMPLATE_PREFIX="pqc-workshop"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
V2_FRONTEND="$PROJECT_ROOT/raw_device_builder/dashboard-v2/frontend"

export AWS_DEFAULT_REGION="$AWS_REGION"
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ARTIFACTS_BUCKET="pqc-workshop-cfn-${ACCOUNT_ID}-${AWS_REGION}"

echo "═══════════════════════════════════════════════════════════════"
echo "  PQC Workshop — Full Stack Deployment (single master)"
echo "═══════════════════════════════════════════════════════════════"
echo "  Profile:          ${PROFILE:-<ambient/default credentials>}"
echo "  Target account:   $ACCOUNT_ID"
echo "  Caller identity:  $CALLER_ARN"
echo "  Region:           $AWS_REGION"
if [[ "$CREATE_VPC" == "true" ]]; then
    echo "  VPC:              <the stack will create its own VPC>"
else
    echo "  VPC / subnets:    $VPC_ID / $SUBNETS"
fi
echo "  Devices:          $DEVICE_COUNT"
echo "  Artifacts bucket: $ARTIFACTS_BUCKET"
echo ""
# Safety guard: confirm the target account before any writes. Skip with
# CONFIRM_DEPLOY=yes for non-interactive/CI use.
if [[ "${CONFIRM_DEPLOY:-}" != "yes" && -t 0 ]]; then
    read -r -p "  Deploy to account $ACCOUNT_ID? [y/N] " reply
    [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "Aborted."; exit 1; }
    echo ""
fi

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
echo "[1/5] Packaging + uploading nested child templates..."
for pair in \
    "$SCRIPT_DIR/network-stack.yaml:network-stack.yaml" \
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

# ─── Discover route tables for the chosen subnets (S3 gateway VPC endpoint) ───
# Build the VPC-related parameter overrides. When creating our own VPC, the
# master derives VpcId/SubnetIds/RouteTableId from the nested network stack, so
# we only set CreateVpc=true. For an existing VPC, resolve its route tables (for
# the broker S3 gateway endpoint) and pass everything through.
VPC_PARAMS="CreateVpc=$CREATE_VPC"
if [[ "$CREATE_VPC" == "false" ]]; then
    echo "[*] Resolving route tables for the S3 gateway endpoint..."
    RT_IDS=""
    for sn in $(echo "$SUBNETS" | tr ',' ' '); do
        rt=$(aws ec2 describe-route-tables \
            --filters "Name=association.subnet-id,Values=$sn" \
            --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null)
        if [[ -z "$rt" || "$rt" == "None" ]]; then
            rt=$(aws ec2 describe-route-tables \
                --filters "Name=vpc-id,Values=$VPC_ID" "Name=association.main,Values=true" \
                --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null)
        fi
        [[ -n "$rt" && "$rt" != "None" ]] && RT_IDS+="${RT_IDS:+,}$rt"
    done
    RT_IDS=$(echo "$RT_IDS" | tr ',' '\n' | awk 'NF && !seen[$0]++' | paste -sd, -)
    echo "    route tables: ${RT_IDS:-<none found — S3 gateway endpoint will be skipped>}"
    VPC_PARAMS="$VPC_PARAMS VpcId=$VPC_ID SubnetIds=$SUBNETS BrokerRouteTableIds=$RT_IDS"
fi

# ─── Step 2: deploy the single master stack ──────────────────────────────────
# BrokerTriggerBuild=false: the broker's container-image CodeBuild pulls its
# source from the broker source bucket, which does not exist until this stack
# creates it. So we create the infra now WITHOUT the in-stack image build, then
# stage the source and trigger the build in step 3 (below). The broker ECS
# service ships at DesiredCount 0, so nothing needs the image mid-deploy.
echo "[2/5] Deploying the master stack (nests all children)..."
aws cloudformation deploy \
    --template-file "$SCRIPT_DIR/pqc-workshop-main.yaml" \
    --stack-name "pqc-workshop" \
    --parameter-overrides \
        DeviceCount="$DEVICE_COUNT" \
        TemplateBucketName="$ARTIFACTS_BUCKET" \
        TemplateBucketPrefix="$TEMPLATE_PREFIX" \
        BrokerTriggerBuild=false \
        BrokerCreateVpcEndpoints=true \
        $VPC_PARAMS \
    --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --tags Workshop=pqc-reinvent

get_out() { aws cloudformation describe-stacks --stack-name pqc-workshop \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text; }

# ─── Step 3: stage container build contexts + build the broker image ─────────
# CloudFormation cannot upload a Docker build context, so we do it here now that
# the source buckets exist.
echo "[3/5] Staging build contexts + building the broker container image..."
BROKER_SOURCE_BUCKET="pqc-workshop-broker-source-${ACCOUNT_ID}"
FIRMWARE_BUCKET="pqc-workshop-firmware-${ACCOUNT_ID}"

# Broker image build context (Dockerfile, buildspec.yml, entrypoint.sh, mosquitto.conf)
echo "        - broker image source -> s3://${BROKER_SOURCE_BUCKET}/broker-image/"
aws s3 cp "$PROJECT_ROOT/broker-container/docker/" \
    "s3://${BROKER_SOURCE_BUCKET}/broker-image/" --recursive >/dev/null

# Firmware builder image context (used by the MANUAL firmware image build below;
# staged here so it is ready when the operator runs that one-time build).
echo "        - firmware image source -> s3://${FIRMWARE_BUCKET}/source/"
aws s3 cp "$PROJECT_ROOT/raw_device_builder/codebuild/Dockerfile" \
    "s3://${FIRMWARE_BUCKET}/source/Dockerfile" >/dev/null
aws s3 cp "$PROJECT_ROOT/raw_device_builder/codebuild/buildspec-image.yml" \
    "s3://${FIRMWARE_BUCKET}/source/buildspec-image.yml" >/dev/null
# The two extra firmware-image context files live in known repo locations. Use a
# plain function (no associative array, whose dotted keys break under zsh) with
# a fallback repo search that only skips the arduino-cli/data/ board cache.
stage_extra() {
    local name="$1" f="$2"
    [[ -f "$f" ]] || f=$(find "$PROJECT_ROOT" -name "$name" -not -path '*/arduino-cli/data/*' 2>/dev/null | head -1)
    if [[ -n "$f" && -f "$f" ]]; then
        echo "          - $name"
        aws s3 cp "$f" "s3://${FIRMWARE_BUCKET}/source/$name" >/dev/null
    else
        echo "          ! WARN: $name not found; firmware image build may fail until it is staged"
    fi
}
stage_extra wolfssl_user_settings_arduino.h "$PROJECT_ROOT/raw_device_builder/codebuild/wolfssl_user_settings_arduino.h"
stage_extra partitions_pqc_workshop.csv     "$PROJECT_ROOT/esp32-flash/partitions_pqc_workshop.csv"

# Trigger the broker image build now that its source is in place, and wait.
BROKER_BUILD_PROJECT=$(get_out CodeBuildProjectName 2>/dev/null || true)
[[ -z "$BROKER_BUILD_PROJECT" || "$BROKER_BUILD_PROJECT" == "None" ]] && BROKER_BUILD_PROJECT="pqc-workshop-broker-image-build"
echo "        - starting broker image build ($BROKER_BUILD_PROJECT)..."
BUILD_ID=$(aws codebuild start-build --project-name "$BROKER_BUILD_PROJECT" \
    --query 'build.id' --output text)
echo "          build: $BUILD_ID (waiting for completion)"
while true; do
    st=$(aws codebuild batch-get-builds --ids "$BUILD_ID" \
        --query 'builds[0].buildStatus' --output text)
    case "$st" in
        SUCCEEDED) echo "          broker image build SUCCEEDED"; break ;;
        FAILED|FAULT|TIMED_OUT|STOPPED) echo "[ERROR] broker image build $st ($BUILD_ID)"; exit 1 ;;
        *) sleep 15 ;;
    esac
done

# Scale the broker ECS service up (it ships at DesiredCount 0). Now that an image
# exists, one task can start and register with the NLB. The cluster and service
# are both named pqc-workshop-broker.
echo "        - scaling broker ECS service to 1 task..."
if ! aws ecs update-service --cluster pqc-workshop-broker --service pqc-workshop-broker \
        --desired-count 1 >/dev/null; then
    echo "          WARN: could not scale the broker service; scale it manually with:"
    echo "            aws ecs update-service --cluster pqc-workshop-broker --service pqc-workshop-broker --desired-count 1${PROFILE:+ --profile $PROFILE}"
fi

# ─── Step 4: build + sync the v2 dashboard (post-deploy build step) ──────────
echo "[4/5] Building + deploying the v2 dashboard..."
APPSYNC_ENDPOINT=$(get_out AppSyncEndpoint)
IDENTITY_POOL=$(get_out CognitoIdentityPoolId)
API_ENDPOINT=$(get_out BuilderApiEndpoint)
DASH_BUCKET=$(get_out DashboardBucketName)
DASH_DIST=$(get_out DashboardDistributionId)
# FIRMWARE_BUCKET already set in step 3 (pqc-workshop-firmware-${ACCOUNT_ID}).

# ─── Publish deployment config to SSM Parameter Store (canonical source) ─────
# Non-secret, deployment-derived values live in Parameter Store so nothing
# environment-specific is committed to source. The dashboard config.json and
# the builder-pi bundle are both materialized from these parameters. Standard
# tier String parameters (free); overwrite on redeploy.
echo "  - publishing config to SSM Parameter Store (/pqc-workshop/*)..."
put_param() {
    # $1 = name, $2 = value
    if [[ -z "$2" || "$2" == "None" ]]; then
        echo "    WARN: skipping $1 (empty value)"
        return
    fi
    aws ssm put-parameter --name "$1" --type String --overwrite --value "$2" \
        --region "$AWS_REGION" >/dev/null \
        || echo "    WARN: failed to put $1"
}
put_param /pqc-workshop/dashboard/appSyncEndpoint "$APPSYNC_ENDPOINT"
put_param /pqc-workshop/dashboard/identityPoolId  "$IDENTITY_POOL"
put_param /pqc-workshop/dashboard/apiEndpoint     "$API_ENDPOINT"
put_param /pqc-workshop/dashboard/region          "$AWS_REGION"
put_param /pqc-workshop/builder-pi/apiEndpoint    "$API_ENDPOINT"
put_param /pqc-workshop/builder-pi/firmwareBucket "$FIRMWARE_BUCKET"
put_param /pqc-workshop/builder-pi/region         "$AWS_REGION"

# ─── Materialize the dashboard's runtime config.json ─────────────────────────
# Written into public/ so Vite copies it into build/ as a normal asset. This
# keeps the built bundle environment-agnostic (no endpoints compiled in) and
# means the ordinary `s3 sync` handles config.json — avoiding a separate upload
# that `--delete` would otherwise remove. The SPA fetches /config.json at load.
mkdir -p "$V2_FRONTEND/public"
cat > "$V2_FRONTEND/public/config.json" <<CONFIG
{
  "appSyncEndpoint": "${APPSYNC_ENDPOINT}",
  "identityPoolId": "${IDENTITY_POOL}",
  "apiEndpoint": "${API_ENDPOINT}",
  "region": "${AWS_REGION}",
  "pollInterval": 5000
}
CONFIG

( cd "$V2_FRONTEND" && npm ci && npm run build )
aws s3 sync "$V2_FRONTEND/build/" "s3://$DASH_BUCKET/" --delete
if [[ -n "$DASH_DIST" && "$DASH_DIST" != "None" ]]; then
    aws cloudfront create-invalidation --distribution-id "$DASH_DIST" --paths '/*' >/dev/null
else
    echo "  WARN: DashboardDistributionId output missing; skipping CloudFront invalidation."
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Deployment complete!"
echo "═══════════════════════════════════════════════════════════════"
echo "  Dashboard:  $(get_out DashboardURL)"
echo "  Broker:     $(get_out BrokerEndpoint):8883"
echo ""
echo "[5/5] Done."
PROFILE_ARG=""
[[ -n "$PROFILE" ]] && PROFILE_ARG=" --profile $PROFILE"
echo ""
echo "REMAINING MANUAL STEPS:"
echo "  1. Build the firmware image once (source already staged by this script;"
echo "     needed before any device firmware build):"
echo "       aws codebuild start-build --project-name $(get_out FirmwareImageBuilderProject)${PROFILE_ARG}"
echo "  2. Register the Builder-Pi via SSM (see raw_device_builder/bundle-for-builder-pi.sh)"
echo "  3. Validate:  ./check_setup_status.sh --region $AWS_REGION${PROFILE_ARG}"
echo ""
