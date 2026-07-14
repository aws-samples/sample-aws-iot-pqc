#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# build-and-push.sh — Build the Mosquitto PQC Docker image and push to ECR
#
# Usage:
#   ./build-and-push.sh [--region REGION]
#
# Prerequisites:
#   - Docker installed and running
#   - AWS CLI configured with ECR push permissions
#   - ECR repository created (via CloudFormation stack)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DOCKER_DIR="$SCRIPT_DIR/../docker"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/pqc-workshop-mosquitto-broker"

echo "═══════════════════════════════════════════════════════════════"
echo "  Building Mosquitto PQC Broker Image"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  ECR: $ECR_REPO"
echo "  Region: $REGION"
echo ""

# Login to ECR
echo "[1/3] Logging in to ECR..."
aws ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"

# Build
echo "[2/3] Building Docker image (this may take 10-15 minutes)..."
docker build -t "$ECR_REPO:latest" -f "$DOCKER_DIR/Dockerfile" "$DOCKER_DIR"
docker tag "$ECR_REPO:latest" "$ECR_REPO:$(date +%Y%m%d)"

# Push
echo "[3/3] Pushing to ECR..."
docker push "$ECR_REPO:latest"
docker push "$ECR_REPO:$(date +%Y%m%d)"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ Image pushed: $ECR_REPO:latest"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  To deploy, update the ECS service:"
echo "    aws ecs update-service --cluster pqc-workshop-broker \\"
echo "      --service pqc-workshop-broker --force-new-deployment"
echo ""
