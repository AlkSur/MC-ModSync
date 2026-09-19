"""Console + file logging with credential masking.

Spec section: 3.6
"""
from __future__ import annotations

import logging
import os
import re
import sys
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


class _ColorFormatter(logging.Formatter):
    """控制台专用：按级别 / 关键词着色。

    只挂在 console handler 上；文件 handler 继续用无色 formatter，
    避免 ANSI 转义码混进日志文件。重定向 / 非 tty 时自动降级为纯文本。
    """

    _LEVEL_COLOR = {
        logging.WARNING: "WARN",
        logging.ERROR: "FAIL",
        logging.CRITICAL: "FAIL",
    }
    # INFO 级里按内容高亮（顺序敏感：先判失败，再判警告，最后判成功）
    _FAIL_WORDS = ("失败", "错误", "不符", "不一致")
    _WARN_WORDS = ("警告", "跳过", "未识别", "缺失")
    _OK_WORDS = ("成功", "完成", "全绿", "已就绪", "通过")

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        try:
            from . import console
        except Exception:
            return text
        if not console.supports_color():
            return text
        level = self._LEVEL_COLOR.get(record.levelno)
        if level:
            return console.paint(text, level)
        if any(w in text for w in self._FAIL_WORDS):
            return console.paint(text, "FAIL")
        if any(w in text for w in self._WARN_WORDS):
            return console.paint(text, "WARN")
        if any(w in text for w in self._OK_WORDS):
            return console.paint(text, "PASS")
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
    color_fmt = _ColorFormatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    mf = _MaskFilter()

    if console:
        # 显式走 stdout：与 print（表头、摘要）同一条流，避免 stdout/stderr
        # 缓冲策略不同导致的顺序错乱；同时让 `> log.txt` 能拿到完整日志。
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(color_fmt)
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
