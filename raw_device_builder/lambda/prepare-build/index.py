# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Prepare Build Lambda — first work step of the PQC migration state machine.

Given a thingName (and optional variant), this:
  1. Looks up the device in the DynamoDB registry (keyed by serialNumber).
  2. Checks whether the target firmware for that variant already exists in S3.
  3. If it does not, resolves the build env (the broker endpoint) so the
     state machine's WaitForBuild step can start the single CodeBuild.
  4. Returns routing data for the state machine:
       { thingName, serialNumber, variant, version, s3Key, buildNeeded, buildEnv }

This Lambda does NOT start a build itself — the state machine's WaitForBuild
(codebuild:startBuild.sync) is the single authoritative build. Starting one here
too previously caused a redundant second build for the same S3 key.

The firmware image carries no device identity: keys, certs, CA roots and WiFi
are in the device's "certs" NVS partition (written over USB at registration,
never touched by OTA). So no secret flows through this Lambda, the Step
Function state, or the firmware build. This Lambda is pure orchestration.

The state machine (not this Lambda) waits for the build to finish, via the
CodeBuild .sync integration — so this Lambda returns quickly and never blocks on
the build.

Environment variables:
  REGISTRY_TABLE          — DynamoDB device registry table
  FIRMWARE_BUCKET         — S3 bucket for firmware
  BUILD_PROJECT           — CodeBuild project name
  DEFAULT_BROKER_ENDPOINT — fallback broker endpoint for the build
