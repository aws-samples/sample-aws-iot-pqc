#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# package.sh
#
# Packages the nested CloudFormation templates (including their Lambda code, via
# `aws cloudformation package`) and uploads them to an assets bucket that the
# parent stack references. Use this for Workshop Studio, where assets are staged
# in a provided bucket and the parent template is deployed by the platform.
#
# For a normal single-command deploy, use deploy-workshop.sh instead (it also
# deploys the broker + device-builder stacks and builds the v2 dashboard).
#
# S3 layout after packaging:
#   s3://<bucket>/<prefix>/
#     templates/     ← packaged nested templates (Lambda Code rewritten to S3)
#       device-provisioning-stack.yaml
#       ssm-hybrid-activation-stack.yaml
#       dashboard-stack.yaml
#     packaged/      ← Lambda code zips uploaded by `cloudformation package`
#
# Usage:
#   ./package.sh <aws-region> [s3-bucket] [s3-prefix]
#
# Examples:
#   ./package.sh us-east-1                                   # auto-create bucket
#   ./package.sh us-east-1 ws-assets-prod-iad-xxxxx pqc-workshop
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

AWS_REGION="${1:-us-east-1}"
BUCKET_NAME="${2:-}"
S3_PREFIX="${3:-pqc-workshop}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DASHBOARD_INFRA="$PROJECT_ROOT/dashboard/infrastructure"

export AWS_DEFAULT_REGION="$AWS_REGION"

echo "═══════════════════════════════════════════════════════════════"
echo "  PQC Workshop — Package nested templates"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ─── Step 1: Determine or create the assets bucket ──────────────────────────
if [[ -z "$BUCKET_NAME" ]]; then
    ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
    BUCKET_NAME="pqc-workshop-assets-${ACCOUNT_ID}-${AWS_REGION}"
    if ! aws s3 ls "s3://$BUCKET_NAME" >/dev/null 2>&1; then
        if [[ "$AWS_REGION" == "us-east-1" ]]; then
            aws s3 mb "s3://$BUCKET_NAME"
        else
            aws s3 mb "s3://$BUCKET_NAME" --create-bucket-configuration LocationConstraint="$AWS_REGION"
        fi
    fi
fi
echo "  Bucket: $BUCKET_NAME"
echo "  Prefix: $S3_PREFIX"
echo "  Region: $AWS_REGION"
echo ""

# ─── Step 2: Package + upload each nested template ──────────────────────────
# `cloudformation package` zips each Lambda Code: dir, uploads it, and rewrites
# the template to reference the S3 object. The packaged template is then placed
# under templates/ where the parent stack expects it.
echo "  Packaging nested templates (Lambda code included)..."
for pair in \
    "$SCRIPT_DIR/device-provisioning-stack.yaml:device-provisioning-stack.yaml" \
    "$SCRIPT_DIR/ssm-hybrid-activation-stack.yaml:ssm-hybrid-activation-stack.yaml" \
    "$DASHBOARD_INFRA/dashboard-stack.yaml:dashboard-stack.yaml"; do
    src="${pair%%:*}"; name="${pair##*:}"
    tmp="$(mktemp -t "${name}.XXXX.yaml")"
    echo "    - $name"
    aws cloudformation package --template-file "$src" \
        --s3-bucket "$BUCKET_NAME" --s3-prefix "$S3_PREFIX/packaged" \
        --output-template-file "$tmp" >/dev/null
    aws s3 cp "$tmp" "s3://$BUCKET_NAME/$S3_PREFIX/templates/$name" >/dev/null
    rm -f "$tmp"
done
echo ""

echo "═══════════════════════════════════════════════════════════════"
echo "  Packaging complete."
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "Workshop Studio usage:"
echo "  1. Provisioning template:  cloudformation/pqc-workshop-main.yaml"
echo "  2. Parameters:"
echo "       TemplateBucketName   = $BUCKET_NAME"
echo "       TemplateBucketPrefix = $S3_PREFIX"
echo ""
echo "NOTE: pqc-workshop-main.yaml deploys device provisioning, the dashboard"
echo "backend, v2 dashboard hosting and SSM activation. The broker-container and"
echo "device-builder stacks (Private CA, CodeBuild, PQC migration) deploy"
echo "separately — see deploy-workshop.sh for the full ordered deploy."
echo ""
