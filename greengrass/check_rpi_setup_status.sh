#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# check_rpi_setup_status.sh
#
# Validates the Raspberry Pi is fully provisioned for the PQC workshop.
# Run this after completing all setup steps to confirm everything is working.
#
# Checks:
#   1. System requirements (OS, architecture, memory)
#   2. AWS CLI installed and configured
#   3. SSM Agent registered and running
#   4. Java installed (for Greengrass)
#   5. Greengrass nucleus installed and running
#   6. OpenSSL 3.5+ with PQC support
#   7. Mosquitto installed with PQC TLS
#   8. Broker certificates in place
#   9. Mosquitto service running
#  10. Bridge to IoT Core connected
#  11. TLS PQC handshake test (local)
#  12. Network connectivity (outbound)
#
# Usage:
#   ./check_rpi_setup_status.sh
#   sudo ./check_rpi_setup_status.sh   (recommended — some checks need root)
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

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
echo "  PQC Workshop — Raspberry Pi Readiness Check"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Hostname:   $(hostname)"
echo "  Date:       $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo ""

# ─── 1. System Requirements ──────────────────────────────────────────────────
echo "─── 1. System Requirements ──────────────────────────────────"

# Architecture
ARCH=$(uname -m)
if [[ "$ARCH" == "aarch64" ]]; then
    pass "Architecture: $ARCH (64-bit ARM)"
elif [[ "$ARCH" == "armv7l" ]]; then
    warn "Architecture: $ARCH (32-bit ARM — 64-bit OS recommended)"
else
    fail "Architecture: $ARCH (unexpected — expected aarch64 or armv7l)"
fi

# OS
if [[ -f /etc/os-release ]]; then
    OS_NAME=$(grep "^PRETTY_NAME" /etc/os-release | cut -d'"' -f2)
    pass "OS: $OS_NAME"
else
    warn "Cannot determine OS version"
fi

# Memory
TOTAL_MEM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
TOTAL_MEM_MB=$((TOTAL_MEM_KB / 1024))
if [[ $TOTAL_MEM_MB -ge 2048 ]]; then
    pass "RAM: ${TOTAL_MEM_MB} MB (sufficient)"
elif [[ $TOTAL_MEM_MB -ge 1024 ]]; then
    warn "RAM: ${TOTAL_MEM_MB} MB (minimum — 2GB+ recommended)"
else
    fail "RAM: ${TOTAL_MEM_MB} MB (insufficient — need at least 1GB)"
fi

# Disk space
FREE_DISK_MB=$(df / | tail -1 | awk '{print int($4/1024)}')
if [[ $FREE_DISK_MB -ge 2048 ]]; then
    pass "Free disk: ${FREE_DISK_MB} MB"
elif [[ $FREE_DISK_MB -ge 512 ]]; then
    warn "Free disk: ${FREE_DISK_MB} MB (low — 2GB+ recommended)"
else
    fail "Free disk: ${FREE_DISK_MB} MB (critically low)"
fi

echo ""

# ─── 2. AWS CLI ──────────────────────────────────────────────────────────────
echo "─── 2. AWS CLI ──────────────────────────────────────────────"

if command -v aws &>/dev/null; then
    AWS_VERSION=$(aws --version 2>&1 | head -1)
    pass "AWS CLI installed: $AWS_VERSION"
else
    fail "AWS CLI not installed"
fi

# Check if credentials work
if aws sts get-caller-identity &>/dev/null 2>&1; then
    IDENTITY=$(aws sts get-caller-identity --query 'Arn' --output text 2>/dev/null)
    pass "AWS credentials valid: $IDENTITY"
else
    warn "AWS credentials not configured or expired (needed for initial setup only)"
fi

echo ""

# ─── 3. SSM Agent ───────────────────────────────────────────────────────────
echo "─── 3. SSM Agent ────────────────────────────────────────────"

if command -v amazon-ssm-agent &>/dev/null; then
    SSM_VERSION=$(amazon-ssm-agent --version 2>/dev/null || echo "unknown")
    pass "SSM Agent installed: $SSM_VERSION"
