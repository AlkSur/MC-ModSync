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
import re
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


def enable_vt() -> bool:
    """开启 ANSI 转义解析并返回“本进程能否安全输出 ANSI”。

    - 非 Windows 平台终端默认支持，返回 True；
    - Windows 上主动 SetConsoleMode 打开 VT，失败（老 conhost）返回 False，
      调用方应据此退回“空格补残影”的降级渲染。

    注意：这与着色开关无关，进度条清行（\\x1b[K）也依赖它，因此独立暴露。
    """
    if os.name != "nt":
        return True
    return _enable_windows_vt()


def is_interactive(stream: Optional[TextIO] = None) -> bool:
    """输出是否连到真实终端（管道 / 重定向 / 日志文件时为假）。"""
    s = stream if stream is not None else sys.stdout
    try:
        return bool(s.isatty())
    except Exception:
        return False


# --------------------------------------------------------------------------
# 终端哈希屏蔽（需求：控制台全程不展示 sha256，哈希细节只写日志文件）
# --------------------------------------------------------------------------

_HASH_KV_RE = re.compile(r"sha256([:=\s]+)[0-9a-fA-F]{8,}")
_HEX64_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{64}(?![0-9a-fA-F])")
_HASH_PLACEHOLDER = "见日志"


def strip_hashes(text: str) -> str:
    """把文本中的 sha256 值替换为占位符（仅用于终端；日志文件保留原文）。"""
    out = _HASH_KV_RE.sub(r"sha256\1" + _HASH_PLACEHOLDER, text)
    return _HEX64_RE.sub(_HASH_PLACEHOLDER, out)


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


# --------------------------------------------------------------------------
# 按内容关键词判定颜色（A 端日志与 C 端控制台共用同一套语义）
# 顺序敏感：先判失败，再判警告，最后判成功。
# --------------------------------------------------------------------------

FAIL_WORDS = ("失败", "错误", "不符", "不一致", "不匹配", "篡改", "异常", "拒绝")
WARN_WORDS = ("警告", "跳过", "未识别", "缺失", "回退")
OK_WORDS = ("成功", "完成", "全绿", "已就绪", "通过", "已下载", "HTTP 200")


def level_of_text(text: str) -> Optional[str]:
    """按关键词给出颜色级别；无匹配返回 None（保持默认色）。"""
    if any(w in text for w in FAIL_WORDS):
        return "FAIL"
    if any(w in text for w in WARN_WORDS):
        return "WARN"
    if any(w in text for w in OK_WORDS):
        return "PASS"
    return None


def paint_text(text: str) -> str:
    """整行按关键词着色（无匹配则原样返回）。"""
    lv = level_of_text(text)
    return paint(text, lv) if lv else text


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
