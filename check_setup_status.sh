#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# check_setup_status.sh
#
# Validates the PQC workshop AWS environment is fully deployed and ready.
# Run this after deploying the CloudFormation stacks to confirm all resources
# are correctly provisioned and operational.
#
# Prerequisites:
#   - AWS CLI v2 installed and configured
#   - Valid AWS credentials in the session (env vars or profile)
#   - Permissions: read-only access to IoT, Lambda, DynamoDB, AppSync,
#     CloudFormation, CloudFront, S3, Cognito, Greengrass, SecretsManager
#
# Usage:
#   ./check_setup_status.sh [--region REGION] [--stack-name STACK_NAME]
#
# Examples:
#   ./check_setup_status.sh
#   ./check_setup_status.sh --region us-west-2
#   ./check_setup_status.sh --stack-name my-pqc-workshop
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────────────────────
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
STACK_NAME="pqc-workshop"

# ─── Parse Arguments ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --region)
            REGION="$2"
            shift 2
            ;;
        --stack-name)
            STACK_NAME="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [--region REGION] [--stack-name STACK_NAME]"
            echo ""
            echo "Options:"
            echo "  --region       AWS region (default: us-east-1 or AWS_DEFAULT_REGION)"
            echo "  --stack-name   CloudFormation stack name (default: pqc-workshop)"
            echo ""
            echo "Environment variables:"
            echo "  AWS_DEFAULT_REGION   Fallback region if --region not specified"
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown argument: $1"
            echo "Run $0 --help for usage."
            exit 1
            ;;
    esac
done

export AWS_DEFAULT_REGION="$REGION"

# ─── Counters ────────────────────────────────────────────────────────────────
PASS=0
WARN=0
FAIL=0

pass() {
    echo "  ✅ $1"
    PASS=$((PASS + 1))
}

warn() {
    echo "  ⚠️  $1"
    WARN=$((WARN + 1))
}

fail() {
    echo "  ❌ $1"
    FAIL=$((FAIL + 1))
}

# ─── Header ──────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  PQC Workshop — Environment Readiness Check"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Region:     $REGION"
echo "  Stack:      $STACK_NAME"
echo "  Timestamp:  $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo ""

# ─── 1. AWS Credentials ─────────────────────────────────────────────────────
echo "─── 1. AWS Credentials ──────────────────────────────────────"
CALLER_IDENTITY=$(aws sts get-caller-identity --output json 2>&1) || {
    fail "No valid AWS credentials found. Configure credentials and retry."
    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    echo "  RESULT: FAILED (cannot proceed without credentials)"
    echo "═══════════════════════════════════════════════════════════════"
    exit 1
}

ACCOUNT_ID=$(echo "$CALLER_IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
ROLE_ARN=$(echo "$CALLER_IDENTITY" | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'])")
pass "Authenticated as $ROLE_ARN (Account: $ACCOUNT_ID)"
echo ""

# ─── 2. CloudFormation Stacks ────────────────────────────────────────────────
echo "─── 2. CloudFormation Stacks ────────────────────────────────"

MAIN_STACK_STATUS=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null) || MAIN_STACK_STATUS="NOT_FOUND"

if [[ "$MAIN_STACK_STATUS" == "CREATE_COMPLETE" || "$MAIN_STACK_STATUS" == "UPDATE_COMPLETE" ]]; then
    pass "Main stack '$STACK_NAME' status: $MAIN_STACK_STATUS"
else
    fail "Main stack '$STACK_NAME' status: $MAIN_STACK_STATUS (expected CREATE_COMPLETE or UPDATE_COMPLETE)"
fi

# Check nested stacks
NESTED_STACKS=$(aws cloudformation list-stacks \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
    --query "StackSummaries[?contains(StackName, '$STACK_NAME-')].StackName" \
    --output json 2>/dev/null)

NESTED_COUNT=$(echo "$NESTED_STACKS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
if [[ "$NESTED_COUNT" -ge 3 ]]; then
    pass "Nested stacks deployed: $NESTED_COUNT found (Device, Dashboard, Greengrass)"
else
    fail "Expected at least 3 nested stacks, found $NESTED_COUNT"
fi

# Get stack outputs for use in later checks
STACK_OUTPUTS=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --query 'Stacks[0].Outputs' \
    --output json 2>/dev/null) || STACK_OUTPUTS="[]"

get_output() {
    echo "$STACK_OUTPUTS" | python3 -c "
import sys, json
outputs = json.load(sys.stdin)
for o in outputs:
    if o['OutputKey'] == '$1':
        print(o['OutputValue'])
        break
" 2>/dev/null
}

IOT_ENDPOINT=$(get_output "IoTEndpoint")
GREENGRASS_THING=$(get_output "GreengrassThingName")
DASHBOARD_URL=$(get_output "DashboardURL")
DASHBOARD_BUCKET=$(get_output "DashboardBucketName")
APPSYNC_ENDPOINT=$(get_output "AppSyncEndpoint")
COGNITO_POOL_ID=$(get_output "CognitoIdentityPoolId")
DEVICE_POLICY=$(get_output "DevicePolicyName")
DEVICE_COUNT=$(get_output "DeviceCount")

echo ""

# ─── 3. IoT Core ────────────────────────────────────────────────────────────
echo "─── 3. IoT Core ─────────────────────────────────────────────"

# Endpoint
ACTUAL_ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS \
    --query 'endpointAddress' --output text 2>/dev/null) || ACTUAL_ENDPOINT=""

