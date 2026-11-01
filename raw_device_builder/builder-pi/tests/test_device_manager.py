# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for DeviceManager download/flash/cleanup of the app pieces and the
per-device identity image. S3 is moto (versioned bucket, like the real
firmware bucket); esptool is a mocked subprocess.run, so no hardware is used.
"""
import os
import subprocess
import sys

import boto3
import pytest
from moto import mock_aws

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

import device_manager  # noqa: E402
from device_manager import DeviceManager, DeviceSlot  # noqa: E402

BUCKET = "pqc-workshop-firmware-123456789012"
SERIAL = "C69DE0"
VERSION = "1.0.0-classical"
IDENTITY_KEY = f"identity/{SERIAL}/certs.bin"
PIECES = {
    "bootloader.bin": b"B" * 16,
    "partitions.bin": b"P" * 16,
    "boot_app0.bin": b"O" * 16,
    "firmware.bin": b"A" * 64,
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(device_manager, "FIRMWARE_DIR", str(tmp_path / "fw"))
    monkeypatch.setattr(device_manager, "LOG_DIR", str(tmp_path / "logs"))
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        s3.put_bucket_versioning(Bucket=BUCKET,
                                 VersioningConfiguration={"Status": "Enabled"})
        for name, body in PIECES.items():
            # Shared image: one build per variant per deployment, no serial.
            s3.put_object(Bucket=BUCKET, Key=f"shared/{VERSION}/{name}", Body=body)
        dm = DeviceManager({"region": "us-east-1", "firmware_bucket": BUCKET,
                            "firmware_version": VERSION})
        slot = DeviceSlot(port="/dev/ttyACM0", serial_number=SERIAL)
        dm.devices[slot.port] = slot
        yield dm, slot, s3


def _put_identity(s3, body=b"N" * 64):
    return s3.put_object(Bucket=BUCKET, Key=IDENTITY_KEY, Body=body)["VersionId"]


class FakeRun:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.argv = None

    def __call__(self, argv, **kwargs):
        self.argv = argv
        return subprocess.CompletedProcess(argv, self.returncode, stdout="Hash of data verified.\n",
                                           stderr="" if self.returncode == 0 else "A fatal error")


def test_download_fetches_pieces_and_identity_with_version(env):
    dm, slot, s3 = env
    version = _put_identity(s3)

    fw_dir = dm.download_firmware(slot, IDENTITY_KEY)

    assert fw_dir
    for _offset, local, s3_name in device_manager.FLASH_LAYOUT:
        expected = PIECES[s3_name] if s3_name else b"N" * 64
        with open(os.path.join(fw_dir, local), "rb") as f:
            assert f.read() == expected
    assert slot.identity_key == IDENTITY_KEY
    assert slot.identity_version_id == version
    # Private keys + WiFi PSK: owner-only on disk.
    assert os.stat(os.path.join(fw_dir, "certs.bin")).st_mode & 0o077 == 0


def test_download_fails_without_identity(env, monkeypatch):
    dm, slot, _s3 = env
    monkeypatch.setattr(device_manager.time, "sleep", lambda s: None)

    assert dm.download_firmware(slot, IDENTITY_KEY) is None
    assert "Download failed" in slot.error


def test_flash_writes_pieces_and_identity_at_partition_offsets(env, monkeypatch):
    dm, slot, s3 = env
    _put_identity(s3)
    fw_dir = dm.download_firmware(slot, IDENTITY_KEY)
    run = FakeRun()
    monkeypatch.setattr(device_manager.subprocess, "run", run)

    assert dm.flash_firmware(slot.port, fw_dir) is True

    argv = run.argv
    assert argv[:argv.index("write_flash")] == [
        "esptool", "--port", slot.port, "--baud", "921600", "--chip", "esp32s3"]
    pairs = argv[argv.index("8MB") + 1:]
    assert [(o, os.path.basename(p)) for o, p in zip(pairs[::2], pairs[1::2])] == [
        ("0x0", "bootloader.bin"),
        ("0x8000", "partitions.bin"),
        ("0xe000", "boot_app0.bin"),
        ("0x10000", "app.bin"),
        ("0x721000", "certs.bin"),
    ]
    # Never the 8 MB merged image: it would overwrite the certs partition.
    assert not any("merged" in a for a in argv)


def test_flash_device_deletes_all_identity_versions_after_success(env, monkeypatch):
    dm, slot, s3 = env
    old = _put_identity(s3, b"old")
    new = _put_identity(s3, b"new")
    fw_dir = dm.download_firmware(slot, IDENTITY_KEY)
    monkeypatch.setattr(device_manager.subprocess, "run", FakeRun())

    assert dm.flash_device(slot.port, fw_dir) is True

    listing = s3.list_object_versions(Bucket=BUCKET, Prefix=IDENTITY_KEY)
    # Every version is gone — including the older, never-flashed identity
    # (a failed registration's) — and no delete marker is left behind.
    assert listing.get("Versions", []) == []
    assert listing.get("DeleteMarkers", []) == []
    assert new != old
    assert not os.path.exists(os.path.join(fw_dir, "certs.bin"))
    assert slot.identity_key is None


def test_flash_device_keeps_identity_on_failure(env, monkeypatch):
    dm, slot, s3 = env
    version = _put_identity(s3)
    fw_dir = dm.download_firmware(slot, IDENTITY_KEY)
    monkeypatch.setattr(device_manager.subprocess, "run", FakeRun(returncode=2))

    assert dm.flash_device(slot.port, fw_dir) is False

    versions = s3.list_object_versions(Bucket=BUCKET, Prefix=IDENTITY_KEY)["Versions"]
    assert [v["VersionId"] for v in versions] == [version]
    assert os.path.exists(os.path.join(fw_dir, "certs.bin"))


def test_flash_refuses_when_a_piece_is_missing(env, monkeypatch):
    dm, slot, _s3 = env
    fw_dir = os.path.join(device_manager.FIRMWARE_DIR, "x")
    os.makedirs(fw_dir)
    run = FakeRun()
    monkeypatch.setattr(device_manager.subprocess, "run", run)

    assert dm.flash_firmware(slot.port, fw_dir) is False
    assert run.argv is None
    assert "certs.bin" in slot.error


# Real serial lines captured from FC22E0 on pqc-a (ESP_LOG, lowercase tags).
@pytest.mark.parametrize("line,expected", [
    ("[  3981][I][aws_iot_pqc_esp32_v1_classical.ino:883] wifiTask(): [wifi] Connected! IP: 10.42.0.63, RSSI: -29 dBm, Channel: 6", "wifi"),
    ("[  7929][I][aws_iot_pqc_esp32_v1_classical.ino:1892] mqttTask(): [mqtt] Connected to PQC Mosquitto broker!", "mqtt"),
    ("[  6911][I][aws_iot_pqc_esp32_v3_mldsa.ino:1920] mqttTask(): [mqtt] Connected to PQC Mosquitto broker!", "mqtt"),
    # Attempts are not successes.
    ("[  4803][I][aws_iot_pqc_esp32_v1_classical.ino:1882] mqttTask(): [mqtt] Connecting to PQC Mosquitto broker (thing='esp32-FC22E0', attempt=1)...", None),
    ("[  1203][I][aws_iot_pqc_esp32.ino:869] wifiTask(): [wifi] Connecting to SSID 'reinvent-pqc-iot-wifi' (attempt 1)...", None),
    ("[    62][E][aws_iot_pqc_esp32_v1_classical.ino:2090] setup(): [main] [Setup] FATAL: no valid device identity for esp32-C69DE0. Register and flash this device at the Builder-Pi.", "identity"),
    ("Guru Meditation Error: Core  1 panic'ed (LoadProhibited).", "panic"),
    ("[  5060][I][aws_iot_pqc_esp32_v1_classical.ino:540] connect(): [tls] TCP connected to pqc-broker-nlb.elb.us-east-1.amazonaws.com:8883", None),
])
def test_classify_boot_line(line, expected):
    assert device_manager.classify_boot_line(line) == expected


def test_download_uses_registrar_firmware_prefix(env):
    """The registrar's firmwarePrefix wins over the configured version."""
    dm, slot, s3 = env
    _put_identity(s3)
    for name, body in PIECES.items():
        s3.put_object(Bucket=BUCKET, Key=f"shared/9.9.9-test/{name}", Body=body + b"!")

    fw_dir = dm.download_firmware(slot, IDENTITY_KEY, "shared/9.9.9-test")

    with open(os.path.join(fw_dir, "app.bin"), "rb") as f:
        assert f.read() == PIECES["firmware.bin"] + b"!"


