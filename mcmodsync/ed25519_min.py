"""Pure-stdlib Ed25519 verification (RFC 8032). C-side use only.

Spec section: 10.5. Implements verification only; signing requires
the `cryptography` library (script A only).
"""
from __future__ import annotations

import hashlib

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _xrecover(y: int) -> int:
    xx = ((y * y - 1) * _inv(_D * y * y + 1)) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if (x * x - xx) % _P != 0:
        raise ValueError("point decompression failed")
    if x % 2 != 0:
        x = _P - x
    return x


_By = (4 * _inv(5)) % _P
_Bx = _xrecover(_By)
_B = (_Bx % _P, _By % _P, 1, (_Bx * _By) % _P)
_IDENT = (0, 1, 1, 0)


def _add(p1: tuple, p2: tuple) -> tuple:
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = 2 * _D * t1 * t2 % _P
    dd = 2 * z1 * z2 % _P
    e = b - a
    f = dd - c
    g = dd + c
    h = b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(point: tuple, e: int) -> tuple:
    q = _IDENT
    while e > 0:
        if e & 1:
            q = _add(q, point)
        point = _add(point, point)
        e >>= 1
    return q


def _compress(point: tuple) -> bytes:
    x, y, z, _t = point
    zi = _inv(z)
    x = x * zi % _P
    y = y * zi % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return None
    try:
        x = _xrecover(y)
    except ValueError:
        return None
    if x & 1 != sign:
        x = _P - x
    return (x, y, 1, x * y % _P)


def verify(pub: bytes, sig: bytes, msg: bytes) -> bool:
    """RFC 8032 Ed25519 verification. Returns True/False, never raises."""
    if not isinstance(pub, (bytes, bytearray)) or not isinstance(sig, (bytes, bytearray)):
        return False
    if len(pub) != 32 or len(sig) != 64:
        return False
    pub = bytes(pub)
    sig = bytes(sig)
    a = _decompress(pub)
    if a is None:
        return False
    r = _decompress(sig[:32])
    if r is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + pub + bytes(msg)).digest(), "little") % _L
    lhs = _mul(_B, s)
    rhs = _add(r, _mul(a, h))
    return _compress(lhs) == _compress(rhs)
