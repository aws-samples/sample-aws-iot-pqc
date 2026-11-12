# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the telemetry-processor Lambda.

Key behavior under test:

1. It uses UpdateItem (not put_item), so it updates only the fields present in
   the event and PRESERVES other fields — critically migrationStatus, which the
   migration state machine writes. A put_item-based processor would clobber
   migrationStatus on every telemetry message.

2. Registered-only guard: the UpdateItem is conditional on
   attribute_exists(registeredAt), so telemetry from a device that was never
   registered through the builder-Pi (e.g. a board left flashed from a previous
   deployment) does NOT create a ghost row / dashboard card. The registrar
   writes registeredAt before the device is flashed, so a legitimate device is
   never blocked.
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


def _register(tbl, thing_name="esp32-FC22E0", **extra):
    """Seed a REGISTERED device row, as the registrar Lambda would (it always
    writes serialNumber + registeredAt). The telemetry-processor's
    registered-only guard requires serialNumber to exist, so telemetry only
    lands for devices with this row."""
    serial = thing_name.split("-")[-1]
    item = {
        "thingName": thing_name,
        "serialNumber": serial,
        "registeredAt": "2026-01-01T00:00:00Z",
        "status": "registered",
    }
    item.update(extra)
    tbl.put_item(Item=item)


@mock_aws
def test_telemetry_event_writes_fields():
    tbl = _make_table()
    _register(tbl)
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
        "ota_sig_alg": "ML-DSA-44",
        "firmware_sha256": "204254104425cb962ba7efd4cf328bcd79e359336c4738f10f4a112914335bf3",
    }
    mod.handler(event, None)

    item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
    assert item["pqcAlgorithm"] == "SECP256R1_MLKEM768"
    assert item["tlsVersion"] == "TLSv1.3"
    assert item["certSigAlg"] == "ML-DSA-44"
    # OTA firmware-signing badge fields (the running image's signature era +
    # the device-computed SHA-256 of its own running partition).
    assert item["otaSigAlg"] == "ML-DSA-44"
    assert item["firmwareSha256"] == (
        "204254104425cb962ba7efd4cf328bcd79e359336c4738f10f4a112914335bf3"
    )
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
    _register(tbl)
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_index2")

    mod.handler(
        {
            "thingName": "esp32-FC22E0", "status": "online", "ip": "10.0.0.5",
            "pqc_algorithm": "SECP256R1_MLKEM768", "tls_version": "TLSv1.3",
            "cert_sig_alg": "ECDSA-P256",
            "ota_sig_alg": "ECDSA-P256",
            "firmware_sha256": "4bb849c436aa6557d33d7d4c15ae78c78406839fb1f97bad54c456597afe1c10",
        },
        None,
    )
    item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
    assert item["status"] == "online"
    assert item["ipAddress"] == "10.0.0.5"
    assert item["certSigAlg"] == "ECDSA-P256"
    # OTA firmware-signing badge fields persist via the lifecycle path too.
    assert item["otaSigAlg"] == "ECDSA-P256"
    assert item["firmwareSha256"] == (
        "4bb849c436aa6557d33d7d4c15ae78c78406839fb1f97bad54c456597afe1c10"
    )


@mock_aws
def test_preserves_migration_status():
    """The critical guard: a telemetry update must NOT wipe migrationStatus."""
    tbl = _make_table()
    _register(tbl, migrationStatus="building")
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
    _register(tbl, migrationStatus="rebooting", migrationError="")
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
    _register(tbl, migrationStatus="rebooting")
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_reconcile2")

    mod.handler(
        {"thingName": "esp32-FC22E0", "status": "online", "pqc_algorithm": "SECP256R1_MLKEM768"},
        None,
    )
    item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
    assert "migrationStatus" not in item


@mock_aws
def test_old_firmware_telemetry_does_not_end_the_migration():
    """While the board downloads the OTA it still runs the OLD image and keeps
    publishing telemetry. With a target version recorded, that telemetry must
    not clear 'rebooting' (seen in the 32-board burst: the picker came back
    ~30 s before the reboot)."""
    tbl = _make_table()
    _register(tbl, migrationStatus="rebooting", migrationTargetVersion="4.0.0-signer")
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_target_old")
    mod.handler({"device": "esp32-FC22E0", "message_id": 7, "firmware_version": "5.0.0-verified"}, None)
    item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
    assert item["migrationStatus"] == "rebooting"
    assert item["migrationTargetVersion"] == "4.0.0-signer"


@mock_aws
@pytest.mark.parametrize("event", [
    {"device": "esp32-FC22E0", "message_id": 1, "firmware_version": "4.0.0-signer"},
    {"thingName": "esp32-FC22E0", "status": "online", "firmware_version": "4.0.0-signer"},
])
def test_report_from_the_target_firmware_ends_the_migration(event):
    tbl = _make_table()
    _register(tbl, migrationStatus="rebooting", migrationError="", migrationTargetVersion="4.0.0-signer")
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_target_new")
    mod.handler(event, None)
    item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
    for field in ("migrationStatus", "migrationError", "migrationTargetVersion"):
        assert field not in item


@mock_aws
def test_report_without_a_version_does_not_clear_a_targeted_migration():
    tbl = _make_table()
    _register(tbl, migrationStatus="rebooting", migrationTargetVersion="4.0.0-signer")
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_target_nover")
    mod.handler({"device": "esp32-FC22E0", "message_id": 3}, None)
    item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
    assert item["migrationStatus"] == "rebooting"