if [[ -n "$ACTUAL_ENDPOINT" ]]; then
    pass "IoT endpoint: $ACTUAL_ENDPOINT"
else
    fail "IoT endpoint not found"
fi

# Thing (Greengrass core)
if [[ -n "$GREENGRASS_THING" ]]; then
    THING_EXISTS=$(aws iot describe-thing --thing-name "$GREENGRASS_THING" \
        --query 'thingName' --output text 2>/dev/null) || THING_EXISTS=""
    if [[ "$THING_EXISTS" == "$GREENGRASS_THING" ]]; then
        pass "Greengrass core Thing exists: $GREENGRASS_THING"
    else
        fail "Greengrass core Thing '$GREENGRASS_THING' not found"
    fi
else
    fail "GreengrassThingName not found in stack outputs"
fi

# Device policy
if [[ -n "$DEVICE_POLICY" ]]; then
    POLICY_DOC=$(aws iot get-policy --policy-name "$DEVICE_POLICY" \
        --query 'policyDocument' --output text 2>/dev/null) || POLICY_DOC=""
    if [[ -n "$POLICY_DOC" ]]; then
        # Check policy allows esp32-* wildcard
        if echo "$POLICY_DOC" | grep -q "esp32-\*"; then
            pass "Device policy '$DEVICE_POLICY' allows esp32-* wildcard"
        else
            warn "Device policy '$DEVICE_POLICY' exists but may not have esp32-* wildcard"
        fi
    else
        fail "Device policy '$DEVICE_POLICY' not found"
    fi
else
    fail "DevicePolicyName not found in stack outputs"
fi

# Certificates
CERT_COUNT=$(aws iot list-certificates --query 'length(certificates[?status==`ACTIVE`])' --output text 2>/dev/null) || CERT_COUNT="0"
if [[ "$CERT_COUNT" -ge 1 ]]; then
    pass "Active IoT certificates: $CERT_COUNT"
else
    fail "No active IoT certificates found"
fi

# IoT Rules
EXPECTED_RULES=("pqc_workshop_auto_register" "pqc_workshop_telemetry" "pqc_workshop_lifecycle")
for RULE_NAME in "${EXPECTED_RULES[@]}"; do
    RULE_SQL=$(aws iot get-topic-rule --rule-name "$RULE_NAME" \
        --query 'rule.sql' --output text 2>/dev/null) || RULE_SQL=""
    if [[ -n "$RULE_SQL" ]]; then
        pass "IoT Rule '$RULE_NAME' exists (SQL: $RULE_SQL)"
    else
        fail "IoT Rule '$RULE_NAME' not found"
    fi
done

echo ""

# ─── 4. Lambda Functions ────────────────────────────────────────────────────
echo "─── 4. Lambda Functions ─────────────────────────────────────"

EXPECTED_LAMBDAS=(
    "pqc-workshop-auto-register"
    "pqc-workshop-telemetry-processor"
    "pqc-workshop-command-publisher"
    "pqc-workshop-greengrass-core-cert-provisioner"
)

