"""
Unit tests for the prepare-build Lambda.

prepare-build is the first work step of the PQC migration state machine. Given a
thingName (and optional variant) it:
  - looks up the device in the registry (keyed by serialNumber),
  - resolves the firmware variant → version (classical / pqc / pqc-mldsa),
  - checks whether that variant's firmware already exists in S3,
  - resolves the per-device build env (WiFi/broker/IoT) for WaitForBuild,
  - returns routing info for the state machine: buildNeeded, buildEnv, s3Key,
    thingName, serialNumber, variant, version.
  It does NOT start a build itself (the state machine's WaitForBuild does), so
  there is no CodeBuild to mock — this Lambda is pure orchestration.

Certificate issuance moved INTO the firmware CodeBuild (shared cert-issuer), so
this Lambda touches no crypto. Runs against moto to stay fast/offline.
"""
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

# Keep in sync with prepare-build/index.py VARIANT_CONFIG.
PQC_VERSION = "2.0.0-pqc"
MLDSA_VERSION = "3.0.0-mldsa"
CLASSICAL_VERSION = "1.0.0-classical"


def _env():
    os.environ.update(
        {
            "REGISTRY_TABLE": REGISTRY_TABLE,
            "FIRMWARE_BUCKET": FIRMWARE_BUCKET,
            "BUILD_PROJECT": BUILD_PROJECT,
            "IOT_ENDPOINT": "example-ats.iot.us-east-1.amazonaws.com",
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


def _load():
    _env()
    return load_handler_module(os.path.join(HERE, "index.py"), "prepare_build_index")


@mock_aws
def test_default_variant_is_pqc(monkeypatch):
    """No variant in event → defaults to pqc (backward compatible with the
    original migrate-to-PQC flow). prepare-build does NOT start a build itself
    (the state machine's WaitForBuild does); it resolves the build env + variant."""
    _seed_registry()
    mod = _load()

    result = mod.handler({"thingName": "esp32-FC22E0"}, None)

    assert result["variant"] == "pqc"
    assert result["version"] == PQC_VERSION
    assert result["buildNeeded"] is True
    assert result["s3Key"] == f"FC22E0/{PQC_VERSION}/firmware.bin"
    # Resolved per-device build env is returned for WaitForBuild to use.
    assert "buildEnv" in result
    assert result["buildEnv"]["brokerEndpoint"] == "broker.example.com"
    # No cert material and no buildId (no build is started here).
    assert "buildId" not in result
    for cert_key in ("caCert", "cert", "key"):
        assert cert_key not in result["buildEnv"]


@mock_aws
def test_mldsa_variant_uses_mldsa_version(monkeypatch):
    """variant=pqc-mldsa → distinct version/S3 key returned for the build."""
    _seed_registry()
    mod = _load()

    result = mod.handler({"thingName": "esp32-FC22E0", "variant": "pqc-mldsa"}, None)

    assert result["variant"] == "pqc-mldsa"
    assert result["version"] == MLDSA_VERSION
    assert result["s3Key"] == f"FC22E0/{MLDSA_VERSION}/firmware.bin"
    assert result["buildNeeded"] is True
    assert "buildEnv" in result


@mock_aws
def test_classical_variant(monkeypatch):
    _seed_registry()
    mod = _load()
    result = mod.handler({"thingName": "esp32-FC22E0", "variant": "classical"}, None)
    assert result["variant"] == "classical"
    assert result["version"] == CLASSICAL_VERSION
    assert result["buildNeeded"] is True


@mock_aws
def test_unknown_variant_raises(monkeypatch):
    _seed_registry()
    mod = _load()
    with pytest.raises(ValueError) as exc:
        mod.handler({"thingName": "esp32-FC22E0", "variant": "bogus"}, None)
    assert "unknown firmware variant" in str(exc.value).lower()


@mock_aws
def test_build_skipped_when_firmware_exists(monkeypatch):
    """If the variant's firmware already exists in S3, buildNeeded is False."""
    _seed_registry()
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=FIRMWARE_BUCKET)
    s3.put_object(Bucket=FIRMWARE_BUCKET, Key=f"FC22E0/{MLDSA_VERSION}/firmware.bin", Body=b"x")

    mod = _load()
    result = mod.handler({"thingName": "esp32-FC22E0", "variant": "pqc-mldsa"}, None)
    assert result["buildNeeded"] is False
    assert result["version"] == MLDSA_VERSION
    # buildEnv is always present (empty) so the state machine ResultSelector
    # never references a missing path.
    assert "buildEnv" in result


@mock_aws
def test_missing_device_raises(monkeypatch):
    _seed_registry()
    mod = _load()
    with pytest.raises(Exception) as exc:
        mod.handler({"thingName": "esp32-UNKNOWN"}, None)
    assert "not found" in str(exc.value).lower()


@mock_aws
def test_missing_thingname_raises(monkeypatch):
    _seed_registry()
    mod = _load()
    with pytest.raises(Exception):
        mod.handler({}, None)
