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
#     4. publishes dashboard config to SSM, stages the dashboard source, and
#        triggers the dashboard CodeBuild project (build + sync + invalidate)
#     5. prints remaining manual steps
#
# Usage:
#   ./deploy-workshop.sh <aws-region> [--vpc-id <vpc-xxxx> --subnets <a,b>] [--profile NAME] [--device-count N] [--wifi-ssid SSID] [--wifi-password PW]
#
# Workshop WiFi: supply via --wifi-ssid/--wifi-password flags, the
# WORKSHOP_WIFI_SSID/WORKSHOP_WIFI_PASSWORD env vars, or the interactive prompt
# (precedence: flags -> env -> prompt). The SSID prompt defaults to
# 'reinvent-pqc-iot-wifi'; the password is always required (no default). Prefer
# the env var or prompt for the real password — a --wifi-password flag is
# visible in shell history and `ps`.
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
# Teardown:  ./teardown-workshop.sh <region> [--profile NAME] [--wait]
#   (a bare `delete-stack` is NOT enough — versioned S3 buckets, ECR images, and
#   IoT things/thing-type block the delete; teardown-workshop.sh clears them first)
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

# ─── Workshop WiFi (operator-supplied, NEVER committed) ──────────────────────
# The device firmware (initial flash AND the classical->PQC migration) is built
# with these WiFi credentials, and the builder-Pi hostapd AP broadcasts the same
# SSID. Supply the real workshop values at deploy time by ONE of three methods,
# resolved in this precedence (first match wins), per value independently:
#   1. CLI flags:  --wifi-ssid <ssid>  --wifi-password <pw>
#   2. Env vars:   WORKSHOP_WIFI_SSID=... WORKSHOP_WIFI_PASSWORD=...
#   3. Interactive prompt (only when run from a terminal): the SSID prompt
#      defaults to 'reinvent-pqc-iot-wifi'; the password is read hidden and is
#      required (re-prompts until non-empty). No password default ever exists.
# Non-interactive with nothing supplied: SSID falls back to the workshop default
# (not a secret), the password falls back to the harmless 'changeme' placeholder
# (devices won't join a real network, but the deploy still succeeds + WARNs).
# The resolved values are threaded to the master stack as parameter overrides
# and published to SSM (password as a SecureString) as the single source of
# truth the registrar/migration Lambdas and the builder-Pi AP setup consume.
#
# SECURITY: --wifi-password lands in shell history and `ps` output. Prefer the
# env var or the interactive prompt for the real password; the flag is best for
# the (non-secret) SSID or scripted/non-secret scenarios.
WORKSHOP_WIFI_SSID_DEFAULT="reinvent-pqc-iot-wifi"
WIFI_SSID_CLI=""       # from --wifi-ssid (highest precedence)
WIFI_PASSWORD_CLI=""   # from --wifi-password
while [[ $# -gt 0 ]]; do
    case "$1" in
        --vpc-id) VPC_ID="$2"; shift 2 ;;
        --subnets) SUBNETS="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --device-count) DEVICE_COUNT="$2"; shift 2 ;;
        --wifi-ssid) WIFI_SSID_CLI="$2"; shift 2 ;;
        --wifi-password) WIFI_PASSWORD_CLI="$2"; shift 2 ;;
        *) echo "[ERROR] Unknown argument: $1"; exit 1 ;;
    esac
done

# Resolve WiFi SSID: CLI flag -> env var -> interactive prompt (default the
# workshop SSID) -> non-interactive default (SSID is not a secret).
if [[ -n "$WIFI_SSID_CLI" ]]; then
    WORKSHOP_WIFI_SSID="$WIFI_SSID_CLI"
elif [[ -n "${WORKSHOP_WIFI_SSID:-}" ]]; then
    WORKSHOP_WIFI_SSID="$WORKSHOP_WIFI_SSID"
elif [[ -t 0 ]]; then
    read -r -p "Workshop WiFi SSID [${WORKSHOP_WIFI_SSID_DEFAULT}]: " _ssid_in
    WORKSHOP_WIFI_SSID="${_ssid_in:-$WORKSHOP_WIFI_SSID_DEFAULT}"
