#!/usr/bin/env python3
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
issue_device_identity.py — build a device's identity partition image.

Runs in the shared cert-issuer image (OpenSSL 3.5 + boto3 + issue_cert.py +
esp-idf-nvs-partition-gen) as the pqc-workshop-device-identity CodeBuild job,
started by the device registrar when a device is plugged into the Builder-Pi.

For one device serial it:
  1. issues a classical (EC P-256) device cert from the classical Private CA;
  2. issues an ML-DSA-44 device cert from the ML-DSA-44 Private CA;
  3. checks every private key matches its certificate;
  4. converts everything to DER (the ML-DSA key as a 54-byte seed-only
     PKCS#8, which wolfSSL 5.8.4 loads directly — spike 0.2);
  5. builds the 64 KB "certs" NVS partition image (namespace "devid");
  6. uploads it to s3://<bucket>/identity/<serial>/certs.bin.

Private keys exist only in a temporary directory inside this job and in the
uploaded image. The Builder-Pi deletes the image after flashing, and a bucket
lifecycle rule expires anything left under identity/.

Inputs (CodeBuild environment; flags override where given):
  DEVICE_SERIAL     --serial            device serial, e.g. C69DE0
  CA_ARN            --classical-ca-arn  classical (EC P-256) Private CA
  MLDSA_CA_ARN      --mldsa-ca-arn      ML-DSA-44 Private CA
  FIRMWARE_BUCKET   --bucket            bucket for the image
  WIFI_SSID, WIFI_PASSWORD              env only, so they never appear in argv
  --out-file PATH / --no-upload         write locally instead (testing)

The NVS layout is defined in .kiro/specs/nvs-device-identity/design.md and must
match the firmware's identity loader.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid

import issue_cert

# NVS layout (must match the firmware loader).
IDENTITY_FORMAT = 1
NVS_NAMESPACE = "devid"
PARTITION_SIZE = "0x10000"  # 64 KB "certs" partition at 0x721000
S3_PREFIX = "identity"

SERIAL_RE = re.compile(r"^[A-Z0-9]{4,16}$")


def validate_serial(serial):
    """Serials go into a cert CN and an S3 key, so accept only a strict format
    (the Builder-Pi derives them from the chip MAC: 6 upper-case hex chars)."""
    serial = (serial or "").strip().upper()
    if not SERIAL_RE.match(serial):
        raise ValueError(f"Invalid device serial {serial!r}: expected 4-16 of A-Z0-9")
    return serial


def validate_wifi(ssid, password):
    """NVS strings and WPA2 have hard limits; fail here rather than on the device."""
    if not ssid or len(ssid.encode()) > 32:
        raise ValueError("WIFI_SSID must be 1-32 bytes")
    if not password or not 8 <= len(password.encode()) <= 63:
        raise ValueError("WIFI_PASSWORD must be 8-63 bytes (WPA2)")


def s3_key(serial):
    return f"{S3_PREFIX}/{serial}/certs.bin"


def _openssl_for(key_mode):
    return issue_cert.OPENSSL35_BIN if key_mode == "mldsa44" else issue_cert.OPENSSL_BIN


def _pubkey_der_of_key(key_path, key_mode):
    return issue_cert._run_bytes(
        [_openssl_for(key_mode), "pkey", "-in", key_path, "-pubout", "-outform", "DER"])


def _pubkey_der_of_cert(cert_path, key_mode):
    o = _openssl_for(key_mode)
    pem = issue_cert._run_bytes([o, "x509", "-in", cert_path, "-pubkey", "-noout"])
    return issue_cert._run_bytes([o, "pkey", "-pubin", "-outform", "DER"], stdin=pem)


def to_der(work, identity):
    """Write the DER blobs the firmware reads and return {nvs_key: path}.

    `identity` maps "classical"/"mldsa" to issue_cert results (PEM strings).
    """
    def write(name, text):
        p = os.path.join(work, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(p, 0o600)
        return p

    out = {}
    for mode, key_mode, suffix in (("classical", "ec-p256", "cls"), ("mldsa", "mldsa44", "mldsa")):
        res = identity[mode]
        o = _openssl_for(key_mode)
        ca_pem = write(f"ca_{suffix}.pem", res["caCert"])
        crt_pem = write(f"crt_{suffix}.pem", res["cert"])
        key_pem = write(f"key_{suffix}.pem", res["key"])

        # The cert must carry the public key of the private key we hold, or the
        # device fails its TLS handshake on site. Check it now.
        if _pubkey_der_of_key(key_pem, key_mode) != _pubkey_der_of_cert(crt_pem, key_mode):
            raise RuntimeError(f"{mode} certificate does not match its private key")

        for name, src in ((f"ca_{suffix}", ca_pem), (f"crt_{suffix}", crt_pem)):
            dst = os.path.join(work, f"{name}.der")
            issue_cert._run([o, "x509", "-in", src, "-outform", "DER", "-out", dst])
            out[name] = dst

        key_der = os.path.join(work, f"key_{suffix}.der")
        if key_mode == "ec-p256":
            # SEC1 DER EC key (121 B for P-256), as loaded in spike 0.2.
            issue_cert._run([o, "ec", "-in", key_pem, "-outform", "DER", "-out", key_der])
        else:
            # 54-byte seed-only PKCS#8: wolfSSL 5.8.4 expands the seed itself.
            issue_cert._run([o, "pkey", "-in", key_pem, "-outform", "DER",
                             "-provparam", "ml-dsa.output_formats=seed-only", "-out", key_der])
        os.chmod(key_der, 0o600)
        out[f"key_{suffix}"] = key_der
    return out


def build_csv(serial, ssid, password, blobs):
    """NVS partition generator CSV. Strings are written as CSV fields, so reject
    characters that would break the format rather than try to escape them."""
    for label, val in (("WIFI_SSID", ssid), ("WIFI_PASSWORD", password)):
        if any(c in val for c in ',"\n\r'):
            raise ValueError(f"{label} contains a comma, quote or newline, which "
                             "the NVS partition CSV cannot carry")
    rows = [
        "key,type,encoding,value",
        f"{NVS_NAMESPACE},namespace,,",
        f"fmt,data,u8,{IDENTITY_FORMAT}",
        f"serial,data,string,{serial}",
        f"wifi_ssid,data,string,{ssid}",
        f"wifi_psk,data,string,{password}",
    ]
    for name in ("ca_cls", "ca_mldsa", "crt_cls", "key_cls", "crt_mldsa", "key_mldsa"):
        rows.append(f"{name},file,binary,{blobs[name]}")
    return "\n".join(rows) + "\n"


def build_partition(work, csv_text, out_path):
    csv_path = os.path.join(work, "identity.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(csv_text)
    os.chmod(csv_path, 0o600)
    issue_cert._run([sys.executable, "-m", "esp_idf_nvs_partition_gen", "generate",
                     csv_path, out_path, PARTITION_SIZE])
    if os.path.getsize(out_path) != int(PARTITION_SIZE, 16):
        raise RuntimeError("NVS partition image has the wrong size")


def build_identity(serial, classical_ca_arn, mldsa_ca_arn, ssid, password,
                   out_path, acm_pca_client=None):
    """Issue both device certs and write the certs partition image to out_path.
    Returns a summary with no secret material."""
    serial = validate_serial(serial)
    validate_wifi(ssid, password)
    cn = f"esp32-{serial}"
    # A fresh token per run: re-registering a device mints a new key, and ACM PCA
    # would otherwise return the previous cert (for the old key) within its
    # idempotency window.
    run_id = uuid.uuid4().hex[:12]

    identity = {
        "classical": issue_cert.issue_certificate(
            common_name=cn, key_mode="ec-p256", ca_arn=classical_ca_arn,
            subject_ou="IoT Device", idempotency_token=f"idc-{serial}-{run_id}",
            acm_pca_client=acm_pca_client),
        "mldsa": issue_cert.issue_certificate(
            common_name=cn, key_mode="mldsa44", ca_arn=mldsa_ca_arn,
            subject_ou="IoT Device PQC ML-DSA", idempotency_token=f"idm-{serial}-{run_id}",
            acm_pca_client=acm_pca_client),
    }

    with tempfile.TemporaryDirectory() as work:
        blobs = to_der(work, identity)
        sizes = {k: os.path.getsize(v) for k, v in blobs.items()}
        build_partition(work, build_csv(serial, ssid, password, blobs), out_path)

    return {
        "serial": serial,
        "format": IDENTITY_FORMAT,
        "classicalCertArn": identity["classical"]["certificateArn"],
        "mldsaCertArn": identity["mldsa"]["certificateArn"],
        "blobSizes": sizes,
    }


def upload(path, bucket, serial, s3_client=None):
    import boto3
    s3 = s3_client or boto3.client("s3")
    key = s3_key(serial)
    with open(path, "rb") as f:
        resp = s3.put_object(Bucket=bucket, Key=key, Body=f,
                             ContentType="application/octet-stream")
    return {"bucket": bucket, "key": key, "versionId": resp.get("VersionId")}


def main(argv=None):
    p = argparse.ArgumentParser(description="Build and upload a device identity partition.")
    p.add_argument("--serial", default=os.environ.get("DEVICE_SERIAL"))
    p.add_argument("--classical-ca-arn", default=os.environ.get("CA_ARN"))
    p.add_argument("--mldsa-ca-arn", default=os.environ.get("MLDSA_CA_ARN"))
    p.add_argument("--bucket", default=os.environ.get("FIRMWARE_BUCKET"))
    p.add_argument("--out-file", default=None, help="also/instead write the image here")
    p.add_argument("--no-upload", action="store_true")
    a = p.parse_args(argv)

    for name, val in (("serial", a.serial), ("classical CA ARN", a.classical_ca_arn),
                      ("ML-DSA CA ARN", a.mldsa_ca_arn)):
        if not val:
            p.error(f"missing {name}")
    if not a.no_upload and not a.bucket:
        p.error("missing bucket (or pass --no-upload)")

    with tempfile.TemporaryDirectory() as work:
        out_path = a.out_file or os.path.join(work, "certs.bin")
        summary = build_identity(
            a.serial, a.classical_ca_arn, a.mldsa_ca_arn,
            os.environ.get("WIFI_SSID", ""), os.environ.get("WIFI_PASSWORD", ""),
            out_path)
        if not a.no_upload:
            summary["s3"] = upload(out_path, a.bucket, summary["serial"])
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