def _ago(seconds):
    """A Step Functions-style EnteredTime this many seconds in the past."""
    import datetime as _dt
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


@mock_aws
def test_ota_that_never_installed_is_marked_failed_after_the_timeout():
    """Seen in the 32-board burst: one board got the OTA command, failed the
    download, and stayed on the old image. After the timeout the migration must
    become 'failed' (picker back, error shown) instead of 'rebooting' forever."""
    tbl = _make_table()
    _register(tbl, migrationStatus="rebooting", migrationTargetVersion="4.0.0-signer",
              migrationRebootingSince=_ago(301))
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_stalled")
    mod.handler({"device": "esp32-FC22E0", "message_id": 9, "firmware_version": "5.0.0-verified"}, None)
    item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
    assert item["migrationStatus"] == "failed"
    assert "still running 5.0.0-verified" in item["migrationError"]
    assert "migrationTargetVersion" not in item and "migrationRebootingSince" not in item


@mock_aws
def test_ota_still_downloading_is_not_failed_early():
    tbl = _make_table()
    _register(tbl, migrationStatus="rebooting", migrationTargetVersion="4.0.0-signer",
              migrationRebootingSince=_ago(90))
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_downloading")
    mod.handler({"device": "esp32-FC22E0", "message_id": 9, "firmware_version": "5.0.0-verified"}, None)
    item = tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
    assert item["migrationStatus"] == "rebooting"


@mock_aws
def test_iso_ms_matches_the_step_functions_timestamp_format():
    import datetime as _dt
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_iso")
    t = _dt.datetime(2026, 11, 11, 17, 56, 39, 782000)
    assert mod._iso_ms(t) == "2026-11-11T17:56:39.782Z"


@mock_aws
def test_does_not_reconcile_building_or_pushing():
    """During building / pushing-ota the device is still on the OLD firmware and
    legitimately publishing telemetry — the picker must stay hidden, so these
    in-progress states must NOT be cleared by an incoming telemetry message."""
    tbl = _make_table()
    _env()
    for i, status in enumerate(("building", "pushing-ota")):
        _register(tbl, migrationStatus=status)
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


@mock_aws
def test_unregistered_device_telemetry_creates_no_ghost_row():
    """A device that publishes telemetry but was never registered (no row with
    serialNumber) must NOT get a row — otherwise it becomes a ghost card on the
    dashboard. This is the leftover-from-a-previous-deployment case: the board
    is still flashed and reconnects with a valid cert, but was not registered on
    this build."""
    tbl = _make_table()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_ghost1")

    result = mod.handler(
        {
            "device": "esp32-C69E44", "message_id": 12,
            "pqc_algorithm": "SECP256R1_MLKEM768", "tls_version": "TLSv1.3",
            "cert_sig_alg": "ML-DSA-44", "firmware_version": "3.0.0-mldsa",
        },
        None,
    )
    assert "Skipped unregistered" in result["body"]
    # No row was created for the unregistered device.
    assert tbl.scan()["Count"] == 0
    assert "Item" not in tbl.get_item(Key={"thingName": "esp32-C69E44"})


@mock_aws
def test_unregistered_device_lifecycle_creates_no_ghost_row():
    """The online lifecycle event path is guarded too — an unregistered device
    coming online must not materialise a row."""
    tbl = _make_table()
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_ghost2")

    result = mod.handler(
        {"thingName": "esp32-C69DE0", "status": "online", "ip": "10.0.0.9"},
        None,
    )
    assert "Skipped unregistered" in result["body"]
    assert tbl.scan()["Count"] == 0


@mock_aws
def test_registered_device_telemetry_is_not_skipped():
    """Sanity counterpart: a properly-registered device (row has serialNumber)
    is updated normally — the guard must not block legitimate devices."""
    tbl = _make_table()
    _register(tbl, "esp32-C69E44")
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_ghost3")

    result = mod.handler(
        {"device": "esp32-C69E44", "message_id": 7, "pqc_algorithm": "SECP256R1_MLKEM768"},
        None,
    )
    assert "Skipped" not in result["body"]
    item = tbl.get_item(Key={"thingName": "esp32-C69E44"})["Item"]
    assert item["messageId"] == 7
    assert item["serialNumber"] == "C69E44"  # preserved


@mock_aws
def test_handshake_ms_is_stored_from_telemetry_and_lifecycle():
    tbl = _make_table()
    _register(tbl)
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_index")

    mod.handler({"device": "esp32-FC22E0", "status": "online", "handshake_ms": 2052}, None)
    assert tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]["handshakeMs"] == 2052

    mod.handler({"device": "esp32-FC22E0", "message_id": 1, "handshake_ms": 3121}, None)
    assert tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]["handshakeMs"] == 3121


@mock_aws
def test_older_firmware_without_handshake_ms_leaves_it_unset():
    tbl = _make_table()
    _register(tbl)
    _env()
    mod = load_handler_module(os.path.join(HERE, "index.py"), "telemetry_processor_index")

    mod.handler({"device": "esp32-FC22E0", "message_id": 1, "uptime_sec": 5}, None)
    assert "handshakeMs" not in tbl.get_item(Key={"thingName": "esp32-FC22E0"})["Item"]