"""
import json
import os

import boto3

dynamodb = boto3.resource("dynamodb")
ssm = boto3.client("ssm")

# Firmware variant → (FIRMWARE_VARIANT build value, firmware version / S3 key
# segment). The version is what distinguishes the S3 artifact and the OTA job,
# so each variant gets its own version namespace and can coexist on a device's
# history (enabling one-step rollback under the Option A OTA-slot strategy).
#   classical  → the initial flash variant (rarely re-built via migration)
#   pqc        → hybrid ML-KEM key exchange, classical (ECDSA) device cert
#   pqc-mldsa  → hybrid ML-KEM key exchange + ML-DSA-44 device cert (Step 2)
#
# OTA firmware signing (Workstream 1): each variant also declares which signer
# ERA signs its firmware image and the signature algorithm, so the build fetches
# the right signer key and the device knows how to verify.
#   signer_era: None       → unsigned (classical 1.0.0 is USB/trusted-flash only,
#                            never delivered OTA, so it carries no signature)
#   signer_era: "classical"→ ECDSA-P256 signer (classical CA); sig_alg ecdsa-p256
#   signer_era: "mldsa"    → ML-DSA-44 signer (ML-DSA CA);     sig_alg mldsa44
# The 2.0.0/3.0.0 OTA era uses the classical signer; the 4.0.0+ era (added in
# Workstream 4: 4.0.0-signer, 5.0.0-verified) uses the ML-DSA-44 signer.
VARIANT_CONFIG = {
    "classical": {
        "build_variant": "classical",
        "version": "1.0.0-classical",
        "signer_era": None,
        "sig_alg": None,
    },
    "pqc": {
        "build_variant": "pqc",
        "version": "2.0.0-pqc",
        "signer_era": "classical",
        "sig_alg": "ecdsa-p256",
    },
    "pqc-mldsa": {
        "build_variant": "pqc-mldsa",
        "version": "3.0.0-mldsa",
        "signer_era": "classical",
        "sig_alg": "ecdsa-p256",
    },
    # ML-DSA signer era (Workstream 4). Same firmware as pqc-mldsa (ML-KEM key
    # exchange + ML-DSA-44 device cert, 8884 listener) — what changes is the OTA
    # SIGNER: these images are signed by the ML-DSA-44 signer, so the device
    # verifies a post-quantum signature against the ML-DSA signer leaf (chaining
    # to the ML-DSA CA root baked in from 3.0.0 onward).
    #   4.0.0-signer   → first ML-DSA-signed OTA (the signer-era cutover)
    #   5.0.0-verified → the acceptance-proof variant (ML-DSA-signed, verified)
    "signer": {
        "build_variant": "pqc-mldsa",
        "version": "4.0.0-signer",
        "signer_era": "mldsa",
        "sig_alg": "mldsa44",
    },
    "verified": {
        "build_variant": "pqc-mldsa",
        "version": "5.0.0-verified",
        "signer_era": "mldsa",
        "sig_alg": "mldsa44",
    },
}
DEFAULT_VARIANT = "pqc"  # backward-compatible with the original migrate-to-PQC flow

# SSM is the authoritative live source of truth for the broker endpoint.
BROKER_SSM_PARAM = os.environ.get("BROKER_SSM_PARAM", "/pqc-workshop/broker/endpoint")


def _table():
    return dynamodb.Table(os.environ["REGISTRY_TABLE"])


def _get_ssm(name):
    """Read one SSM param; return None if missing/unreadable so the caller can
    fall back. Never raises."""
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as e:  # ParameterNotFound, AccessDenied, throttling, etc.
        print(f"SSM read {name} failed ({e}); falling back")
        return None


def broker_from_ssm():
    """Return the broker endpoint from SSM, or None if unavailable."""
    return _get_ssm(BROKER_SSM_PARAM)


def resolve_variant(event):
    """Pick the firmware variant from the event, defaulting to pqc. Raises on an
    unknown variant so a bad dashboard/picker value fails loudly instead of
    silently building the wrong firmware."""
    variant = (event.get("variant") or DEFAULT_VARIANT).strip()
    if variant not in VARIANT_CONFIG:
        raise ValueError(
            f"Unknown firmware variant '{variant}'. "
            f"Expected one of: {', '.join(sorted(VARIANT_CONFIG))}"
        )
    return variant


def handler(event, context):
    """Look up the device, ensure the requested variant's firmware is (being) built."""
    print(f"Event: {json.dumps(event)}")

    thing_name = event.get("thingName")
    if not thing_name:
        raise ValueError("Missing thingName")

    variant = resolve_variant(event)
    cfg = VARIANT_CONFIG[variant]
    version = cfg["version"]
    signer_era = cfg.get("signer_era")
    sig_alg = cfg.get("sig_alg")
    # The BUILD variant selects the sketch/CA/port in buildspec.yml. It can
    # differ from the picker's variant KEY: e.g. "signer"/"verified" (ML-DSA
    # signer era) build the SAME firmware as "pqc-mldsa" and only change the OTA
    # signer, so their build_variant is "pqc-mldsa". Falls back to the key for
    # the original variants where key == build_variant.
    build_variant = cfg.get("build_variant", variant)

    serial_number = (
        thing_name.replace("esp32-", "") if thing_name.startswith("esp32-") else thing_name
    )

    # 1. Registry lookup.
    response = _table().get_item(Key={"serialNumber": serial_number})
    if "Item" not in response:
        raise ValueError(f"Device {thing_name} not found in registry")
    device = response["Item"]

    # 2. Does this variant's firmware already exist?
    s3_key = f"{serial_number}/{version}/firmware.bin"
    if _firmware_exists(s3_key):
        return {
            "thingName": thing_name,
            "serialNumber": serial_number,
            "variant": variant,
            "buildVariant": build_variant,
            "version": version,
            # Signer era + algorithm so publish-ota/device know how the image is
            # signed. Empty string (not None) so the state machine's
            # EnvironmentVariablesOverride never dereferences a missing/null path.
            "signerEra": signer_era or "",
            "sigAlg": sig_alg or "",
            "s3Key": s3_key,
            "buildNeeded": False,
            # buildEnv is unused when buildNeeded is False, but always present so
            # the state machine's ResultSelector never references a missing path.
            "buildEnv": {"brokerEndpoint": ""},
        }

    # 3. Build needed. The state machine's WaitForBuild step starts + waits for
    #    the single CodeBuild (codebuild:startBuild.sync); this Lambda does NOT
    #    start a build itself (that previously caused a redundant second build).
    #    We resolve the build env (broker endpoint) here and return it for
    #    WaitForBuild.
    build_env = resolve_build_env(device)
    return {
        "thingName": thing_name,
        "serialNumber": serial_number,
        "variant": variant,
        "buildVariant": build_variant,
        "version": version,
        "signerEra": signer_era or "",
        "sigAlg": sig_alg or "",
        "s3Key": s3_key,
        "buildNeeded": True,
        "buildEnv": build_env,
    }


def _firmware_exists(s3_key):
    s3 = boto3.client("s3")
    try:
        s3.head_object(Bucket=os.environ["FIRMWARE_BUCKET"], Key=s3_key)
        return True
    except Exception:
        return False


def resolve_build_env(device):
    """Resolve the firmware build environment for WaitForBuild.

    Only the broker endpoint: the image is otherwise device-independent. WiFi
    and the device certs live in the device's "certs" NVS partition.
    """
    # Broker resolution: SSM (live source of truth) -> registry value -> env.
    broker_endpoint = (
        broker_from_ssm()
        or device.get("brokerEndpoint")
        or os.environ.get("DEFAULT_BROKER_ENDPOINT", "")
    )
    return {"brokerEndpoint": broker_endpoint}
