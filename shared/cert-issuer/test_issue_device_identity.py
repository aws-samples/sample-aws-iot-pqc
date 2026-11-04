# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for issue_device_identity.py (the device identity partition builder).

ACM PCA is replaced by a fake that REALLY signs the submitted CSR with a local
test CA, so key generation, the key/cert match check and the DER conversions run
for real. ML-DSA needs an OpenSSL with ML-DSA-44 (3.5+): the cert-issuer image
has one; on a dev box the system openssl is used if it supports ML-DSA,
otherwise those tests are skipped. Building the partition needs
esp-idf-nvs-partition-gen (requirements-dev.txt); skipped if absent.
"""
import importlib.util
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import issue_cert  # noqa: E402
import issue_device_identity as idi  # noqa: E402

SSID = "spike-test-ssid"
PSK = "spike-test-password"
CLS_ARN = "arn:aws:acm-pca:us-east-1:111:certificate-authority/classical"
MLDSA_ARN = "arn:aws:acm-pca:us-east-1:111:certificate-authority/mldsa"
SIG_CLS_ARN = "arn:aws:acm-pca:us-east-1:111:certificate-authority/signer-classical"
SIG_MLDSA_ARN = "arn:aws:acm-pca:us-east-1:111:certificate-authority/signer-mldsa"
SIGNERS = dict(signer_classical_ca_arn=SIG_CLS_ARN, signer_mldsa_ca_arn=SIG_MLDSA_ARN)


def _openssl(args, bin_dir=None, **kwargs):
    """Run the static 'openssl' command, optionally from a specific bin dir.

    The command is always the literal 'openssl'; which build runs is chosen by
    putting bin_dir first on PATH (the ML-DSA-capable OpenSSL 3.5 lives outside
    the default PATH in the cert-issuer image).
    """
    env = dict(os.environ)
    if bin_dir:
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return subprocess.run(["openssl", *args], env=env, **kwargs)


def _mldsa_openssl():
    """The path of an ML-DSA-44 capable openssl, or None."""
    for cand in (issue_cert.OPENSSL35_BIN, shutil.which("openssl")):
        if cand and os.path.exists(cand):
            probe = _openssl(["genpkey", "-algorithm", "ML-DSA-44"],
                             bin_dir=os.path.dirname(cand), capture_output=True)
            if probe.returncode == 0:
                return cand
    return None


MLDSA_OPENSSL = _mldsa_openssl()
MLDSA_OPENSSL_DIR = os.path.dirname(MLDSA_OPENSSL) if MLDSA_OPENSSL else None
HAVE_NVS_GEN = importlib.util.find_spec("esp_idf_nvs_partition_gen") is not None
needs_crypto = pytest.mark.skipif(MLDSA_OPENSSL is None, reason="no ML-DSA-capable openssl")
needs_nvs = pytest.mark.skipif(not HAVE_NVS_GEN, reason="esp-idf-nvs-partition-gen not installed")


class SigningPca:
    """ACM PCA stand-in: signs CSRs with a local CA per ARN (EC or ML-DSA)."""

    class exceptions:  # noqa: N801 - mirrors boto3 client attribute
        class RequestInProgressException(Exception):
            pass

    def __init__(self, workdir):
        self.dir = workdir
        self.cas = {}
        self.leaves = {}
        self.tokens = []

    def _ca(self, arn):
        if arn not in self.cas:
            tag = "mldsa" if "mldsa" in arn else "cls"
            name = arn.rsplit("/", 1)[-1]   # one key + cert per CA ARN
            key, crt = (os.path.join(self.dir, f"ca_{name}.{x}") for x in ("key", "pem"))
            # bin dir of the openssl that handles this CA (None = default PATH)
            o = MLDSA_OPENSSL_DIR if tag == "mldsa" else None
            if tag == "mldsa":
                _openssl(["genpkey", "-algorithm", "ML-DSA-44", "-out", key],
                         bin_dir=o, check=True, capture_output=True)
            else:
                _openssl(["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", key],
                         check=True, capture_output=True)
            _openssl(["req", "-x509", "-new", "-key", key, "-subj", f"/CN=Test {name} CA",
                      "-days", "2", "-out", crt], bin_dir=o, check=True, capture_output=True)
            self.cas[arn] = (o, key, crt)
        return self.cas[arn]

    def issue_certificate(self, CertificateAuthorityArn, Csr, IdempotencyToken, **_):
        self.tokens.append(IdempotencyToken)
        o, key, crt = self._ca(CertificateAuthorityArn)
        n = len(self.leaves)
        csr, leaf = (os.path.join(self.dir, f"leaf{n}.{x}") for x in ("csr", "pem"))
        with open(csr, "wb") as f:
            f.write(Csr)
        _openssl(["x509", "-req", "-in", csr, "-CA", crt, "-CAkey", key,
                  "-CAcreateserial", "-days", "2", "-out", leaf],
                 bin_dir=o, check=True, capture_output=True)
        arn = f"{CertificateAuthorityArn}/certificate/{n}"
        self.leaves[arn] = leaf
        return {"CertificateArn": arn}

    def get_certificate(self, CertificateArn, **_):
        with open(self.leaves[CertificateArn], encoding="utf-8") as f:
            return {"Certificate": f.read()}

    def get_certificate_authority_certificate(self, CertificateAuthorityArn):
        with open(self._ca(CertificateAuthorityArn)[2], encoding="utf-8") as f:
            return {"Certificate": f.read()}


@pytest.fixture
def pca(tmp_path, monkeypatch):
    monkeypatch.setattr(issue_cert, "OPENSSL35_BIN", MLDSA_OPENSSL or issue_cert.OPENSSL35_BIN)
    monkeypatch.setattr(issue_cert.time, "sleep", lambda *_: None)
    d = tmp_path / "ca"
    d.mkdir()
    return SigningPca(str(d))


# ── validation ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,ok", [("c69de0", "C69DE0"), (" FC22E0 ", "FC22E0")])
def test_validate_serial_normalises(raw, ok):
    assert idi.validate_serial(raw) == ok


@pytest.mark.parametrize("bad", ["", "AB", "C69DE0/../x", "esp32-C69DE0", "A" * 17, None])
def test_validate_serial_rejects(bad):
    with pytest.raises(ValueError):
        idi.validate_serial(bad)


@pytest.mark.parametrize("ssid,psk", [("", PSK), ("x" * 33, PSK), (SSID, "short"), (SSID, "p" * 64)])
def test_validate_wifi_rejects(ssid, psk):
    with pytest.raises(ValueError):
        idi.validate_wifi(ssid, psk)


def test_s3_key_layout():
    assert idi.s3_key("C69DE0") == "identity/C69DE0/certs.bin"


def test_build_csv_layout_matches_firmware_contract():
    blobs = {k: f"/w/{k}.der" for k in idi.BLOB_KEYS}
    assert {"sig_cls", "sig_mldsa"} <= set(blobs)
    lines = idi.build_csv("C69DE0", SSID, PSK, blobs).splitlines()
    assert lines[0] == "key,type,encoding,value"
    assert lines[1] == "devid,namespace,,"
    assert "fmt,data,u8,2" in lines
    assert "serial,data,string,C69DE0" in lines
    assert f"wifi_psk,data,string,{PSK}" in lines
    for k, path in blobs.items():
        assert f"{k},file,binary,{path}" in lines
        assert len(k) <= 15  # NVS key-name limit


@pytest.mark.parametrize("bad", ["a,b", 'a"b', "a\nb"])
def test_build_csv_rejects_unsafe_wifi_values(bad):
    with pytest.raises(ValueError):
        idi.build_csv("C69DE0", SSID, f"password{bad}", {})


# ── issuance with a real-signing fake PCA ─────────────────────────────────────

@needs_crypto
def test_to_der_produces_expected_blobs(pca, tmp_path):
    identity = {
        "classical": issue_cert.issue_certificate("esp32-T1", "ec-p256", CLS_ARN, acm_pca_client=pca),
        "mldsa": issue_cert.issue_certificate("esp32-T1", "mldsa44", MLDSA_ARN, acm_pca_client=pca),
    }
    w = tmp_path / "w"
    w.mkdir()
    blobs = idi.to_der(str(w), identity)
    assert set(blobs) == {"ca_cls", "ca_mldsa", "crt_cls", "key_cls", "crt_mldsa", "key_mldsa"}
    size = {k: os.path.getsize(v) for k, v in blobs.items()}
    assert size["key_mldsa"] == 54          # seed-only PKCS#8
    assert 100 < size["key_cls"] < 140      # SEC1 P-256
    assert size["crt_mldsa"] > 3500         # ML-DSA-44 cert
    for k, p in blobs.items():
        with open(p, "rb") as f:
            assert f.read(1) == b"\x30", k  # DER SEQUENCE, not PEM


@needs_crypto
def test_mismatched_key_and_cert_is_rejected(pca, tmp_path):
    a = issue_cert.issue_certificate("esp32-T1", "ec-p256", CLS_ARN, acm_pca_client=pca)
    b = issue_cert.issue_certificate("esp32-T2", "ec-p256", CLS_ARN, acm_pca_client=pca)
    m = issue_cert.issue_certificate("esp32-T1", "mldsa44", MLDSA_ARN, acm_pca_client=pca)
    a["key"] = b["key"]
    w = tmp_path / "w"
    w.mkdir()
    with pytest.raises(RuntimeError, match="does not match"):
        idi.to_der(str(w), {"classical": a, "mldsa": m})


@needs_crypto
@needs_nvs
def test_build_identity_end_to_end(pca, tmp_path):
    out = tmp_path / "certs.bin"
    summary = idi.build_identity("c69de0", CLS_ARN, MLDSA_ARN, SSID, PSK, str(out),
                                 acm_pca_client=pca, **SIGNERS)
    assert out.stat().st_size == 0x10000
    assert summary["serial"] == "C69DE0"
    assert summary["blobSizes"]["key_mldsa"] == 54
    assert summary["format"] == 2
    assert summary["blobSizes"]["sig_mldsa"] > 3500   # ML-DSA-44 signing root
    assert 300 < summary["blobSizes"]["sig_cls"] < 700
    # No secret material in the summary that ends up in the build log.
    text = str(summary)
    assert PSK not in text and "PRIVATE" not in text
    # Fresh, distinct idempotency tokens per cert, within ACM PCA's 36-char limit.
    assert len(set(pca.tokens)) == 2
    assert all(len(issue_cert._sanitize_token(t)) == len(t) <= 36 for t in pca.tokens)
    # The WiFi password is in the image (it is the device's config) ...
    assert PSK.encode() in out.read_bytes()


@needs_crypto
@needs_nvs
def test_reissue_uses_new_tokens(pca, tmp_path):
    idi.build_identity("C69DE0", CLS_ARN, MLDSA_ARN, SSID, PSK, str(tmp_path / "a.bin"),
                       acm_pca_client=pca, **SIGNERS)
    idi.build_identity("C69DE0", CLS_ARN, MLDSA_ARN, SSID, PSK, str(tmp_path / "b.bin"),
                       acm_pca_client=pca, **SIGNERS)
    assert len(set(pca.tokens)) == 4


def test_upload_writes_identity_key():
    class FakeS3:
        def put_object(self, **kw):
            self.kw = kw
            return {"VersionId": "v1"}

    s3 = FakeS3()
    res = idi.upload(__file__, "bucket-1", "C69DE0", s3_client=s3)
    assert res == {"bucket": "bucket-1", "key": "identity/C69DE0/certs.bin", "versionId": "v1"}
    assert s3.kw["Bucket"] == "bucket-1"


def test_main_requires_inputs(monkeypatch):
    for v in ("DEVICE_SERIAL", "CA_ARN", "MLDSA_CA_ARN", "FIRMWARE_BUCKET",
              "SIGNER_CLASSICAL_CA_ARN", "SIGNER_MLDSA_CA_ARN"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(SystemExit):
        idi.main(["--no-upload"])


@needs_crypto
@needs_nvs
def test_signing_roots_are_in_the_partition_and_differ_from_device_cas(pca, tmp_path):
    out = tmp_path / "certs.bin"
    idi.build_identity("C69DE0", CLS_ARN, MLDSA_ARN, SSID, PSK, str(out),
                       acm_pca_client=pca, **SIGNERS)
    image = out.read_bytes()
    der = lambda arn: _openssl(  # noqa: E731
        ["x509", "-in", pca.cas[arn][2], "-outform", "DER"],
        bin_dir=pca.cas[arn][0], check=True, capture_output=True).stdout
    # NVS splits blobs over ~4 KB into per-page chunks, so match each root's
    # first 256 bytes (it includes the unique serial number) rather than the
    # whole DER.
    for arn in (CLS_ARN, MLDSA_ARN, SIG_CLS_ARN, SIG_MLDSA_ARN):
        assert der(arn)[:256] in image, arn
    # The job only READS the signing roots: it never asks them to issue.
    issued_by = {t.split("/certificate/")[0] for t in pca.leaves}
    assert issued_by == {CLS_ARN, MLDSA_ARN}


@pytest.mark.parametrize("signers", [
    dict(signer_classical_ca_arn=CLS_ARN, signer_mldsa_ca_arn=SIG_MLDSA_ARN),
    dict(signer_classical_ca_arn=SIG_CLS_ARN, signer_mldsa_ca_arn=MLDSA_ARN),
    dict(signer_classical_ca_arn="", signer_mldsa_ca_arn=SIG_MLDSA_ARN),
])
def test_rejects_signing_root_that_is_a_device_ca_or_missing(tmp_path, signers):
    with pytest.raises(ValueError):
        idi.build_identity("C69DE0", CLS_ARN, MLDSA_ARN, SSID, PSK, str(tmp_path / "x.bin"),
                           acm_pca_client=object(), **signers)


@needs_crypto
def test_same_root_certificate_is_rejected(pca, tmp_path):
    """Two ARNs that serve the same certificate are caught by comparing the DER."""
    identity = {
        "classical": issue_cert.issue_certificate("esp32-T1", "ec-p256", CLS_ARN, acm_pca_client=pca),
        "mldsa": issue_cert.issue_certificate("esp32-T1", "mldsa44", MLDSA_ARN, acm_pca_client=pca),
    }
    w = tmp_path / "w"
    w.mkdir()
    blobs = idi.to_der(str(w), identity)
    pca.cas[SIG_CLS_ARN] = pca.cas[CLS_ARN]          # alias the device CA
    with pytest.raises(RuntimeError, match="separate CA"):
        idi.signer_roots_to_der(str(w), pca, SIG_CLS_ARN, SIG_MLDSA_ARN, blobs)
