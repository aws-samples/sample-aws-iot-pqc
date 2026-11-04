# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for issue_broker_certs.py: the broker job issues ONLY the two broker
server certs, from the device CAs, with the explicit end-entity template that
its IAM policy pins. It must never mint an OTA firmware signer (T024): that is
issue_signer_certs.py's job, under a separate role.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import issue_broker_certs as ibc  # noqa: E402
import issue_cert  # noqa: E402

ENV = {
    "CLASSICAL_CA_ARN": "arn:ca/device-classical",
    "MLDSA_CA_ARN": "arn:ca/device-mldsa",
}
SECRETS = ("BROKER_CA_CERT_SECRET", "BROKER_CERT_SECRET", "BROKER_KEY_SECRET",
           "MLDSA_CA_CERT_SECRET", "MLDSA_BROKER_CERT_SECRET", "MLDSA_BROKER_KEY_SECRET")


@pytest.fixture
def calls(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    for k in SECRETS:
        monkeypatch.setenv(k, f"secret/{k}")
    # Even if signer env vars leak into this job's environment, it must ignore them.
    monkeypatch.setenv("SIGNER_CLASSICAL_CA_ARN", "arn:ca/signer-classical")
    monkeypatch.setenv("SIGNER_MLDSA_CA_ARN", "arn:ca/signer-mldsa")
    issued = []

    def fake_issue(**kw):
        issued.append(kw)
        return {"caCert": f"CA:{kw['ca_arn']}", "cert": "C", "key": "K",
                "certificateArn": kw["ca_arn"] + "/certificate/1"}

    stored = {}
    monkeypatch.setattr(ibc, "issue_certificate", fake_issue)
    monkeypatch.setattr(ibc, "_store", lambda sid, val: stored.__setitem__(sid, val))
    assert ibc.main() == 0
    return issued, stored


def test_only_the_two_broker_certs_are_issued(calls):
    issued, _ = calls
    assert [c["common_name"] for c in issued] == ["pqc-mqtt-broker", "pqc-mqtt-broker"]
    assert {c["ca_arn"] for c in issued} == {ENV["CLASSICAL_CA_ARN"], ENV["MLDSA_CA_ARN"]}


def test_broker_certs_use_the_explicit_end_entity_template(calls):
    # The CertProvisionerRole pins acm-pca:TemplateArn to EndEntityCertificate/V1;
    # a request without the template would not match that condition.
    issued, _ = calls
    assert all(c["template_arn"] == issue_cert.END_ENTITY_TEMPLATE_ARN for c in issued)


def test_no_signing_ca_is_touched(calls):
    issued, stored = calls
    assert not any("signer" in c["ca_arn"] for c in issued)
    assert set(stored) == {f"secret/{k}" for k in SECRETS}
