#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# entrypoint.sh — Fetches certificates from Secrets Manager on container start
#
# Environment variables (set by ECS task definition):
#   AWS_REGION            — AWS region
#   CA_CERT_SECRET        — Secrets Manager ARN for device-facing CA cert
#   BROKER_CERT_SECRET    — Secrets Manager ARN for broker cert
#   BROKER_KEY_SECRET     — Secrets Manager ARN for broker private key
#   IOT_ENDPOINT          — AWS IoT Core endpoint (for bridge)
#   GREENGRASS_THING      — Thing name prefix (for bridge client ID)
#   BRIDGE_CERT_SECRET    — Secrets Manager ARN for IoT Core bridge cert
#   BRIDGE_KEY_SECRET     — Secrets Manager ARN for IoT Core bridge key
#   BRIDGE_ROOT_CA_SECRET — Secrets Manager ARN for Amazon Root CA
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

CERT_DIR="/mosquitto/certs"
BRIDGE_CERT_DIR="/mosquitto/certs/bridge"
REGION="${AWS_REGION:-us-east-1}"

mkdir -p "$BRIDGE_CERT_DIR"

# ─── Identify this broker instance in the logs ──────────────────────────────
# Make the CloudWatch log content self-identifying so it is unambiguous WHICH
# broker produced these logs. During the workshop debugging, a second (dead)
# Raspberry-Pi broker logged elsewhere and reading the wrong group cost hours.
# ECS injects task metadata at ECS_CONTAINER_METADATA_URI_V4.
TASK_ARN="unknown"
CLUSTER="unknown"
if [[ -n "${ECS_CONTAINER_METADATA_URI_V4:-}" ]]; then
    META="$(curl -fsS "${ECS_CONTAINER_METADATA_URI_V4}/task" 2>/dev/null || true)"
    if [[ -n "$META" ]]; then
        TASK_ARN="$(echo "$META" | python3 -c "import sys,json;print(json.load(sys.stdin).get('TaskARN','unknown'))" 2>/dev/null || echo unknown)"
        CLUSTER="$(echo "$META" | python3 -c "import sys,json;print(json.load(sys.stdin).get('Cluster','unknown'))" 2>/dev/null || echo unknown)"
    fi
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  Mosquitto PQC Broker — Container Startup"
echo "  broker=pqc-workshop-broker-container (ECS Fargate)"
echo "  cluster=${CLUSTER}"
echo "  task=${TASK_ARN}"
echo "  region=${REGION}  log_group=/pqc-workshop/broker-container"
echo "═══════════════════════════════════════════════════════════════"

# ─── Fetch device-facing certificates from Secrets Manager ───────────────────
echo "[1/4] Fetching broker certificates from Secrets Manager..."

if [[ -n "${CA_CERT_SECRET:-}" ]]; then
    aws secretsmanager get-secret-value \
        --secret-id "$CA_CERT_SECRET" \
        --query 'SecretString' --output text \
        --region "$REGION" > "$CERT_DIR/ca.crt"
    echo "       CA cert fetched"
fi

if [[ -n "${BROKER_CERT_SECRET:-}" ]]; then
    aws secretsmanager get-secret-value \
        --secret-id "$BROKER_CERT_SECRET" \
        --query 'SecretString' --output text \
        --region "$REGION" > "$CERT_DIR/broker.crt"
    echo "       Broker cert fetched"
fi

if [[ -n "${BROKER_KEY_SECRET:-}" ]]; then
    aws secretsmanager get-secret-value \
        --secret-id "$BROKER_KEY_SECRET" \
        --query 'SecretString' --output text \
        --region "$REGION" > "$CERT_DIR/broker.key"
    echo "       Broker key fetched"
fi

# ─── Fetch IoT Core bridge credentials ──────────────────────────────────────
echo "[2/4] Fetching IoT Core bridge credentials..."

BRIDGE_READY=true

if [[ -n "${BRIDGE_CERT_SECRET:-}" ]]; then
    aws secretsmanager get-secret-value \
        --secret-id "$BRIDGE_CERT_SECRET" \
        --query 'SecretString' --output text \
        --region "$REGION" > "$BRIDGE_CERT_DIR/bridge.crt"
    echo "       Bridge cert fetched"
else
    echo "       BRIDGE_CERT_SECRET not set"
    BRIDGE_READY=false
fi

if [[ -n "${BRIDGE_KEY_SECRET:-}" ]]; then
    aws secretsmanager get-secret-value \
        --secret-id "$BRIDGE_KEY_SECRET" \
        --query 'SecretString' --output text \
        --region "$REGION" > "$BRIDGE_CERT_DIR/bridge.key"
    echo "       Bridge key fetched"
else
    echo "       BRIDGE_KEY_SECRET not set"
    BRIDGE_READY=false
fi

if [[ -n "${BRIDGE_ROOT_CA_SECRET:-}" ]]; then
    aws secretsmanager get-secret-value \
        --secret-id "$BRIDGE_ROOT_CA_SECRET" \
        --query 'SecretString' --output text \
        --region "$REGION" > "$BRIDGE_CERT_DIR/AmazonRootCA1.pem"
    echo "       Amazon Root CA fetched"
else
    echo "       BRIDGE_ROOT_CA_SECRET not set"
    BRIDGE_READY=false
fi

# ─── Configure bridge to IoT Core ───────────────────────────────────────────
echo "[3/4] Configuring IoT Core bridge..."

if [[ -n "${IOT_ENDPOINT:-}" && "$BRIDGE_READY" == "true" ]]; then
    BRIDGE_CLIENT_ID="${GREENGRASS_THING:-pqc-workshop-broker}-bridge"
    BRIDGE_UNIQUE_ID="${BRIDGE_CLIENT_ID}-$(date +%s)"
    cat > /mosquitto/config/conf.d/bridge.conf << BRIDGE_EOF
# IoT Core MQTT Bridge — mTLS with IoT Core certificate
connection iot-core-bridge
address ${IOT_ENDPOINT}:8883
bridge_protocol_version mqttv311
cleansession true
clientid ${BRIDGE_UNIQUE_ID}
notifications false
keepalive_interval 30
restart_timeout 30
bridge_attempt_unsubscribe false

# Bridge mTLS credentials (IoT Core issued certificate)
bridge_tls_version tlsv1.2
bridge_cafile ${BRIDGE_CERT_DIR}/AmazonRootCA1.pem
bridge_certfile ${BRIDGE_CERT_DIR}/bridge.crt
bridge_keyfile ${BRIDGE_CERT_DIR}/bridge.key

# Topic bridging: only forward telemetry (non-retained) to IoT Core.
# Lifecycle messages use MQTT retain which IoT Core rejects, so we exclude them.
topic dt/+/telemetry out 0
topic cmd/# in 0
BRIDGE_EOF
    echo "       Bridge configured: ${IOT_ENDPOINT} (mTLS)"
    echo "       Client ID: ${BRIDGE_CLIENT_ID}"
elif [[ -n "${IOT_ENDPOINT:-}" ]]; then
    echo "       WARNING: Bridge certs not available, bridge disabled"
else
    echo "       No IOT_ENDPOINT set, bridge disabled"
fi

# ─── Verify OpenSSL PQC support ─────────────────────────────────────────────
echo "[4/4] Verifying PQC support..."
openssl list -providers 2>/dev/null | head -5
echo ""
echo "  Broker ready. Starting Mosquitto..."
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ─── Start Mosquitto ─────────────────────────────────────────────────────────
exec "$@"