else
    WORKSHOP_WIFI_SSID="$WORKSHOP_WIFI_SSID_DEFAULT"
fi

# Resolve WiFi password: CLI flag -> env var -> interactive hidden prompt
# (required, no default) -> non-interactive 'changeme' placeholder + later WARN.
if [[ -n "$WIFI_PASSWORD_CLI" ]]; then
    WORKSHOP_WIFI_PASSWORD="$WIFI_PASSWORD_CLI"
elif [[ -n "${WORKSHOP_WIFI_PASSWORD:-}" ]]; then
    WORKSHOP_WIFI_PASSWORD="$WORKSHOP_WIFI_PASSWORD"
elif [[ -t 0 ]]; then
    while [[ -z "${WORKSHOP_WIFI_PASSWORD:-}" ]]; do
        read -r -s -p "Workshop WiFi password (WPA2 PSK, 8-63 chars): " WORKSHOP_WIFI_PASSWORD
        echo ""
        [[ -z "$WORKSHOP_WIFI_PASSWORD" ]] && echo "  Password cannot be empty — try again."
    done
else
    WORKSHOP_WIFI_PASSWORD="changeme"
fi

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

# Resolve the REAL IoT Core data endpoint. It is account-specific and cannot be
# derived from the account id (the old '${AccountId}-ats...' guess does not
# resolve, which silently broke the broker->IoT bridge). Pass it into the stack
# so the broker bridge + device-builder get a working endpoint. Fail fast if it
# can't be resolved rather than deploy a broken bridge.
echo "    - resolving IoT Core data endpoint..."
IOT_ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS \
    --query 'endpointAddress' --output text 2>/dev/null) || IOT_ENDPOINT=""
if [[ -z "$IOT_ENDPOINT" || "$IOT_ENDPOINT" == "None" ]]; then
    echo "[ERROR] Could not resolve the IoT Core data endpoint via"
    echo "        'aws iot describe-endpoint --endpoint-type iot:Data-ATS'."
    echo "        The broker->IoT bridge needs this; aborting."
    exit 1
fi
echo "      IoT endpoint: $IOT_ENDPOINT"

aws cloudformation deploy \
    --template-file "$SCRIPT_DIR/pqc-workshop-main.yaml" \
    --stack-name "pqc-workshop" \
    --parameter-overrides \
        DeviceCount="$DEVICE_COUNT" \
        TemplateBucketName="$ARTIFACTS_BUCKET" \
        TemplateBucketPrefix="$TEMPLATE_PREFIX" \
        IoTEndpoint="$IOT_ENDPOINT" \
        BrokerTriggerBuild=false \
        BrokerCreateVpcEndpoints=false \
        DefaultWifiSsid="$WORKSHOP_WIFI_SSID" \
        DefaultWifiPassword="$WORKSHOP_WIFI_PASSWORD" \
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

# Shared cert-issuer build context (Dockerfile + issue_cert.py +
# issue_broker_certs.py + buildspec-broker-certs.yml). Feeds TWO CodeBuild jobs:
#   - CertIssuerBuildProject         -> builds the shared cert-issuer image
#   - BrokerCertProvisionerProject   -> runs in that image to mint broker certs
echo "        - cert-issuer source -> s3://${BROKER_SOURCE_BUCKET}/cert-issuer/"
aws s3 cp "$PROJECT_ROOT/shared/cert-issuer/" \
    "s3://${BROKER_SOURCE_BUCKET}/cert-issuer/" --recursive \
    --exclude "__pycache__/*" --exclude "*.pyc" >/dev/null

