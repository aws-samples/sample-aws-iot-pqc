"""
Unit tests for the message-logger Lambda (dashboard device log view source).

Asserts topic parsing (thingName + direction), TTL, and the DynamoDB put.
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

TABLE = "pqc-workshop-message-log"


def _env():
    os.environ["LOG_TABLE_NAME"] = TABLE


def _make_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "thingName", "KeyType": "HASH"},
            {"AttributeName": "timestamp", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "thingName", "AttributeType": "S"},
            {"AttributeName": "timestamp", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(TABLE)


@mock_aws
def test_inbound_telemetry_logged():
    tbl = _make_table()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "message_logger_index")

    mod.handler({"mqtttopic": "dt/esp32-FC22E0/telemetry", "message_id": 3}, None)
    items = tbl.scan()["Items"]
    assert len(items) == 1
    it = items[0]
    assert it["thingName"] == "esp32-FC22E0"
    assert it["direction"] == "inbound"
    assert it["topic"] == "dt/esp32-FC22E0/telemetry"
    assert "ttl" in it


@mock_aws
def test_outbound_command_logged():
    tbl = _make_table()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "message_logger_index2")
    mod.handler({"mqtttopic": "cmd/esp32-FC22E0/ota", "action": "ota-update"}, None)
    it = tbl.scan()["Items"][0]
    assert it["direction"] == "outbound"


@mock_aws
def test_unknown_topic_skipped():
    tbl = _make_table()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "message_logger_index3")
    result = mod.handler({"topic": "unknown"}, None)
    assert "skipped" in result["body"].lower()
    assert tbl.scan()["Count"] == 0
