# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the device-deregistrar Lambda.

Includes the REGRESSION test for the placeholder-stub bug: the deregistrar was
once a stub returning {'body': 'placeholder'}, which broke the GUI with an
"Unexpected token" JSON parse error. These tests assert the handler always
returns an API Gateway proxy response whose body is valid JSON, and that it
performs the documented IoT / DynamoDB / S3 deletions.
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
FIRMWARE_BUCKET = "pqc-workshop-firmware-123456789012"


def _env():
    os.environ.update({"REGISTRY_TABLE": REGISTRY_TABLE, "FIRMWARE_BUCKET": FIRMWARE_BUCKET})


def _seed_registry_and_bucket():
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
    s3.put_object(Bucket=FIRMWARE_BUCKET, Key="FC22E0/1.0.0/firmware.bin", Body=b"x")


def _assert_valid_proxy_response(result):
    """Every response must be a valid API Gateway proxy response with JSON body."""
    assert "statusCode" in result
    assert "body" in result
    # The body must be a JSON *string* that parses — this is the regression guard.
    assert isinstance(result["body"], str)
    parsed = json.loads(result["body"])  # raises if invalid JSON
    return parsed


@mock_aws
def test_deregister_returns_valid_json_and_deletes(monkeypatch):
    _seed_registry_and_bucket()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_deregistrar_index")

    # moto's IoT doesn't have the thing/cert, which is fine: the handler treats
    # those as non-critical errors and still returns success. We assert the
    # DynamoDB + S3 deletions happen and the response is valid JSON.
    result = mod.handler({"serialNumber": "FC22E0"}, None)

    parsed = _assert_valid_proxy_response(result)
    assert parsed["success"] is True
    assert parsed["thingName"] == "esp32-FC22E0"

    # Registry row deleted.
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    got = ddb.Table(REGISTRY_TABLE).get_item(Key={"serialNumber": "FC22E0"})
    assert "Item" not in got

    # Firmware deleted from S3.
    s3 = boto3.client("s3", region_name="us-east-1")
    listing = s3.list_objects_v2(Bucket=FIRMWARE_BUCKET, Prefix="FC22E0/")
    assert listing.get("KeyCount", 0) == 0


@mock_aws
def test_missing_serial_returns_valid_json_400(monkeypatch):
    _seed_registry_and_bucket()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_deregistrar_index2")
    result = mod.handler({}, None)
    parsed = _assert_valid_proxy_response(result)
    assert result["statusCode"] == 400
    assert "error" in parsed


@mock_aws
def test_apigateway_body_wrapped_event(monkeypatch):
    """When invoked via API Gateway proxy, the serialNumber is inside event['body']."""
    _seed_registry_and_bucket()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_deregistrar_index3")
    event = {"body": json.dumps({"serialNumber": "FC22E0"})}
    result = mod.handler(event, None)
    parsed = _assert_valid_proxy_response(result)
    assert parsed["success"] is True


@mock_aws
def test_handler_never_returns_placeholder(monkeypatch):
    """Explicit guard against the old stub that returned body='placeholder'."""
    _seed_registry_and_bucket()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "device_deregistrar_index4")
    result = mod.handler({"serialNumber": "FC22E0"}, None)
    assert result["body"] != "placeholder"
    json.loads(result["body"])  # must be valid JSON, not the stub string
