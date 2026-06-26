#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh
#
# Deploys the PQC Mosquitto component to the local Greengrass core device.
# This uses the Greengrass CLI to create a local deployment.
#
# Usage:
#   ./deploy.sh <iot-core-endpoint> <greengrass-thing-name>
#
# Example:
#   ./deploy.sh a1b2c3-ats.iot.us-east-1.amazonaws.com my-greengrass-core
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

IOT_ENDPOINT="${1:-}"
GG_THING_NAME="${2:-}"

if [[ -z "$IOT_ENDPOINT" || -z "$GG_THING_NAME" ]]; then
    echo "Usage: $0 <iot-core-endpoint> <greengrass-thing-name>"
    echo ""
    echo "  iot-core-endpoint:    Your AWS IoT Core ATS endpoint"
    echo "  greengrass-thing-name: The Greengrass core device Thing name"
    echo ""
    echo "Example:"
    echo "  $0 a1b2c3-ats.iot.us-east-1.amazonaws.com my-greengrass-core"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RECIPE_FILE="$SCRIPT_DIR/recipe.yaml"
GG_CLI="/greengrass/v2/bin/greengrass-cli"

# Verify Greengrass CLI exists
if [[ ! -x "$GG_CLI" ]]; then
    echo "[ERROR] Greengrass CLI not found at $GG_CLI"
    echo "        Is Greengrass v2 installed?"
    exit 1
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  Deploying PQC Mosquitto Component"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "IoT Core Endpoint: $IOT_ENDPOINT"
echo "Greengrass Thing:  $GG_THING_NAME"
echo "Recipe File:       $RECIPE_FILE"
echo ""

# Create local deployment
sudo "$GG_CLI" deployment create \
    --recipeDir "$SCRIPT_DIR" \
    --artifactDir "$SCRIPT_DIR/artifacts" \
    --merge "com.workshop.pqc-mosquitto=1.0.0" \
    --update-config "{\"com.workshop.pqc-mosquitto\":{\"MERGE\":{\"iotCoreEndpoint\":\"$IOT_ENDPOINT\",\"greengrassThingName\":\"$GG_THING_NAME\"}}}"

echo ""
echo "Deployment submitted. Check status with:"
echo "  sudo $GG_CLI component list"
echo "  sudo $GG_CLI component details --name com.workshop.pqc-mosquitto"
echo ""
echo "View logs:"
echo "  sudo tail -f /greengrass/v2/logs/com.workshop.pqc-mosquitto.log"
echo ""