def test_delete_identity_falls_back_to_flashed_version(env, monkeypatch):
    """Without ListBucketVersions, still delete the version that was flashed."""
    dm, slot, s3 = env
    _put_identity(s3, b"old")
    new = _put_identity(s3, b"new")
    fw_dir = dm.download_firmware(slot, IDENTITY_KEY)

    def denied(*a, **k):
        raise RuntimeError("AccessDenied")
    monkeypatch.setattr(DeviceManager, "_identity_versions", staticmethod(denied))

    dm.delete_identity(slot, fw_dir)

    versions = [v["VersionId"] for v in s3.list_object_versions(Bucket=BUCKET, Prefix=IDENTITY_KEY)["Versions"]]
    assert new not in versions and len(versions) == 1


class FakePort:
    def __init__(self, device, vid):
        self.device = device
        self.vid = vid


def test_scan_ports_keeps_only_esp32_usb_vids(monkeypatch):
    from serial.tools import list_ports
    monkeypatch.setattr(list_ports, "comports", lambda: [
        FakePort("/dev/ttyACM1", 0x303A),   # XIAO ESP32-S3 native USB
        FakePort("/dev/ttyACM0", 0x303A),
        FakePort("/dev/ttyUSB0", 0x10C4),   # CP210x bridge
        FakePort("/dev/ttyAMA10", None),    # Pi UART: not an ESP32
        FakePort("/dev/ttyACM9", 0x2341),   # some other USB device
    ])
    dm = DeviceManager({"region": "us-east-1"})
    assert dm.scan_ports() == ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0"]


def test_scan_ports_falls_back_to_glob(monkeypatch):
    from serial.tools import list_ports
    monkeypatch.setattr(list_ports, "comports", lambda: [])
    monkeypatch.setattr(device_manager.glob, "glob",
                        lambda pat: ["/dev/ttyACM0"] if pat == "/dev/ttyACM*" else [])
    dm = DeviceManager({"region": "us-east-1"})
    assert dm.scan_ports() == ["/dev/ttyACM0"]


def test_get_serial_number_uses_esptool_entry_point(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="MAC: 10:20:ba:c6:9d:e0\n", stderr="")

    monkeypatch.setattr(device_manager.subprocess, "run", fake_run)
    dm = DeviceManager({"region": "us-east-1"})
    assert dm.get_serial_number("/dev/ttyACM0") == "C69DE0"
    assert calls[0][0] == "esptool"   # not the deprecated esptool.py alias