# Firmware source context. The FIRMWARE_BUCKET/source/ prefix feeds TWO
# CodeBuild projects that share it:
#   - ImageBuilderProject  (BuildSpec: buildspec-image.yml)  -> builds the
#     firmware-builder Docker image (one-time).
#   - FirmwareBuildProject (BuildSpec: buildspec.yml)        -> per-device
#     firmware compile; it also downloads the sketch .ino from source/.
# So we must stage BOTH buildspecs AND the two sketch .ino variants, or the
# per-device build fails DOWNLOAD_SOURCE ("buildspec.yml: no such file") or
# can't fetch its sketch.
echo "        - firmware source -> s3://${FIRMWARE_BUCKET}/source/"
aws s3 cp "$PROJECT_ROOT/raw_device_builder/codebuild/Dockerfile" \
    "s3://${FIRMWARE_BUCKET}/source/Dockerfile" >/dev/null
aws s3 cp "$PROJECT_ROOT/raw_device_builder/codebuild/buildspec-image.yml" \
    "s3://${FIRMWARE_BUCKET}/source/buildspec-image.yml" >/dev/null
# Per-device firmware build spec (FirmwareBuildProject BuildSpec: buildspec.yml).
aws s3 cp "$PROJECT_ROOT/raw_device_builder/codebuild/buildspec.yml" \
    "s3://${FIRMWARE_BUCKET}/source/buildspec.yml" >/dev/null
# The two sketch variants the per-device build downloads from source/ by name:
#   classical -> aws_iot_pqc_esp32_v1_classical.ino ; pqc -> aws_iot_pqc_esp32.ino
aws s3 cp "$PROJECT_ROOT/aws_iot_pqc_esp32_v1_classical/aws_iot_pqc_esp32_v1_classical.ino" \
    "s3://${FIRMWARE_BUCKET}/source/aws_iot_pqc_esp32_v1_classical.ino" >/dev/null
aws s3 cp "$PROJECT_ROOT/aws_iot_pqc_esp32/aws_iot_pqc_esp32.ino" \
    "s3://${FIRMWARE_BUCKET}/source/aws_iot_pqc_esp32.ino" >/dev/null
# pqc-mldsa variant (Step 2): ML-KEM key exchange + ML-DSA-44 device cert, 8884.
aws s3 cp "$PROJECT_ROOT/aws_iot_pqc_esp32_v3_mldsa/aws_iot_pqc_esp32_v3_mldsa.ino" \
    "s3://${FIRMWARE_BUCKET}/source/aws_iot_pqc_esp32_v3_mldsa.ino" >/dev/null
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
# Shared cert-issuer script — baked into the firmware-builder image (Dockerfile
# COPYs it) so the per-device firmware build mints its device cert via the SAME
# issuance code path as the broker cert-issuer container.
stage_extra issue_cert.py "$PROJECT_ROOT/shared/cert-issuer/issue_cert.py"

# ─── Verify the firmware source/ prefix is complete (fail fast) ──────────────
# The per-device firmware build (FirmwareBuildProject) and the one-time image
# build (ImageBuilderProject) both read from this prefix. A missing file here
# does not surface until a device flash fails deep in CodeBuild
# (DOWNLOAD_SOURCE "buildspec.yml: no such file", or the sketch download 404s),
# which is confusing and easy to ship. Assert every required object exists NOW
# so a broken deploy fails loudly here instead of at first device flash.
echo "        - verifying firmware source/ is complete..."
REQUIRED_FW_SOURCE=(
    buildspec.yml                          # per-device firmware build spec
    buildspec-image.yml                    # one-time builder-image spec
    Dockerfile                             # builder image
    aws_iot_pqc_esp32_v1_classical.ino     # classical sketch variant
    aws_iot_pqc_esp32.ino                  # pqc sketch variant
    aws_iot_pqc_esp32_v3_mldsa.ino         # pqc-mldsa sketch variant (Step 2)
    wolfssl_user_settings_arduino.h        # wolfSSL PQC config
    partitions_pqc_workshop.csv            # partition table
    issue_cert.py                          # shared cert-issuer (baked into image)
)
_missing_fw=0
for obj in "${REQUIRED_FW_SOURCE[@]}"; do
    if ! aws s3api head-object --bucket "$FIRMWARE_BUCKET" --key "source/$obj" >/dev/null 2>&1; then
        echo "          ! MISSING: s3://${FIRMWARE_BUCKET}/source/$obj"
        _missing_fw=1
    fi
