"""Console + file logging with credential masking.

Spec section: 3.6
"""
from __future__ import annotations

import logging
import os
import re
from typing import List, Optional

_SENSITIVE_KEYS = ("accessKey", "secretKey", "password", "token", "privateKey")
_MASK_RE_PAIRS: List = []


def mask(value: str) -> str:
    """Keep first/last 2 chars; mask the middle."""
    s = str(value)
    if len(s) <= 4:
        return "****"
    return s[:2] + "****" + s[-2:]


def mask_text(text: str) -> str:
    """Mask credential-looking values in free text (key: value / key=value / json)."""
    for key in _SENSITIVE_KEYS:
        # JSON style: "accessKey": "AKID..."
        text = re.sub(
            r'("%s"\s*:\s*")([^"]{5,})(")' % re.escape(key),
            lambda m: m.group(1) + mask(m.group(2)) + m.group(3),
            text,
        )
        # shell/config style: accessKey=AKID... or --password xxx
        text = re.sub(
            r'(%s[= ])([^\s"\']{5,})' % re.escape(key),
            lambda m: m.group(1) + mask(m.group(2)),
            text,
        )
    # raw AWS-style AK/SK in free text
    text = re.sub(r"\b(AK[A-Z0-9]{16,})\b", lambda m: mask(m.group(1)), text)
    return text


class _MaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask_text(record.msg)
        if record.args:
            try:
                record.msg = record.msg % record.args
                record.args = None
            except Exception:
                pass
        return True


def setup_logger(log_file: Optional[str], name: str = "mcmodsync",
                 console: bool = True) -> logging.Logger:
    """Console + file logger; UTF-8; credential masking on all handlers."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    # fresh handlers each setup (subcommand processes are one-shot)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    mf = _MaskFilter()

    if console:
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        ch.addFilter(mf)
        logger.addHandler(ch)

    if log_file:
        d = os.path.dirname(os.path.abspath(log_file))
        os.makedirs(d, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        fh.addFilter(mf)
        logger.addHandler(fh)
    return logger


def get_logger(name: str = "mcmodsync") -> logging.Logger:
    return logging.getLogger(name)
