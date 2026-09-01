FROM public.ecr.aws/ubuntu/ubuntu:22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git cmake pkg-config \
    libcjson-dev libsystemd-dev \
    python3 python3-pip curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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
    rm -rf /var/lib/apt/lists/* && \
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
    && rm -rf /var/lib/apt/lists/*

# Copy built artifacts
COPY --from=builder /opt/openssl-pqc /opt/openssl-pqc
COPY --from=builder /opt/mosquitto /opt/mosquitto

# Set library path
ENV LD_LIBRARY_PATH=/opt/openssl-pqc/lib:/opt/mosquitto/lib
ENV PATH=/opt/mosquitto/sbin:/opt/mosquitto/bin:/opt/openssl-pqc/bin:$PATH

# Configure OpenSSL to use oqs-provider and set PQC key exchange groups
RUN mkdir -p /opt/openssl-pqc/ssl && \
    printf '[openssl_init]\nproviders = provider_sect\nssl_conf = ssl_sect\n\n[provider_sect]\ndefault = default_sect\noqsprovider = oqs_sect\n\n[default_sect]\nactivate = 1\n\n[oqs_sect]\nactivate = 1\nmodule = /opt/openssl-pqc/lib/ossl-modules/oqsprovider.so\n\n[ssl_sect]\nsystem_default = system_default_sect\n\n[system_default_sect]\nGroups = x25519_mlkem768:secp256r1_mlkem768:X25519:P-256\n' \
    > /opt/openssl-pqc/ssl/openssl.cnf

# Create mosquitto directories
RUN mkdir -p /mosquitto/config/conf.d /mosquitto/data /mosquitto/log /mosquitto/certs

# Copy config and entrypoint
COPY mosquitto.conf /mosquitto/config/mosquitto.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8883

ENTRYPOINT ["/entrypoint.sh"]
CMD ["mosquitto", "-c", "/mosquitto/config/mosquitto.conf"]
