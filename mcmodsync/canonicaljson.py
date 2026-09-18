"""Canonical JSON serialization, Ed25519 signing and verification.

Spec section: 4.11 / 10.4

This module is stdlib-only (B-compatible). Signing/verification dispatch:
- if `cryptography` is available, use it (A side);
- otherwise delegate to ed25519_min (pure stdlib, verify-only for real keys;
  sign() requires cryptography on A).
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict

_ALG = "ed25519"


def canonical(obj: Any) -> bytes:
    """Deep sort keys, compact separators, UTF-8, no whitespace/newline."""
    return json.dumps(_sort_keys(obj), separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sort_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sort_keys(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [_sort_keys(v) for v in obj]
    return obj


def strip_signature(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Deep copy without the 'signature' field."""
    import copy
    clone = copy.deepcopy(obj)
    clone.pop("signature", None)
    return clone


def _have_cryptography() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except Exception:
        return False


def sign(obj: Dict[str, Any], privkey_pem: bytes) -> Dict[str, Any]:
    """Sign a dict with an Ed25519 private key (PEM); returns dict with signature."""
    if not _have_cryptography():
        raise RuntimeError("签名需要 cryptography 库（仅脚本 A 允许）")
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = load_pem_private_key(privkey_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("私钥不是 Ed25519 类型")
    sig = key.sign(canonical(strip_signature(obj)))
    out = dict(obj)
    out["signature"] = {"alg": _ALG, "value": base64.b64encode(sig).decode("ascii")}
    return out


def verify(obj: Dict[str, Any], pubkey_b64: str) -> bool:
    """Verify signature of *obj* with Base64 raw Ed25519 public key."""
    sig_obj = obj.get("signature") or {}
    if sig_obj.get("alg") != _ALG:
        return False
    try:
        sig = base64.b64decode(sig_obj.get("value", ""), validate=True)
        pub = base64.b64decode(pubkey_b64, validate=True)
    except Exception:
        return False
    if len(pub) != 32:
        return False
    msg = canonical(strip_signature(obj))
    if _have_cryptography():
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
            return True
        except InvalidSignature:
            return False
    from . import ed25519_min
    return ed25519_min.verify(pub, sig, msg)


def public_key_b64_from_private(privkey_pem: bytes) -> str:
    """Derive Base64 raw public key from a PEM private key (A side)."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = load_pem_private_key(privkey_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("私钥不是 Ed25519 类型")
    pub = key.public_key().public_bytes_raw()
    return base64.b64encode(pub).decode("ascii")
