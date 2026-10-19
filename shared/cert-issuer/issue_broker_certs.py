#!/usr/bin/env python3
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Broker certificate provisioner — CodeBuild entry point.

Mints the broker's TWO server certificates via the shared issuer and stores each
cert/key/CA-cert in Secrets Manager:

  classical (ECDSA P-256) from the classical CA  → 8883 listener
  ML-DSA-44               from the ML-DSA-44 CA   → 8884 listener (Step 2)

Runs inside the shared cert-issuer image (OpenSSL 3.5 for ML-DSA) as a CodeBuild
job, triggered by the deploy script AFTER the cert-issuer image is built. This is
the broker-side counterpart to the device-cert issuance in the firmware build —
same shared issue_certificate(), one code path for classical and post-quantum.

Because it runs as a CodeBuild job (not a container-image Lambda), it fits the
deploy model where the stack is created first (no images yet) and images/values
are filled in post-stack. The same shape drops into the future registration Step
Function via codebuild:startBuild.sync.

Environment variables (set by the CodeBuild project):
  CLASSICAL_CA_ARN         — classical (ECDSA) Private CA ARN
  MLDSA_CA_ARN             — ML-DSA-44 Private CA ARN
  BROKER_CA_CERT_SECRET    — Secrets Manager id: classical CA cert (device trust root)
  BROKER_CERT_SECRET       — Secrets Manager id: classical broker cert
  BROKER_KEY_SECRET        — Secrets Manager id: classical broker key
  MLDSA_CA_CERT_SECRET     — Secrets Manager id: ML-DSA CA cert (device trust root)
  MLDSA_BROKER_CERT_SECRET — Secrets Manager id: ML-DSA broker cert
  MLDSA_BROKER_KEY_SECRET  — Secrets Manager id: ML-DSA broker key
  BROKER_CN                — broker cert CommonName (default pqc-mqtt-broker)

OTA firmware signer env vars (optional; skipped if unset):
  SIGNER_CLASSICAL_CA_SECRET   — Secrets Manager id: classical signer CA cert (device trust root)
  SIGNER_CLASSICAL_CERT_SECRET — Secrets Manager id: classical ECDSA signer leaf cert
  SIGNER_CLASSICAL_KEY_SECRET  — Secrets Manager id: classical ECDSA signer key
  SIGNER_MLDSA_CA_SECRET       — Secrets Manager id: ML-DSA signer CA cert (device trust root)
  SIGNER_MLDSA_CERT_SECRET     — Secrets Manager id: ML-DSA signer leaf cert
  SIGNER_MLDSA_KEY_SECRET      — Secrets Manager id: ML-DSA signer key
  SIGNER_CN                    — signer cert CommonName base (default pqc-firmware-signer)
