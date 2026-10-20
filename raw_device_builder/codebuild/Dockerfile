# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# checkov:skip=CKV_DOCKER_3:short-lived CodeBuild firmware-builder image; CodeBuild runs the build as its own user, no long-running process to drop privileges for
# checkov:skip=CKV_DOCKER_2:short-lived build container, no long-running service to health-check
FROM public.ecr.aws/ubuntu/ubuntu:22.04

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Fail piped RUN steps if any stage of the pipe fails (e.g. curl ... | sh).
# Ubuntu base ships bash, so a pipefail-enabled bash shell is available.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install system dependencies
# (build-essential/perl/wget added to compile OpenSSL 3.5 from source below)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip curl git ca-certificates unzip \
    build-essential perl wget \
    && rm -rf /var/lib/apt/lists/*

# ─── OpenSSL 3.5 (for post-quantum ML-DSA-44 key/CSR generation) ─────────────
# Ubuntu 22.04 ships OpenSSL 3.0.2, which has NO ML-DSA support. The firmware
# build issues device certificates from AWS Private CA (see buildspec.yml); the
# ML-DSA-44 variant needs OpenSSL >= 3.5 to generate the ML-DSA key + CSR.
# ML-DSA is NATIVE in OpenSSL 3.5 (no oqs-provider needed). We install it into
# /opt/openssl-3.5 so the system OpenSSL 3.0.2 (used by the OS, AWS CLI, etc.)
# is left untouched; the buildspec calls /opt/openssl-3.5/bin/openssl explicitly
# for ML-DSA and the stock `openssl` for classical EC keys.
ARG OPENSSL_VERSION=3.5.8
RUN wget -q "https://github.com/openssl/openssl/releases/download/openssl-${OPENSSL_VERSION}/openssl-${OPENSSL_VERSION}.tar.gz" -O /tmp/openssl.tar.gz \
    && mkdir -p /tmp/openssl-src \
    && tar -xzf /tmp/openssl.tar.gz -C /tmp/openssl-src --strip-components=1 \
    && cd /tmp/openssl-src \
    && ./Configure --prefix=/opt/openssl-3.5 --openssldir=/opt/openssl-3.5/ssl \
        --libdir=lib no-shared no-docs \
    && make -j"$(nproc)" \
    && make install_sw \
    # install_sw does NOT install the default openssl.cnf, but `openssl req`
    # needs one — provide a minimal config at the configured openssldir so CSR
    # generation works.
    && mkdir -p /opt/openssl-3.5/ssl \
    && printf '[req]\ndistinguished_name = dn\n[dn]\n' > /opt/openssl-3.5/ssl/openssl.cnf \
    && rm -rf /tmp/openssl-src /tmp/openssl.tar.gz \
    && /opt/openssl-3.5/bin/openssl version \
    # Fail the image build if this OpenSSL cannot do ML-DSA-44 end to end
    # (key + CSR — the CSR step is what needs openssl.cnf).
    && /opt/openssl-3.5/bin/openssl genpkey -algorithm ML-DSA-44 -out /tmp/_mldsa_probe.key \
    && /opt/openssl-3.5/bin/openssl req -new -key /tmp/_mldsa_probe.key -out /tmp/_mldsa_probe.csr -subj "/CN=probe" \
    && rm -f /tmp/_mldsa_probe.key /tmp/_mldsa_probe.csr \
    && echo "OpenSSL 3.5 with ML-DSA-44 verified (key + CSR)"

# Install AWS CLI v2
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip

# Install esptool (for partition table generation and flashing reference) and
# boto3 (used by the shared cert-issuer script issue_cert.py to call ACM PCA).
RUN pip3 install --no-cache-dir esptool boto3

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

# Shared cert-issuer script — the per-device firmware build calls this to mint
# the device certificate, the SAME code path the broker cert-issuer container
# uses. It calls /opt/openssl-3.5/bin/openssl for ML-DSA and stock openssl for EC.
COPY issue_cert.py /opt/cert-issuer/issue_cert.py
ENV OPENSSL35_BIN=/opt/openssl-3.5/bin/openssl

# Verify installation
RUN arduino-cli core list && arduino-cli lib list && esptool.py version \
    && python3 /opt/cert-issuer/issue_cert.py --help >/dev/null && echo "cert-issuer present"

WORKDIR /build
