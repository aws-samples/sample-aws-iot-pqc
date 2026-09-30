"""
Unit tests for the shared cert-issuer (issue_cert.py).

This is the ONE place all workshop TLS certs are minted (device + broker), so it
gets direct coverage. EC keygen/CSR uses the real system openssl (present on any
dev/CI box). ML-DSA keygen requires OpenSSL 3.5 (only in the cert-issuer image),
so that path is exercised only for its guard/error behavior here, not real
ML-DSA crypto. ACM PCA is mocked — no real AWS.
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import issue_cert  # noqa: E402


class _RequestInProgress(Exception):
    pass


class _FakeExceptions:
    RequestInProgressException = _RequestInProgress


class FakeAcmPca:
    """Minimal ACM PCA stub that returns pending once, then the cert."""

    def __init__(self, pending_rounds=1):
        self.exceptions = _FakeExceptions()
        self._gets = 0
        self._pending = pending_rounds
        self.issued = {}

    def issue_certificate(self, **kw):
        self.issued = kw
        return {"CertificateArn": kw["CertificateAuthorityArn"] + "/certificate/abc"}

    def get_certificate(self, **kw):
        self._gets += 1
        if self._gets <= self._pending:
            raise _RequestInProgress()
        return {"Certificate": "-----BEGIN CERTIFICATE-----\nLEAF\n-----END CERTIFICATE-----"}

    def get_certificate_authority_certificate(self, **kw):
        return {"Certificate": "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----"}


def test_key_modes_map_to_signing_algorithms():
    assert issue_cert.KEY_MODES["ec-p256"] == "SHA256WITHECDSA"
    assert issue_cert.KEY_MODES["mldsa44"] == "ML_DSA_44"


def test_sanitize_token_caps_and_strips():
    tok = issue_cert._sanitize_token("dev-ABC/123 xyz!@#" * 5)
    assert len(tok) <= 36
    assert all(c.isalnum() or c == "-" for c in tok)
    # Empty / junk falls back to a stable default.
    assert issue_cert._sanitize_token("!@#$") == "issue"


def test_generate_ec_key_and_csr_real_openssl(tmp_path):
    key, csr = issue_cert.generate_key_and_csr(
        "esp32-TEST123", "ec-p256", "AWS PQC Workshop", "IoT Device", "US", str(tmp_path)
    )
    assert os.path.exists(key) and os.path.exists(csr)
    out = subprocess.run(
        ["openssl", "req", "-in", csr, "-noout", "-subject"], capture_output=True, text=True
    )
    assert "esp32-TEST123" in out.stdout
    assert out.returncode == 0


def test_unknown_key_mode_raises(tmp_path):
    with pytest.raises(ValueError):
        issue_cert.generate_key_and_csr("cn", "rsa-9999", "O", "OU", "US", str(tmp_path))


def test_issue_certificate_end_to_end_mocked(monkeypatch):
    # Skip the real sleep in the poll loop.
    monkeypatch.setattr(issue_cert.time, "sleep", lambda *_: None)
    fake = FakeAcmPca(pending_rounds=1)

    res = issue_cert.issue_certificate(
        common_name="pqc-mqtt-broker",
        key_mode="ec-p256",
        ca_arn="arn:aws:acm-pca:us-east-1:111:certificate-authority/x",
        subject_ou="MQTT Broker",
        idempotency_token="broker-classical-pqc-mqtt-broker",
        acm_pca_client=fake,
    )

    assert set(res) == {"caCert", "cert", "key", "certificateArn"}
    assert "LEAF" in res["cert"]
    assert "CA" in res["caCert"]
    assert res["key"].startswith("-----BEGIN")
    assert res["certificateArn"].endswith("/certificate/abc")
    # The issuer chose the signing algorithm from the key mode.
    assert fake.issued["SigningAlgorithm"] == "SHA256WITHECDSA"
    assert fake.issued["Validity"] == {"Type": "DAYS", "Value": 365}
    assert len(fake.issued["IdempotencyToken"]) <= 36


def test_issue_certificate_unknown_key_mode():
    with pytest.raises(ValueError):
        issue_cert.issue_certificate(
            common_name="x", key_mode="bogus", ca_arn="arn:...", acm_pca_client=FakeAcmPca()
        )


def test_mldsa_requires_openssl35(monkeypatch, tmp_path):
    """When OpenSSL 3.5 is absent (as on a normal box), the ML-DSA path raises a
    clear error rather than silently falling back to a classical key."""
    monkeypatch.setattr(issue_cert.os.path, "exists", lambda p: False)
    with pytest.raises(RuntimeError) as exc:
        issue_cert.generate_key_and_csr("cn", "mldsa44", "O", "OU", "US", str(tmp_path))
    assert "3.5" in str(exc.value)
