# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the command-publisher Lambda (IoT publish is mocked)."""
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setenv("IOT_ENDPOINT", "example-ats.iot.us-east-1.amazonaws.com")
    spec = importlib.util.spec_from_file_location("command_publisher_index", os.path.join(HERE, "index.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    class FakeIot:
        def __init__(self):
            self.calls = []

        def publish(self, **kw):
            self.calls.append(kw)

    fake = FakeIot()
    monkeypatch.setattr(m, "_client", lambda: fake)
    m.fake = fake
    return m


@pytest.mark.parametrize("action", ["led-on", "led-off"])
def test_led_commands_go_to_control_topic(mod, action):
    r = mod.handler({"thingName": "esp32-FC22E0", "action": action, "topicSuffix": "control"}, None)
    assert r["success"] is True
    assert mod.fake.calls == [{"topic": "cmd/esp32-FC22E0/control", "qos": 1,
                               "payload": json.dumps({"action": action})}]


def test_topic_suffix_defaults_to_control(mod):
    r = mod.handler({"thingName": "esp32-FC22E0", "action": "led-off"}, None)
    assert r["success"] is True
    assert mod.fake.calls[0]["topic"] == "cmd/esp32-FC22E0/control"


def test_diag_command(mod):
    r = mod.handler({"thingName": "esp32-FC22E0", "action": "diag", "topicSuffix": "diag"}, None)
    assert r["success"] is True
    assert mod.fake.calls[0]["topic"] == "cmd/esp32-FC22E0/diag"


@pytest.mark.parametrize("event", [
    {"thingName": "esp32-FC22E0", "action": "migrate-pqc", "topicSuffix": "ota"},   # never the OTA topic
    {"thingName": "esp32-FC22E0", "action": "ota-update", "topicSuffix": "control"},
    {"thingName": "esp32-FC22E0", "action": "led-on", "topicSuffix": "diag"},
    {"thingName": "esp32-FC22E0", "action": "", "topicSuffix": "control"},
    {"thingName": "esp32-FC22E0/../x", "action": "led-on"},
    {"thingName": "+", "action": "led-on"},
    {"thingName": "", "action": "led-on"},
])
def test_rejects_anything_outside_the_allowlist(mod, event):
    r = mod.handler(event, None)
    assert r["success"] is False
    assert mod.fake.calls == []


def test_publish_failure_is_reported(mod, monkeypatch):
    def boom(**kw):
        raise RuntimeError("throttled")
    monkeypatch.setattr(mod.fake, "publish", boom)
    r = mod.handler({"thingName": "esp32-FC22E0", "action": "led-on"}, None)
    assert r["success"] is False
