#!/bin/bash
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# ─────────────────────────────────────────────────────────────────────────────
# build-and-push.sh — Build the Mosquitto PQC Docker image via CodeBuild
#
# Usage:
#   ./build-and-push.sh [--local]
#
# Without --local: uploads source to S3 and triggers CodeBuild (recommended)
# With --local:    builds locally with Docker and pushes directly to ECR
#
# Prerequisites:
#   - AWS CLI configured with appropriate permissions
#   - For --local: Docker installed and running
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DOCKER_DIR="$SCRIPT_DIR/../docker"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/pqc-workshop-mosquitto-broker"
S3_BUCKET="pqc-workshop-broker-source-$ACCOUNT_ID"
CODEBUILD_PROJECT="pqc-workshop-broker-image-build"

echo "═══════════════════════════════════════════════════════════════"
echo "  Mosquitto PQC Broker — Image Build"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  ECR: $ECR_REPO"
echo "  Region: $REGION"
echo ""

if [[ "${1:-}" == "--local" ]]; then
    # ─── Local Docker Build ─────────────────────────────────────────────────
    echo "  Mode: LOCAL (Docker)"
    echo ""

    echo "[1/3] Logging in to ECR..."
    aws ecr get-login-password --region "$REGION" | \
        docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"

    echo "[2/3] Building Docker image (this may take 10-15 minutes)..."
    docker build -t "$ECR_REPO:latest" -f "$DOCKER_DIR/Dockerfile" "$DOCKER_DIR"
    docker tag "$ECR_REPO:latest" "$ECR_REPO:$(date +%Y%m%d)"

    echo "[3/3] Pushing to ECR..."
    docker push "$ECR_REPO:latest"
    docker push "$ECR_REPO:$(date +%Y%m%d)"

else
    # ─── CodeBuild (Remote) ─────────────────────────────────────────────────
    echo "  Mode: CODEBUILD (Remote)"
    echo "  S3 Source: s3://$S3_BUCKET/broker-image/"
    echo ""

    echo "[1/3] Uploading source files to S3..."
    aws s3 cp "$DOCKER_DIR/Dockerfile" "s3://$S3_BUCKET/broker-image/Dockerfile" --region "$REGION"
    aws s3 cp "$DOCKER_DIR/entrypoint.sh" "s3://$S3_BUCKET/broker-image/entrypoint.sh" --region "$REGION"
    aws s3 cp "$DOCKER_DIR/mosquitto.conf" "s3://$S3_BUCKET/broker-image/mosquitto.conf" --region "$REGION"
    aws s3 cp "$DOCKER_DIR/buildspec.yml" "s3://$S3_BUCKET/broker-image/buildspec.yml" --region "$REGION"
    echo "       Source uploaded"

    echo "[2/3] Starting CodeBuild..."
    BUILD_ID=$(aws codebuild start-build \
        --project-name "$CODEBUILD_PROJECT" \
        --region "$REGION" \
        --query 'build.id' --output text)
    echo "       Build ID: $BUILD_ID"

    echo "[3/3] Waiting for build to complete..."
    echo "       (You can also monitor at: https://$REGION.console.aws.amazon.com/codesuite/codebuild/projects/$CODEBUILD_PROJECT)"
    echo ""

    while true; do
        STATUS=$(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" \
            --query 'builds[0].buildStatus' --output text)
        PHASE=$(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" \
            --query 'builds[0].currentPhase' --output text)
        echo "       Status: $STATUS | Phase: $PHASE"

        if [[ "$STATUS" == "SUCCEEDED" ]]; then
            break
        elif [[ "$STATUS" == "FAILED" || "$STATUS" == "FAULT" || "$STATUS" == "TIMED_OUT" || "$STATUS" == "STOPPED" ]]; then
            echo ""
            echo "  ERROR: Build $STATUS"
            echo "  Check logs: aws logs tail /aws/codebuild/$CODEBUILD_PROJECT --follow"
            exit 1
        fi
        sleep 10
    done
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Image pushed: $ECR_REPO:latest"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  To deploy, update the ECS service:"
echo "    aws ecs update-service --cluster pqc-workshop-broker \\"
echo "      --service pqc-workshop-broker --force-new-deployment"
echo ""
