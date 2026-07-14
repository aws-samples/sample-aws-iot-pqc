#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-builder-pi.sh
#
# One-time setup for the builder Raspberry Pi. Installs all dependencies
# needed for the device registration GUI and firmware flashing.
#
# Prerequisites:
#   - Raspberry Pi OS (Bookworm or later)
#   - Internet connectivity
#   - SSM Agent already installed and registered (see main project docs)
#
# Usage:
#   chmod +x setup-builder-pi.sh
#   sudo ./setup-builder-pi.sh
#
# Copyright 2024-2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

echo "═══════════════════════════════════════════════════════════════"
echo "  Builder-Pi Setup"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ─── System Dependencies ─────────────────────────────────────────────────────
echo "[1/5] Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    python3-serial \
    git \
    usbutils

# ─── Python Virtual Environment ─────────────────────────────────────────────
echo "[2/5] Creating Python environment..."
INSTALL_DIR="/opt/device-builder"
mkdir -p "$INSTALL_DIR"

python3 -m venv "$INSTALL_DIR/venv"
source "$INSTALL_DIR/venv/bin/activate"

pip install --quiet --upgrade pip
pip install --quiet \
    flask==3.0.* \
    requests==2.31.* \
    pyserial==3.5.* \
    boto3==1.34.* \
    esptool==4.7.*

# ─── arduino-cli ─────────────────────────────────────────────────────────────
echo "[3/5] Installing arduino-cli..."
if ! command -v arduino-cli &>/dev/null; then
    curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=/usr/local/bin sh
fi

# Configure arduino-cli for ESP32
arduino-cli config init --overwrite 2>/dev/null || true
arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32

# Install required libraries
arduino-cli lib install wolfSSL
arduino-cli lib install MQTT

echo "       arduino-cli ready with ESP32 support"

# ─── Copy Application Files ──────────────────────────────────────────────────
echo "[4/5] Installing builder application..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$SCRIPT_DIR/app.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/device_manager.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/config.json" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/templates" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/static" "$INSTALL_DIR/"

# ─── Systemd Service ─────────────────────────────────────────────────────────
echo "[5/5] Creating systemd service..."
cat > /etc/systemd/system/device-builder.service << EOF
[Unit]
Description=PQC Workshop Device Builder GUI
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python app.py
Restart=always
RestartSec=5
Environment=PATH=$INSTALL_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable device-builder
systemctl start device-builder

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ Builder-Pi setup complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  GUI available at: http://$(hostname -I | awk '{print $1}'):8080"
echo "  Service: systemctl status device-builder"
echo "  Logs: journalctl -u device-builder -f"
echo ""
echo "  Next steps:"
echo "    1. Update config.json with your API Gateway endpoint"
echo "    2. Connect ESP32 devices via USB"
echo "    3. Access the GUI from your browser"
echo ""