for FUNC in "${EXPECTED_LAMBDAS[@]}"; do
    FUNC_STATE=$(aws lambda get-function --function-name "$FUNC" \
        --query 'Configuration.State' --output text 2>/dev/null) || FUNC_STATE=""
    if [[ "$FUNC_STATE" == "Active" ]]; then
        pass "Lambda '$FUNC' is Active"
    elif [[ -n "$FUNC_STATE" ]]; then
        warn "Lambda '$FUNC' exists but state is: $FUNC_STATE"
    else
        fail "Lambda '$FUNC' not found"
    fi
done

echo ""

# ─── 5. DynamoDB ─────────────────────────────────────────────────────────────
echo "─── 5. DynamoDB ─────────────────────────────────────────────"

TABLE_NAME="pqc-workshop-devices"
TABLE_STATUS=$(aws dynamodb describe-table --table-name "$TABLE_NAME" \
    --query 'Table.TableStatus' --output text 2>/dev/null) || TABLE_STATUS=""

if [[ "$TABLE_STATUS" == "ACTIVE" ]]; then
    pass "DynamoDB table '$TABLE_NAME' is ACTIVE"
else
    fail "DynamoDB table '$TABLE_NAME' status: ${TABLE_STATUS:-NOT_FOUND}"
fi

echo ""

# ─── 6. AppSync API ─────────────────────────────────────────────────────────
echo "─── 6. AppSync API ──────────────────────────────────────────"

if [[ -n "$APPSYNC_ENDPOINT" ]]; then
    # Verify the API actually exists by listing and matching
    API_NAME=$(aws appsync list-graphql-apis \
        --query "graphqlApis[?uris.GRAPHQL=='$APPSYNC_ENDPOINT'].name | [0]" \
        --output text 2>/dev/null) || API_NAME=""
    if [[ -n "$API_NAME" && "$API_NAME" != "None" ]]; then
        pass "AppSync API '$API_NAME' active at: $APPSYNC_ENDPOINT"
    else
        fail "AppSync API not found for endpoint: $APPSYNC_ENDPOINT"
    fi
else
    fail "AppSyncEndpoint not found in stack outputs"
fi

echo ""

# ─── 7. Cognito Identity Pool ───────────────────────────────────────────────
echo "─── 7. Cognito Identity Pool ────────────────────────────────"

if [[ -n "$COGNITO_POOL_ID" ]]; then
    ALLOW_UNAUTH=$(aws cognito-identity describe-identity-pool \
        --identity-pool-id "$COGNITO_POOL_ID" \
        --query 'AllowUnauthenticatedIdentities' \
        --output text 2>/dev/null) || ALLOW_UNAUTH=""
    if [[ "$ALLOW_UNAUTH" == "True" ]]; then
        pass "Cognito Identity Pool allows unauthenticated access (attendees don't need login)"
    elif [[ "$ALLOW_UNAUTH" == "False" ]]; then
        warn "Cognito Identity Pool does NOT allow unauthenticated access — dashboard may require login"
    else
        fail "Cognito Identity Pool '$COGNITO_POOL_ID' not found or inaccessible"
    fi
else
    fail "CognitoIdentityPoolId not found in stack outputs"
fi

echo ""

# ─── 8. Dashboard (CloudFront + S3) ─────────────────────────────────────────
echo "─── 8. Dashboard (CloudFront + S3) ──────────────────────────"

if [[ -n "$DASHBOARD_BUCKET" ]]; then
    FILE_COUNT=$(aws s3 ls "s3://$DASHBOARD_BUCKET/" --recursive \
        --summarize --human-readable 2>/dev/null | grep "Total Objects" | awk '{print $3}') || FILE_COUNT="0"
    if [[ "$FILE_COUNT" -ge 2 ]]; then
        pass "S3 bucket '$DASHBOARD_BUCKET' has $FILE_COUNT files deployed"
    else
        warn "S3 bucket '$DASHBOARD_BUCKET' has $FILE_COUNT file(s) — expected at least index.html + assets"
    fi
else
    fail "DashboardBucketName not found in stack outputs"
fi

