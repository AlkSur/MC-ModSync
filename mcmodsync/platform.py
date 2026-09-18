"""Platform helpers: same-device check and game-running detection.

Spec sections: 5.2-2 (st_dev), 9.2-3 (game process detection)
"""
from __future__ import annotations

import os

GAME_PROC_KEYWORDS = ("java", "javaw", "minecraft")


def same_device(a: str, b: str) -> bool:
    """True if both paths are on the same filesystem (st_dev)."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def disk_usage(path: str):
    """Wrapper for shutil.disk_usage (test seams can monkeypatch this)."""
    import shutil
    return shutil.disk_usage(path)


def is_game_running() -> bool:
    """Detect java/javaw/minecraft processes among running processes.

    Windows: enumerate via psutil-free approach (toolhelp snapshot through
    ctypes); POSIX: scan /proc/<pid>/comm and cmdline.
    """
    if os.name == "nt":
        return _win_game_running()
    return _posix_game_running()


def _matches(name: str) -> bool:
    low = name.lower()
    for kw in GAME_PROC_KEYWORDS:
        if kw in low:
            return True
    return False


def _posix_game_running() -> bool:
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            for fname in ("comm", "cmdline"):
                try:
                    with open(os.path.join("/proc", pid, fname), "rb") as f:
                        data = f.read()
                except OSError:
                    continue
                if _matches(data.decode("utf-8", "ignore")):
                    return True
    except OSError:
        return False
    return False


def _win_game_running() -> bool:
    import ctypes
    import ctypes.wintypes as wt

    TH32CS_SNAPPROCESS = 0x2
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wt.DWORD),
            ("szExeFile", wt.WCHAR * 260),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return False
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if _matches(entry.szExeFile):
                return True
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        return False
    finally:
        kernel32.CloseHandle(snap)
