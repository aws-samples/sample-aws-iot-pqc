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
FIRMWARE_BUCKET = "pqc-workshop-firmware-569757034460"


def _env():
    os.environ.update({
        "REGISTRY_TABLE": REGISTRY_TABLE,
        "DEVICE_TABLE": DEVICE_TABLE,
        "FIRMWARE_BUCKET": FIRMWARE_BUCKET,
        "BUILD_PROJECT": "pqc-workshop-build-firmware",
        "IOT_ENDPOINT": "example-ats.iot.us-east-1.amazonaws.com",
        "DEFAULT_WIFI_SSID": "reinvent-pqc-iot-wifi",
        "DEFAULT_WIFI_PASSWORD": "changeme",
        "DEFAULT_BROKER_ENDPOINT": "broker.example.com",
    })


def _setup_aws():
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
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=FIRMWARE_BUCKET)
    # The registrar creates the Thing with thingTypeName='esp32-pqc-device',
    # which must exist (the deployment's device-provisioning stack creates it).
    boto3.client("iot", region_name="us-east-1").create_thing_type(
        thingTypeName="esp32-pqc-device"
    )
    cb = boto3.client("codebuild", region_name="us-east-1")
    cb.create_project(
        name="pqc-workshop-build-firmware",
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
