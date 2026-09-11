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

# Stage the wolfSSL PQC header from the bundle into $INSTALL_DIR (the bundle
# ships it next to this script). This is what the apply step below copies into
# the Arduino wolfSSL library.
SCRIPT_SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$SCRIPT_SRC_DIR/wolfssl_user_settings_arduino.h" ]]; then
    cp "$SCRIPT_SRC_DIR/wolfssl_user_settings_arduino.h" "$INSTALL_DIR/wolfssl_user_settings_arduino.h"
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

# Install the workshop-builder CLI so operators have a single, reviewable,
# logged command surface over SSM (shares builder_core with the GUI). The CLI
# source (cli.py, builder_core/, pyproject.toml) is bundled alongside this
# script; install from whichever directory it lives in.
CLI_SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$CLI_SRC_DIR/pyproject.toml" ]]; then
    echo "       Installing workshop-builder CLI from $CLI_SRC_DIR"
    pip3 install --no-cache-dir --break-system-packages "$CLI_SRC_DIR" 2>&1 | tail -3
    # Persistent log directory for CLI operation transcripts.
    mkdir -p /var/log/workshop-builder
    command -v workshop-builder >/dev/null 2>&1 \
        && echo "       workshop-builder installed: $(command -v workshop-builder)" \
        || echo "       [WARN] workshop-builder not on PATH after install"
else
    echo "       [WARN] pyproject.toml not found next to setup script; skipping CLI install"
fi

# ─── Deploy the builder-pi app files into $APP_DIR ───────────────────────────
# The GUI (app.py) and its config resolve config.json relative to the package
# location (builder_core/config.py uses a __file__-relative path), and the
# systemd unit runs `python3 app.py` from $APP_DIR. So the app package must be
# co-located in $APP_DIR. Copy it from the bundle (next to this script).
echo "       Deploying builder-app files to $APP_DIR"
APP_FILES=(app.py device_manager.py config.json)
for f in "${APP_FILES[@]}"; do
    if [[ -f "$SCRIPT_SRC_DIR/$f" ]]; then
        cp "$SCRIPT_SRC_DIR/$f" "$APP_DIR/"
    else
        echo "       [WARN] $f not found in bundle; GUI may not start"
    fi
done
for d in builder_core templates static; do
    if [[ -d "$SCRIPT_SRC_DIR/$d" ]]; then
        rm -rf "$APP_DIR/$d"
        cp -r "$SCRIPT_SRC_DIR/$d" "$APP_DIR/"
    fi
done

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

# ─── CloudWatch agent: ship Pi flash/serial logs off-box for remote debug ────
# The register->build stages log to CloudWatch already (Lambda + CodeBuild), but
# the Pi-side flash/download/serial output previously lived only in memory. We
# persist it to /var/log/workshop-builder/ (device_manager + CLI) and ship it,
# plus the GUI service journal, to CloudWatch so a failed physical flash can be
# diagnosed remotely without SSHing to the Pi.
echo "[+] Installing + configuring the CloudWatch agent..."
mkdir -p /var/log/workshop-builder

# Region for the agent: prefer the deployed app config.json, else SSM's
# registered region, else us-east-1.
CW_REGION=$(python3 -c "import json;print(json.load(open('$APP_DIR/config.json')).get('region',''))" 2>/dev/null || true)
if [[ -z "$CW_REGION" ]]; then
    CW_REGION=$(cat /var/lib/amazon/ssm/registration 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('Region',''))" 2>/dev/null || true)
fi
[[ -z "$CW_REGION" ]] && CW_REGION="us-east-1"

if command -v amazon-cloudwatch-agent-ctl &>/dev/null || [[ -x /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl ]]; then
    echo "       CloudWatch agent already installed"