done
if [[ "$_missing_fw" -ne 0 ]]; then
    echo "[ERROR] Firmware source/ prefix is incomplete (see MISSING above)."
    echo "        Device firmware builds WILL fail. Aborting so this is not shipped."
    exit 1
fi
echo "          firmware source/ complete (${#REQUIRED_FW_SOURCE[@]} files)"

# ─── Stage the builder-Pi app + assets for the SSM deploy-app document ────────
# The builder-Pi is no longer provisioned from a self-contained bundle; the SSM
# documents (pqc-workshop-deploy-app) fetch the app + assets from the firmware
# bucket instead of the bundle shipping them. So stage them here, next to the
# firmware source, under the builder-pi/ prefix:
#   - app.tgz                          the Flask GUI + workshop-builder CLI tree
#   - partitions_pqc_workshop.csv      custom ESP32 partition table
#   - wolfssl_user_settings_arduino.h  wolfSSL PQC config applied to the lib
# The deploy-app document unpacks app.tgz into /opt/pqc-workshop/builder-app and
# applies the two asset files. config.json is written by the document from SSM
# (not from the tarball), so the tarball's config.json is just a harmless
# template. setup-builder-pi.sh is deliberately excluded — it is retired.
echo "        - staging builder-Pi app + assets -> s3://${FIRMWARE_BUCKET}/builder-pi/"
BUILDER_PI_SRC="$PROJECT_ROOT/raw_device_builder/builder-pi"
APP_TGZ="$(mktemp -t builder-pi-app.XXXX.tgz)"
# Pack the app tree, excluding: the retired on-Pi setup script, python caches,
# and the tests dir (not needed on the Pi). Paths inside the tarball are
# relative to builder-pi/ so they unpack directly into $APP_DIR.
tar -czf "$APP_TGZ" -C "$BUILDER_PI_SRC" \
    --exclude 'setup-builder-pi.sh' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'tests' \
    .
aws s3 cp "$APP_TGZ" "s3://${FIRMWARE_BUCKET}/builder-pi/app.tgz" >/dev/null
rm -f "$APP_TGZ"

# Partition table (canonical copy under esp32-flash/) and the wolfSSL PQC header
# (with the firmware-builder codebuild sources). Reuse stage_extra's fallback
# search so a moved file still resolves.
_stage_builder_asset() {
    local name="$1" f="$2"
    [[ -f "$f" ]] || f=$(find "$PROJECT_ROOT" -name "$name" -not -path '*/arduino-cli/data/*' 2>/dev/null | head -1)
    if [[ -n "$f" && -f "$f" ]]; then
        echo "          - $name"
        aws s3 cp "$f" "s3://${FIRMWARE_BUCKET}/builder-pi/$name" >/dev/null
    else
        echo "          ! WARN: $name not found; builder-Pi deploy-app doc will warn"
    fi
}
_stage_builder_asset partitions_pqc_workshop.csv     "$PROJECT_ROOT/esp32-flash/partitions_pqc_workshop.csv"
_stage_builder_asset wolfssl_user_settings_arduino.h "$PROJECT_ROOT/raw_device_builder/codebuild/wolfssl_user_settings_arduino.h"

# Fail fast if the app tarball didn't land — the deploy-app SSM doc hard-depends
# on it, and a missing object only surfaces as a confusing SSM failure later.
if ! aws s3api head-object --bucket "$FIRMWARE_BUCKET" --key "builder-pi/app.tgz" >/dev/null 2>&1; then
    echo "[ERROR] builder-Pi app.tgz did not upload to s3://${FIRMWARE_BUCKET}/builder-pi/."
    echo "        The builder-Pi deploy-app SSM document would fail. Aborting."
    exit 1
fi
echo "          builder-Pi app + assets staged"

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

