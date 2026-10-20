#!/usr/bin/env python3
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Shared certificate issuer — the ONE place all workshop TLS certificates are minted.

Generates a private key + CSR and issues an X.509 certificate from an AWS Private
CA (ACM PCA), then writes the CA cert, leaf cert, and private key as PEM files.
Used for BOTH device certificates (from the firmware CodeBuild) and broker server
certificates (from a container-image Lambda custom resource). Because issuance
lives in one script inside one container image, classical (ECDSA) and post-quantum
(ML-DSA-44) certs share a single, inspectable code path — and the same image drops
into a future registration Step Function as a task with no rework.

Key modes:
  ec-p256   → EC prime256v1 key (classical). Uses whichever `openssl` is on PATH;
              the system OpenSSL 3.0.x is fine for EC.
  mldsa44   → ML-DSA-44 key (FIPS 204, post-quantum). Requires OpenSSL >= 3.5,
              which the cert-issuer image provides at OPENSSL35_BIN
              (default /opt/openssl-3.5/bin/openssl). ML-DSA is native in 3.5.

Signing algorithm (ACM PCA `SigningAlgorithm`) is chosen from the key mode unless
overridden: ec-p256 → SHA256WITHECDSA, mldsa44 → ML_DSA_44.

CLI (used from CodeBuild / shell):
    issue_cert.py \
        --common-name esp32-ABC123 \
        --key-mode mldsa44 \
        --ca-arn arn:aws:acm-pca:...:certificate-authority/... \
        --out-dir /tmp/certs \
        [--org "AWS PQC Workshop"] [--ou "IoT Device PQC ML-DSA"] [--country US] \
        [--validity-days 365] [--idempotency-token dev-ABC123] \
        [--signing-algorithm ML_DSA_44]

Writes <out-dir>/ca.crt, <out-dir>/cert.crt, <out-dir>/key.pem and prints a JSON
summary ({"caCert","cert","key","certificateArn"}) to stdout.

Python API (used from the container-image Lambda):
    from issue_cert import issue_certificate
    result = issue_certificate(common_name=..., key_mode=..., ca_arn=..., ...)
    # returns dict: {"caCert","cert","key","certificateArn"}
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import boto3

# OpenSSL 3.5 (ML-DSA-capable) location inside the cert-issuer image. Overridable
# so the same script works in any environment that provides a 3.5 build.
OPENSSL35_BIN = os.environ.get("OPENSSL35_BIN", "/opt/openssl-3.5/bin/openssl")
# Stock openssl for classical EC keys (system 3.0.x is fine).
OPENSSL_BIN = os.environ.get("OPENSSL_BIN", "openssl")

# key mode → (openssl keygen argv builder, default ACM PCA signing algorithm)
KEY_MODES = {
    "ec-p256": "SHA256WITHECDSA",
    "mldsa44": "ML_DSA_44",
}


def _sanitize_token(raw):
    """ACM PCA IdempotencyToken: alphanumeric-ish, <= 36 chars. Strip anything
    else and cap length so a serial/CN can be passed in safely."""
    cleaned = re.sub(r"[^A-Za-z0-9-]", "", raw or "")
    return cleaned[:36] or "issue"


def _run(argv):
    """Run a subprocess, raising with captured stderr on failure."""
    proc = subprocess.run(argv, capture_output=True, text=True)  # nosemgrep: dangerous-subprocess-use-audit — argv list, no shell, inputs sanitized
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(argv)}\n{proc.stderr}"
        )
    return proc


def generate_key_and_csr(common_name, key_mode, subject_org, subject_ou, country, workdir):
    """Generate the private key + CSR for the given key mode. Returns (key_path,
    csr_path). ML-DSA uses OpenSSL 3.5; EC uses the stock openssl."""
    key_path = os.path.join(workdir, "key.pem")
    csr_path = os.path.join(workdir, "cert.csr")
    subj = f"/CN={common_name}/O={subject_org}/OU={subject_ou}/C={country}"

    if key_mode == "mldsa44":
        if not os.path.exists(OPENSSL35_BIN):
            raise RuntimeError(
                f"OpenSSL 3.5 not found at {OPENSSL35_BIN}; required for ML-DSA-44. "
                "Run this in the cert-issuer image (or set OPENSSL35_BIN)."
            )
        _run([OPENSSL35_BIN, "genpkey", "-algorithm", "ML-DSA-44", "-out", key_path])
        _run([OPENSSL35_BIN, "req", "-new", "-key", key_path, "-out", csr_path, "-subj", subj])
    elif key_mode == "ec-p256":
        _run([OPENSSL_BIN, "ecparam", "-genkey", "-name", "prime256v1", "-noout", "-out", key_path])
        _run([OPENSSL_BIN, "req", "-new", "-key", key_path, "-out", csr_path, "-subj", subj])
    else:
        raise ValueError(f"Unknown key mode '{key_mode}'. Expected one of: {', '.join(KEY_MODES)}")

    return key_path, csr_path


