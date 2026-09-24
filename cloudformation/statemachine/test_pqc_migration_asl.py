"""
Definition tests for the PQC migration Step Functions state machine (ASL).

These validate the state graph contract without running a live engine:
  - valid JSON, single StartAt, all transitions resolve to real states,
  - the happy path writes migrationStatus building -> pushing-ota -> rebooting,
  - the failure path writes migrationStatus=failed + migrationError,
  - every fallible Task has a Catch routing to SetStatusFailed,
  - the long build wait uses the CodeBuild .sync integration (not a Lambda),
  - CloudFormation DefinitionSubstitution placeholders are present.

This is the fast, deterministic complement to a full Step Functions Local run
(kept out of the default suite as it needs Docker).
"""
import json
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ASL_PATH = os.path.join(HERE, "pqc-migration.asl.json")


@pytest.fixture(scope="module")
def asl():
    with open(ASL_PATH) as f:
        return json.load(f)


def test_valid_json_and_start_state(asl):
    assert asl["StartAt"] == "SetStatusBuilding"
    assert "States" in asl


def test_all_transitions_resolve(asl):
    states = asl["States"]
    names = set(states)
    for name, state in states.items():
        if "Next" in state:
            assert state["Next"] in names, f"{name}.Next -> unknown {state['Next']}"
        for choice in state.get("Choices", []):
            assert choice["Next"] in names
        if "Default" in state:
            assert state["Default"] in names
        for catch in state.get("Catch", []):
            assert catch["Next"] in names


def test_happy_path_status_sequence(asl):
    """Following Next transitions on the success path writes the 3 statuses in order."""
    states = asl["States"]
    seq = []
    name = asl["StartAt"]
    seen = set()
    while name and name not in seen:
        seen.add(name)
        state = states[name]
        # Record the migrationStatus a SetStatus* DynamoDB write sets.
        if state.get("Resource") == "arn:aws:states:::dynamodb:updateItem":
            vals = state["Parameters"]["ExpressionAttributeValues"]
            if ":s" in vals and "S" in vals[":s"]:
                seq.append(vals[":s"]["S"])
        # Advance along the success path: Choice takes buildNeeded->true branch.
        if state["Type"] == "Choice":
            name = state["Choices"][0]["Next"]
        else:
            name = state.get("Next")
    assert seq == ["building", "pushing-ota", "rebooting"], seq


def test_failure_state_writes_failed_and_error(asl):
    failed = asl["States"]["SetStatusFailed"]
    vals = failed["Parameters"]["ExpressionAttributeValues"]
    assert vals[":s"]["S"] == "failed"
    assert ":e" in vals  # migrationError captured from $.error.Cause
    assert "migrationError" in failed["Parameters"]["UpdateExpression"]
    assert failed["Next"] == "Fail"


def test_every_fallible_task_catches_to_failed(asl):
    """Task states before the terminal writes must Catch to SetStatusFailed."""
    states = asl["States"]
    # SetStatusRebooting is intentionally not caught (terminal success write);
    # SetStatusFailed itself is not caught.
    exempt = {"SetStatusRebooting", "SetStatusFailed"}
    for name, state in states.items():
        if state["Type"] == "Task" and name not in exempt:
            catches = state.get("Catch", [])
            assert any(c["Next"] == "SetStatusFailed" for c in catches), f"{name} lacks Catch->SetStatusFailed"


def test_build_wait_uses_codebuild_sync(asl):
    wait = asl["States"]["WaitForBuild"]
    assert wait["Resource"] == "arn:aws:states:::codebuild:startBuild.sync"


def test_variant_threaded_through(asl):
    """The firmware variant is data-driven, not hardcoded: PrepareBuild receives
    $.variant, and WaitForBuild's FIRMWARE_VARIANT comes from the prepared
    variant (so the same state machine builds classical / pqc / pqc-mldsa)."""
    prepare = asl["States"]["PrepareBuild"]
    assert prepare["Parameters"]["Payload"]["variant.$"] == "$.variant"

    wait_env = asl["States"]["WaitForBuild"]["Parameters"]["EnvironmentVariablesOverride"]
    by_name = {e["Name"]: e for e in wait_env}
    # FIRMWARE_VARIANT must be sourced from the prepared variant, never a literal.
    assert by_name["FIRMWARE_VARIANT"].get("Value.$") == "$.prepare.variant"
    assert "Value" not in by_name["FIRMWARE_VARIANT"], "FIRMWARE_VARIANT must not be hardcoded"
    # The per-device WiFi/broker env resolved by prepare-build flows to the build.
    assert by_name["WIFI_SSID"].get("Value.$") == "$.prepare.buildEnv.wifiSsid"
    assert by_name["BROKER_ENDPOINT"].get("Value.$") == "$.prepare.buildEnv.brokerEndpoint"


def test_substitution_placeholders_present(asl):
    raw = json.dumps(asl)
    for placeholder in ("${DeviceTableName}", "${PrepareBuildFunctionArn}",
                        "${PublishOtaFunctionArn}", "${BuildProjectName}"):
        assert placeholder in raw, f"missing substitution {placeholder}"
