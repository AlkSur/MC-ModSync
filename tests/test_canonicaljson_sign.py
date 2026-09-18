"""TL-1: canonicaljson.py —— 规范化、排除 signature、签名稳定性。"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from mcmodsync import canonicaljson
from mcmodsync import signing

REPO_ROOT = str(Path(__file__).resolve().parents[1])

OBJ = {
    "schemaVersion": 1,
    "packId": "p",
    "files": [{"path": "mods/b.jar", "size": 2, "sha256": "bb"},
              {"path": "mods/a.jar", "size": 1, "sha256": "aa"}],
    "nested": {"z": 1, "a": {"y": 2, "x": 3}},
}


class TestCanonical:
    def test_deep_key_sort_and_compact(self) -> None:
        got = canonicaljson.canonical(OBJ).decode("utf-8")
        assert " " not in got
        assert "\n" not in got
        # 顶层与嵌套 key 均按字典序
        assert got.startswith('{"files":')
        assert '"nested":{"a":{"x":3,"y":2},"z":1}' in got

    def test_output_is_utf8_bytes(self) -> None:
        out = canonicaljson.canonical({"k": "中文"})
        assert isinstance(out, bytes)
        assert "中文" in out.decode("utf-8")

    def test_key_order_does_not_matter(self) -> None:
        a = canonicaljson.canonical({"b": 1, "a": 2})
        b = canonicaljson.canonical({"a": 2, "b": 1})
        assert a == b

    def test_stable_across_processes(self) -> None:
        local = hashlib.sha256(canonicaljson.canonical(OBJ)).hexdigest()
        code = (
            "import hashlib, json, sys\n"
            "sys.path.insert(0, %r)\n"
            "from mcmodsync import canonicaljson\n"
            "obj = json.loads(sys.argv[1])\n"
            "print(hashlib.sha256(canonicaljson.canonical(obj)).hexdigest())\n"
            % REPO_ROOT
        )
        out = subprocess.run([sys.executable, "-c", code,
                              json.dumps(OBJ, ensure_ascii=False)],
                             capture_output=True, text=True, check=True)
        assert out.stdout.strip() == local


class TestStripSignature:
    def test_removes_signature(self) -> None:
        obj = dict(OBJ, signature={"alg": "ed25519", "value": "x"})
        stripped = canonicaljson.strip_signature(obj)
        assert "signature" not in stripped
        assert "signature" in obj  # 不修改原对象

    def test_deep_copy(self) -> None:
        obj = {"a": {"b": [1, 2]}, "signature": {"alg": "ed25519"}}
        stripped = canonicaljson.strip_signature(obj)
        stripped["a"]["b"].append(3)
        assert obj["a"]["b"] == [1, 2]


class TestSignVerify:
    def test_roundtrip_and_excludes_signature(self, tmp_path) -> None:
        pub_b64, key_path = signing.keygen(str(tmp_path / "private.key"))
        pem = signing.load_private_key(key_path)

        signed = canonicaljson.sign(OBJ, pem)
        assert signed["signature"]["alg"] == "ed25519"
        assert canonicaljson.verify(signed, pub_b64) is True

        # signature 字段本身不参与被签内容：篡改 value 仍应被剥离后验签
        signed2 = dict(signed)
        signed2["signature"] = {"alg": "ed25519", "value": "AAAA"}
        assert canonicaljson.verify(signed2, pub_b64) is False

    def test_tampered_payload_fails(self, tmp_path) -> None:
        pub_b64, key_path = signing.keygen(str(tmp_path / "private.key"))
        pem = signing.load_private_key(key_path)
        signed = canonicaljson.sign(OBJ, pem)

        tampered = json.loads(json.dumps(signed))
        tampered["nested"]["z"] = 999
        assert canonicaljson.verify(tampered, pub_b64) is False

    def test_wrong_public_key_fails(self, tmp_path) -> None:
        pub_a, key_a = signing.keygen(str(tmp_path / "a.key"))
        pub_b, _ = signing.keygen(str(tmp_path / "b.key"))
        signed = canonicaljson.sign(OBJ, signing.load_private_key(key_a))
        assert canonicaljson.verify(signed, pub_b) is False

    def test_public_key_from_private_matches(self, tmp_path) -> None:
        pub_b64, key_path = signing.keygen(str(tmp_path / "private.key"))
        assert canonicaljson.public_key_b64_from_private(
            signing.load_private_key(key_path)) == pub_b64

    def test_verify_rejects_garbage(self, tmp_path) -> None:
        pub_b64, key_path = signing.keygen(str(tmp_path / "private.key"))
        signed = canonicaljson.sign(OBJ, signing.load_private_key(key_path))
        assert canonicaljson.verify(signed, "not-base64!!") is False
        assert canonicaljson.verify({"a": 1}, pub_b64) is False
