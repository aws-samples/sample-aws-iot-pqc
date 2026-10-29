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
        return FakeResp(200, {"buildId": "b-1", "identityBuildId": "i-1",
                              "identityKey": "identity/FC22E0/certs.bin"})

    monkeypatch.setattr(operations.aws, "sigv4_post", fake_post)
    result = operations.register_device(CONFIG, "FC22E0")
    assert result["success"] is True
    assert result["buildId"] == "b-1"
    assert result["identityBuildId"] == "i-1"
    assert result["identityKey"] == "identity/FC22E0/certs.bin"
    assert operations.build_ids(result) == ["b-1", "i-1"]
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


def test_register_identity_only_sends_flag(monkeypatch):
    captured = {}

    def fake_post(url, payload, region, timeout=30):
        captured["payload"] = payload
        return FakeResp(200, {"identityBuildId": "i-2"})

    monkeypatch.setattr(operations.aws, "sigv4_post", fake_post)
    result = operations.register_device(CONFIG, "FC22E0", identity_only=True)
    assert captured["payload"] == {"serialNumber": "FC22E0", "identityOnly": True}
    assert result["buildId"] is None
    # Falls back to the well-known key when the API omits it.
    assert result["identityKey"] == "identity/FC22E0/certs.bin"
    assert operations.build_ids(result) == ["i-2"]


class FakeCodeBuild:
    """statuses: one entry per poll, either a status for every id or an
    {id: status} dict."""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.calls = []

    def batch_get_builds(self, ids):
        self.calls.append(list(ids))
        step = self._statuses.pop(0)
        return {"builds": [{"id": i, "buildStatus": step[i] if isinstance(step, dict) else step}
                           for i in ids]}


def test_wait_for_builds_waits_for_all():
    cb = FakeCodeBuild([
        {"b-1": "SUCCEEDED", "i-1": "IN_PROGRESS"},
        {"i-1": "SUCCEEDED"},
    ])
    result = operations.wait_for_builds(CONFIG, ["b-1", "i-1"], poll_interval=0,
                                        codebuild_client=cb)
    assert result["success"] is True
    assert result["statuses"] == {"b-1": "SUCCEEDED", "i-1": "SUCCEEDED"}
    # A build that already succeeded is not polled again.
    assert cb.calls == [["b-1", "i-1"], ["i-1"]]


def test_wait_for_builds_fails_if_any_fails():
    cb = FakeCodeBuild([{"b-1": "IN_PROGRESS", "i-1": "FAILED"}])
    result = operations.wait_for_builds(CONFIG, ["b-1", "i-1"], poll_interval=0,
                                        codebuild_client=cb)
    assert result["success"] is False
    assert result["buildId"] == "i-1"
    assert result["status"] == "FAILED"


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
