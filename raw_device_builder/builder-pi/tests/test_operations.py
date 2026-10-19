# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for builder_core.operations (the shared Builder-Pi cloud operations).

AWS is mocked: register/deregister via a stubbed sigv4_post, wait_for_build via
a fake codebuild client.
"""
import os
import sys

import pytest

# Make the builder-pi dir importable (builder_core + cli live here).
HERE = os.path.dirname(os.path.abspath(__file__))
BUILDER_PI = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, BUILDER_PI)

from builder_core import operations  # noqa: E402


CONFIG = {
    "api_endpoint": "https://api.example.com/prod",
    "region": "us-east-1",
}


class FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_register_success(monkeypatch):
    captured = {}

    def fake_post(url, payload, region, timeout=30):
        captured["url"] = url
        captured["payload"] = payload
        return FakeResp(200, {"buildId": "b-1"})

    monkeypatch.setattr(operations.aws, "sigv4_post", fake_post)
    result = operations.register_device(CONFIG, "FC22E0")
    assert result["success"] is True
    assert result["buildId"] == "b-1"
    assert captured["url"].endswith("/register")
    # Plug-and-flash: the payload carries only the auto-detected serial; WiFi,
    # broker endpoint and firmware version are resolved server-side from SSM.
    assert captured["payload"] == {"serialNumber": "FC22E0"}


def test_register_api_error(monkeypatch):
    monkeypatch.setattr(operations.aws, "sigv4_post",
                        lambda *a, **k: FakeResp(500, text="boom"))
    result = operations.register_device(CONFIG, "FC22E0")
    assert result["success"] is False
    assert "500" in result["message"]


def test_deregister_success(monkeypatch):
    captured = {}

    def fake_post(url, payload, region, timeout=30):
        captured["url"] = url
        return FakeResp(200, {"success": True})

    monkeypatch.setattr(operations.aws, "sigv4_post", fake_post)
    result = operations.deregister_device(CONFIG, "FC22E0")
    assert result["success"] is True
    assert captured["url"].endswith("/deregister")


class FakeCodeBuild:
    def __init__(self, statuses):
        self._statuses = list(statuses)

    def batch_get_builds(self, ids):
        return {"builds": [{"buildStatus": self._statuses.pop(0)}]}


def test_wait_for_build_succeeds():
    cb = FakeCodeBuild(["IN_PROGRESS", "SUCCEEDED"])
    result = operations.wait_for_build(CONFIG, "b-1", poll_interval=0, codebuild_client=cb)
    assert result["success"] is True
    assert result["status"] == "SUCCEEDED"


def test_wait_for_build_fails():
    cb = FakeCodeBuild(["IN_PROGRESS", "FAILED"])
    result = operations.wait_for_build(CONFIG, "b-1", poll_interval=0, codebuild_client=cb)
    assert result["success"] is False
    assert result["status"] == "FAILED"