else
    CW_ARCH=$(dpkg --print-architecture 2>/dev/null || echo arm64)
    case "$CW_ARCH" in
        arm64) CW_PKG="arm64/latest/amazon-cloudwatch-agent.deb" ;;
        armhf) CW_PKG="arm/latest/amazon-cloudwatch-agent.deb" ;;
        *)     CW_PKG="${CW_ARCH}/latest/amazon-cloudwatch-agent.deb" ;;
    esac
    CW_URL="https://amazoncloudwatch-agent-${CW_REGION}.s3.${CW_REGION}.amazonaws.com/debian/${CW_PKG}"
    CW_TMP="/tmp/amazon-cloudwatch-agent.deb"
    if curl -fsSL "$CW_URL" -o "$CW_TMP"; then
        dpkg -i -E "$CW_TMP" 2>&1 | tail -2 || echo "       [WARN] CloudWatch agent install failed"
    else
        echo "       [WARN] could not download CloudWatch agent from $CW_URL"
        echo "              Pi logs will persist locally in /var/log/workshop-builder but not ship."
    fi
fi

# Agent config: tail the persistent builder logs + the GUI service journal to a
# dedicated /pqc-workshop/builder-pi log group, stream per host+source.
CW_AGENT_DIR="/opt/aws/amazon-cloudwatch-agent/etc"
if [[ -d "$CW_AGENT_DIR" || -x /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl ]]; then
    mkdir -p "$CW_AGENT_DIR"

    # This is an SSM HYBRID (on-prem) instance: the SSM agent maintains rotating
    # role credentials in /root/.aws/credentials ([default] profile). The
    # CloudWatch agent's onPremise mode needs (a) that credentials profile and
    # (b) a resolvable region, or it fails start with "please make sure the
    # credentials and region set correctly". Point common-config at the SSM
    # creds and ensure a region is set in /root/.aws/config.
    cat > "$CW_AGENT_DIR/common-config.toml" << 'COMMONCFG'
[credentials]
  shared_credential_profile = "default"
  shared_credential_file = "/root/.aws/credentials"
COMMONCFG
    mkdir -p /root/.aws
    if [[ ! -f /root/.aws/config ]] || ! grep -q '^\[default\]' /root/.aws/config 2>/dev/null; then
        printf '[default]\nregion = %s\noutput = json\n' "$CW_REGION" > /root/.aws/config
    fi

    cat > "$CW_AGENT_DIR/builder-pi-cloudwatch.json" << CWCONFIG
{
  "agent": { "region": "${CW_REGION}", "run_as_user": "root" },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/workshop-builder/*.log",
            "log_group_name": "/pqc-workshop/builder-pi",
            "log_stream_name": "{hostname}/workshop-builder",
            "retention_in_days": 14
          }
        ]
      }
    }
  }
}
CWCONFIG
    /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
        -a fetch-config -m onPremise -s \
        -c file:"$CW_AGENT_DIR/builder-pi-cloudwatch.json" 2>&1 | tail -3 \
        || echo "       [WARN] could not start CloudWatch agent; logs stay local"
    echo "       CloudWatch agent shipping /var/log/workshop-builder/*.log -> /pqc-workshop/builder-pi (region $CW_REGION)"
else
    echo "       [WARN] CloudWatch agent not present; Pi logs persist locally only"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Setup complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  App files, wolfSSL PQC config, and the workshop-builder CLI are"
echo "  already installed. Remaining steps:"
echo ""
echo "    1. Confirm $APP_DIR/config.json has the right values"
echo "       (api_endpoint, region, firmware_bucket are set by the bundle)."
echo "       Plug-and-flash: WiFi, broker endpoint and firmware version are"
echo "       resolved server-side from SSM — no operator config needed."
echo ""
echo "    2. Start the GUI:"
echo "       systemctl enable pqc-builder-gui"
echo "       systemctl start pqc-builder-gui"
echo ""
echo "    3. Access at: http://$(hostname -I | awk '{print $1}'):8080"
echo ""
echo "  Tools installed:"
echo "    - arduino-cli $(arduino-cli version 2>/dev/null | grep -o 'v[0-9.]*' | head -1)"
echo "    - esptool $(esptool.py version 2>/dev/null | head -1)"
echo "    - ESP32 core $(arduino-cli --config-file $ARDUINO_CONFIG core list 2>/dev/null | grep esp32 | awk '{print $2}')"
echo ""