def issue_certificate(
    common_name,
    key_mode,
    ca_arn,
    subject_org="AWS PQC Workshop",
    subject_ou="IoT Device",
    country="US",
    validity_days=365,
    idempotency_token=None,
    signing_algorithm=None,
    acm_pca_client=None,
):
    """Generate a keypair + CSR, issue a cert from the given ACM PCA, and return
    {"caCert","cert","key","certificateArn"} (all PEM strings). The private key
    never leaves this process except in the returned dict / written files."""
    if key_mode not in KEY_MODES:
        raise ValueError(
            f"Unknown key mode '{key_mode}'. Expected one of: {', '.join(KEY_MODES)}"
        )
    signing_algorithm = signing_algorithm or KEY_MODES[key_mode]
    acm_pca = acm_pca_client or boto3.client("acm-pca")

    with tempfile.TemporaryDirectory() as workdir:
        key_path, csr_path = generate_key_and_csr(
            common_name, key_mode, subject_org, subject_ou, country, workdir
        )
        with open(key_path, encoding="utf-8") as f:
            key_pem = f.read()
        with open(csr_path, "rb") as f:
            csr_bytes = f.read()

    issue_resp = acm_pca.issue_certificate(
        CertificateAuthorityArn=ca_arn,
        Csr=csr_bytes,
        SigningAlgorithm=signing_algorithm,
        Validity={"Type": "DAYS", "Value": validity_days},
        IdempotencyToken=_sanitize_token(idempotency_token or common_name),
    )
    cert_arn = issue_resp["CertificateArn"]

    # ACM PCA issuance is async; poll get_certificate until ready.
    cert_pem = None
    last_err = None
    for _ in range(15):
        try:
            cert_resp = acm_pca.get_certificate(
                CertificateAuthorityArn=ca_arn, CertificateArn=cert_arn
            )
            cert_pem = cert_resp["Certificate"]
            break
        except acm_pca.exceptions.RequestInProgressException as e:
            last_err = e
            time.sleep(3)  # nosemgrep: arbitrary-sleep
    if cert_pem is None:
        raise RuntimeError(f"Certificate issuance timed out for {cert_arn}: {last_err}")

    ca_resp = acm_pca.get_certificate_authority_certificate(CertificateAuthorityArn=ca_arn)
    ca_pem = ca_resp["Certificate"]

    return {
        "caCert": ca_pem,
        "cert": cert_pem,
        "key": key_pem,
        "certificateArn": cert_arn,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Issue a TLS cert from an AWS Private CA.")
    parser.add_argument("--common-name", required=True)
    parser.add_argument("--key-mode", required=True, choices=sorted(KEY_MODES))
    parser.add_argument("--ca-arn", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--org", default="AWS PQC Workshop")
    parser.add_argument("--ou", default="IoT Device")
    parser.add_argument("--country", default="US")
    parser.add_argument("--validity-days", type=int, default=365)
    parser.add_argument("--idempotency-token", default=None)
    parser.add_argument("--signing-algorithm", default=None)
    args = parser.parse_args(argv)

    result = issue_certificate(
        common_name=args.common_name,
        key_mode=args.key_mode,
        ca_arn=args.ca_arn,
        subject_org=args.org,
        subject_ou=args.ou,
        country=args.country,
        validity_days=args.validity_days,
        idempotency_token=args.idempotency_token,
        signing_algorithm=args.signing_algorithm,
    )

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "ca.crt"), "w", encoding="utf-8") as f:
        f.write(result["caCert"])
    with open(os.path.join(args.out_dir, "cert.crt"), "w", encoding="utf-8") as f:
        f.write(result["cert"])
    key_file = os.path.join(args.out_dir, "key.pem")
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(result["key"])
    os.chmod(key_file, 0o600)

    # Print a summary WITHOUT the private key to stdout (key is on disk only).
    print(json.dumps({
        "certificateArn": result["certificateArn"],
        "caCert": os.path.join(args.out_dir, "ca.crt"),
        "cert": os.path.join(args.out_dir, "cert.crt"),
        "key": key_file,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
