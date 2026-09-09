"""
Unit tests for the prepare-build Lambda.

prepare-build is the first work step of the PQC migration state machine. Given a
thingName it:
  - looks up the device in the registry (keyed by serialNumber),
  - checks whether the target PQC firmware already exists in S3,
  - if not, issues a device cert from the Private CA and starts a CodeBuild,
  - returns routing info for the state machine: buildNeeded, buildId, s3Key,
    thingName, serialNumber.

All AWS is mocked. The acm-pca issuance + local openssl/subprocess are patched
so the test stays fast and offline.
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
BUILD_PROJECT = "pqc-workshop-firmware-build"
PQC_VERSION = "1.0.0-pqc"


def _env():
    os.environ.update(
        {
            "REGISTRY_TABLE": REGISTRY_TABLE,
            "FIRMWARE_BUCKET": FIRMWARE_BUCKET,
            "BUILD_PROJECT": BUILD_PROJECT,
            "IOT_ENDPOINT": "example-ats.iot.us-east-1.amazonaws.com",
            "PRIVATE_CA_ARN": "arn:aws:acm-pca:us-east-1:087195661091:certificate-authority/abc",
            "DEFAULT_WIFI_SSID": "test-ssid",
            "DEFAULT_WIFI_PASSWORD": "test-pass",
            "DEFAULT_BROKER_ENDPOINT": "broker.example.com",
        }
    )


def _seed_registry():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=REGISTRY_TABLE,
        KeySchema=[{"AttributeName": "serialNumber", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "serialNumber", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.Table(REGISTRY_TABLE).put_item(
        Item={"serialNumber": "FC22E0", "brokerEndpoint": "broker.example.com"}
    )


def _load(monkeypatch):
    """Load prepare-build with cert-issuance and build-trigger internals patched."""
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "prepare_build_index")
    return mod


@mock_aws
def test_build_needed_triggers_codebuild(monkeypatch):
    _seed_registry()
    mod = _load(monkeypatch)

    # Patch the cert issuance (acm-pca + openssl subprocess) to avoid real crypto.
    monkeypatch.setattr(
        mod, "issue_device_cert",
        lambda serial: {"ca_cert": "CA", "device_cert": "CERT", "device_key": "KEY"},
        raising=True,
    )
    # Patch the build trigger to return a deterministic build id.
    monkeypatch.setattr(mod, "trigger_pqc_build", lambda serial, device, certs: "build-123", raising=True)

    result = mod.handler({"thingName": "esp32-FC22E0"}, None)

    assert result["thingName"] == "esp32-FC22E0"
    assert result["serialNumber"] == "FC22E0"
    assert result["buildNeeded"] is True
    assert result["buildId"] == "build-123"
    assert result["s3Key"] == f"FC22E0/{PQC_VERSION}/firmware.bin"


@mock_aws
def test_build_skipped_when_firmware_exists(monkeypatch):
    _seed_registry()
    # Pre-create the firmware object so buildNeeded is False.
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=FIRMWARE_BUCKET)
    s3.put_object(Bucket=FIRMWARE_BUCKET, Key=f"FC22E0/{PQC_VERSION}/firmware.bin", Body=b"x")

    mod = _load(monkeypatch)
    # If build were triggered this would raise, proving it was skipped.
    monkeypatch.setattr(mod, "trigger_pqc_build", lambda *a, **k: pytest.fail("should not build"), raising=True)

    result = mod.handler({"thingName": "esp32-FC22E0"}, None)
    assert result["buildNeeded"] is False
    assert result["buildId"] is None


@mock_aws
def test_missing_device_raises(monkeypatch):
    _seed_registry()
    mod = _load(monkeypatch)
    with pytest.raises(Exception) as exc:
        mod.handler({"thingName": "esp32-UNKNOWN"}, None)
    assert "not found" in str(exc.value).lower()


@mock_aws
def test_missing_thingname_raises(monkeypatch):
    _seed_registry()
    mod = _load(monkeypatch)
    with pytest.raises(Exception):
        mod.handler({}, None)
