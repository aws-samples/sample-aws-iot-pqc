# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Static checks on dashboard-stack.yaml for the workshop-passcode sign-in: no
guest (unauthenticated) access, only signed-in users get a role, and that role
can call only the AppSync fields the dashboard uses (never updateDevice).
"""
import os

import pytest
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "dashboard-stack.yaml")


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation short-form tags as {tag: value}."""


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {suffix: value}


_CfnLoader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def resources():
    with open(TEMPLATE, encoding="utf-8") as f:
        # _CfnLoader is a SafeLoader subclass (adds only CloudFormation tags),
        # so this is as safe as yaml.safe_load.
        return _CfnLoader(f).get_single_data()["Resources"]


def _of_type(resources, cfn_type):
    return {k: v for k, v in resources.items() if v["Type"] == cfn_type}


def test_identity_pool_has_no_guest_access(resources):
    (pool,) = _of_type(resources, "AWS::Cognito::IdentityPool").values()
    props = pool["Properties"]
    assert props["AllowUnauthenticatedIdentities"] is False
    (provider,) = props["CognitoIdentityProviders"]
    assert provider["ServerSideTokenCheck"] is True


def test_only_an_authenticated_role_is_attached(resources):
    (attachment,) = _of_type(resources, "AWS::Cognito::IdentityPoolRoleAttachment").values()
    assert set(attachment["Properties"]["Roles"]) == {"authenticated"}
    role = resources["CognitoAuthRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert role["Condition"]["ForAnyValue:StringLike"]["cognito-identity.amazonaws.com:amr"] == "authenticated"


def test_no_role_trusts_unauthenticated_identities(resources):
    for name, res in _of_type(resources, "AWS::IAM::Role").items():
        doc = res["Properties"]["AssumeRolePolicyDocument"]
        assert "unauthenticated" not in str(doc), name


def _granted_fields(resources):
    stmts = resources["CognitoAuthRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    fields = set()
    for stmt in stmts:
        assert stmt["Action"] == ["appsync:GraphQL"]
        for arn in stmt["Resource"]:
            sub = arn["Sub"]
            assert sub.startswith("${DeviceApi.Arn}/types/"), sub
            assert "*" not in sub, sub
            _, type_name, _, field = sub.split("/", 1)[1].split("/")
            fields.add(f"{type_name}.{field}")
    return fields


def test_signed_in_role_gets_exactly_the_dashboard_fields(resources):
    assert _granted_fields(resources) == {
        "Query.listDevices", "Query.getDevice", "Query.getDeviceLog", "Query.getBrokerLog",
        "Mutation.sendCommand", "Mutation.migrateFirmware",
    }


def test_browser_cannot_write_device_state(resources):
    # Only the telemetry processor writes device rows (threat-model M5).
    assert "Mutation.updateDevice" not in _granted_fields(resources)
    assert "Mutation.deleteDevice" not in _granted_fields(resources)


def test_user_pool_is_admin_only_with_a_browser_client(resources):
    pool = resources["DashboardUserPool"]["Properties"]
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    assert pool["UserPoolTier"] == "LITE"
    assert pool["Policies"]["PasswordPolicy"]["MinimumLength"] == 8
    client = resources["DashboardUserPoolClient"]["Properties"]
    assert client["GenerateSecret"] is False
    assert set(client["ExplicitAuthFlows"]) == {"ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"}
    assert client["PreventUserExistenceErrors"] == "ENABLED"
