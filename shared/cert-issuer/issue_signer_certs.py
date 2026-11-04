#!/usr/bin/env python3
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Firmware-signer certificate provisioner — CodeBuild entry point.

Mints the two stable per-era OTA firmware SIGNER certificates and stores each
cert/key/signing-root cert in Secrets Manager:

  classical (ECDSA P-256) from FirmwareSignerCA      → 2.0.0/3.0.0 OTA era
  ML-DSA-44               from MlDsaFirmwareSignerCA → 4.0.0+ OTA era

Each signer is issued by its own dedicated firmware-signing root (never by the
device/broker CAs, threat T024) with the CodeSigningCertificate/V1 template, so
the leaf carries the codeSigning EKU and can never act as a TLS identity.

This runs as its OWN CodeBuild job with its OWN IAM role, separate from the
broker cert provisioner. That role is the only principal allowed to issue from
the firmware-signing CAs, and only with the code-signing template, so a
compromise of the broker cert job can no longer mint a firmware signer.

Environment variables (set by the CodeBuild project):
  SIGNER_CLASSICAL_CA_ARN      — ECDSA P-256 firmware-signing root CA ARN
  SIGNER_MLDSA_CA_ARN          — ML-DSA-44 firmware-signing root CA ARN
  SIGNER_CLASSICAL_CA_SECRET   — Secrets Manager id: classical signing root cert
  SIGNER_CLASSICAL_CERT_SECRET — Secrets Manager id: classical ECDSA signer leaf
  SIGNER_CLASSICAL_KEY_SECRET  — Secrets Manager id: classical ECDSA signer key
  SIGNER_MLDSA_CA_SECRET       — Secrets Manager id: ML-DSA signing root cert
  SIGNER_MLDSA_CERT_SECRET     — Secrets Manager id: ML-DSA signer leaf
  SIGNER_MLDSA_KEY_SECRET      — Secrets Manager id: ML-DSA signer key
  SIGNER_CN                    — signer CommonName base (default pqc-firmware-signer)
"""

import os
import sys

import boto3

from issue_cert import CODE_SIGNING_TEMPLATE_ARN, issue_certificate

secrets = boto3.client("secretsmanager")


def _store(secret_id, value):
    secrets.put_secret_value(SecretId=secret_id, SecretString=value)


def _issue_signer(era, signer_cn, key_mode, subject_ou):
    """Issue one signer from its era's firmware-signing root and store it."""
    prefix = f"SIGNER_{era.upper()}"
    common_name = f"{signer_cn}-{era}"
    print(f"Issuing {era} firmware signer cert (CN={common_name})...")
    issued = issue_certificate(
        common_name=common_name,
        key_mode=key_mode,
        ca_arn=os.environ[f"{prefix}_CA_ARN"],
        subject_ou=subject_ou,
        idempotency_token=f"codesign-{era}-{signer_cn}",
        template_arn=CODE_SIGNING_TEMPLATE_ARN,
    )
    _store(os.environ[f"{prefix}_CA_SECRET"], issued["caCert"])
    _store(os.environ[f"{prefix}_CERT_SECRET"], issued["cert"])
    _store(os.environ[f"{prefix}_KEY_SECRET"], issued["key"])
    print(f"  {era} firmware signer cert stored ({issued['certificateArn']})")


def main():
    signer_cn = os.environ.get("SIGNER_CN", "pqc-firmware-signer")
    _issue_signer("classical", signer_cn, "ec-p256", "Firmware Signer")
    _issue_signer("mldsa", signer_cn, "mldsa44", "Firmware Signer PQC ML-DSA")
    print("Firmware signer certificates provisioned successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
