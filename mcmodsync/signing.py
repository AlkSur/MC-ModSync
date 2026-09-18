"""Ed25519 keypair generation, PEM loading and sign/verify (A side; cryptography).

Spec section: [6] signing.py —— keygen()、load_private_key(path)、sign/verify
（cryptography 实现，仅 A 端允许依赖第三方库）。

C 端不使用本模块: 其验签走 ed25519_min.py（纯标准库）。
本模块顶层 import 全为标准库，cryptography 在函数内惰性导入，
因此可被纯标准库环境 import（不触发第三方依赖）。
"""
from __future__ import annotations

import base64
import os
from typing import Tuple


class SigningError(Exception):
    """签名/验签相关的通用错误。"""


def _load_private(pem: bytes):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        key = load_pem_private_key(pem, password=None)
    except Exception as e:  # 格式错误 / 加密私钥等
        raise SigningError("私钥加载失败: %s" % e)
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningError("私钥不是 Ed25519 类型")
    return key


def keygen(private_key_path: str) -> Tuple[str, str]:
    """Generate Ed25519 keypair; write PEM (600 on POSIX); return (pub_b64, path)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = os.path.expanduser(private_key_path)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    if os.path.exists(path):
        raise FileExistsError("私钥已存在，拒绝覆盖: %s" % path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    pub_b64 = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")
    return pub_b64, path


def load_private_key(path: str) -> bytes:
    """Load PEM private key bytes from disk."""
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        raise FileNotFoundError("私钥不存在: %s（先运行 mcmodsync keygen）" % p)
    with open(p, "rb") as f:
        return f.read()


def sign(data: bytes, private_key_pem: bytes) -> bytes:
    """Ed25519-sign raw *data*; returns the 64-byte signature."""
    return _load_private(private_key_pem).sign(bytes(data))


def verify(data: bytes, signature: bytes, public_key_b64: str) -> bool:
    """Verify a 64-byte Ed25519 signature against a Base64 raw public key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature

    try:
        pub = base64.b64decode(public_key_b64 or "", validate=True)
    except Exception:
        return False
    if len(pub) != 32 or len(signature) != 64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(bytes(signature), bytes(data))
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


def public_key_b64_from_private(private_key_pem: bytes) -> str:
    """Derive the Base64 raw public key from a PEM private key."""
    pub = _load_private(private_key_pem).public_key().public_bytes_raw()
    return base64.b64encode(pub).decode("ascii")