# ─── Shared cert-issuer image + broker certificate provisioning ──────────────
# The cert-issuer image (OpenSSL 3.5 + issue_cert.py) is the ONE image that mints
# every TLS cert. It must be built BEFORE the broker-cert provisioner job runs in
# it. Both CAs are already ACTIVE (created by the stack in step 2). Ordering:
#   1. build cert-issuer image        (CertIssuerImageBuildProject)
#   2. run broker cert provisioner    (BrokerCertProvisionerProject, in that image)
# This mirrors the broker image flow and keeps the whole deploy one-shot.
run_build() {
    # $1 = human label, $2 = CodeBuild project name
    local label="$1" project="$2" id st
    echo "        - starting $label ($project)..."
    id=$(aws codebuild start-build --project-name "$project" \
        --query 'build.id' --output text) || { echo "[ERROR] could not start $label"; exit 1; }
    echo "          build: $id (waiting for completion)"
    while true; do
        st=$(aws codebuild batch-get-builds --ids "$id" \
            --query 'builds[0].buildStatus' --output text)
        case "$st" in
            SUCCEEDED) echo "          $label SUCCEEDED"; break ;;
            FAILED|FAULT|TIMED_OUT|STOPPED) echo "[ERROR] $label $st ($id)"; exit 1 ;;
            *) sleep 15 ;;
        esac
    done
}

CERT_ISSUER_BUILD_PROJECT=$(get_out CertIssuerImageBuildProject 2>/dev/null || true)
[[ -z "$CERT_ISSUER_BUILD_PROJECT" || "$CERT_ISSUER_BUILD_PROJECT" == "None" ]] && CERT_ISSUER_BUILD_PROJECT="pqc-workshop-cert-issuer-image-build"
run_build "cert-issuer image build" "$CERT_ISSUER_BUILD_PROJECT"

BROKER_CERT_PROJECT=$(get_out BrokerCertProvisionerProjectName 2>/dev/null || true)
[[ -z "$BROKER_CERT_PROJECT" || "$BROKER_CERT_PROJECT" == "None" ]] && BROKER_CERT_PROJECT="pqc-workshop-broker-cert-provisioner"
run_build "broker cert provisioning (classical + ML-DSA-44)" "$BROKER_CERT_PROJECT"

# Scale the broker ECS service up (it ships at DesiredCount 0). Now that an image
# exists, one task can start and register with the NLB. The cluster and service
# are both named pqc-workshop-broker.
echo "        - scaling broker ECS service to 1 task..."
if ! aws ecs update-service --cluster pqc-workshop-broker --service pqc-workshop-broker \
        --desired-count 1 >/dev/null; then
    echo "          WARN: could not scale the broker service; scale it manually with:"
    echo "            aws ecs update-service --cluster pqc-workshop-broker --service pqc-workshop-broker --desired-count 1${PROFILE:+ --profile $PROFILE}"
fi

# ─── Step 4: publish config to SSM + build the dashboard via CodeBuild ───────
echo "[4/5] Publishing config + building the v2 dashboard (CodeBuild)..."
APPSYNC_ENDPOINT=$(get_out AppSyncEndpoint)
IDENTITY_POOL=$(get_out CognitoIdentityPoolId)
API_ENDPOINT=$(get_out BuilderApiEndpoint)
# The dashboard's site bucket + CloudFront distribution are consumed by the
# CodeBuild project (via its stack-supplied DASHBOARD_BUCKET /
# DASHBOARD_DISTRIBUTION_ID env vars), so the deploy script no longer needs
# them here — it only stages source + triggers the build.
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
put_param /pqc-workshop/iotEndpoint               "$IOT_ENDPOINT"
# Broker NLB endpoint — the single source of truth for where devices connect
# (port 8883). Published from the broker stack output so the registrar Lambda
# and the builder-Pi GUI both read the same canonical value; no operator input.
put_param /pqc-workshop/broker/endpoint           "$(get_out BrokerEndpoint)"

