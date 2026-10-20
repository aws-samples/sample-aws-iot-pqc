# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

FROM public.ecr.aws/ubuntu/ubuntu:22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

# Install build dependencies.
# Apt is configured to retry and time out per-download so a slow/flaky Ubuntu
# mirror fails fast and retries instead of stalling the whole build for many
# minutes (which previously blew the CodeBuild timeout). Reliability matters:
# this image is built during the one-shot Workshop Studio deploy.
RUN printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' \
      > /etc/apt/apt.conf.d/80-retries && \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential git cmake pkg-config \
    libcjson-dev libsystemd-dev \
    python3 python3-pip curl ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ─── Build OpenSSL 3.5 with PQC support ─────────────────────────────────────
WORKDIR /tmp
RUN git clone --depth 1 --branch openssl-3.5.0 https://github.com/openssl/openssl.git && \
    cd openssl && \
    ./Configure --prefix=/opt/openssl-pqc --openssldir=/opt/openssl-pqc/ssl \
        --libdir=lib shared && \
    make -j$(nproc) && \
    make install_sw install_ssldirs && \
    cd .. && rm -rf openssl

# ─── Build liboqs (post-quantum crypto library) ─────────────────────────────
RUN git clone --depth 1 --branch 0.10.1 https://github.com/open-quantum-safe/liboqs.git && \
    cd liboqs && \
    cmake -S . -B build \
        -DCMAKE_INSTALL_PREFIX=/opt/openssl-pqc \
        -DBUILD_SHARED_LIBS=ON \
        -DOQS_USE_OPENSSL=ON \
        -DOPENSSL_ROOT_DIR=/opt/openssl-pqc \
        -DOPENSSL_INCLUDE_DIR=/opt/openssl-pqc/include \
        -DOPENSSL_CRYPTO_LIBRARY=/opt/openssl-pqc/lib/libcrypto.so \
        -DOPENSSL_SSL_LIBRARY=/opt/openssl-pqc/lib/libssl.so && \
    cmake --build build -j$(nproc) && \
    cmake --install build && \
    cd .. && rm -rf liboqs

# ─── Build oqs-provider (OpenSSL 3.x provider for PQC algorithms) ───────────
RUN git clone --depth 1 --branch 0.6.1 https://github.com/open-quantum-safe/oqs-provider.git && \
    cd oqs-provider && \
    cmake -S . -B build \
        -DCMAKE_PREFIX_PATH=/opt/openssl-pqc \
        -DOPENSSL_ROOT_DIR=/opt/openssl-pqc \
        -Dliboqs_DIR=/opt/openssl-pqc/lib/cmake/liboqs && \
    cmake --build build -j$(nproc) && \
    cp build/lib/oqsprovider.so /opt/openssl-pqc/lib/ossl-modules/ && \
    cd .. && rm -rf oqs-provider

# ─── Build Mosquitto against PQC OpenSSL ────────────────────────────────────
# Install system libssl-dev for headers, then replace libs with our custom build.
# This ensures the compiler finds openssl headers in the standard location,
# and links ONLY against our PQC-enabled OpenSSL 3.5.
RUN apt-get update && apt-get install -y --no-install-recommends libssl-dev && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    rm -rf /usr/include/openssl && \
    ln -sf /opt/openssl-pqc/include/openssl /usr/include/openssl && \
    rm -f /usr/lib/x86_64-linux-gnu/libssl.so* /usr/lib/x86_64-linux-gnu/libcrypto.so* && \
    ln -sf /opt/openssl-pqc/lib/libssl.so.3 /usr/lib/x86_64-linux-gnu/libssl.so && \
    ln -sf /opt/openssl-pqc/lib/libssl.so.3 /usr/lib/x86_64-linux-gnu/libssl.so.3 && \
    ln -sf /opt/openssl-pqc/lib/libcrypto.so.3 /usr/lib/x86_64-linux-gnu/libcrypto.so && \
    ln -sf /opt/openssl-pqc/lib/libcrypto.so.3 /usr/lib/x86_64-linux-gnu/libcrypto.so.3 && \
    ldconfig

