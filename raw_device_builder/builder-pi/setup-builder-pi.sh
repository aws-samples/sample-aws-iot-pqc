#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-builder-pi.sh — Provision the builder-pi with all required tools
#
# Designed to be executed via SSM Run Command on the builder Raspberry Pi.
# Installs arduino-cli, ESP32 platform, wolfSSL config, Python dependencies,
# and the builder-pi web GUI application.
#
# Usage (via SSM from your workstation):
#   aws ssm send-command \
#     --instance-ids mi-0f2c31334f3e87c8b \
#     --document-name AWS-RunShellScript \
#     --parameters 'commands=["curl -fsSL https://raw.githubusercontent.com/.../setup-builder-pi.sh | bash"]' \
#     --timeout-seconds 600
#
# Or copy this script to the Pi and run directly:
#   chmod +x setup-builder-pi.sh && sudo ./setup-builder-pi.sh
#
# Idempotent — safe to run multiple times.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

echo "═══════════════════════════════════════════════════════════════"
echo "  Builder-Pi Setup — PQC Workshop Device Builder"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Must run as root (SSM runs as root by default)
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (or via SSM)"
    exit 1
fi

INSTALL_DIR="/opt/pqc-workshop"
APP_DIR="$INSTALL_DIR/builder-app"
FIRMWARE_DIR="$INSTALL_DIR/firmware"
ARDUINO_DATA_DIR="$INSTALL_DIR/arduino-data"
ARDUINO_USER_DIR="$INSTALL_DIR/arduino-user"
ARDUINO_CONFIG="$INSTALL_DIR/arduino-cli.yaml"

mkdir -p "$INSTALL_DIR" "$APP_DIR" "$FIRMWARE_DIR" "$ARDUINO_DATA_DIR" "$ARDUINO_USER_DIR"

# ─── Step 1: System packages ────────────────────────────────────────────────
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    curl ca-certificates git udev \
    libusb-1.0-0 \
    2>&1 | tail -3

# ─── Step 2: Install arduino-cli ─────────────────────────────────────────────
echo "[2/7] Installing arduino-cli..."
if command -v arduino-cli &>/dev/null; then
    echo "       Already installed: $(arduino-cli version | head -1)"
else
    curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=/usr/local/bin sh
    echo "       Installed: $(arduino-cli version | head -1)"
fi

# ─── Step 3: Configure arduino-cli ──────────────────────────────────────────
echo "[3/7] Configuring arduino-cli..."
cat > "$ARDUINO_CONFIG" << EOF
board_manager:
  additional_urls:
    - https://espressif.github.io/arduino-esp32/package_esp32_index.json
daemon:
  port: "50051"
directories:
  data: $ARDUINO_DATA_DIR
  downloads: $ARDUINO_DATA_DIR/staging
  user: $ARDUINO_USER_DIR
logging:
  file: ""
  format: text
  level: info
EOF

export ARDUINO_CLI_CONFIG="$ARDUINO_CONFIG"
CLI="arduino-cli --config-file $ARDUINO_CONFIG"

# ─── Step 4: Install ESP32 board package ─────────────────────────────────────
echo "[4/7] Installing ESP32 board package (this may take a few minutes)..."
$CLI core update-index 2>&1 | tail -1

if $CLI core list 2>/dev/null | grep -q "esp32:esp32"; then
    echo "       ESP32 core already installed"
else
    $CLI core install esp32:esp32@3.3.10 2>&1 | tail -3
    echo "       ESP32 core installed"
fi

# Install wolfSSL library
if $CLI lib list 2>/dev/null | grep -q "wolfssl"; then
    echo "       wolfSSL already installed"
else
    $CLI lib install "wolfssl" 2>&1 | tail -1
    echo "       wolfSSL installed"
fi

# Install PubSubClient (MQTT)
if $CLI lib list 2>/dev/null | grep -q "PubSubClient"; then
    echo "       PubSubClient already installed"
else
    $CLI lib install "PubSubClient" 2>&1 | tail -1
    echo "       PubSubClient installed"
fi

# ─── Step 5: Install custom partition table and wolfSSL config ───────────────
echo "[5/7] Installing custom partition table and wolfSSL config..."

# Install pqc_workshop partition table
PARTITIONS_DIR=$(find "$ARDUINO_DATA_DIR" -path "*/tools/partitions" -type d 2>/dev/null | grep esp32 | head -1)
if [[ -n "$PARTITIONS_DIR" ]]; then
    cat > "$PARTITIONS_DIR/pqc_workshop.csv" << 'PARTCSV'
