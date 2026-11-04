# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for issue_signer_certs.py: the OTA signers come from the dedicated
firmware-signing roots with the code-signing template, never from the
device/broker CAs (T024), and keep their existing idempotency-token format.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import issue_cert  # noqa: E402
import issue_signer_certs as isc  # noqa: E402

ENV = {
    "SIGNER_CLASSICAL_CA_ARN": "arn:ca/signer-classical",
    "SIGNER_MLDSA_CA_ARN": "arn:ca/signer-mldsa",
}
SECRETS = ("SIGNER_CLASSICAL_CA_SECRET", "SIGNER_CLASSICAL_CERT_SECRET",
           "SIGNER_CLASSICAL_KEY_SECRET", "SIGNER_MLDSA_CA_SECRET",
           "SIGNER_MLDSA_CERT_SECRET", "SIGNER_MLDSA_KEY_SECRET")


@pytest.fixture
def calls(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    for k in SECRETS:
        monkeypatch.setenv(k, f"secret/{k}")
    monkeypatch.delenv("SIGNER_CN", raising=False)
    issued = []

    def fake_issue(**kw):
        issued.append(kw)
        return {"caCert": f"CA:{kw['ca_arn']}", "cert": f"C:{kw['common_name']}",
                "key": f"K:{kw['common_name']}",
                "certificateArn": kw["ca_arn"] + "/certificate/1"}

    stored = {}
    monkeypatch.setattr(isc, "issue_certificate", fake_issue)
    monkeypatch.setattr(isc, "_store", lambda sid, val: stored.__setitem__(sid, val))
    assert isc.main() == 0
    return issued, stored


def test_signers_come_from_the_signing_roots_with_code_signing_template(calls):
    issued, _ = calls
    by_cn = {c["common_name"]: c for c in issued}
    assert set(by_cn) == {"pqc-firmware-signer-classical", "pqc-firmware-signer-mldsa"}
    cls = by_cn["pqc-firmware-signer-classical"]
    mld = by_cn["pqc-firmware-signer-mldsa"]
    assert (cls["ca_arn"], cls["key_mode"]) == (ENV["SIGNER_CLASSICAL_CA_ARN"], "ec-p256")
    assert (mld["ca_arn"], mld["key_mode"]) == (ENV["SIGNER_MLDSA_CA_ARN"], "mldsa44")
    for c in (cls, mld):
        assert c["template_arn"] == issue_cert.CODE_SIGNING_TEMPLATE_ARN


def test_idempotency_tokens_are_unchanged(calls):
    issued, _ = calls
    assert {c["idempotency_token"] for c in issued} == {
        "codesign-classical-pqc-firmware-signer",
        "codesign-mldsa-pqc-firmware-signer",
    }


def test_each_secret_gets_the_right_value(calls):
    _, stored = calls
    assert stored == {
        "secret/SIGNER_CLASSICAL_CA_SECRET": "CA:arn:ca/signer-classical",
        "secret/SIGNER_CLASSICAL_CERT_SECRET": "C:pqc-firmware-signer-classical",
        "secret/SIGNER_CLASSICAL_KEY_SECRET": "K:pqc-firmware-signer-classical",
        "secret/SIGNER_MLDSA_CA_SECRET": "CA:arn:ca/signer-mldsa",
        "secret/SIGNER_MLDSA_CERT_SECRET": "C:pqc-firmware-signer-mldsa",
        "secret/SIGNER_MLDSA_KEY_SECRET": "K:pqc-firmware-signer-mldsa",
    }
