# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Prepare Build Lambda — first work step of the PQC migration state machine.

Given a thingName (and optional variant), this:
  1. Looks up the device in the DynamoDB registry (keyed by serialNumber).
  2. Resolves the variant to its firmware version (variants.json).
  3. Checks that the SHARED image for that version exists in S3 at
     shared/<version>/firmware.bin.
  4. Returns routing data for the state machine:
       { thingName, serialNumber, variant, buildVariant, version, signerEra,
         sigAlg, s3Key }

Firmware images carry no device identity (keys, certs and WiFi live in each
device's "certs" NVS partition), so every variant is built and signed ONCE per
deployment, into shared/<version>/, by build-shared-firmware.sh (chained off
the firmware-builder image build). A migration therefore never builds: it only
publishes the prebuilt signed image. If the shared image is missing the
deployment is incomplete, and this Lambda fails the migration with an
actionable message rather than starting a build (concurrent builds of one
version could pair a firmware.bin with another build's signature).

Environment variables:
  REGISTRY_TABLE   — DynamoDB device registry table
  FIRMWARE_BUCKET  — S3 bucket for firmware
"""
import json
import os

import boto3

dynamodb = boto3.resource("dynamodb")

SHARED_PREFIX = "shared"
_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_variants():
    """Variant table, shared with build-shared-firmware.sh (one source of truth)."""
    with open(os.path.join(_HERE, "variants.json"), encoding="utf-8") as f:
        return json.load(f)["variants"]


# Picker variant key → {build_variant, version, signer_era, sig_alg}. The version
# names the shared S3 dir and the OTA job, so each variant has its own version
# namespace and a device can roll back one step. build_variant selects the
# FIRMWARE_VARIANT define/port in buildspec.yml: "signer"/"verified" build the same pqc-mldsa
# firmware as 3.0.0 and differ only in their OTA signer (ML-DSA-44).
VARIANT_CONFIG = _load_variants()
DEFAULT_VARIANT = "pqc"  # backward-compatible with the original migrate-to-PQC flow


class SharedFirmwareMissing(Exception):
    """The shared image for a variant is not in S3 (deployment incomplete)."""


def shared_key(version, name="firmware.bin"):
    return f"{SHARED_PREFIX}/{version}/{name}"


def _table():
    return dynamodb.Table(os.environ["REGISTRY_TABLE"])


def resolve_variant(event):
    """Pick the firmware variant from the event, defaulting to pqc. Raises on an
    unknown variant so a bad dashboard/picker value fails loudly instead of
    silently pushing the wrong firmware."""
    variant = (event.get("variant") or DEFAULT_VARIANT).strip()
    if variant not in VARIANT_CONFIG:
        raise ValueError(
            f"Unknown firmware variant '{variant}'. "
            f"Expected one of: {', '.join(sorted(VARIANT_CONFIG))}"
        )
    return variant


def handler(event, context):
    """Look up the device and point the migration at the shared signed image."""
    print(f"Event: {json.dumps(event)}")

    thing_name = event.get("thingName")
    if not thing_name:
        raise ValueError("Missing thingName")

    variant = resolve_variant(event)
    cfg = VARIANT_CONFIG[variant]
    version = cfg["version"]
    signer_era = cfg.get("signer_era")
    sig_alg = cfg.get("sig_alg")
    build_variant = cfg.get("build_variant", variant)

    serial_number = (
        thing_name.replace("esp32-", "") if thing_name.startswith("esp32-") else thing_name
    )

    # 1. Registry lookup: only registered devices can be migrated.
    response = _table().get_item(Key={"serialNumber": serial_number})
    if "Item" not in response:
        raise ValueError(f"Device {thing_name} not found in registry")

    # 2. The shared image must already exist (built once per deployment).
    s3_key = shared_key(version)
    if not _firmware_exists(s3_key):
        raise SharedFirmwareMissing(
            f"Shared firmware {version} is not built yet "
            f"(s3://{os.environ['FIRMWARE_BUCKET']}/{s3_key} missing). It is built "
            f"automatically after the firmware-builder image build; to rebuild it, "
            f"run raw_device_builder/codebuild/build-shared-firmware.sh."
        )

    return {
        "thingName": thing_name,
        "serialNumber": serial_number,
        "variant": variant,
        "buildVariant": build_variant,
        "version": version,
        # Empty string (not None) so the state machine never sees a null path.
        "signerEra": signer_era or "",
        "sigAlg": sig_alg or "",
        "s3Key": s3_key,
    }


def _firmware_exists(s3_key):
    s3 = boto3.client("s3")
    try:
        s3.head_object(Bucket=os.environ["FIRMWARE_BUCKET"], Key=s3_key)
        return True
    except Exception:
        return False
