# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for issue_broker_certs.py: which CA and template each certificate
is issued from. The OTA signers must come from the dedicated firmware-signing
roots with the code-signing template, never from the device/broker CAs (T024).
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
    "SIGNER_CLASSICAL_CA_ARN": "arn:ca/signer-classical",
    "SIGNER_MLDSA_CA_ARN": "arn:ca/signer-mldsa",
}
SECRETS = ("BROKER_CA_CERT_SECRET", "BROKER_CERT_SECRET", "BROKER_KEY_SECRET",
           "MLDSA_CA_CERT_SECRET", "MLDSA_BROKER_CERT_SECRET", "MLDSA_BROKER_KEY_SECRET",
           "SIGNER_CLASSICAL_CA_SECRET", "SIGNER_CLASSICAL_CERT_SECRET",
           "SIGNER_CLASSICAL_KEY_SECRET", "SIGNER_MLDSA_CA_SECRET",
           "SIGNER_MLDSA_CERT_SECRET", "SIGNER_MLDSA_KEY_SECRET")


@pytest.fixture
def calls(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    for k in SECRETS:
        monkeypatch.setenv(k, f"secret/{k}")
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


def test_signers_come_from_the_signing_roots_with_code_signing_template(calls):
    issued, _ = calls
    by_cn = {c["common_name"]: c for c in issued}
    cls = by_cn["pqc-firmware-signer-classical"]
    mld = by_cn["pqc-firmware-signer-mldsa"]
    assert cls["ca_arn"] == ENV["SIGNER_CLASSICAL_CA_ARN"]
    assert mld["ca_arn"] == ENV["SIGNER_MLDSA_CA_ARN"]
    for c in (cls, mld):
        assert c["template_arn"] == issue_cert.CODE_SIGNING_TEMPLATE_ARN


def test_broker_certs_stay_on_the_device_cas_with_default_template(calls):
    issued, _ = calls
    brokers = [c for c in issued if c["common_name"] == "pqc-mqtt-broker"]
    assert {c["ca_arn"] for c in brokers} == {ENV["CLASSICAL_CA_ARN"], ENV["MLDSA_CA_ARN"]}
    assert all(c.get("template_arn") is None for c in brokers)


def test_signer_ca_secrets_hold_the_signing_roots(calls):
    _, stored = calls
    assert stored["secret/SIGNER_CLASSICAL_CA_SECRET"] == "CA:arn:ca/signer-classical"
    assert stored["secret/SIGNER_MLDSA_CA_SECRET"] == "CA:arn:ca/signer-mldsa"
