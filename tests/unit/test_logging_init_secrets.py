"""deploy/logging/scripts/init-secrets.sh が作る証明書が X.509 の厳格検査を通ること（I-16）。

Python 3.13 から ``ssl.create_default_context()`` は ``VERIFY_X509_STRICT`` を立てる。
keyUsage の無い自前 CA はそれで拒否された
（担当C の試験用 search-assert.py が strict だけ外して回避していた）。
OpenSSL の ``verify -x509_strict`` は同じ flag（X509_V_FLAG_X509_STRICT）。理由は
docs/testing/logging-platform-rationale.md §6。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "logging" / "scripts" / "init-secrets.sh"


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("openssl") is None:
        pytest.skip("openssl が無い")
    out = tmp_path_factory.mktemp("secrets") / "test"
    subprocess.run(
        ["bash", str(SCRIPT), "--env", "test", "--dir", str(out)],
        check=True,
        capture_output=True,
        text=True,
    )
    return out / "pki"


def _openssl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["openssl", *args], capture_output=True, text=True, check=False)


def test_ca_declares_its_key_usage_and_constraints(pki: Path) -> None:
    ext = _openssl(
        "x509", "-in", str(pki / "ca.pem"), "-noout",
        "-ext", "basicConstraints,keyUsage,subjectKeyIdentifier",
    ).stdout  # fmt: skip
    assert "X509v3 Basic Constraints: critical" in ext and "CA:TRUE" in ext
    assert "X509v3 Key Usage: critical" in ext
    assert "Certificate Sign" in ext and "CRL Sign" in ext
    assert "Subject Key Identifier" in ext


@pytest.mark.parametrize(("name", "purpose"), [("node", "sslserver"), ("admin", "sslclient")])
def test_issued_certificates_pass_strict_verification(pki: Path, name: str, purpose: str) -> None:
    res = _openssl(
        "verify", "-x509_strict", "-purpose", purpose,
        "-CAfile", str(pki / "ca.pem"), str(pki / f"{name}.pem"),
    )  # fmt: skip
    assert res.returncode == 0, res.stdout + res.stderr
