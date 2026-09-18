"""Relative path normalization and safety checks.

Spec section: 10.1
"""
from __future__ import annotations

import os
import re
from typing import Union

EXIT_UNSAFE_PATH = 13

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class UnsafePath(ValueError):
    """Raised when a relative path is illegal or escapes its base."""

    exit_code = EXIT_UNSAFE_PATH


def normalize_rel(p: str) -> str:
    """Normalize a manifest-relative path; reject illegal forms.

    - unify backslashes to forward slashes
    - strip a leading "./"
    - reject: empty, absolute, Windows drive prefix, any ".." segment
    """
    if not isinstance(p, str):
        raise UnsafePath("路径必须是字符串")
    s = p.replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    if s == "":
        raise UnsafePath("空路径: %r" % (p,))
    if s.startswith("/"):
        raise UnsafePath("绝对路径被拒绝: %r" % (p,))
    if _DRIVE_RE.match(s):
        raise UnsafePath("Windows 盘符路径被拒绝: %r" % (p,))
    for seg in s.split("/"):
        if seg == "..":
            raise UnsafePath("包含 .. 的路径被拒绝: %r" % (p,))
    return s


def is_within(child: str, parent: str) -> bool:
    """True if resolved *child* equals or lives under resolved *parent*."""
    try:
        c = os.path.realpath(child)
        p = os.path.realpath(parent)
        return os.path.commonpath([c, p]) == p
    except (ValueError, OSError):
        return False


def safe_join(base: Union[str, os.PathLike], rel: str) -> str:
    """Join *base* with a normalized *rel*; guarantee the result stays inside base."""
    norm = normalize_rel(rel)
    base_s = os.fspath(base)
    candidate = os.path.normpath(os.path.join(base_s, norm))
    if not is_within(candidate, base_s):
        raise UnsafePath("路径越界: %r" % (rel,))
    return candidate
