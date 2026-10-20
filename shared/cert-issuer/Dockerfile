# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# ─────────────────────────────────────────────────────────────────────────────
# Shared cert-issuer image — the ONE place all workshop TLS certs are minted.
#
# Contains OpenSSL 3.5 (ML-DSA-44 capable) + Python + boto3 + the shared
# issue_cert.py. Used as a CodeBuild image:
#   • broker certs  → CodeBuild runs issue_broker_certs.py (both server certs)
#   • device certs  → the firmware-builder image bakes in the SAME issue_cert.py
#   • future        → drops into the registration Step Function via
#                     codebuild:startBuild.sync, unchanged
#
# ML-DSA is NATIVE in OpenSSL 3.5 (no oqs-provider needed). We install 3.5 into
# /opt/openssl-3.5 so the base image's system OpenSSL is left untouched; the
# issuer calls /opt/openssl-3.5/bin/openssl for ML-DSA and stock openssl for EC.
#
# NOTE: this is a PLAIN image (not the Lambda base) so CodeBuild can run a shell
# in it. The broker cert provisioning moved from a container-image Lambda to a
# CodeBuild job to fit the deploy model (stack first, images/values post-stack).
# ─────────────────────────────────────────────────────────────────────────────
# checkov:skip=CKV_DOCKER_3:short-lived CodeBuild image; CodeBuild runs the job as its own build user, no long-running process to drop privileges for
# checkov:skip=CKV_DOCKER_2:short-lived task container, no long-running service to health-check
FROM public.ecr.aws/ubuntu/ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Build toolchain for compiling OpenSSL from source + python/boto3 for the issuer.
# nosemgrep: remove-package-cache -- false positive: this RUN cleans the apt cache in-layer (rm -rf /var/lib/apt/lists/* on its last line)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential perl wget ca-certificates python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# ─── OpenSSL 3.5 (post-quantum ML-DSA-44) into /opt/openssl-3.5 ──────────────
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
    # generation works. (Discovered via a real ML-DSA CSR run in the image.)
    && mkdir -p /opt/openssl-3.5/ssl \
    && printf '[req]\ndistinguished_name = dn\n[dn]\n' > /opt/openssl-3.5/ssl/openssl.cnf \
    && rm -rf /tmp/openssl-src /tmp/openssl.tar.gz \
    && /opt/openssl-3.5/bin/openssl version \
    # Fail the image build if this OpenSSL cannot actually do ML-DSA-44 end to
    # end: generate an ML-DSA-44 key AND a CSR from it (the CSR step is what
    # needs openssl.cnf).
    && /opt/openssl-3.5/bin/openssl genpkey -algorithm ML-DSA-44 -out /tmp/_mldsa_probe.key \
    && /opt/openssl-3.5/bin/openssl req -new -key /tmp/_mldsa_probe.key -out /tmp/_mldsa_probe.csr -subj "/CN=probe" \
    && rm -f /tmp/_mldsa_probe.key /tmp/_mldsa_probe.csr \
    && echo "OpenSSL 3.5 with ML-DSA-44 verified (key + CSR)"

RUN pip3 install --no-cache-dir boto3

# Shared issuance code. /var/task keeps the path stable for the buildspec.
COPY issue_cert.py issue_broker_certs.py /var/task/
ENV OPENSSL35_BIN=/opt/openssl-3.5/bin/openssl
ENV PYTHONPATH=/var/task

WORKDIR /var/task
