#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# entrypoint.sh — Fetches certificates from Secrets Manager on container start
#
# Environment variables (set by ECS task definition):
#   AWS_REGION          — AWS region
#   BROKER_CERT_ARN     — ACM Private CA certificate ARN
#   CA_CERT_SECRET      — Secrets Manager ARN for CA cert
#   BROKER_CERT_SECRET  — Secrets Manager ARN for broker cert
#   BROKER_KEY_SECRET   — Secrets Manager ARN for broker private key
#   IOT_ENDPOINT        — AWS IoT Core endpoint (for bridge)
#   GREENGRASS_THING    — Greengrass thing name (for bridge client ID)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

CERT_DIR="/mosquitto/certs"
REGION="${AWS_REGION:-us-east-1}"

echo "═══════════════════════════════════════════════════════════════"
echo "  Mosquitto PQC Broker — Container Startup"
echo "═══════════════════════════════════════════════════════════════"

# ─── Fetch certificates from Secrets Manager ─────────────────────────────────
echo "[1/3] Fetching certificates from Secrets Manager..."

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
    chmod 600 "$CERT_DIR/broker.key"
    echo "       Broker key fetched"
fi

# ─── Configure bridge to IoT Core ───────────────────────────────────────────
echo "[2/3] Configuring IoT Core bridge..."

if [[ -n "${IOT_ENDPOINT:-}" ]]; then
    BRIDGE_CLIENT_ID="${GREENGRASS_THING:-pqc-broker}-bridge"
    cat > /mosquitto/config/bridge.conf << BRIDGE_EOF
# IoT Core MQTT Bridge
connection iot-core-bridge
address ${IOT_ENDPOINT}:8883
bridge_protocol_version mqttv311
cleansession true
clientid ${BRIDGE_CLIENT_ID}
try_private false
start_type automatic
notifications false

# Bridge TLS (uses system OpenSSL for IoT Core connection)
bridge_tls_version tlsv1.2
bridge_cafile /etc/ssl/certs/ca-certificates.crt

# Topic bridging: forward device telemetry/lifecycle to IoT Core
topic dt/# out 1
topic cmd/# in 1
BRIDGE_EOF
    echo "       Bridge configured: ${IOT_ENDPOINT}"
else
    echo "       No IOT_ENDPOINT set, bridge disabled"
fi

# ─── Verify OpenSSL PQC support ─────────────────────────────────────────────
echo "[3/3] Verifying PQC support..."
openssl list -providers 2>/dev/null | head -5
echo ""
echo "  Broker ready. Starting Mosquitto..."
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ─── Start Mosquitto ─────────────────────────────────────────────────────────
exec "$@"
