#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# generate-device-cert.sh
#
# Generates a PQC-signed certificate for a specific device using the
# broker-pi's local CA. Called remotely via SSM RunCommand from the
# device-registrar Lambda.
#
# The generated cert is signed by the same CA that Mosquitto trusts,
# so the device will be able to connect with mutual TLS.
#
# Usage:
#   ./generate-device-cert.sh <serial-number>
#
# Output (JSON to stdout for SSM capture):
#   {"ca_cert": "...", "device_cert": "...", "device_key": "..."}
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SERIAL="${1:-}"
if [[ -z "$SERIAL" ]]; then
    echo "Usage: $0 <serial-number>" >&2
    exit 1
fi

OPENSSL="/opt/openssl-pqc/bin/openssl"
CERT_DIR="/opt/mosquitto-pqc/etc/mosquitto/certs"
DEVICE_DIR="/opt/mosquitto-pqc/etc/mosquitto/certs/devices"
CA_CERT="$CERT_DIR/ca.crt"
CA_KEY="$CERT_DIR/ca.key"

THING_NAME="esp32-${SERIAL}"
DEVICE_CERT_DAYS=365

# Validate CA exists
if [[ ! -f "$CA_CERT" || ! -f "$CA_KEY" ]]; then
    echo '{"error": "CA certificate or key not found on broker-pi"}' 
    exit 1
fi

# Create device cert directory
mkdir -p "$DEVICE_DIR"

# Generate device private key (EC P-256 for compatibility)
$OPENSSL ecparam -genkey -name prime256v1 -noout \
    -out "$DEVICE_DIR/${THING_NAME}.key" 2>/dev/null

# Generate CSR
$OPENSSL req -new \
    -key "$DEVICE_DIR/${THING_NAME}.key" \
    -out "$DEVICE_DIR/${THING_NAME}.csr" \
    -subj "/CN=${THING_NAME}/O=AWS PQC Workshop/C=US" 2>/dev/null

# Sign with the PQC CA
# The CA uses whatever algorithm it was generated with (likely ML-DSA or
# dilithium via oqs-provider). The device cert key is EC P-256 but the
# CA SIGNATURE on it will be PQC — this is the hybrid approach.
$OPENSSL x509 -req \
    -in "$DEVICE_DIR/${THING_NAME}.csr" \
    -CA "$CA_CERT" \
    -CAkey "$CA_KEY" \
    -CAcreateserial \
    -out "$DEVICE_DIR/${THING_NAME}.crt" \
    -days $DEVICE_CERT_DAYS 2>/dev/null

# Clean up CSR
rm -f "$DEVICE_DIR/${THING_NAME}.csr"

# Output as JSON (for SSM command output capture)
CA_PEM=$(cat "$CA_CERT")
DEVICE_CRT=$(cat "$DEVICE_DIR/${THING_NAME}.crt")
DEVICE_KEY=$(cat "$DEVICE_DIR/${THING_NAME}.key")

# Use python3 to safely JSON-encode the PEM strings
python3 -c "
import json, sys
data = {
    'ca_cert': '''${CA_PEM}''',
    'device_cert': open('$DEVICE_DIR/${THING_NAME}.crt').read(),
    'device_key': open('$DEVICE_DIR/${THING_NAME}.key').read(),
    'thing_name': '${THING_NAME}',
    'serial': '${SERIAL}'
}
print(json.dumps(data))
"
