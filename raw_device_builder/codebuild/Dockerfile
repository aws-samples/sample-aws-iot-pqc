FROM public.ecr.aws/ubuntu/ubuntu:22.04

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip curl git ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

# Install AWS CLI v2
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip

# Install esptool
RUN pip3 install --no-cache-dir esptool

# Install arduino-cli
RUN curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=/usr/local/bin sh

# Configure arduino-cli
RUN arduino-cli config init \
    && arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json \
    && arduino-cli core update-index \
    && arduino-cli core install esp32:esp32 \
    && arduino-cli lib install wolfSSL \
    && arduino-cli lib install MQTT

# Apply wolfSSL user_settings.h for PQC support
# This file is copied in during the image build from S3
COPY wolfssl_user_settings_arduino.h /tmp/wolfssl_settings.h
RUN WOLFSSL_DIR=$(find / -path "*/libraries/wolfSSL/src" -type d 2>/dev/null | head -1) \
    && cp /tmp/wolfssl_settings.h "$WOLFSSL_DIR/user_settings.h" \
    && echo "wolfSSL PQC settings installed at $WOLFSSL_DIR"

# Verify installation
RUN arduino-cli core list && arduino-cli lib list && esptool.py version

WORKDIR /build
