# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the prepare-build Lambda.

prepare-build is the first work step of the PQC migration state machine. Given a
thingName (and optional variant) it:
  - looks up the device in the registry (keyed by serialNumber),
  - resolves the firmware variant → version from variants.json,
  - checks the SHARED image shared/<version>/firmware.bin exists,
  - returns routing info for the state machine: thingName, serialNumber,
    variant, buildVariant, version, signerEra, sigAlg, s3Key.
It never builds: every variant is built once per deployment into
shared/<version>/ (build-shared-firmware.sh). A missing shared image fails the
migration with an actionable error. Runs against moto to stay fast/offline.
"""
import json
import os
import sys

import boto3
import pytest
from moto import mock_aws

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, REPO_ROOT)
from conftest import load_handler_module  # noqa: E402

REGISTRY_TABLE = "pqc-workshop-device-registry"
FIRMWARE_BUCKET = "pqc-workshop-firmware-087195661091"

with open(os.path.join(HERE, "variants.json"), encoding="utf-8") as _f:
    VARIANTS = json.load(_f)["variants"]


def _env():
    os.environ.update({"REGISTRY_TABLE": REGISTRY_TABLE, "FIRMWARE_BUCKET": FIRMWARE_BUCKET})


def _seed(shared_versions=tuple(v["version"] for v in VARIANTS.values())):
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=REGISTRY_TABLE,
        KeySchema=[{"AttributeName": "serialNumber", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "serialNumber", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.Table(REGISTRY_TABLE).put_item(Item={"serialNumber": "FC22E0"})
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=FIRMWARE_BUCKET)
    for version in shared_versions:
        s3.put_object(Bucket=FIRMWARE_BUCKET, Key=f"shared/{version}/firmware.bin", Body=b"x")


def _load(name="prepare_build_index"):
    _env()
    return load_handler_module(os.path.join(HERE, "index.py"), name)


@mock_aws
def test_default_variant_is_pqc_and_uses_shared_image():
    _seed()
    mod = _load()
    result = mod.handler({"thingName": "esp32-FC22E0"}, None)
    assert result["variant"] == "pqc"
    assert result["version"] == "2.0.0-pqc"
    assert result["s3Key"] == "shared/2.0.0-pqc/firmware.bin"
    assert result["serialNumber"] == "FC22E0"
    assert result["signerEra"] == "classical"
    assert result["sigAlg"] == "ecdsa-p256"
    # No build routing any more: migrations only publish the shared image.
    for gone in ("buildNeeded", "buildEnv", "buildId"):
        assert gone not in result


@mock_aws
@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_every_variant_maps_to_its_shared_image(variant):
    _seed()
    mod = _load(f"prepare_build_index_{variant.replace('-', '_')}")
    cfg = VARIANTS[variant]
    result = mod.handler({"thingName": "esp32-FC22E0", "variant": variant}, None)
    assert result["version"] == cfg["version"]
    assert result["buildVariant"] == cfg["build_variant"]
    assert result["s3Key"] == f"shared/{cfg['version']}/firmware.bin"
    # Empty string, not None, so the state machine never sees a null path.
    assert result["signerEra"] == (cfg["signer_era"] or "")
    assert result["sigAlg"] == (cfg["sig_alg"] or "")


def test_variant_table_matches_the_known_firmware():
    """The table is shared with build-shared-firmware.sh; pin its contract."""
    assert {k: v["version"] for k, v in VARIANTS.items()} == {
        "classical": "1.0.0-classical", "pqc": "2.0.0-pqc", "pqc-mldsa": "3.0.0-mldsa",
        "signer": "4.0.0-signer", "verified": "5.0.0-verified"}
    # ML-DSA signer era builds the same pqc-mldsa sketch; only the signer differs.
    assert VARIANTS["signer"]["build_variant"] == VARIANTS["verified"]["build_variant"] == "pqc-mldsa"
    assert VARIANTS["classical"]["signer_era"] is None


@mock_aws
def test_missing_shared_image_fails_with_actionable_error():
    """No per-device fallback build (concurrent builds of one version could
    pair a firmware.bin with another build's signature)."""
    _seed(shared_versions=("2.0.0-pqc",))
    mod = _load()
    with pytest.raises(mod.SharedFirmwareMissing) as exc:
        mod.handler({"thingName": "esp32-FC22E0", "variant": "pqc-mldsa"}, None)
    assert "3.0.0-mldsa" in str(exc.value)
    assert "build-shared-firmware.sh" in str(exc.value)


@mock_aws
def test_unknown_variant_raises():
    _seed()
    mod = _load()
    with pytest.raises(ValueError) as exc:
        mod.handler({"thingName": "esp32-FC22E0", "variant": "bogus"}, None)
    assert "unknown firmware variant" in str(exc.value).lower()


@mock_aws
def test_unregistered_device_raises():
    _seed()
    mod = _load()
    with pytest.raises(ValueError) as exc:
        mod.handler({"thingName": "esp32-UNKNOWN"}, None)
    assert "not found" in str(exc.value).lower()


@mock_aws
def test_missing_thingname_raises():
    _seed()
    mod = _load()
    with pytest.raises(Exception):
        mod.handler({}, None)