# Workshop WiFi: the single source of truth consumed by the builder-Pi hostapd
# AP setup (broadcasts this SSID/PSK) and mirrored into firmware at build time.
# SSID is a plain String; the password is a SecureString (put_param hardcodes
# --type String, so publish the password with a dedicated call).
put_param /pqc-workshop/wifi/ssid "$WORKSHOP_WIFI_SSID"
aws ssm put-parameter --name /pqc-workshop/wifi/password --type SecureString \
    --overwrite --value "$WORKSHOP_WIFI_PASSWORD" --region "$AWS_REGION" >/dev/null \
    || echo "    WARN: failed to put /pqc-workshop/wifi/password"
if [[ "$WORKSHOP_WIFI_SSID" == "changeme" || "$WORKSHOP_WIFI_PASSWORD" == "changeme" ]]; then
    echo "    WARN: workshop WiFi is the 'changeme' placeholder — devices will NOT"
    echo "          join a real network. Re-run with WORKSHOP_WIFI_SSID and"
    echo "          WORKSHOP_WIFI_PASSWORD set to the real workshop credentials."
fi

# ─── Build + deploy the dashboard via CodeBuild (not on this host) ───────────
# The Vite/React build used to run here (npm ci && npm run build) and sync from
# the operator's laptop, making the deploy laptop-dependent and slow. It now
# runs in the pqc-workshop-dashboard-build CodeBuild project (mirrors how the
# broker image is built above): we stage the frontend source to the dashboard
# source bucket, trigger the build, and wait. The build reads its config from
# the SSM parameters published just above, materializes public/config.json,
# builds, syncs build/ to the site bucket, and invalidates CloudFront.
DASHBOARD_SOURCE_BUCKET="pqc-workshop-dashboard-source-${ACCOUNT_ID}"

# Stage the frontend build source. Exclude node_modules and the local build/
# output (regenerated in CodeBuild) and the gitignored public/config.json (the
# build materializes it from SSM). --delete keeps the source prefix clean across
# redeploys.
echo "        - staging dashboard source -> s3://${DASHBOARD_SOURCE_BUCKET}/frontend/"
aws s3 sync "$V2_FRONTEND/" "s3://${DASHBOARD_SOURCE_BUCKET}/frontend/" --delete \
    --exclude "node_modules/*" \
    --exclude "build/*" \
    --exclude "public/config.json" >/dev/null

# Trigger the dashboard build now that its source is in place, and wait.
DASHBOARD_BUILD_PROJECT=$(get_out DashboardBuildProjectName 2>/dev/null || true)
[[ -z "$DASHBOARD_BUILD_PROJECT" || "$DASHBOARD_BUILD_PROJECT" == "None" ]] && DASHBOARD_BUILD_PROJECT="pqc-workshop-dashboard-build"
echo "        - starting dashboard build ($DASHBOARD_BUILD_PROJECT)..."
DASH_BUILD_ID=$(aws codebuild start-build --project-name "$DASHBOARD_BUILD_PROJECT" \
    --query 'build.id' --output text)
echo "          build: $DASH_BUILD_ID (waiting for completion)"
while true; do
    st=$(aws codebuild batch-get-builds --ids "$DASH_BUILD_ID" \
        --query 'builds[0].buildStatus' --output text)
    case "$st" in
        SUCCEEDED) echo "          dashboard build SUCCEEDED"; break ;;
        FAILED|FAULT|TIMED_OUT|STOPPED) echo "[ERROR] dashboard build $st ($DASH_BUILD_ID)"; exit 1 ;;
        *) sleep 15 ;;
    esac
done

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
echo "  2. Provision the Builder-Pi (two steps — see DEPLOYMENT.md §5):"
echo "       a. Register the Pi:  raw_device_builder/bundle-for-builder-pi.sh --region $AWS_REGION"
echo "          then on the Pi:   unzip builder-pi-bundle.zip && cd builder-pi-bundle && sudo ./setup-ssm.sh"
echo "       b. Provision via SSM: ./raw_device_builder/setup-builder-pi-remote.sh --region $AWS_REGION${PROFILE_ARG}"
echo "  3. Validate:  ./check_setup_status.sh --region $AWS_REGION${PROFILE_ARG}"
echo ""
