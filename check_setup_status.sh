#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# check_setup_status.sh
#
# Validates the PQC workshop AWS environment is fully deployed and ready.
# Reflects the CURRENT architecture:
#   - ECS Fargate PQC broker (broker-container stack) behind an NLB, bridging to
#     IoT Core; single CloudWatch log group /pqc-workshop/broker-container
#   - Device-builder stack: registry table, builder API, CodeBuild, and the PQC
#     migration Step Functions workflow (prepare-build + publish-ota +
#     start-migration Lambdas)
#   - Dashboard/backend: AppSync pqc-workshop-device-api, DynamoDB
#     (pqc-workshop-devices, pqc-workshop-message-log), Cognito, telemetry/
#     command/message-logger/broker-log-reader Lambdas, IoT rules
#   - v2 "Firmware Manager" dashboard hosting (S3 + CloudFront)
#
# Greengrass has been removed from the workshop; this script no longer checks
# for any Greengrass resources.
#
# Prerequisites:
#   - AWS CLI v2 configured with read-only access to IoT, Lambda, DynamoDB,
#     AppSync, CloudFormation, CloudFront, S3, Cognito, StepFunctions, ECS,
#     ElasticLoadBalancing, ACM-PCA, Logs.
#
# Usage:
#   ./check_setup_status.sh [--region REGION] [--profile NAME]
#
# --profile targets a named AWS CLI profile for every aws call (useful for A-B
# testing across accounts). Omit to use ambient/default credentials.
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────────────────────
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
PROFILE=""

# Live stack names (the workshop deploys these; names are fixed).
BROKER_STACK="pqc-workshop-broker-container"
DEVICE_BUILDER_STACK="pqc-workshop-device-builder"

# ─── Parse Arguments ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --region) REGION="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--region REGION] [--profile NAME]"
            echo "  --region   AWS region (default: us-east-1 or AWS_DEFAULT_REGION)"
            echo "  --profile  Named AWS CLI profile to use (default: ambient credentials)"
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown argument: $1"; echo "Run $0 --help for usage."; exit 1 ;;
    esac
done

export AWS_DEFAULT_REGION="$REGION"
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# ─── Counters ────────────────────────────────────────────────────────────────
PASS=0; WARN=0; FAIL=0
pass() { echo "  ✅ $1"; PASS=$((PASS + 1)); }
warn() { echo "  ⚠️  $1"; WARN=$((WARN + 1)); }
fail() { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }

# Helper: does a Lambda function exist and is it Active?
check_lambda() {
    local fn="$1"
    local state
    state=$(aws lambda get-function --function-name "$fn" \
        --query 'Configuration.State' --output text 2>/dev/null) || state=""
    if [[ "$state" == "Active" ]]; then
        pass "Lambda '$fn' is Active"
    elif [[ -n "$state" ]]; then
        warn "Lambda '$fn' exists but state is: $state"
    else
        fail "Lambda '$fn' not found"
    fi
}

# Helper: does an IoT topic rule exist?
check_rule() {
    local rule="$1"
    local sql
    sql=$(aws iot get-topic-rule --rule-name "$rule" --query 'rule.sql' --output text 2>/dev/null) || sql=""
    if [[ -n "$sql" ]]; then
        pass "IoT Rule '$rule' exists"
    else
        fail "IoT Rule '$rule' not found"
    fi
}

# Helper: DynamoDB table ACTIVE?
check_table() {
    local tb="$1"
    local status
    status=$(aws dynamodb describe-table --table-name "$tb" --query 'Table.TableStatus' --output text 2>/dev/null) || status=""
    if [[ "$status" == "ACTIVE" ]]; then
        pass "DynamoDB table '$tb' is ACTIVE"
    else
        fail "DynamoDB table '$tb' status: ${status:-NOT_FOUND}"
    fi
}

# ─── Header ──────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  PQC Workshop — Environment Readiness Check"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Region:     $REGION"
echo "  Timestamp:  $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo ""

