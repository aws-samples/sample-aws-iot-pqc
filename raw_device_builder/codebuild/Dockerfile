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

# Install esptool (for partition table generation and flashing reference)
RUN pip3 install --no-cache-dir esptool

# Install arduino-cli
RUN curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=/usr/local/bin sh

# Configure arduino-cli with ESP32 board package
RUN arduino-cli config init \
    && arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json \
    && arduino-cli core update-index \
    && arduino-cli core install esp32:esp32@3.3.10

# Install required libraries
RUN arduino-cli lib install "wolfssl" \
    && arduino-cli lib install "MQTT" \
    && arduino-cli lib install "PubSubClient"

# Apply wolfSSL user_settings.h for PQC support (needed even for classical
# firmware as wolfSSL is compiled in — just uses classical cipher suites)
COPY wolfssl_user_settings_arduino.h /tmp/wolfssl_settings.h
RUN WOLFSSL_DIR=$(find /root -path "*/libraries/wolfssl/src" -type d 2>/dev/null | head -1) \
    && if [ -n "$WOLFSSL_DIR" ]; then \
         cp /tmp/wolfssl_settings.h "$WOLFSSL_DIR/user_settings.h" && \
         echo "wolfSSL settings installed at $WOLFSSL_DIR"; \
       else \
         WOLFSSL_DIR=$(find / -path "*/libraries/wolfssl/src" -type d 2>/dev/null | head -1) && \
         cp /tmp/wolfssl_settings.h "$WOLFSSL_DIR/user_settings.h" && \
         echo "wolfSSL settings installed at $WOLFSSL_DIR"; \
       fi

# Install the custom pqc_workshop partition table into the ESP32 board package
# This matches the partition layout used by the working arduino-cli/build.sh
COPY partitions_pqc_workshop.csv /tmp/partitions_pqc_workshop.csv
RUN PARTITIONS_DIR=$(find / -path "*/tools/partitions" -type d 2>/dev/null | grep esp32 | head -1) \
    && cp /tmp/partitions_pqc_workshop.csv "$PARTITIONS_DIR/pqc_workshop.csv" \
    && echo "Partition table installed at $PARTITIONS_DIR/pqc_workshop.csv"

# Verify installation
RUN arduino-cli core list && arduino-cli lib list && esptool.py version

WORKDIR /build
