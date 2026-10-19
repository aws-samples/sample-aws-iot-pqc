# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the publish-ota Lambda.

publish-ota is the final work step of the PQC migration state machine. Given
{thingName, serialNumber, s3Key} it:
  - reads the firmware size (head_object) and computes its sha256,
  - generates a regional presigned S3 GET URL (s3v4, virtual addressing),
  - publishes an ota-update command to cmd/<thingName>/ota,
  - marks the registry item targetFirmwareVersion + status=ota-pending.

All AWS mocked. We assert the published topic/payload and the presign call
parameters, since those exact details (regional endpoint, cmd/<thing>/ota) were
hard-won OTA fixes.
"""
import json
import os
import sys
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, REPO_ROOT)
from conftest import load_handler_module  # noqa: E402

REGISTRY_TABLE = "pqc-workshop-device-registry"
FIRMWARE_BUCKET = "pqc-workshop-firmware-087195661091"
PQC_VERSION = "2.0.0-pqc"


def _env():
    os.environ.update(
        {
            "REGISTRY_TABLE": REGISTRY_TABLE,
            "FIRMWARE_BUCKET": FIRMWARE_BUCKET,
            "IOT_ENDPOINT": "example-ats.iot.us-east-1.amazonaws.com",
        }
    )


def _seed(sig_artifacts=False):
    """Seed the registry row + firmware object. When sig_artifacts is True, also
    seed the detached signature + signer cert the signed-variant path presigns."""
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
    s3.put_object(
        Bucket=FIRMWARE_BUCKET,
        Key=f"FC22E0/{PQC_VERSION}/firmware.bin",
        Body=b"firmware-bytes",
    )
    if sig_artifacts:
        s3.put_object(
            Bucket=FIRMWARE_BUCKET,
            Key=f"FC22E0/{PQC_VERSION}/firmware.bin.sig",
            Body=b"\x30\x45signature-der-bytes",
        )
        s3.put_object(
            Bucket=FIRMWARE_BUCKET,
            Key=f"FC22E0/{PQC_VERSION}/signer.crt",
            Body=b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
        )


@mock_aws
def test_publishes_ota_command_and_updates_registry(monkeypatch):
    _seed()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "publish_ota_index")

    # Capture the IoT publish call.
    published = {}

    def fake_publish(topic, qos, payload):
        published["topic"] = topic
        published["qos"] = qos
        published["payload"] = json.loads(payload)

    monkeypatch.setattr(mod.iot_data, "publish", fake_publish, raising=True)

    event = {"thingName": "esp32-FC22E0", "serialNumber": "FC22E0",
             "s3Key": f"FC22E0/{PQC_VERSION}/firmware.bin"}
    result = mod.handler(event, None)

    assert result["success"] is True
    assert result["thingName"] == "esp32-FC22E0"

    # Correct topic and ota-update action.
    assert published["topic"] == "cmd/esp32-FC22E0/ota"
    assert published["qos"] == 1
    assert published["payload"]["action"] == "ota-update"
    assert published["payload"]["url"].startswith("https://")
    assert published["payload"]["version"] == PQC_VERSION
    assert published["payload"]["size"] == len(b"firmware-bytes")
    # sha256 of the firmware bytes is present.
    assert len(published["payload"]["sha256"]) == 64
    # No sigAlg in the event → unsigned push → NO signature fields (legacy shape).
    assert "sigUrl" not in published["payload"]
    assert "sigAlg" not in published["payload"]
    assert "signerCertUrl" not in published["payload"]

    # Registry updated to ota-pending.
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(REGISTRY_TABLE).get_item(Key={"serialNumber": "FC22E0"})["Item"]
    assert item["status"] == "ota-pending"
    assert item["targetFirmwareVersion"] == PQC_VERSION


@mock_aws
def test_signed_variant_adds_signature_fields(monkeypatch):
    """A signed variant (sigAlg set) adds sigUrl/sigAlg/signerCertUrl, presigned
    from the sibling .sig and signer.crt, while preserving the firmware fields."""
    _seed(sig_artifacts=True)
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "publish_ota_signed")

    published = {}

    def fake_publish(topic, qos, payload):
        published["payload"] = json.loads(payload)

    monkeypatch.setattr(mod.iot_data, "publish", fake_publish, raising=True)

    event = {
        "thingName": "esp32-FC22E0",
        "serialNumber": "FC22E0",
        "s3Key": f"FC22E0/{PQC_VERSION}/firmware.bin",
        "version": PQC_VERSION,
        "sigAlg": "ecdsa-p256",
    }
    result = mod.handler(event, None)
    assert result["success"] is True

    p = published["payload"]
    # Firmware fields preserved.
    assert p["action"] == "ota-update"
    assert p["url"].startswith("https://")
    assert len(p["sha256"]) == 64
    # Signature fields present and pointing at the sibling artifacts.
    assert p["sigAlg"] == "ecdsa-p256"
    assert p["sigUrl"].startswith("https://")
    assert "firmware.bin.sig" in p["sigUrl"]
    assert p["signerCertUrl"].startswith("https://")
    assert "signer.crt" in p["signerCertUrl"]


@mock_aws
def test_signed_variant_missing_signature_raises(monkeypatch):
    """If a variant declares a signature but the .sig never lands in S3, fail
    loudly rather than publishing an OTA the device cannot verify."""
    _seed(sig_artifacts=False)  # firmware present, but NO .sig / signer.crt
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "publish_ota_missing_sig")

    # Keep the test fast: don't actually sleep through the bounded retry.
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None, raising=True)
    monkeypatch.setattr(mod.iot_data, "publish", lambda **_: None, raising=True)

    event = {
        "thingName": "esp32-FC22E0",
        "serialNumber": "FC22E0",
        "s3Key": f"FC22E0/{PQC_VERSION}/firmware.bin",
        "version": PQC_VERSION,
        "sigAlg": "mldsa44",
    }
    with pytest.raises(RuntimeError) as exc:
        mod.handler(event, None)
    assert "signature not found" in str(exc.value).lower()


@mock_aws
def test_regional_presign_endpoint(monkeypatch):
    """The S3 client must use the regional endpoint + s3v4 (the OTA redirect fix)."""
    _seed()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "publish_ota_index2")
    endpoint = mod.s3.meta.endpoint_url
    assert "s3.us-east-1.amazonaws.com" in endpoint
    assert mod.s3.meta.config.signature_version == "s3v4"