# ─── 1. AWS Credentials ─────────────────────────────────────────────────────
echo "─── 1. AWS Credentials ──────────────────────────────────────"
CALLER_IDENTITY=$(aws sts get-caller-identity --output json 2>&1) || {
    fail "No valid AWS credentials found. Configure credentials and retry."
    echo ""
    echo "  RESULT: FAILED (cannot proceed without credentials)"
    exit 1
}
ACCOUNT_ID=$(echo "$CALLER_IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
ROLE_ARN=$(echo "$CALLER_IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])")
pass "Authenticated as $ROLE_ARN (Account: $ACCOUNT_ID)"
echo ""

# ─── 2. CloudFormation Stacks ────────────────────────────────────────────────
echo "─── 2. CloudFormation Stacks ────────────────────────────────"
for stack in "$BROKER_STACK" "$DEVICE_BUILDER_STACK"; do
    status=$(aws cloudformation describe-stacks --stack-name "$stack" \
        --query 'Stacks[0].StackStatus' --output text 2>/dev/null) || status="NOT_FOUND"
    if [[ "$status" == "CREATE_COMPLETE" || "$status" == "UPDATE_COMPLETE" ]]; then
        pass "Stack '$stack' status: $status"
    else
        fail "Stack '$stack' status: $status (expected CREATE_COMPLETE/UPDATE_COMPLETE)"
    fi
done
# The dashboard/device provisioning stacks are nested under pqc-workshop-* names.
DASHBOARD_STACK=$(aws cloudformation list-stacks \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
    --query "StackSummaries[?contains(StackName, 'DashboardStack')].StackName | [0]" \
    --output text 2>/dev/null) || DASHBOARD_STACK=""
if [[ -n "$DASHBOARD_STACK" && "$DASHBOARD_STACK" != "None" ]]; then
    pass "Dashboard stack deployed: $DASHBOARD_STACK"
else
    warn "Dashboard stack not found (checked for *DashboardStack*)"
fi
echo ""

# ─── 3. IoT Core ────────────────────────────────────────────────────────────
echo "─── 3. IoT Core ─────────────────────────────────────────────"
ACTUAL_ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS \
    --query 'endpointAddress' --output text 2>/dev/null) || ACTUAL_ENDPOINT=""
[[ -n "$ACTUAL_ENDPOINT" ]] && pass "IoT endpoint: $ACTUAL_ENDPOINT" || fail "IoT endpoint not found"

# IoT rules of the current architecture (no Greengrass; auto-register + telemetry
# + lifecycle + message log rules).
for rule in pqc_workshop_auto_register pqc_workshop_telemetry pqc_workshop_lifecycle \
            pqc_workshop_message_log_dt pqc_workshop_message_log_cmd; do
    check_rule "$rule"
done
echo ""

# ─── 4. Lambda Functions ────────────────────────────────────────────────────
echo "─── 4. Lambda Functions ─────────────────────────────────────"
# Backend (dashboard) + device-builder + migration workflow + broker provisioners.
for fn in \
    pqc-workshop-telemetry-processor \
    pqc-workshop-command-publisher \
    pqc-workshop-message-logger \
    pqc-workshop-broker-log-reader \
    pqc-workshop-auto-register \
    pqc-workshop-device-registrar \
    pqc-workshop-device-deregistrar \
    pqc-workshop-firmware-pusher \
    pqc-workshop-prepare-build \
    pqc-workshop-publish-ota \
    pqc-workshop-start-migration; do
    check_lambda "$fn"
done
echo ""

# ─── 5. PQC Migration State Machine ──────────────────────────────────────────
echo "─── 5. Step Functions (PQC migration) ───────────────────────"
SM_ARN=$(aws stepfunctions list-state-machines \
    --query "stateMachines[?name=='pqc-workshop-pqc-migration'].stateMachineArn | [0]" \
    --output text 2>/dev/null) || SM_ARN=""
if [[ -n "$SM_ARN" && "$SM_ARN" != "None" ]]; then
    pass "State machine 'pqc-workshop-pqc-migration' exists"
else
    fail "State machine 'pqc-workshop-pqc-migration' not found"
fi
echo ""

# ─── 6. DynamoDB ─────────────────────────────────────────────────────────────
echo "─── 6. DynamoDB ─────────────────────────────────────────────"
check_table "pqc-workshop-devices"          # dashboard device state (listDevices)
check_table "pqc-workshop-device-registry"  # device-builder registry (certs)
check_table "pqc-workshop-message-log"      # dashboard message log view
echo ""

# ─── 7. AppSync API ─────────────────────────────────────────────────────────
echo "─── 7. AppSync API ──────────────────────────────────────────"
APPSYNC=$(aws appsync list-graphql-apis \
    --query "graphqlApis[?name=='pqc-workshop-device-api'].{id:apiId,uri:uris.GRAPHQL} | [0]" \
    --output json 2>/dev/null) || APPSYNC=""
APPSYNC_URI=$(echo "$APPSYNC" | python3 -c "import sys,json; d=json.load(sys.stdin) if sys.stdin.read else None; print((d or {}).get('uri','') if d else '')" 2>/dev/null) || APPSYNC_URI=""
if [[ -n "$APPSYNC_URI" ]]; then
    pass "AppSync API 'pqc-workshop-device-api' at: $APPSYNC_URI"
else
    fail "AppSync API 'pqc-workshop-device-api' not found"
fi
echo ""

# ─── 8. Cognito (unauthenticated dashboard access) ───────────────────────────
echo "─── 8. Cognito ──────────────────────────────────────────────"
POOL_ID=$(aws cognito-identity list-identity-pools --max-results 60 \
    --query "IdentityPools[?IdentityPoolName=='pqc_workshop_dashboard'].IdentityPoolId | [0]" \
    --output text 2>/dev/null) || POOL_ID=""
