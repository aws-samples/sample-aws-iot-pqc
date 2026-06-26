#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-mosquitto-pqc.sh
#
# Builds and installs Mosquitto with PQC (Post-Quantum Cryptography) support
# on a Raspberry Pi running 64-bit Raspberry Pi OS.
#
# This script:
#   1. Installs build dependencies
#   2. Builds OpenSSL 3.5 from source (with native ML-KEM support)
#   3. Builds Mosquitto from source, linked against PQC-capable OpenSSL
#   4. Installs Mosquitto to /opt/mosquitto-pqc
#   5. Creates a systemd service for management
#
# Requirements:
#   - Raspberry Pi 4/5 with 64-bit Raspberry Pi OS (Bookworm or later)
#   - At least 2GB free disk space
#   - Internet connectivity
#   - Run as root (sudo)
#
# Usage:
#   sudo ./setup-mosquitto-pqc.sh
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────────────────
OPENSSL_VERSION="3.5.0"
OPENSSL_URL="https://github.com/openssl/openssl/releases/download/openssl-${OPENSSL_VERSION}/openssl-${OPENSSL_VERSION}.tar.gz"
MOSQUITTO_VERSION="2.0.20"
MOSQUITTO_URL="https://mosquitto.org/files/source/mosquitto-${MOSQUITTO_VERSION}.tar.gz"

INSTALL_PREFIX="/opt/mosquitto-pqc"
OPENSSL_PREFIX="/opt/openssl-pqc"
BUILD_DIR="/tmp/pqc-build"

# ─── Colors for output ───────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ─── Preflight Checks ───────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (use sudo)"
    exit 1
fi

ARCH=$(uname -m)
if [[ "$ARCH" != "aarch64" ]]; then
    log_warn "This script is designed for 64-bit Raspberry Pi OS (aarch64)."
    log_warn "Detected architecture: $ARCH. Proceeding anyway..."
fi

log_info "Starting PQC Mosquitto build for Raspberry Pi"
log_info "OpenSSL version: $OPENSSL_VERSION"
log_info "Mosquitto version: $MOSQUITTO_VERSION"
log_info "Install prefix: $INSTALL_PREFIX"

# ─── Step 1: Install Build Dependencies ─────────────────────────────────────
log_info "Step 1/5: Installing build dependencies..."

apt-get update -qq
apt-get install -y -qq \
    build-essential \
    cmake \
    pkg-config \
    libcjson-dev \
    libsystemd-dev \
    libwebsockets-dev \
    xsltproc \
    docbook-xsl \
    perl \
    wget \
    ca-certificates

log_info "Build dependencies installed."

# ─── Step 2: Build OpenSSL 3.5 with ML-KEM Support ──────────────────────────
log_info "Step 2/5: Building OpenSSL ${OPENSSL_VERSION} with ML-KEM support..."

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

if [[ ! -f "openssl-${OPENSSL_VERSION}.tar.gz" ]]; then
    wget -q "$OPENSSL_URL" -O "openssl-${OPENSSL_VERSION}.tar.gz"
fi

rm -rf "openssl-${OPENSSL_VERSION}"
tar -xzf "openssl-${OPENSSL_VERSION}.tar.gz"
cd "openssl-${OPENSSL_VERSION}"

# Configure OpenSSL with ML-KEM enabled (it's on by default in 3.5)
./Configure \
    --prefix="$OPENSSL_PREFIX" \
    --openssldir="$OPENSSL_PREFIX/ssl" \
    linux-aarch64 \
    shared \
    no-tests \
    -DOPENSSL_TLS_SECURITY_LEVEL=0

# Build (use all available cores)
make -j"$(nproc)"
make install_sw install_ssldirs

# Create symlinks for library resolution
echo "$OPENSSL_PREFIX/lib64" > /etc/ld.so.conf.d/openssl-pqc.conf
ldconfig

log_info "OpenSSL ${OPENSSL_VERSION} installed to ${OPENSSL_PREFIX}"

# Verify ML-KEM support
if "$OPENSSL_PREFIX/bin/openssl" list -kem-algorithms 2>/dev/null | grep -qi "ML-KEM"; then
    log_info "ML-KEM algorithms confirmed available in OpenSSL"
else
    # Try alternative verification
    log_info "Verifying TLS 1.3 groups..."
    "$OPENSSL_PREFIX/bin/openssl" s_client -help 2>&1 | grep -q "groups" && \
        log_info "OpenSSL TLS groups support confirmed" || \
        log_warn "Could not verify ML-KEM support. Proceeding..."
fi

# ─── Step 3: Build Mosquitto Linked Against PQC OpenSSL ──────────────────────
log_info "Step 3/5: Building Mosquitto ${MOSQUITTO_VERSION} with PQC OpenSSL..."

cd "$BUILD_DIR"

