"""TL(T-11): signing.py —— A 端 cryptography 实现，并与 ed25519_min 跨实现互验。"""
from __future__ import annotations

import base64
import os
import stat

import pytest

from mcmodsync import ed25519_min, signing


def test_keygen_creates_key_and_returns_pub(tmp_path) -> None:
    pub_b64, path = signing.keygen(str(tmp_path / "sub" / "private.key"))
    assert os.path.isfile(path)
    assert len(base64.b64decode(pub_b64)) == 32
    assert signing.load_private_key(path)[:27] == b"-----BEGIN PRIVATE KEY-----"
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_keygen_refuses_overwrite(tmp_path) -> None:
    p = str(tmp_path / "private.key")
    signing.keygen(p)
    with pytest.raises(FileExistsError):
        signing.keygen(p)


def test_load_missing_key(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        signing.load_private_key(str(tmp_path / "nope.key"))


class TestSignVerify:
    @pytest.mark.parametrize("size", [0, 1, 64, 1024, 100000])
    def test_roundtrip(self, tmp_path, size: int) -> None:
        pub_b64, path = signing.keygen(str(tmp_path / "private.key"))
        pem = signing.load_private_key(path)
        data = os.urandom(size)
        sig = signing.sign(data, pem)
        assert len(sig) == 64
        assert signing.verify(data, sig, pub_b64) is True

    def test_deterministic(self, tmp_path) -> None:
        pub_b64, path = signing.keygen(str(tmp_path / "private.key"))
        pem = signing.load_private_key(path)
        data = b"stable"
        assert signing.sign(data, pem) == signing.sign(data, pem)

    def test_wrong_key_fails(self, tmp_path) -> None:
        _, pa = signing.keygen(str(tmp_path / "a.key"))
        pub_b, _ = signing.keygen(str(tmp_path / "b.key"))
        sig = signing.sign(b"m", signing.load_private_key(pa))
        assert signing.verify(b"m", sig, pub_b) is False

    def test_tampered_fails(self, tmp_path) -> None:
        pub_b64, path = signing.keygen(str(tmp_path / "private.key"))
        pem = signing.load_private_key(path)
        sig = signing.sign(b"m", pem)
        assert signing.verify(b"m!", sig, pub_b64) is False
        bad = bytearray(sig)
        bad[0] ^= 0x01
        assert signing.verify(b"m", bytes(bad), pub_b64) is False

    def test_bad_inputs_fail(self, tmp_path) -> None:
        pub_b64, path = signing.keygen(str(tmp_path / "private.key"))
        sig = signing.sign(b"m", signing.load_private_key(path))
        assert signing.verify(b"m", sig, "not-base64!!") is False
        assert signing.verify(b"m", b"short", pub_b64) is False
        assert signing.verify(b"m", sig, "") is False

    def test_public_key_from_private(self, tmp_path) -> None:
        pub_b64, path = signing.keygen(str(tmp_path / "private.key"))
        assert signing.public_key_b64_from_private(
            signing.load_private_key(path)) == pub_b64

    def test_bad_pem_raises(self) -> None:
        with pytest.raises(signing.SigningError):
            signing.sign(b"m", b"not a pem")


class TestCrossImplementation:
    """A 端（cryptography）签名 -> C 端（ed25519_min）验签。"""

    @pytest.mark.parametrize("size", [0, 1, 64, 1023, 1024])
    def test_a_signs_c_verifies(self, tmp_path, size: int) -> None:
        pub_b64, path = signing.keygen(str(tmp_path / "private.key"))
        pem = signing.load_private_key(path)
        data = os.urandom(size)
        sig = signing.sign(data, pem)
        assert ed25519_min.verify(base64.b64decode(pub_b64), sig, data) is True

    def test_canonical_payload_a_signs_c_verifies(self, tmp_path) -> None:
        from mcmodsync import canonicaljson

        pub_b64, path = signing.keygen(str(tmp_path / "private.key"))
        pem = signing.load_private_key(path)
        obj = {"schemaVersion": 1, "packId": "p", "latest": "1.0.0"}
        signed = canonicaljson.sign(obj, pem)

        sig = base64.b64decode(signed["signature"]["value"])
        msg = canonicaljson.canonical(canonicaljson.strip_signature(signed))
        assert ed25519_min.verify(base64.b64decode(pub_b64), sig, msg) is True