if [[ -n "$POOL_ID" && "$POOL_ID" != "None" ]]; then
    pass "Cognito identity pool 'pqc_workshop_dashboard' exists ($POOL_ID)"
else
    fail "Cognito identity pool 'pqc_workshop_dashboard' not found"
fi
echo ""

# ─── 9. v2 Dashboard Hosting (S3 + CloudFront) ───────────────────────────────
echo "─── 9. Dashboard Hosting (v2 Firmware Manager) ──────────────"
DASH_BUCKET="pqc-workshop-firmware-dashboard-${ACCOUNT_ID}"
FILE_COUNT=$(aws s3 ls "s3://$DASH_BUCKET/" --recursive 2>/dev/null | grep -c "index.html") || FILE_COUNT=0
if [[ "$FILE_COUNT" -ge 1 ]]; then
    pass "Dashboard bucket '$DASH_BUCKET' has the built app deployed"
else
    warn "Dashboard bucket '$DASH_BUCKET' missing index.html (run the build+sync step)"
fi
CF_DIST=$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?Comment=='PQC Workshop Firmware Manager Dashboard'].{Id:Id,Domain:DomainName,Status:Status} | [0]" \
    --output json 2>/dev/null) || CF_DIST=""
CF_DOMAIN=$(echo "$CF_DIST" | python3 -c "import sys,json; d=json.load(sys.stdin) if sys.stdin.read else None; print((d or {}).get('Domain','') if d else '')" 2>/dev/null) || CF_DOMAIN=""
if [[ -n "$CF_DOMAIN" ]]; then
    pass "CloudFront distribution for the v2 dashboard: https://$CF_DOMAIN"
else
    warn "CloudFront distribution for the v2 dashboard not found"
fi
echo ""

# ─── 10. PQC Broker (ECS Fargate + NLB + Private CA) ─────────────────────────
echo "─── 10. PQC Broker (ECS Fargate) ────────────────────────────"
# Broker log group (the single, live group — the Pi group is retired).
LOG_GROUP=$(aws logs describe-log-groups --log-group-name-prefix /pqc-workshop/broker-container \
    --query "logGroups[?logGroupName=='/pqc-workshop/broker-container'].logGroupName | [0]" \
    --output text 2>/dev/null) || LOG_GROUP=""
if [[ -n "$LOG_GROUP" && "$LOG_GROUP" != "None" ]]; then
    pass "Broker log group '/pqc-workshop/broker-container' exists"
else
    fail "Broker log group '/pqc-workshop/broker-container' not found"
fi
# Running ECS service/task for the broker.
CLUSTER_ARN=$(aws ecs list-clusters --query "clusterArns[?contains(@,'pqc-workshop') || contains(@,'broker')] | [0]" --output text 2>/dev/null) || CLUSTER_ARN=""
if [[ -n "$CLUSTER_ARN" && "$CLUSTER_ARN" != "None" ]]; then
    RUNNING=$(aws ecs list-tasks --cluster "$CLUSTER_ARN" --desired-status RUNNING \
        --query 'length(taskArns)' --output text 2>/dev/null) || RUNNING=0
    if [[ "$RUNNING" -ge 1 ]]; then
        pass "Broker ECS cluster has $RUNNING running task(s)"
    else
        warn "Broker ECS cluster found but no running tasks"
    fi
else
    warn "Broker ECS cluster not found"
fi
# Private CA (issues device certs) — from the broker stack.
CA_COUNT=$(aws acm-pca list-certificate-authorities \
    --query "length(CertificateAuthorities[?Status=='ACTIVE'])" --output text 2>/dev/null) || CA_COUNT=0
if [[ "$CA_COUNT" -ge 1 ]]; then
    pass "Active Private CA(s): $CA_COUNT"
else
    warn "No ACTIVE Private CA found (device cert issuance depends on it)"
fi
echo ""

# ─── 11. SSM Hybrid Managed Instance (Builder-Pi) ────────────────────────────
echo "─── 11. Builder-Pi (SSM Hybrid) ─────────────────────────────"
PI_COUNT=$(aws ssm describe-instance-information \
    --query "length(InstanceInformationList[?PingStatus=='Online'])" --output text 2>/dev/null) || PI_COUNT=0
if [[ "$PI_COUNT" -ge 1 ]]; then
    pass "Online SSM managed instance(s) (Builder-Pi): $PI_COUNT"
else
    warn "No Online SSM managed instances found (Builder-Pi may be offline)"
fi
echo ""

# ─── Summary ─────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  RESULT: $PASS passed, $WARN warnings, $FAIL failed"
echo "═══════════════════════════════════════════════════════════════"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