RUN git clone --depth 1 --branch v2.0.18 https://github.com/eclipse/mosquitto.git && \
    cd mosquitto && \
    make -j$(nproc) -C lib \
        WITH_TLS=yes \
        WITH_CJSON=yes \
        WITH_SYSTEMD=no && \
    make -j$(nproc) -C src \
        WITH_TLS=yes \
        WITH_CJSON=yes \
        WITH_SYSTEMD=no \
        WITH_WEBSOCKETS=no \
        prefix=/opt/mosquitto && \
    mkdir -p /opt/mosquitto/sbin /opt/mosquitto/lib /opt/mosquitto/include && \
    cp src/mosquitto /opt/mosquitto/sbin/ && \
    cp lib/libmosquitto.so.1 /opt/mosquitto/lib/ && \
    ln -sf libmosquitto.so.1 /opt/mosquitto/lib/libmosquitto.so && \
    cp include/mosquitto.h /opt/mosquitto/include/ && \
    cd .. && rm -rf mosquitto

# ═══════════════════════════════════════════════════════════════════════════════
# Runtime image (slim)
# ═══════════════════════════════════════════════════════════════════════════════
FROM public.ecr.aws/ubuntu/ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip curl ca-certificates libcjson1 \
    && pip3 install --no-cache-dir awscli boto3 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy built artifacts
COPY --from=builder /opt/openssl-pqc /opt/openssl-pqc
COPY --from=builder /opt/mosquitto /opt/mosquitto

# Set library path
ENV LD_LIBRARY_PATH=/opt/openssl-pqc/lib:/opt/mosquitto/lib
ENV PATH=/opt/mosquitto/sbin:/opt/mosquitto/bin:/opt/openssl-pqc/bin:$PATH

# Configure OpenSSL to load the oqs-provider (makes ML-KEM groups AVAILABLE).
#
# We deliberately DO NOT set a system-default `Groups` list here. A system-wide
# `[system_default_sect] Groups = x25519_mlkem768:...` forces ML-KEM key-exchange
# groups onto EVERY TLS connection this container makes — including the OUTBOUND
# Mosquitto->IoT Core bridge. AWS IoT Core is a standard (non-PQC) TLS endpoint
# and resets any ClientHello that advertises ML-KEM groups, so the bridge never
# completes TLS ("unexpected eof"), never gets a CONNACK, and loops forever.
#
# The device-facing listener still negotiates ML-KEM without a forced Groups
# list: the oqs-provider makes the hybrid groups AVAILABLE, and OpenSSL selects
# them when the ESP32 client proposes them. The outbound bridge, whose peer
# (IoT Core) offers only classical groups, then negotiates classical cleanly.
#
# Regression history: commit 22f0f73 added the system-wide Groups list, which
# broke the bridge on any image built after it (pqc-a kept working only because
# it still ran a pre-22f0f73 image). Reverting to provider-only restores the
# proven-good behaviour: PQC on the device listener, classical on the bridge.
RUN mkdir -p /opt/openssl-pqc/ssl && \
    printf '[openssl_init]\nproviders = provider_sect\n\n[provider_sect]\ndefault = default_sect\noqsprovider = oqs_sect\n\n[default_sect]\nactivate = 1\n\n[oqs_sect]\nactivate = 1\nmodule = /opt/openssl-pqc/lib/ossl-modules/oqsprovider.so\n' \
    > /opt/openssl-pqc/ssl/openssl.cnf

# Create mosquitto directories
RUN mkdir -p /mosquitto/config/conf.d /mosquitto/data /mosquitto/log /mosquitto/certs

# Copy config and entrypoint
COPY mosquitto.conf /mosquitto/config/mosquitto.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8883

# checkov:skip=CKV_DOCKER_3:container requires root — entrypoint.sh fetches TLS
# certs/keys from Secrets Manager and writes bridge config into root-owned
# /mosquitto/{certs,config/conf.d} dirs created above, before exec'ing mosquitto.
# TCP-connect check on the device-facing TLS listener (8883). 8883 is raw
# MQTT-over-TLS, not HTTP, so we test only that the port accepts a connection
# using bash's /dev/tcp (bash is present; no netcat/mosquitto_sub in this image).
# nosemgrep: missing-user -- container intentionally runs as root: entrypoint.sh
# fetches TLS certs/keys from Secrets Manager and writes bridge config into
# root-owned /mosquitto/{certs,config/conf.d} before exec'ing mosquitto.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD bash -c 'exec 3<>/dev/tcp/localhost/8883' 2>/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["mosquitto", "-c", "/mosquitto/config/mosquitto.conf"]
