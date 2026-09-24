"""
Unit tests for the prepare-build Lambda.

prepare-build is the first work step of the PQC migration state machine. Given a
thingName (and optional variant) it:
  - looks up the device in the registry (keyed by serialNumber),
  - resolves the firmware variant → version (classical / pqc / pqc-mldsa),
  - checks whether that variant's firmware already exists in S3,
  - if not, starts a CodeBuild (the build issues the device cert itself now),
  - returns routing info for the state machine: buildNeeded, buildId, s3Key,
    thingName, serialNumber, variant, version.

Certificate issuance moved INTO the firmware CodeBuild (shared cert-issuer), so
this Lambda is pure orchestration — there is nothing crypto to mock here. Only
the CodeBuild trigger is patched (or run against moto) to stay fast/offline.
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
PQC_VERSION = "1.0.0-pqc"
MLDSA_VERSION = "2.0.0-mldsa"
CLASSICAL_VERSION = "0.1.0"


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
def test_default_variant_is_pqc_and_triggers_build(monkeypatch):
    """No variant in event → defaults to pqc (backward compatible with the
    original migrate-to-PQC flow), and starts a build with FIRMWARE_VARIANT=pqc."""
    _seed_registry()
    mod = _load()

    captured = {}

    def fake_start_build(**kwargs):
        captured["projectName"] = kwargs["projectName"]
        captured["env"] = {e["name"]: e["value"] for e in kwargs["environmentVariablesOverride"]}
        return {"build": {"id": "build-123"}}

    monkeypatch.setattr(mod.codebuild, "start_build", fake_start_build, raising=True)

    result = mod.handler({"thingName": "esp32-FC22E0"}, None)

    assert result["variant"] == "pqc"
    assert result["version"] == PQC_VERSION
    assert result["buildNeeded"] is True
    assert result["buildId"] == "build-123"
    assert result["s3Key"] == f"FC22E0/{PQC_VERSION}/firmware.bin"
    # The build is told which variant to build, and NO cert material is passed.
    assert captured["env"]["FIRMWARE_VARIANT"] == "pqc"
    assert captured["projectName"] == BUILD_PROJECT
    for cert_key in ("CA_CERT", "DEVICE_CERT", "DEVICE_KEY"):
        assert cert_key not in captured["env"], f"{cert_key} must not be passed from the Lambda"
    # BROKER_PORT is intentionally left to the build's variant default.
    assert "BROKER_PORT" not in captured["env"]


@mock_aws
def test_mldsa_variant_uses_mldsa_version(monkeypatch):
    """variant=pqc-mldsa → distinct version/S3 key + FIRMWARE_VARIANT=pqc-mldsa."""
    _seed_registry()
    mod = _load()

    captured = {}
    monkeypatch.setattr(
        mod.codebuild, "start_build",
        lambda **kw: (captured.update({e["name"]: e["value"] for e in kw["environmentVariablesOverride"]}) or {"build": {"id": "build-mldsa"}}),
        raising=True,
    )

    result = mod.handler({"thingName": "esp32-FC22E0", "variant": "pqc-mldsa"}, None)

    assert result["variant"] == "pqc-mldsa"
    assert result["version"] == MLDSA_VERSION
    assert result["s3Key"] == f"FC22E0/{MLDSA_VERSION}/firmware.bin"
    assert result["buildId"] == "build-mldsa"
    assert captured["FIRMWARE_VARIANT"] == "pqc-mldsa"


@mock_aws
def test_classical_variant(monkeypatch):
    _seed_registry()
    mod = _load()
    captured = {}
    monkeypatch.setattr(
        mod.codebuild, "start_build",
        lambda **kw: (captured.update({e["name"]: e["value"] for e in kw["environmentVariablesOverride"]}) or {"build": {"id": "b"}}),
        raising=True,
    )
    result = mod.handler({"thingName": "esp32-FC22E0", "variant": "classical"}, None)
    assert result["variant"] == "classical"
    assert result["version"] == CLASSICAL_VERSION
    assert captured["FIRMWARE_VARIANT"] == "classical"


@mock_aws
def test_unknown_variant_raises(monkeypatch):
    _seed_registry()
    mod = _load()
    with pytest.raises(ValueError) as exc:
        mod.handler({"thingName": "esp32-FC22E0", "variant": "bogus"}, None)
    assert "unknown firmware variant" in str(exc.value).lower()


@mock_aws
def test_build_skipped_when_firmware_exists(monkeypatch):
    """If the variant's firmware already exists in S3, no build is started."""
    _seed_registry()
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=FIRMWARE_BUCKET)
    s3.put_object(Bucket=FIRMWARE_BUCKET, Key=f"FC22E0/{MLDSA_VERSION}/firmware.bin", Body=b"x")

    mod = _load()
    monkeypatch.setattr(
        mod.codebuild, "start_build",
        lambda **k: pytest.fail("should not build when firmware exists"),
        raising=True,
    )

    result = mod.handler({"thingName": "esp32-FC22E0", "variant": "pqc-mldsa"}, None)
    assert result["buildNeeded"] is False
    assert result["buildId"] is None
    assert result["version"] == MLDSA_VERSION


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
