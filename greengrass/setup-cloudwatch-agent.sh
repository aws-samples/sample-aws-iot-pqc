#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-cloudwatch-agent.sh
#
# Installs and configures the CloudWatch Agent on the Raspberry Pi to push
# Mosquitto broker logs to CloudWatch Logs. This enables attendees to query
# broker activity (including PQC handshake details) from the AWS Console.
#
# The agent uses the SSM managed instance role credentials (no additional
# IAM user needed).
#
# Prerequisites:
#   - SSM Agent registered and running (provides IAM credentials)
#   - Mosquitto running with logs at /opt/mosquitto-pqc/var/log/mosquitto/
#
# Usage:
#   sudo ./setup-cloudwatch-agent.sh [aws-region]
#
# After setup, view logs in CloudWatch:
#   Log Group: /pqc-workshop/mosquitto-broker
#
# Query PQC handshakes in CloudWatch Logs Insights:
#   fields @timestamp, @message
#   | filter @message like /CONNACK|SecP256r1|MLKEM|handshake|New connection/
#   | sort @timestamp desc
#   | limit 50
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REGION="${1:-us-east-1}"

if [[ $EUID -ne 0 ]]; then
    echo "[ERROR] Run as root: sudo $0 $*"
    exit 1
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  CloudWatch Agent Setup — Mosquitto Broker Logs"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Region:    $REGION"
echo "  Log Group: /pqc-workshop/mosquitto-broker"
echo ""

# ─── Step 1: Install CloudWatch Agent ────────────────────────────────────────
echo "[1/3] Installing CloudWatch Agent..."

ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
case "$ARCH" in
    arm64|aarch64) CW_ARCH="arm64" ;;
    armhf|armv7l) CW_ARCH="arm" ;;
    *) echo "[ERROR] Unsupported arch: $ARCH"; exit 1 ;;
esac

CW_DEB="/tmp/amazon-cloudwatch-agent.deb"
CW_URL="https://amazoncloudwatch-agent-${REGION}.s3.${REGION}.amazonaws.com/debian/${CW_ARCH}/latest/amazon-cloudwatch-agent.deb"

if command -v amazon-cloudwatch-agent-ctl &>/dev/null; then
    echo "       CloudWatch Agent already installed"
else
    echo "       Downloading from: $CW_URL"
    curl -sL -o "$CW_DEB" "$CW_URL"
    dpkg -i "$CW_DEB" >/dev/null 2>&1 || {
        apt-get install -f -y -qq >/dev/null 2>&1
        dpkg -i "$CW_DEB" >/dev/null 2>&1
    }
    rm -f "$CW_DEB"
    echo "       ✅ CloudWatch Agent installed"
fi

# ─── Step 2: Configure the agent ────────────────────────────────────────────
echo "[2/3] Writing configuration..."

CONFIG_DIR="/opt/aws/amazon-cloudwatch-agent/etc"
mkdir -p "$CONFIG_DIR"

cat > "$CONFIG_DIR/amazon-cloudwatch-agent.json" << EOF
{
  "agent": {
    "region": "$REGION",
    "logfile": "/opt/aws/amazon-cloudwatch-agent/logs/amazon-cloudwatch-agent.log",
    "debug": false
  },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/opt/mosquitto-pqc/var/log/mosquitto/mosquitto.log",
            "log_group_name": "/pqc-workshop/mosquitto-broker",
            "log_stream_name": "{hostname}",
            "timestamp_format": "%Y-%m-%dT%H:%M:%S",
            "timezone": "UTC",
            "retention_in_days": 7
          },
          {
            "file_path": "/greengrass/v2/logs/greengrass.log",
            "log_group_name": "/pqc-workshop/greengrass",
            "log_stream_name": "{hostname}",
            "timestamp_format": "%Y-%m-%dT%H:%M:%S",
            "timezone": "UTC",
            "retention_in_days": 7
          }
        ]
      }
    },
    "force_flush_interval": 5
  }
}
EOF

echo "       ✅ Configuration written to $CONFIG_DIR/amazon-cloudwatch-agent.json"

# ─── Step 3: Start the agent ────────────────────────────────────────────────
echo "[3/3] Starting CloudWatch Agent..."

amazon-cloudwatch-agent-ctl \
    -a fetch-config \
    -m onPremise \
    -s \
    -c file:"$CONFIG_DIR/amazon-cloudwatch-agent.json"

# Verify it's running
sleep 3
if amazon-cloudwatch-agent-ctl -a status | grep -q "running"; then
    echo "       ✅ CloudWatch Agent running"
else
    echo "       ⚠️  Agent may not be running — check:"
    echo "       amazon-cloudwatch-agent-ctl -a status"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ CloudWatch Agent configured!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Log Groups:"
echo "    /pqc-workshop/mosquitto-broker  — Device connections, bridge activity"
echo "    /pqc-workshop/greengrass        — Greengrass nucleus, IoT Core connectivity"
echo ""
echo "  Log Stream: $(hostname)"
echo "  Region:     $REGION"
echo ""
echo "  View logs in the console:"
echo "    CloudWatch > Logs > Log Groups > /pqc-workshop/mosquitto-broker"
echo ""
echo "  Or via CLI:"
echo "    aws logs tail /pqc-workshop/mosquitto-broker --follow --region $REGION"
echo "    aws logs tail /pqc-workshop/greengrass --follow --region $REGION"
echo ""
echo "  Query device connections in CloudWatch Logs Insights:"
echo "    fields @timestamp, @message"
echo "    | filter @message like /New connection|New client|CONNACK|PUBLISH/"
echo "    | sort @timestamp desc"
echo "    | limit 50"
echo ""
echo "  Query PQC telemetry (from IoT Rules → Lambda → DynamoDB):"
echo "    Check the dashboard or IoT Core MQTT test client for"
echo "    'pqc_algorithm' field in device telemetry messages."
echo ""