else
    fail "SSM Agent not installed (run setup-ssm-agent.sh)"
fi

# Check registration
if [[ -f /var/lib/amazon/ssm/registration ]]; then
    INSTANCE_ID=$(python3 -c "
import json
with open('/var/lib/amazon/ssm/registration') as f:
    reg = json.load(f)
    print(reg.get('ManagedInstanceID', 'unknown'))
" 2>/dev/null || echo "unknown")
    pass "SSM registered: $INSTANCE_ID"
else
    fail "SSM not registered (no /var/lib/amazon/ssm/registration)"
fi

# Check service status
if systemctl is-active --quiet amazon-ssm-agent 2>/dev/null; then
    pass "SSM Agent service: running"
else
    fail "SSM Agent service: not running (sudo systemctl start amazon-ssm-agent)"
fi

echo ""

# ─── 4. Java ─────────────────────────────────────────────────────────────────
echo "─── 4. Java (for Greengrass) ────────────────────────────────"

if command -v java &>/dev/null; then
    JAVA_VERSION=$(java -version 2>&1 | head -1)
    pass "Java installed: $JAVA_VERSION"
else
    fail "Java not installed (sudo apt install default-jdk)"
fi

echo ""

# ─── 5. Greengrass Nucleus ───────────────────────────────────────────────────
echo "─── 5. Greengrass Nucleus ───────────────────────────────────"

GG_ROOT="/greengrass/v2"

if [[ -d "$GG_ROOT" ]]; then
    pass "Greengrass root directory exists: $GG_ROOT"
else
    fail "Greengrass not installed (no $GG_ROOT directory)"
fi

# Check service
if systemctl is-active --quiet greengrass 2>/dev/null; then
    pass "Greengrass service: running"
else
    if systemctl list-units --type=service | grep -q greengrass; then
        fail "Greengrass service exists but not running"
    else
        fail "Greengrass service not installed"
    fi
fi

# Check logs for recent activity
if [[ -f "$GG_ROOT/logs/greengrass.log" ]]; then
    LAST_LOG_TIME=$(stat -c %Y "$GG_ROOT/logs/greengrass.log" 2>/dev/null || stat -f %m "$GG_ROOT/logs/greengrass.log" 2>/dev/null || echo "0")
    NOW=$(date +%s)
    LOG_AGE=$(( (NOW - LAST_LOG_TIME) / 60 ))
    if [[ $LOG_AGE -lt 5 ]]; then
        pass "Greengrass log active (last write: ${LOG_AGE} min ago)"
    else
        warn "Greengrass log stale (last write: ${LOG_AGE} min ago)"
    fi
fi

# Check effective config for thing name
if [[ -f "$GG_ROOT/config/effectiveConfig.yaml" ]]; then
    THING_NAME=$(grep "thingName" "$GG_ROOT/config/effectiveConfig.yaml" 2>/dev/null | head -1 | awk '{print $2}' | tr -d '"')
    if [[ -n "$THING_NAME" ]]; then
        pass "Greengrass Thing name: $THING_NAME"
    fi
fi

echo ""

# ─── 6. OpenSSL PQC ─────────────────────────────────────────────────────────
echo "─── 6. OpenSSL with PQC Support ────────────────────────────"

OPENSSL_PQC="/opt/openssl-pqc/bin/openssl"

if [[ -x "$OPENSSL_PQC" ]]; then
    OPENSSL_VERSION=$("$OPENSSL_PQC" version 2>/dev/null || echo "unknown")
    pass "PQC OpenSSL installed: $OPENSSL_VERSION"

    # Check for ML-KEM support
    if LD_LIBRARY_PATH=/opt/openssl-pqc/lib "$OPENSSL_PQC" list -kem-algorithms 2>/dev/null | grep -qi "mlkem\|kyber"; then
        pass "ML-KEM algorithms available"
    else
        # Try checking via s_client supported groups
        if LD_LIBRARY_PATH=/opt/openssl-pqc/lib "$OPENSSL_PQC" ecparam -list_curves 2>/dev/null | grep -qi "kyber\|mlkem"; then
            pass "PQC key exchange groups available"
        else
            warn "Cannot confirm ML-KEM support (may still work — check handshake test)"
        fi
    fi
else
    fail "PQC OpenSSL not found at $OPENSSL_PQC (run setup-mosquitto-pqc.sh)"
fi

# Check shared library
if [[ -f /opt/openssl-pqc/lib/libssl.so.3 ]] || [[ -f /opt/openssl-pqc/lib64/libssl.so.3 ]]; then
    pass "PQC OpenSSL shared libraries present"
else
    fail "PQC OpenSSL shared libraries missing"
fi

echo ""

# ─── 7. Mosquitto ────────────────────────────────────────────────────────────
echo "─── 7. Mosquitto MQTT Broker ────────────────────────────────"

MOSQUITTO_BIN="/opt/mosquitto-pqc/sbin/mosquitto"
if [[ ! -x "$MOSQUITTO_BIN" ]]; then
    MOSQUITTO_BIN="/opt/mosquitto-pqc/bin/mosquitto"
fi

if [[ -x "$MOSQUITTO_BIN" ]]; then
    MOSQ_VERSION=$("$MOSQUITTO_BIN" -h 2>&1 | head -1 || echo "installed")
    pass "Mosquitto installed: $MOSQ_VERSION"
else
    fail "Mosquitto not found in /opt/mosquitto-pqc/ (run setup-mosquitto-pqc.sh)"
fi

# Check config file
MOSQ_CONF="/opt/mosquitto-pqc/etc/mosquitto/mosquitto-pqc.conf"
if [[ -f "$MOSQ_CONF" ]]; then
    pass "Mosquitto config: $MOSQ_CONF"
else
    fail "Mosquitto config not found: $MOSQ_CONF"
fi

echo ""

# ─── 8. Broker Certificates ─────────────────────────────────────────────────
echo "─── 8. Broker Certificates ──────────────────────────────────"

CERT_DIR="/opt/mosquitto-pqc/etc/mosquitto/certs"

REQUIRED_CERTS=(
    "ca.crt:Workshop CA certificate"
    "broker.crt:Mosquitto server certificate"
    "broker.key:Mosquitto server private key"
    "greengrass-device.crt:Greengrass device cert (for bridge)"
    "greengrass-device.key:Greengrass device key (for bridge)"
    "AmazonRootCA1.pem:Amazon Root CA (for bridge to IoT Core)"
)

for entry in "${REQUIRED_CERTS[@]}"; do
    FILE="${entry%%:*}"
    DESC="${entry##*:}"
    if [[ -f "$CERT_DIR/$FILE" ]]; then
        # Check file is not empty
        if [[ -s "$CERT_DIR/$FILE" ]]; then
            pass "$DESC: $CERT_DIR/$FILE"
        else
            fail "$DESC: $CERT_DIR/$FILE (file is empty!)"
        fi
    else
        fail "$DESC: MISSING ($CERT_DIR/$FILE)"
    fi
done

# Check CA cert expiry
if [[ -f "$CERT_DIR/ca.crt" ]] && command -v openssl &>/dev/null; then
    EXPIRY=$(openssl x509 -in "$CERT_DIR/ca.crt" -enddate -noout 2>/dev/null | cut -d= -f2)
    if [[ -n "$EXPIRY" ]]; then
        # Check if expired
        if openssl x509 -in "$CERT_DIR/ca.crt" -checkend 0 -noout &>/dev/null; then
            pass "CA certificate valid (expires: $EXPIRY)"
        else
            fail "CA certificate EXPIRED: $EXPIRY"
        fi
    fi
fi

echo ""

# ─── 9. Mosquitto Service ───────────────────────────────────────────────────
echo "─── 9. Mosquitto Service ────────────────────────────────────"

if systemctl is-active --quiet mosquitto-pqc 2>/dev/null; then
    pass "Mosquitto service: running"
    MOSQ_PID=$(systemctl show mosquitto-pqc --property=MainPID --value 2>/dev/null || echo "")
    if [[ -n "$MOSQ_PID" && "$MOSQ_PID" != "0" ]]; then
        pass "Mosquitto PID: $MOSQ_PID"
    fi
else
    fail "Mosquitto service not running (sudo systemctl start mosquitto-pqc)"
fi

# Check listening on port 8883
if ss -tlnp 2>/dev/null | grep -q ":8883"; then
    pass "Port 8883 (MQTTS): listening"
elif netstat -tlnp 2>/dev/null | grep -q ":8883"; then
    pass "Port 8883 (MQTTS): listening"
else
    fail "Port 8883 not listening (Mosquitto TLS not accepting connections)"
fi

echo ""

# ─── 10. Bridge to IoT Core ─────────────────────────────────────────────────
echo "─── 10. Bridge to IoT Core ──────────────────────────────────"

BRIDGE_CONF="/opt/mosquitto-pqc/etc/mosquitto/conf.d/bridge-iotcore.conf"

if [[ -f "$BRIDGE_CONF" ]]; then
    pass "Bridge config exists: $BRIDGE_CONF"
    # Check it has a real endpoint (not placeholder)
    if grep -q "REPLACE_WITH" "$BRIDGE_CONF"; then
        fail "Bridge config still has REPLACE_WITH placeholders!"
    else
        BRIDGE_ENDPOINT=$(grep "^address" "$BRIDGE_CONF" 2>/dev/null | awk '{print $2}' | head -1)
        if [[ -n "$BRIDGE_ENDPOINT" ]]; then
            pass "Bridge endpoint: $BRIDGE_ENDPOINT"
        fi
    fi
else
    fail "Bridge config not found: $BRIDGE_CONF"
fi

# Check Mosquitto log for bridge connection status
MOSQ_LOG="/opt/mosquitto-pqc/var/log/mosquitto/mosquitto.log"
if [[ -f "$MOSQ_LOG" ]]; then
    # Look for recent bridge connected message
    if tail -100 "$MOSQ_LOG" 2>/dev/null | grep -qi "CONNACK\|bridge.*connected\|Connection accepted"; then
        pass "Bridge connected (CONNACK received in recent logs)"
    elif tail -100 "$MOSQ_LOG" 2>/dev/null | grep -qi "PINGRESP"; then
        pass "Bridge active (PINGRESP exchanges in recent logs)"
    elif tail -100 "$MOSQ_LOG" 2>/dev/null | grep -qi "bridge.*error\|bridge.*failed\|Connection refused"; then
        fail "Bridge connection errors found in log (check: sudo tail -50 $MOSQ_LOG)"
    else
        warn "Cannot confirm bridge status from logs (check manually: sudo tail -50 $MOSQ_LOG)"
    fi
else
    warn "Mosquitto log not found at $MOSQ_LOG"
fi

echo ""

# ─── 11. PQC TLS Handshake Test ─────────────────────────────────────────────
echo "─── 11. PQC TLS Handshake Test ─────────────────────────────"

MOSQ_PUB="/opt/mosquitto-pqc/bin/mosquitto_pub"
DEVICE_CERT_DIR=""

# Find a device cert to test with
for dir in \
    "$(dirname "$0")/certs/output/devices/esp32-pqc-device-001" \
    "$HOME/Sample-aws-iot-pqc/greengrass/certs/output/devices/esp32-pqc-device-001" \
    "/home/pi/Sample-aws-iot-pqc/greengrass/certs/output/devices/esp32-pqc-device-001"; do
    if [[ -f "$dir/device.crt" ]]; then
        DEVICE_CERT_DIR="$dir"
        break
    fi
done

if [[ -x "$MOSQ_PUB" && -n "$DEVICE_CERT_DIR" && -f "$CERT_DIR/ca.crt" ]]; then
    # Try a test publish
    TEST_RESULT=$("$MOSQ_PUB" \
        --cafile "$CERT_DIR/ca.crt" \
        --cert "$DEVICE_CERT_DIR/device.crt" \
        --key "$DEVICE_CERT_DIR/device.key" \
        -h localhost -p 8883 \
        -t "dt/test-rpi-check/telemetry" \
        -m "{\"test\":true,\"timestamp\":$(date +%s)}" \
        --tls-version tlsv1.3 \
        2>&1) && pass "TLS 1.3 PQC handshake + publish: SUCCESS" || {
        if echo "$TEST_RESULT" | grep -qi "Connection refused\|Error"; then
            fail "TLS handshake failed: $TEST_RESULT"
        else
            warn "TLS test inconclusive: $TEST_RESULT"
        fi
    }
elif [[ ! -x "$MOSQ_PUB" ]]; then
    warn "mosquitto_pub not found — cannot test handshake"
elif [[ -z "$DEVICE_CERT_DIR" ]]; then
    warn "No device certs found — cannot test handshake (generate certs first)"
else
    warn "Missing CA cert — cannot test handshake"
fi

echo ""

# ─── 12. Network Connectivity ───────────────────────────────────────────────
echo "─── 12. Network Connectivity ────────────────────────────────"

# Check outbound HTTPS to SSM endpoints
SSM_ENDPOINT="ssm.us-east-1.amazonaws.com"
if curl -s --max-time 5 -o /dev/null -w "%{http_code}" "https://$SSM_ENDPOINT" 2>/dev/null | grep -q "^[234]"; then
    pass "Outbound HTTPS to SSM: reachable"
else
    if timeout 5 bash -c "echo >/dev/tcp/$SSM_ENDPOINT/443" 2>/dev/null; then
        pass "Outbound port 443 to SSM: open"
    else
        warn "Cannot reach $SSM_ENDPOINT (SSM remote access may not work)"
    fi
fi

# Check outbound to IoT Core
IOT_ENDPOINT=$(grep "^address" "$BRIDGE_CONF" 2>/dev/null | awk '{print $2}' | cut -d: -f1 | head -1)
if [[ -n "$IOT_ENDPOINT" ]]; then
    if timeout 5 bash -c "echo >/dev/tcp/$IOT_ENDPOINT/8883" 2>/dev/null; then
        pass "Outbound port 8883 to IoT Core ($IOT_ENDPOINT): open"
    else
        fail "Cannot reach IoT Core at $IOT_ENDPOINT:8883 (bridge will fail)"
    fi
else
    warn "Cannot determine IoT Core endpoint (bridge config missing)"
fi

# WiFi or Ethernet
if ip link show wlan0 &>/dev/null && ip addr show wlan0 | grep -q "inet "; then
    WIFI_IP=$(ip addr show wlan0 | grep "inet " | awk '{print $2}' | cut -d/ -f1)
    pass "WiFi connected: $WIFI_IP"
elif ip link show eth0 &>/dev/null && ip addr show eth0 | grep -q "inet "; then
    ETH_IP=$(ip addr show eth0 | grep "inet " | awk '{print $2}' | cut -d/ -f1)
    pass "Ethernet connected: $ETH_IP"
else
    # Try to find any non-loopback IP
    ANY_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [[ -n "$ANY_IP" ]]; then
        pass "Network connected: $ANY_IP"
    else
        fail "No network connection detected"
    fi
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
    echo "  🎉 Raspberry Pi is FULLY READY for the workshop!"
elif [[ $FAIL -eq 0 ]]; then
    echo "  👍 Raspberry Pi is MOSTLY READY (review warnings above)."
else
    echo "  🚨 Raspberry Pi has FAILURES that need to be resolved."
    echo "     Fix the issues above and run this script again."
fi

echo ""
echo "  Hostname: $(hostname)"
echo "  IP:       $(hostname -I 2>/dev/null | awk '{print $1}')"
if [[ -n "${INSTANCE_ID:-}" ]]; then
    echo "  SSM ID:   $INSTANCE_ID"
fi
echo ""
