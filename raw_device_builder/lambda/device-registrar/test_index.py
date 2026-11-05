# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the device-registrar Lambda.

Key behavior under test: the registrar SEEDS the dashboard device table
(pqc-workshop-devices, keyed by thingName) at registration, in ADDITION to the
device registry table (keyed by serialNumber). The seed row must carry
serialNumber so the telemetry-processor's registered-only guard
(attribute_exists(serialNumber)) accepts the device's later telemetry. Without
this seed a registered device would never appear on the dashboard.
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


class _Ctx:
    """Minimal Lambda context stub — the handler reads invoked_function_arn to
    derive the account id for IoT policy ARNs."""
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:pqc-workshop-device-registrar"


REGISTRY_TABLE = "pqc-workshop-device-registry"
DEVICE_TABLE = "pqc-workshop-devices"
FIRMWARE_BUCKET = "pqc-workshop-firmware-123456789012"


def _env():
    os.environ.update({
        "REGISTRY_TABLE": REGISTRY_TABLE,
        "DEVICE_TABLE": DEVICE_TABLE,
        "FIRMWARE_BUCKET": FIRMWARE_BUCKET,
        "IDENTITY_PROJECT": "pqc-workshop-device-identity",
        "IOT_ENDPOINT": "example-ats.iot.us-east-1.amazonaws.com",
    })


