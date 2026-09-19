"""终端彩色输出（ANSI）。

设计约束：
- 只在 stdout 是**真实终端**时着色；被管道/重定向/测试捕获时自动退回纯文本，
  避免转义码污染日志文件。
- 尊重 NO_COLOR 环境变量（https://no-color.org）与 --no-color 参数。
- Windows 上主动开启控制台的 VT（虚拟终端）解析，否则老版 conhost
  会把转义码原样打印成乱码。

颜色约定（A 端 doctor 全局统一）：
    绿  = 通过 PASS
    红  = 报错 FAIL
    橙  = 异常/跳过/需注意 SKIP / WARN
"""
from __future__ import annotations

import os
import sys
from typing import Optional, TextIO

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

# 状态 -> 颜色
_LEVEL_COLOR = {
    "PASS": "\033[32m",   # 绿
    "OK": "\033[32m",
    "FAIL": "\033[31m",   # 红
    "ERROR": "\033[31m",
    "SKIP": "\033[38;5;214m",   # 橙
    "WARN": "\033[38;5;214m",
    "WARNING": "\033[38;5;214m",
}

_MARK = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}

_force_off = False   # 由 --no-color 设置


def disable_color() -> None:
    """关闭彩色（CLI --no-color）。"""
    global _force_off
    _force_off = True


def _enable_windows_vt() -> bool:
    """Windows 控制台开启 ANSI 转义解析；非 Windows 或失败返回 False。"""
    if os.name != "nt":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        return True
    except Exception:
        return False


def supports_color(stream: Optional[TextIO] = None) -> bool:
    """当前输出环境是否应该使用彩色。"""
    if _force_off:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("MCMODSYNC_COLOR") == "always":
        _enable_windows_vt()
        return True
    s = stream if stream is not None else sys.stdout
    try:
        if not hasattr(s, "isatty") or not s.isatty():
            return False
    except Exception:
        return False
    if os.name == "nt":
        _enable_windows_vt()
    return True


def bold(text: str) -> str:
    if not supports_color():
        return text
    return "%s%s%s" % (BOLD, text, RESET)


def dim(text: str) -> str:
    if not supports_color():
        return text
    return "%s%s%s" % (DIM, text, RESET)


def paint(text: str, level: str) -> str:
    """按 level 给 text 上色；不支持彩色时原样返回。"""
    color = _LEVEL_COLOR.get(level.upper())
    if not color or not supports_color():
        return text
    return "%s%s%s" % (color, text, RESET)


def status_mark(status: str) -> str:
    """返回带色的 [PASS] / [FAIL] / [SKIP] 标记。"""
    mark = _MARK.get(status, "[%s]" % status)
    return paint(mark, status)


def status_line(status: str, name: str, detail: str = "") -> str:
    """拼一行 doctor 检查结果：'[PASS] 名称 — 详情'。"""
    line = "%s %s" % (status_mark(status), name)
    if detail:
        line += " — " + (paint(detail, status) if status == "FAIL" else detail)
    return line
