# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the start-migration Lambda.

start-migration is the AppSync migrateFirmware target. It starts the Step
Functions migration execution and returns immediately (well under the 30s
AppSync resolver timeout) so the dashboard never sees a spurious error.
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, REPO_ROOT)
from conftest import load_handler_module  # noqa: E402

STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:pqc-workshop-pqc-migration"


def test_starts_execution_and_returns_success(monkeypatch):
    os.environ["STATE_MACHINE_ARN"] = STATE_MACHINE_ARN
    mod = load_handler_module(os.path.join(HERE, "index.py"), "start_migration_index")

    calls = {}

    def fake_start_execution(**kwargs):
        calls.update(kwargs)
        return {"executionArn": "arn:aws:states:us-east-1:123456789012:execution:pqc-workshop-pqc-migration:run1"}

    monkeypatch.setattr(mod.sfn, "start_execution", fake_start_execution, raising=True)

    result = mod.handler({"thingName": "esp32-FC22E0"}, None)

    assert result["success"] is True
    assert result["thingName"] == "esp32-FC22E0"
    # Started the correct state machine with the thingName in the input.
    assert calls["stateMachineArn"] == STATE_MACHINE_ARN
    body = json.loads(calls["input"])
    assert body["thingName"] == "esp32-FC22E0"
    # Variant defaults to pqc (backward compatible) and is always present so the
    # state machine's $.variant reference never resolves a missing path.
    assert body["variant"] == "pqc"


def test_explicit_variant_is_forwarded(monkeypatch):
    os.environ["STATE_MACHINE_ARN"] = STATE_MACHINE_ARN
    mod = load_handler_module(os.path.join(HERE, "index.py"), "start_migration_variant")
    calls = {}
    monkeypatch.setattr(
        mod.sfn, "start_execution",
        lambda **kw: (calls.update(kw) or {"executionArn": "arn:...:run2"}),
        raising=True,
    )
    result = mod.handler({"thingName": "esp32-FC22E0", "variant": "pqc-mldsa"}, None)
    assert result["success"] is True
    assert result["variant"] == "pqc-mldsa"
    assert json.loads(calls["input"])["variant"] == "pqc-mldsa"


@pytest.mark.parametrize("variant", ["signer", "verified"])
def test_mldsa_signer_era_variants_forwarded(monkeypatch, variant):
    """The Workstream 4 ML-DSA-signer-era variants (4.0.0-signer / 5.0.0-verified)
    are accepted by the allow-list and forwarded into the execution input."""
    os.environ["STATE_MACHINE_ARN"] = STATE_MACHINE_ARN
    mod = load_handler_module(os.path.join(HERE, "index.py"), f"start_migration_{variant}")
    calls = {}
    monkeypatch.setattr(
        mod.sfn, "start_execution",
        lambda **kw: (calls.update(kw) or {"executionArn": "arn:...:runN"}),
        raising=True,
    )
    result = mod.handler({"thingName": "esp32-FC22E0", "variant": variant}, None)
    assert result["success"] is True
    assert result["variant"] == variant
    assert json.loads(calls["input"])["variant"] == variant


def test_invalid_variant_returns_failure(monkeypatch):
    os.environ["STATE_MACHINE_ARN"] = STATE_MACHINE_ARN
    mod = load_handler_module(os.path.join(HERE, "index.py"), "start_migration_badvariant")
    monkeypatch.setattr(mod.sfn, "start_execution",
                        lambda **k: pytest.fail("should not start on invalid variant"), raising=True)
    result = mod.handler({"thingName": "esp32-FC22E0", "variant": "bogus"}, None)
    assert result["success"] is False
    assert "invalid variant" in result["message"].lower()


def test_missing_thingname_returns_failure(monkeypatch):
    os.environ["STATE_MACHINE_ARN"] = STATE_MACHINE_ARN
    mod = load_handler_module(os.path.join(HERE, "index.py"), "start_migration_index2")
    # Should not call start_execution when thingName missing.
    monkeypatch.setattr(mod.sfn, "start_execution",
                        lambda **k: pytest.fail("should not start"), raising=True)
    result = mod.handler({}, None)
    assert result["success"] is False
