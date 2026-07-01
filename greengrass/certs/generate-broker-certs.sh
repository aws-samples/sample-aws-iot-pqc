#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# generate-broker-certs.sh
#
# Generates a local Certificate Authority, broker server certificate,
# and a shared fleet device certificate for the PQC workshop.
#
# This creates:
#   - ca.key / ca.crt             — Local CA (signs broker + fleet cert)
#   - broker.key / broker.crt     — Broker server certificate
#   - fleet-device.key / fleet-device.crt — Shared cert for all ESP32 devices
#
# All ESP32 devices use the same fleet certificate. They are uniquely
# identified by their MQTT client ID (derived from MAC address at runtime).
#
# Usage:
#   ./generate-broker-certs.sh
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# Use the PQC-capable OpenSSL
OPENSSL="/opt/openssl-pqc/bin/openssl"
CERT_DIR="$(cd "$(dirname "$0")" && pwd)/output"

# Certificate validity (days)
CA_DAYS=3650
BROKER_DAYS=365
FLEET_DAYS=365

# Broker settings
BROKER_CN="pqc-mqtt-broker"
CA_CN="PQC Workshop CA"
FLEET_CN="esp32-pqc-fleet"

echo "═══════════════════════════════════════════════════════════════"
echo "  PQC Workshop Certificate Generator"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "OpenSSL: $OPENSSL"
echo "Output dir: $CERT_DIR"
echo ""

# Check OpenSSL exists
if [[ ! -x "$OPENSSL" ]]; then
    echo "[ERROR] PQC OpenSSL not found at $OPENSSL"
    echo "        Run setup-mosquitto-pqc.sh first."
    exit 1
fi

# Create output directory
mkdir -p "$CERT_DIR"

# ─── Step 1: Generate CA ────────────────────────────────────────────────────
echo "[1/3] Generating Certificate Authority..."

$OPENSSL req -new -x509 \
    -days $CA_DAYS \
    -keyout "$CERT_DIR/ca.key" \
    -out "$CERT_DIR/ca.crt" \
    -nodes \
    -subj "/CN=${CA_CN}/O=AWS PQC Workshop/C=US"

echo "      CA certificate: $CERT_DIR/ca.crt"
echo "      CA private key: $CERT_DIR/ca.key"

# ─── Step 2: Generate Broker Certificate ────────────────────────────────────
echo "[2/3] Generating broker certificate..."

# Generate broker key
$OPENSSL genrsa -out "$CERT_DIR/broker.key" 2048

# Generate broker CSR with Subject Alternative Names
cat > "$CERT_DIR/broker.cnf" << EOF
[req]
default_bits = 2048
prompt = no
distinguished_name = dn
req_extensions = v3_req

[dn]
CN = ${BROKER_CN}
O = AWS PQC Workshop
C = US

[v3_req]
subjectAltName = @alt_names

[alt_names]
# Add the Pi's IP address(es) and hostname
DNS.1 = ${BROKER_CN}
DNS.2 = localhost
IP.1 = 127.0.0.1
# IMPORTANT: Update IP.2 with your Raspberry Pi's actual IP address
# IP.2 = 192.168.1.100
EOF

$OPENSSL req -new \
    -key "$CERT_DIR/broker.key" \
    -out "$CERT_DIR/broker.csr" \
    -config "$CERT_DIR/broker.cnf"

# Sign with CA
$OPENSSL x509 -req \
    -in "$CERT_DIR/broker.csr" \
    -CA "$CERT_DIR/ca.crt" \
    -CAkey "$CERT_DIR/ca.key" \
    -CAcreateserial \
    -out "$CERT_DIR/broker.crt" \
    -days $BROKER_DAYS \
    -extensions v3_req \
    -extfile "$CERT_DIR/broker.cnf"

echo "      Broker cert: $CERT_DIR/broker.crt"
echo "      Broker key:  $CERT_DIR/broker.key"

# ─── Step 3: Generate Fleet Device Certificate ──────────────────────────────
echo "[3/3] Generating shared fleet device certificate..."

# Generate fleet device key
$OPENSSL genrsa -out "$CERT_DIR/fleet-device.key" 2048

# Generate fleet device CSR
$OPENSSL req -new \
    -key "$CERT_DIR/fleet-device.key" \
    -out "$CERT_DIR/fleet-device.csr" \
    -subj "/CN=${FLEET_CN}/O=AWS PQC Workshop/C=US"

# Sign with CA
$OPENSSL x509 -req \
    -in "$CERT_DIR/fleet-device.csr" \
    -CA "$CERT_DIR/ca.crt" \
    -CAkey "$CERT_DIR/ca.key" \
    -CAcreateserial \
    -out "$CERT_DIR/fleet-device.crt" \
    -days $FLEET_DAYS

echo "      Fleet cert:  $CERT_DIR/fleet-device.crt"
echo "      Fleet key:   $CERT_DIR/fleet-device.key"

# Clean up CSRs and temp files
rm -f "$CERT_DIR/broker.csr" "$CERT_DIR/broker.cnf" "$CERT_DIR/ca.srl" "$CERT_DIR/fleet-device.csr"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Certificate generation complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "Broker certificates (copy to /opt/mosquitto-pqc/etc/mosquitto/certs/):"
echo "  $CERT_DIR/ca.crt"
echo "  $CERT_DIR/broker.crt"
echo "  $CERT_DIR/broker.key"
echo ""
echo "Fleet device certificate (flash to ALL ESP32 devices via secrets.h):"
echo "  $CERT_DIR/ca.crt           → AWS_CERT_CA in secrets.h"
echo "  $CERT_DIR/fleet-device.crt → AWS_CERT_CRT in secrets.h"
echo "  $CERT_DIR/fleet-device.key → AWS_CERT_PRIVATE in secrets.h"
echo ""
echo "All 100 devices share the same fleet certificate."
echo "Each device is uniquely identified by its MQTT client ID"
echo "(derived from its MAC address: esp32-XXYYZZ)."
echo ""