"""

import os
import sys

import boto3

from issue_cert import issue_certificate

secrets = boto3.client("secretsmanager")


def _store(secret_id, value):
    secrets.put_secret_value(SecretId=secret_id, SecretString=value)


def main():
    broker_cn = os.environ.get("BROKER_CN", "pqc-mqtt-broker")

    # ── Classical broker cert (ECDSA P-256) from the classical CA ──
    print(f"Issuing classical broker cert (CN={broker_cn})...")
    classical = issue_certificate(
        common_name=broker_cn,
        key_mode="ec-p256",
        ca_arn=os.environ["CLASSICAL_CA_ARN"],
        subject_ou="MQTT Broker",
        idempotency_token=f"broker-classical-{broker_cn}",
    )
    _store(os.environ["BROKER_CA_CERT_SECRET"], classical["caCert"])
    _store(os.environ["BROKER_CERT_SECRET"], classical["cert"])
    _store(os.environ["BROKER_KEY_SECRET"], classical["key"])
    print(f"  classical broker cert stored ({classical['certificateArn']})")

    # ── ML-DSA-44 broker cert from the ML-DSA-44 CA (Step 2) ──
    print(f"Issuing ML-DSA-44 broker cert (CN={broker_cn})...")
    mldsa = issue_certificate(
        common_name=broker_cn,
        key_mode="mldsa44",
        ca_arn=os.environ["MLDSA_CA_ARN"],
        subject_ou="MQTT Broker PQC ML-DSA",
        idempotency_token=f"broker-mldsa-{broker_cn}",
    )
    _store(os.environ["MLDSA_CA_CERT_SECRET"], mldsa["caCert"])
    _store(os.environ["MLDSA_BROKER_CERT_SECRET"], mldsa["cert"])
    _store(os.environ["MLDSA_BROKER_KEY_SECRET"], mldsa["key"])
    print(f"  ML-DSA-44 broker cert stored ({mldsa['certificateArn']})")

    # ── OTA firmware SIGNER certs (stable per era) ──────────────────────────
    # One classical ECDSA-P256 signer (classical CA) for the 2.0.0/3.0.0 OTA era,
    # one ML-DSA-44 signer (ML-DSA CA) for the 4.0.0+ era. These are the stable
    # firmware-signing identities, distinct from the broker TLS cert and the
    # per-device certs. The firmware build reads the era's signer KEY to sign
    # firmware.bin; the device verifies the signature against the signer leaf
    # (shipped with the OTA), which chains to the baked-in CA root. Provisioning
    # is gated on the signer env vars being present so this script stays
    # compatible with an older stack that has not yet added the signer secrets.
    _provision_signer_certs(signer_cn=os.environ.get("SIGNER_CN", "pqc-firmware-signer"))

    print("Broker certificates provisioned successfully.")
    return 0


def _provision_signer_certs(signer_cn):
    """Mint the two stable per-era firmware signer certs into Secrets Manager.

    No-op (with a log line) if the signer secret env vars are not set, so an
    older stack without the signer secrets still provisions broker certs cleanly.
    """
    classical_key_secret = os.environ.get("SIGNER_CLASSICAL_KEY_SECRET")
    mldsa_key_secret = os.environ.get("SIGNER_MLDSA_KEY_SECRET")
    if not classical_key_secret and not mldsa_key_secret:
        print("No signer secret env vars set; skipping firmware-signer provisioning.")
        return

    # ── Classical ECDSA-P256 firmware signer (classical CA) ──
    print(f"Issuing classical firmware signer cert (CN={signer_cn}-classical)...")
    classical = issue_certificate(
        common_name=f"{signer_cn}-classical",
        key_mode="ec-p256",
        ca_arn=os.environ["CLASSICAL_CA_ARN"],
        subject_ou="Firmware Signer",
        idempotency_token=f"signer-classical-{signer_cn}",
    )
    _store(os.environ["SIGNER_CLASSICAL_CA_SECRET"], classical["caCert"])
    _store(os.environ["SIGNER_CLASSICAL_CERT_SECRET"], classical["cert"])
    _store(classical_key_secret, classical["key"])
    print(f"  classical firmware signer cert stored ({classical['certificateArn']})")

    # ── ML-DSA-44 firmware signer (ML-DSA-44 CA) ──
    print(f"Issuing ML-DSA-44 firmware signer cert (CN={signer_cn}-mldsa)...")
    mldsa = issue_certificate(
        common_name=f"{signer_cn}-mldsa",
        key_mode="mldsa44",
        ca_arn=os.environ["MLDSA_CA_ARN"],
        subject_ou="Firmware Signer PQC ML-DSA",
        idempotency_token=f"signer-mldsa-{signer_cn}",
    )
    _store(os.environ["SIGNER_MLDSA_CA_SECRET"], mldsa["caCert"])
    _store(os.environ["SIGNER_MLDSA_CERT_SECRET"], mldsa["cert"])
    _store(mldsa_key_secret, mldsa["key"])
    print(f"  ML-DSA-44 firmware signer cert stored ({mldsa['certificateArn']})")


if __name__ == "__main__":
    sys.exit(main())
