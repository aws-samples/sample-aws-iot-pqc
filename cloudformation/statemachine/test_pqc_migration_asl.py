# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Definition tests for the PQC migration Step Functions state machine (ASL).

These validate the state graph contract without running a live engine:
  - valid JSON, single StartAt, all transitions resolve to real states,
  - the happy path writes migrationStatus building -> pushing-ota -> rebooting,
  - the failure path writes migrationStatus=failed + migrationError,
  - every fallible Task has a Catch routing to SetStatusFailed,
  - no firmware build: migrations push the shared prebuilt image,
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
    with open(ASL_PATH, encoding="utf-8") as f:
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


def test_no_firmware_build_in_the_migration(asl):
    """Every variant is built once per deployment into shared/<version>/, so a
    migration must never start (or wait on) a CodeBuild."""
    for name, state in asl["States"].items():
        assert "codebuild" not in str(state.get("Resource", "")), f"{name} builds firmware"
    assert "WaitForBuild" not in asl["States"]
    assert "BuildNeeded?" not in asl["States"]


def test_variant_and_shared_key_threaded_through(asl):
    """PrepareBuild gets the picker variant; PublishOta pushes the shared s3Key
    (shared/<version>/firmware.bin) and signature algorithm prepare-build
    resolved — nothing is hardcoded."""
    prepare = asl["States"]["PrepareBuild"]
    assert prepare["Parameters"]["Payload"]["variant.$"] == "$.variant"
    assert prepare["Next"] == "SetStatusPushing"
    rs = prepare["ResultSelector"]
    assert rs["s3Key.$"] == "$.Payload.s3Key"
    assert "buildNeeded.$" not in rs and "buildEnv.$" not in rs
    payload = asl["States"]["PublishOta"]["Parameters"]["Payload"]
    assert payload["s3Key.$"] == "$.prepare.s3Key"
    assert payload["sigAlg.$"] == "$.prepare.sigAlg"
    assert payload["version.$"] == "$.prepare.version"


def test_substitution_placeholders_present(asl):
    raw = json.dumps(asl)
    for placeholder in ("${DeviceTableName}", "${PrepareBuildFunctionArn}",
                        "${PublishOtaFunctionArn}"):
        assert placeholder in raw, f"missing substitution {placeholder}"


# ─────────────────────────────────────────────────────────────────────────────
# Drift guard: the DEPLOYED definition is the inline DefinitionString in
# device-builder-stack.yaml, NOT this .asl.json file. The .asl.json is the
# reviewed/tested source of truth, but CloudFormation deploys the inline copy.
# These two silently drifted once (the inline copy kept an old buildId
# ResultSelector and a hardcoded FIRMWARE_VARIANT after the .asl.json was
# updated), which passed every test above yet failed at runtime with
# States.Runtime "$.Payload.buildId could not be found". This test fails if the
# two ever diverge again on the states that carry the variant/build contract.
# ─────────────────────────────────────────────────────────────────────────────
_STACK_YAML = os.path.normpath(
    os.path.join(HERE, "..", "..", "raw_device_builder", "cloudformation",
                 "device-builder-stack.yaml")
)


def _load_inline_definition():
    """Extract + parse the inline Step Functions DefinitionString from the
    device-builder stack. Returns the parsed ASL dict (the ${...} substitution
    tokens are quoted strings, so it is valid JSON)."""
    with open(_STACK_YAML, encoding="utf-8") as f:
        txt = f.read()
    # The block runs from 'DefinitionString: |' to the next blank line + comment.
    m = re.search(r"DefinitionString:\s*\|\n(.*?)\n\n\s*#", txt, re.DOTALL)
    assert m, "could not locate the inline DefinitionString in device-builder-stack.yaml"
    block = m.group(1)
    lines = block.split("\n")
    indent = min(len(l) - len(l.lstrip()) for l in lines if l.strip())
    dedented = "\n".join(l[indent:] for l in lines)
    return json.loads(dedented)


def _strip_comments(state):
    return {k: v for k, v in state.items() if k != "Comment"}


@pytest.fixture(scope="module")
def inline_asl():
    return _load_inline_definition()


@pytest.mark.parametrize("state_name", ["PrepareBuild", "PublishOta", "SetStatusPushing"])
def test_inline_definition_matches_asl_file(asl, inline_asl, state_name):
    """The deployed (inline) definition must match the tested .asl.json on the
    states that carry the variant + build-env contract. Comment fields are
    ignored (wording may differ); everything functional must be identical."""
    file_state = _strip_comments(asl["States"][state_name])
    inline_state = _strip_comments(inline_asl["States"][state_name])
    assert inline_state == file_state, (
        f"{state_name} has drifted between pqc-migration.asl.json and the inline "
        f"DefinitionString in device-builder-stack.yaml — update the inline copy "
        f"(that is the one CloudFormation actually deploys)."
    )


def test_inline_definition_has_no_build_path(inline_asl):
    """The deployed (inline) definition must not keep a build path either."""
    rs = inline_asl["States"]["PrepareBuild"]["ResultSelector"]
    assert "buildId.$" not in rs and "buildEnv.$" not in rs and "buildNeeded.$" not in rs
    assert rs.get("variant.$") == "$.Payload.variant"
    assert "WaitForBuild" not in inline_asl["States"]