# ESP32-S3 Partition Table for PQC Workshop (8MB Flash)
# Dual OTA layout: partition 0 = classical TLS, partition 1 = PQC TLS
# Name,       Type, SubType,  Offset,    Size,     Flags
nvs,          data, nvs,      0x9000,    0x5000,
otadata,      data, ota,      0xe000,    0x2000,
ota_0,        app,  ota_0,    0x10000,   0x380000,
ota_1,        app,  ota_1,    0x390000,  0x380000,
nvs_key,      data, nvs_keys, 0x710000,  0x1000,
coredump,     data, coredump, 0x711000,  0x10000,
PARTCSV
    echo "       Partition table installed: $PARTITIONS_DIR/pqc_workshop.csv"
else
    echo "       WARNING: Could not find partitions directory"
fi

# Apply wolfSSL user_settings.h for PQC support
WOLFSSL_DIR=$(find "$ARDUINO_USER_DIR" -path "*/libraries/wolfssl/src" -type d 2>/dev/null | head -1)
if [[ -z "$WOLFSSL_DIR" ]]; then
    WOLFSSL_DIR=$(find "$ARDUINO_DATA_DIR" -path "*/libraries/wolfssl/src" -type d 2>/dev/null | head -1)
fi

if [[ -n "$WOLFSSL_DIR" && -f "$INSTALL_DIR/wolfssl_user_settings_arduino.h" ]]; then
    cp "$INSTALL_DIR/wolfssl_user_settings_arduino.h" "$WOLFSSL_DIR/user_settings.h"
    echo "       wolfSSL PQC config applied: $WOLFSSL_DIR/user_settings.h"
elif [[ -n "$WOLFSSL_DIR" ]]; then
    echo "       WARNING: wolfssl_user_settings_arduino.h not found at $INSTALL_DIR/"
    echo "       Upload it via: aws s3 cp ... or scp"
else
    echo "       WARNING: wolfSSL library directory not found"
fi

# ─── Step 6: Install Python dependencies for builder-pi app ──────────────────
echo "[6/7] Installing Python dependencies..."
pip3 install --no-cache-dir --break-system-packages \
    flask==3.1.* \
    boto3 \
    pyserial \
    requests \
    esptool \
    2>&1 | tail -3

# ─── Step 7: Setup udev rules for ESP32 USB access ──────────────────────────
echo "[7/7] Configuring USB access for ESP32 devices..."
cat > /etc/udev/rules.d/99-esp32.rules << 'UDEV'
# ESP32 USB serial (CP210x, CH340, FTDI, ESP32-S3 native USB CDC)
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", MODE="0666"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", MODE="0666"
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", MODE="0666"
SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", MODE="0666"
UDEV
udevadm control --reload-rules 2>/dev/null || true

# ─── Create systemd service for the builder-pi GUI ───────────────────────────
cat > /etc/systemd/system/pqc-builder-gui.service << SERVICE
[Unit]
Description=PQC Workshop Builder-Pi GUI
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
Environment=ARDUINO_CLI_CONFIG=$ARDUINO_CONFIG
ExecStart=/usr/bin/python3 app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

# Create a symlink for arduino-cli config so device_manager finds it
cat > /etc/profile.d/pqc-workshop.sh << 'PROFILE'
export ARDUINO_CLI_CONFIG=/opt/pqc-workshop/arduino-cli.yaml
export PATH=/usr/local/bin:$PATH
PROFILE

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Setup complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Next steps:"
echo "    1. Copy builder-pi app files to: $APP_DIR/"
echo "       (app.py, device_manager.py, config.json, templates/, static/)"
echo ""
echo "    2. Copy wolfssl_user_settings_arduino.h to: $INSTALL_DIR/"
echo ""
echo "    3. Update config.json with your API Gateway URL"
echo ""
echo "    4. Start the GUI:"
echo "       systemctl enable pqc-builder-gui"
echo "       systemctl start pqc-builder-gui"
echo ""
echo "    5. Access at: http://$(hostname -I | awk '{print $1}'):8080"
echo ""
echo "  Tools installed:"
echo "    - arduino-cli $(arduino-cli version 2>/dev/null | grep -o 'v[0-9.]*' | head -1)"
echo "    - esptool $(esptool.py version 2>/dev/null | head -1)"
echo "    - ESP32 core $(arduino-cli --config-file $ARDUINO_CONFIG core list 2>/dev/null | grep esp32 | awk '{print $2}')"
echo ""