def _setup_aws(shared_classical=True):
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=REGISTRY_TABLE,
        KeySchema=[{"AttributeName": "serialNumber", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "serialNumber", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName=DEVICE_TABLE,
        KeySchema=[{"AttributeName": "thingName", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "thingName", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=FIRMWARE_BUCKET)
    if shared_classical:
        # Built once per deployment by build-shared-firmware.sh.
        s3.put_object(Bucket=FIRMWARE_BUCKET, Key="shared/1.0.0-classical/firmware.bin", Body=b"x")
    # The registrar creates the Thing with thingTypeName='esp32-pqc-device',
    # which must exist (the deployment's device-provisioning stack creates it).
    boto3.client("iot", region_name="us-east-1").create_thing_type(
        thingTypeName="esp32-pqc-device"
    )
    cb = boto3.client("codebuild", region_name="us-east-1")
    for name in ("pqc-workshop-build-firmware", "pqc-workshop-device-identity"):
        cb.create_project(
            name=name,
            source={"type": "S3", "location": f"{FIRMWARE_BUCKET}/source/"},
            artifacts={"type": "NO_ARTIFACTS"},
            environment={
                "type": "LINUX_CONTAINER",
                "image": "aws/codebuild/standard:7.0",
                "computeType": "BUILD_GENERAL1_SMALL",
            },
            serviceRole="arn:aws:iam::123456789012:role/service-role/cb",
        )


@mock_aws
def test_registration_seeds_dashboard_row_with_serial():
    _setup_aws()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_registrar_index")

    result = mod.handler({"serialNumber": "C69E44"}, _Ctx())

    # Registration succeeded.
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["success"] is True
    assert body["thingName"] == "esp32-C69E44"

    ddb = boto3.resource("dynamodb", region_name="us-east-1")

    # Registry row (keyed by serialNumber) written as before.
    reg = ddb.Table(REGISTRY_TABLE).get_item(Key={"serialNumber": "C69E44"})["Item"]
    assert reg["status"] == "registered"

    # Dashboard row (keyed by thingName) SEEDED, and carries serialNumber so the
    # telemetry-processor guard will accept this device's telemetry.
    dev = ddb.Table(DEVICE_TABLE).get_item(Key={"thingName": "esp32-C69E44"})["Item"]
    assert dev["serialNumber"] == "C69E44"
    assert dev["status"] == "registered"
    assert "registeredAt" in dev
    assert dev["firmwareVersion"] == "1.0.0-classical"


@mock_aws
def test_seed_does_not_clobber_existing_telemetry_fields():
    """A re-register of a device that has already reported telemetry must keep
    the telemetry fields — the seed uses UpdateItem SET, not put_item."""
    _setup_aws()
    _env()
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    # Simulate a device that already has telemetry in the dashboard row.
    ddb.Table(DEVICE_TABLE).put_item(Item={
        "thingName": "esp32-C69E44",
        "serialNumber": "C69E44",
        "pqcAlgorithm": "SECP256R1_MLKEM768",
        "messageId": 42,
    })
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_registrar_index2")

    mod.handler({"serialNumber": "C69E44"}, _Ctx())

    dev = ddb.Table(DEVICE_TABLE).get_item(Key={"thingName": "esp32-C69E44"})["Item"]
    # Seed fields set...
    assert dev["status"] == "registered"
    assert "registeredAt" in dev
    # ...and pre-existing telemetry preserved.
    assert dev["pqcAlgorithm"] == "SECP256R1_MLKEM768"
    assert dev["messageId"] == 42


@mock_aws
def test_missing_serial_returns_400():
    _setup_aws()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_registrar_index3")
    result = mod.handler({}, None)
    assert result["statusCode"] == 400
    assert "error" in json.loads(result["body"])


@mock_aws
def test_registration_starts_identity_build_and_returns_its_key():
    """Registration starts the device-identity job (certs NVS image) as well as
    the firmware build, and tells the Pi where the identity image will land."""
    _setup_aws()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_registrar_index4")

    body = json.loads(mod.handler({"serialNumber": "C69DE0"}, _Ctx())["body"])

    assert body["identityKey"] == "identity/C69DE0/certs.bin"
    assert body["identityBuildId"].startswith("pqc-workshop-device-identity:")
    # No firmware build: the Pi flashes the shared classical image.
    assert body["buildId"] is None
    assert body["firmwarePrefix"] == "shared/1.0.0-classical"
    cb = boto3.client("codebuild", region_name="us-east-1")
    assert len(cb.list_builds_for_project(projectName="pqc-workshop-device-identity")["ids"]) == 1
    assert cb.list_builds_for_project(projectName="pqc-workshop-build-firmware")["ids"] == []


@mock_aws
def test_registry_does_not_store_wifi_password():
    """SSM is the single source of truth for the WiFi password; the registry
    must not keep a plaintext copy (threat model T013)."""
    _setup_aws()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_registrar_index5")

    mod.handler({"serialNumber": "C69DE0", "wifiPassword": "secret-pass-123"}, _Ctx())

    reg = boto3.resource("dynamodb", region_name="us-east-1").Table(REGISTRY_TABLE).get_item(
        Key={"serialNumber": "C69DE0"})["Item"]
    assert "wifiPassword" not in reg
    assert "secret-pass-123" not in json.dumps(reg, default=str)
    assert reg["identityKey"] == "identity/C69DE0/certs.bin"


@mock_aws
def test_invalid_serial_returns_400_without_starting_builds():
    _setup_aws()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_registrar_index6")

    for bad in ("c69de0", "C69DE0/../x", "AB", "esp32-C69DE0"):
        result = mod.handler({"serialNumber": bad}, _Ctx())
        assert result["statusCode"] == 400, bad

    cb = boto3.client("codebuild", region_name="us-east-1")
    assert cb.list_builds_for_project(projectName="pqc-workshop-device-identity")["ids"] == []


@mock_aws
def test_identity_only_starts_only_the_identity_build():
    """Reflash re-issues the identity without rebuilding firmware or touching
    the Thing/policy/registry."""
    _setup_aws()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_registrar_index7")
    mod.handler({"serialNumber": "C69DE0"}, _Ctx())  # initial registration

    result = mod.handler({"serialNumber": "C69DE0", "identityOnly": True}, _Ctx())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["identityKey"] == "identity/C69DE0/certs.bin"
    assert body["identityBuildId"].startswith("pqc-workshop-device-identity:")
    assert "buildId" not in body
    assert body["firmwarePrefix"] == "shared/1.0.0-classical"
    cb = boto3.client("codebuild", region_name="us-east-1")
    assert len(cb.list_builds_for_project(projectName="pqc-workshop-device-identity")["ids"]) == 2
    assert cb.list_builds_for_project(projectName="pqc-workshop-build-firmware")["ids"] == []


@mock_aws
def test_identity_only_rejects_unregistered_device():
    _setup_aws()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_registrar_index8")

    result = mod.handler({"serialNumber": "C69DE0", "identityOnly": True}, _Ctx())

    assert result["statusCode"] == 404
    cb = boto3.client("codebuild", region_name="us-east-1")
    assert cb.list_builds_for_project(projectName="pqc-workshop-device-identity")["ids"] == []


@mock_aws
def test_registration_starts_no_firmware_build_and_passes_no_secrets():
    """Firmware is shared and identity-free: the registrar starts only the
    identity job, with only the serial — even if a caller sends WiFi."""
    _setup_aws()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_registrar_index9")
    calls = []
    real_start = mod.codebuild.start_build

    def spy(**kwargs):
        calls.append(kwargs)
        return real_start(**kwargs)

    mod.codebuild.start_build = spy
    mod.handler({"serialNumber": "C69DE0", "wifiSsid": "x", "wifiPassword": "secret-pass-123"}, _Ctx())

    assert [c["projectName"] for c in calls] == ["pqc-workshop-device-identity"]
    assert [e["name"] for e in calls[0]["environmentVariablesOverride"]] == ["DEVICE_SERIAL"]
    assert "secret-pass-123" not in json.dumps(calls)


@mock_aws
@pytest.mark.parametrize("body", [{"serialNumber": "C69DE0"},
                                  {"serialNumber": "C69DE0", "identityOnly": True}])
def test_503_until_shared_classical_firmware_exists(body):
    """Both register and reflash flash the shared classical image, so they wait
    for it instead of leaving the Pi to fail mid-download."""
    _setup_aws(shared_classical=False)
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_registrar_index10")

    result = mod.handler(dict(body), _Ctx())

    assert result["statusCode"] == 503
    assert "1.0.0-classical" in json.loads(result["body"])["error"]
    cb = boto3.client("codebuild", region_name="us-east-1")
    assert cb.list_builds_for_project(projectName="pqc-workshop-device-identity")["ids"] == []
