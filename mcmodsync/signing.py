"""Ed25519 keypair generation and PEM key loading (A side; requires cryptography).

Spec section: 7.9
"""
from __future__ import annotations

import base64
import os
from typing import Tuple


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