if [[ ! -f "mosquitto-${MOSQUITTO_VERSION}.tar.gz" ]]; then
    wget -q "$MOSQUITTO_URL" -O "mosquitto-${MOSQUITTO_VERSION}.tar.gz"
fi

rm -rf "mosquitto-${MOSQUITTO_VERSION}"
tar -xzf "mosquitto-${MOSQUITTO_VERSION}.tar.gz"
cd "mosquitto-${MOSQUITTO_VERSION}"

# Build with cmake, pointing to our PQC OpenSSL
mkdir -p build && cd build

cmake .. \
    -DCMAKE_INSTALL_PREFIX="$INSTALL_PREFIX" \
    -DOPENSSL_ROOT_DIR="$OPENSSL_PREFIX" \
    -DOPENSSL_INCLUDE_DIR="$OPENSSL_PREFIX/include" \
    -DOPENSSL_CRYPTO_LIBRARY="$OPENSSL_PREFIX/lib64/libcrypto.so" \
    -DOPENSSL_SSL_LIBRARY="$OPENSSL_PREFIX/lib64/libssl.so" \
    -DWITH_TLS=ON \
    -DWITH_TLS_PSK=OFF \
    -DWITH_WEBSOCKETS=OFF \
    -DWITH_DOCS=OFF \
    -DCMAKE_BUILD_TYPE=Release

make -j"$(nproc)"
make install

log_info "Mosquitto ${MOSQUITTO_VERSION} installed to ${INSTALL_PREFIX}"

# Verify Mosquitto links against our OpenSSL
LINKED_SSL=$(ldd "$INSTALL_PREFIX/sbin/mosquitto" | grep libssl || true)
if echo "$LINKED_SSL" | grep -q "$OPENSSL_PREFIX"; then
    log_info "Mosquitto correctly linked against PQC OpenSSL: $LINKED_SSL"
else
    log_warn "Mosquitto may not be linked against PQC OpenSSL."
    log_warn "Linked libraries: $LINKED_SSL"
fi

# ─── Step 4: Create Directory Structure ─────────────────────────────────────
log_info "Step 4/5: Setting up configuration directories..."

mkdir -p "$INSTALL_PREFIX/etc/mosquitto/conf.d"
mkdir -p "$INSTALL_PREFIX/etc/mosquitto/certs"
mkdir -p "$INSTALL_PREFIX/var/log/mosquitto"
mkdir -p "$INSTALL_PREFIX/var/lib/mosquitto"

# Create mosquitto user if it doesn't exist
if ! id "mosquitto" &>/dev/null; then
    useradd -r -s /sbin/nologin -d "$INSTALL_PREFIX/var/lib/mosquitto" mosquitto
fi

chown -R mosquitto:mosquitto "$INSTALL_PREFIX/var/log/mosquitto"
chown -R mosquitto:mosquitto "$INSTALL_PREFIX/var/lib/mosquitto"

log_info "Directory structure created."

# ─── Step 5: Create Systemd Service ─────────────────────────────────────────
log_info "Step 5/5: Creating systemd service..."

cat > /etc/systemd/system/mosquitto-pqc.service << 'SERVICEEOF'
[Unit]
Description=Mosquitto MQTT Broker with PQC (Post-Quantum Cryptography)
Documentation=https://mosquitto.org/documentation/
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
NotifyAccess=main
ExecStart=/opt/mosquitto-pqc/sbin/mosquitto -c /opt/mosquitto-pqc/etc/mosquitto/mosquitto-pqc.conf
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
User=root
Group=root

# Security hardening
ProtectSystem=strict
ReadWritePaths=/opt/mosquitto-pqc/var/log/mosquitto
ReadWritePaths=/opt/mosquitto-pqc/var/lib/mosquitto
PrivateTmp=true
NoNewPrivileges=false

# Environment for PQC OpenSSL
Environment="LD_LIBRARY_PATH=/opt/openssl-pqc/lib64"

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload

log_info "Systemd service 'mosquitto-pqc' created."

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
log_info "═══════════════════════════════════════════════════════════════"
log_info "  PQC Mosquitto installation complete!"
log_info "═══════════════════════════════════════════════════════════════"
echo ""
log_info "OpenSSL: ${OPENSSL_PREFIX}/bin/openssl"
log_info "Mosquitto: ${INSTALL_PREFIX}/sbin/mosquitto"
log_info "Config dir: ${INSTALL_PREFIX}/etc/mosquitto/"
log_info "Service: mosquitto-pqc.service"
echo ""
log_info "Next steps:"
log_info "  1. Copy mosquitto-pqc.conf to ${INSTALL_PREFIX}/etc/mosquitto/"
log_info "  2. Generate certificates: cd certs && ./generate-broker-certs.sh"
log_info "  3. Start the service: sudo systemctl start mosquitto-pqc"
log_info "  4. Check status: sudo systemctl status mosquitto-pqc"
echo ""

# Cleanup build directory (optional, saves disk space)
# rm -rf "$BUILD_DIR"
