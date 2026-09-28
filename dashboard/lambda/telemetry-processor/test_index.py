"""
Unit tests for the telemetry-processor Lambda.

Key behavior under test: it uses UpdateItem (not put_item), so it updates only
the fields present in the event and PRESERVES other fields — critically
migrationStatus, which the migration state machine writes. A put_item-based
processor would clobber migrationStatus on every telemetry message.
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

TABLE = "pqc-workshop-devices"


def _env():
    os.environ.update({"TABLE_NAME": TABLE, "APPSYNC_ENDPOINT": ""})


def _make_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "thingName", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "thingName", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(TABLE)


@mock_aws
def test_telemetry_event_writes_fields():
    tbl = _make_table()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_index")

    event = {
        "device": "esp32-FC22E0", "message_id": 5, "uptime_sec": 100,
        "wifi_rssi": -50, "free_heap": 200000, "heap_total": 327680,
        "min_heap": 180000, "max_alloc_heap": 110000,
        "sketch_size": 1200000, "free_sketch_space": 1900000,
        "psram_size": 0, "psram_free": 0,
        "chip_temp_c": 42, "pqc_algorithm": "SECP256R1_MLKEM768",
        "tls_version": "TLSv1.3", "firmware_version": "3.0.0-mldsa",
        "cert_sig_alg": "ML-DSA-44",
    }
    mod.handler(event, None)

    item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
    assert item["pqcAlgorithm"] == "SECP256R1_MLKEM768"
    assert item["tlsVersion"] == "TLSv1.3"
    assert item["certSigAlg"] == "ML-DSA-44"
    # firmwareVersion is now carried on the periodic telemetry payload (not only
    # the connect-time lifecycle message), so the dashboard shows it reliably.
    assert item["firmwareVersion"] == "3.0.0-mldsa"
    # Operational memory/storage metrics land from the telemetry payload.
    assert item["heapTotal"] == 327680
    assert item["maxAllocHeap"] == 110000
    assert item["freeSketchSpace"] == 1900000
    assert item["sketchSize"] == 1200000
    # PSRAM is reported even when disabled (0) so the panel is self-documenting.
    assert item["psramSize"] == 0
    assert item["status"] == "online"
    assert item["messageId"] == 5
    assert "lastSeen" in item and "ttl" in item


@mock_aws
def test_lifecycle_event_writes_status_and_ip():
    tbl = _make_table()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_index2")

    mod.handler(
        {
            "thingName": "esp32-FC22E0", "status": "online", "ip": "10.0.0.5",
            "pqc_algorithm": "SECP256R1_MLKEM768", "tls_version": "TLSv1.3",
            "cert_sig_alg": "ECDSA-P256",
        },
        None,
    )
    item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
    assert item["status"] == "online"
    assert item["ipAddress"] == "10.0.0.5"
    assert item["certSigAlg"] == "ECDSA-P256"


@mock_aws
def test_preserves_migration_status():
    """The critical guard: a telemetry update must NOT wipe migrationStatus."""
    tbl = _make_table()
    tbl.put_item(Item={"thingName": "esp32-FC22E0", "migrationStatus": "building"})
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_index3")

    mod.handler({"device": "esp32-FC22E0", "message_id": 1, "pqc_algorithm": "none"}, None)
    item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
    # migrationStatus survives because the processor uses UpdateItem, not put_item.
    # It is ALSO preserved through "building" (the device is still on the OLD
    # firmware here) — only "rebooting" is reconciled (see the tests below).
    assert item["migrationStatus"] == "building"
    assert item["pqcAlgorithm"] == "none"


@mock_aws
def test_reconciles_rebooting_migration_on_telemetry():
    """A device that reports in while migrationStatus=='rebooting' has finished
    its reboot into the new firmware, so the processor clears migrationStatus /
    migrationError — otherwise the dashboard hides the picker forever and the
    operator can't start the next migration."""
    tbl = _make_table()
    tbl.put_item(Item={
        "thingName": "esp32-FC22E0",
        "migrationStatus": "rebooting",
        "migrationError": "",
    })
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_reconcile")

    mod.handler(
        {"device": "esp32-FC22E0", "message_id": 1, "pqc_algorithm": "SECP256R1_MLKEM768"},
        None,
    )
    item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
    assert "migrationStatus" not in item
    assert "migrationError" not in item
    assert item["pqcAlgorithm"] == "SECP256R1_MLKEM768"


@mock_aws
def test_reconciles_rebooting_on_online_lifecycle():
    """The online lifecycle event (no message_id) also reconciles a rebooting
    device — it is equally a 'device is back on the new firmware' signal."""
    tbl = _make_table()
    tbl.put_item(Item={"thingName": "esp32-FC22E0", "migrationStatus": "rebooting"})
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_reconcile2")

    mod.handler(
        {"thingName": "esp32-FC22E0", "status": "online", "pqc_algorithm": "SECP256R1_MLKEM768"},
        None,
    )
    item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
    assert "migrationStatus" not in item


@mock_aws
def test_does_not_reconcile_building_or_pushing():
    """During building / pushing-ota the device is still on the OLD firmware and
    legitimately publishing telemetry — the picker must stay hidden, so these
    in-progress states must NOT be cleared by an incoming telemetry message."""
    tbl = _make_table()
    _env()
    for i, status in enumerate(("building", "pushing-ota")):
        tbl.put_item(Item={"thingName": "esp32-FC22E0", "migrationStatus": status})
        mod = load_handler_module(
            os.path.join(HERE, "index.py"), f"telemetry_processor_noreconcile{i}"
        )
        mod.handler({"device": "esp32-FC22E0", "message_id": 1}, None)
        item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
        assert item["migrationStatus"] == status


@mock_aws
def test_skips_unknown_device():
    tbl = _make_table()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_index4")
    result = mod.handler({"message_id": 1}, None)  # no device/thingName
    assert "Skipped" in result["body"]
    # Nothing written.
    scan = tbl.scan()
    assert scan["Count"] == 0