if [[ -n "$DASHBOARD_URL" ]]; then
    # Extract domain from URL for CloudFront check
    CF_DOMAIN=$(echo "$DASHBOARD_URL" | sed 's|https://||')
    CF_STATUS=$(aws cloudfront list-distributions \
        --query "DistributionList.Items[?DomainName=='$CF_DOMAIN'].Status | [0]" \
        --output text 2>/dev/null) || CF_STATUS=""
    if [[ "$CF_STATUS" == "Deployed" ]]; then
        pass "CloudFront distribution deployed: $DASHBOARD_URL"
    elif [[ -n "$CF_STATUS" && "$CF_STATUS" != "None" ]]; then
        warn "CloudFront distribution status: $CF_STATUS (expected: Deployed)"
    else
        fail "CloudFront distribution not found for $DASHBOARD_URL"
    fi
else
    fail "DashboardURL not found in stack outputs"
fi

echo ""

# ─── 9. Secrets Manager ─────────────────────────────────────────────────────
echo "─── 9. Secrets Manager ──────────────────────────────────────"

SECRET_ID="iot/${GREENGRASS_THING:-pqc-workshop-greengrass-core}/greengrass-credentials"
SECRET_STATUS=$(aws secretsmanager describe-secret --secret-id "$SECRET_ID" \
    --query 'Name' --output text 2>/dev/null) || SECRET_STATUS=""

if [[ -n "$SECRET_STATUS" ]]; then
    pass "Greengrass credentials secret exists: $SECRET_ID"
else
    fail "Greengrass credentials secret not found: $SECRET_ID"
fi

echo ""

# ─── 10. Greengrass Core Device ──────────────────────────────────────────────
echo "─── 10. Greengrass Core Device ──────────────────────────────"

GG_CORE_STATUS=$(aws greengrassv2 list-core-devices \
    --query "coreDevices[?coreDeviceThingName=='${GREENGRASS_THING}'].status | [0]" \
    --output text 2>/dev/null) || GG_CORE_STATUS=""

if [[ "$GG_CORE_STATUS" == "HEALTHY" ]]; then
    pass "Greengrass core '$GREENGRASS_THING' is HEALTHY"
elif [[ -n "$GG_CORE_STATUS" && "$GG_CORE_STATUS" != "None" ]]; then
    warn "Greengrass core '$GREENGRASS_THING' status: $GG_CORE_STATUS"
else
    warn "Greengrass core '$GREENGRASS_THING' not registered yet (Pi needs provisioning)"
fi

echo ""

# ─── 11. ESP32 Device Registrations ─────────────────────────────────────────
echo "─── 11. ESP32 Device Registrations ──────────────────────────"

ESP32_THINGS=$(aws iot list-things \
    --query "things[?starts_with(thingName, 'esp32-')].thingName" \
    --output json 2>/dev/null) || ESP32_THINGS="[]"

ESP32_COUNT=$(echo "$ESP32_THINGS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [[ "$ESP32_COUNT" -gt 0 ]]; then
    pass "$ESP32_COUNT ESP32 device(s) registered (auto-registered on first connect)"
    echo "$ESP32_THINGS" | python3 -c "
import sys, json
things = json.load(sys.stdin)
for t in things[:10]:
    print(f'       - {t}')
if len(things) > 10:
    print(f'       ... and {len(things) - 10} more')
"
else
    warn "No ESP32 devices registered yet (they register on first connection — expected before workshop)"
fi

echo ""

# ─── Summary ─────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  RESULTS"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  ✅ Passed:   $PASS"
echo "  ⚠️  Warnings: $WARN"
echo "  ❌ Failed:   $FAIL"
echo ""

if [[ $FAIL -eq 0 && $WARN -eq 0 ]]; then
    echo "  🎉 Environment is FULLY READY for the workshop!"
elif [[ $FAIL -eq 0 ]]; then
    echo "  👍 Environment is MOSTLY READY (review warnings above)."
    echo ""
    echo "  Common warnings before the session:"
    echo "    - Greengrass core not registered: Pi not yet provisioned"
    echo "    - No ESP32 devices: They auto-register when powered on"
else
    echo "  🚨 Environment has FAILURES that need to be resolved."
    echo "     Fix the issues above and run this script again."
fi

echo ""
echo "  Dashboard:  ${DASHBOARD_URL:-N/A}"
echo "  IoT Endpoint: ${ACTUAL_ENDPOINT:-N/A}"
echo "  Region:     $REGION"
echo "  Account:    $ACCOUNT_ID"
echo ""
