# GENERATED, do not edit
# 由 tools/build_server.py 拼接生成（白名单纯标准库共享模块 + server/b_main.py）。
# 请勿手工修改：CI 会重新生成并做防漂移 diff。

from __future__ import annotations

import hashlib
import os
from typing import Dict, List, Optional
import re
from typing import Union
from typing import Dict, List
import json
from typing import Any, Dict, List, Optional
from typing import Callable, Dict, List, Optional
import base64
from typing import Any, Dict
import sys
from datetime import datetime, timezone


# ===== hashing.py =====

"""SHA-256 hashing and flat mods scanning.

Spec sections: 3.3, 10.3
"""

CHUNK = 1024 * 1024

EXIT_IO_ERROR = 19

class FileEntry(dict):
    """Manifest file entry: path/sha256/size plus optional extras."""

    def __init__(self, path: str, sha256: str, size: int, **extra: object) -> None:
        super().__init__(path=path, sha256=sha256, size=size)
        self.update(extra)

def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def hash_file(path: str) -> Dict[str, object]:
    """Stream-hash a file; returns {"sha256": hex, "size": bytes}."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
            size += len(block)
    return {"sha256": h.hexdigest(), "size": size}

def is_jar(name: str) -> bool:
    return name.lower().endswith(".jar")

def scan_tree(root: str, subdir: str = "mods", exts: tuple = (".jar",),
              recursive: bool = False) -> List[FileEntry]:
    """Scan root/subdir for flat files matching exts (case-insensitive).

    - one level only when recursive is False (spec: no subdirectory recursion)
    - does not follow symlinks
    - returns entries with "path" prefixed as "<subdir>/<name>" using "/"
    """
    target = os.path.join(root, subdir)
    if not os.path.isdir(target):
        return []
    exts_l = tuple(e.lower() for e in exts)
    entries: List[FileEntry] = []
    if recursive:
        walker = os.walk(target, followlinks=False)
        for dirpath, _dirnames, filenames in walker:
            for name in filenames:
                if not name.lower().endswith(exts_l):
                    continue
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                rel = os.path.relpath(full, root).replace("\\", "/")
                info = hash_file(full)
                entries.append(FileEntry(rel, str(info["sha256"]), int(info["size"])))
    else:
        with os.scandir(target) as it:
            for d in it:
                if d.is_symlink() or not d.is_file():
                    continue
                if not d.name.lower().endswith(exts_l):
                    continue
                rel = subdir + "/" + d.name
                info = hash_file(d.path)
                entries.append(FileEntry(rel, str(info["sha256"]), int(info["size"])))
    entries.sort(key=lambda e: e["path"])
    return entries

def entry_map(entries: List[dict]) -> Dict[str, dict]:
    """List of entries -> {path: entry} map (planner input)."""
    return {e["path"]: e for e in entries}

def mtime_of(path: str) -> Optional[int]:
    try:
        return int(os.stat(path).st_mtime)
    except OSError:
        return None


# ===== paths.py =====

"""Relative path normalization and safety checks.

Spec section: 10.1
"""

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


# ===== planner.py =====

"""Change planner: pure diff between base and desired file maps.

Spec section: 10.2
"""

def plan(base: Dict[str, dict], desired: Dict[str, dict]) -> Dict[str, List[dict]]:
    """Compare {path: entry} maps; returns added/replaced/deleted/unchanged lists.

    - added:    in desired, not in base
    - replaced: in both, sha256 differs
    - deleted:  in base, not in desired
    - unchanged: in both, sha256 identical
    Rename with same content shows up as added + deleted (blob dedup handles it).
    """
    added: List[dict] = []
    replaced: List[dict] = []
    deleted: List[dict] = []
    unchanged: List[dict] = []

    for path, want in sorted(desired.items()):
        cur = base.get(path)
        if cur is None:
            added.append({
                "path": path,
                "newSha256": want["sha256"],
                "size": want["size"],
            })
        elif cur.get("sha256") != want["sha256"]:
            replaced.append({
                "path": path,
                "oldSha256": cur.get("sha256"),
                "newSha256": want["sha256"],
                "size": want["size"],
            })
        else:
            unchanged.append({
                "path": path,
                "sha256": want["sha256"],
                "size": want["size"],
            })

    for path, cur in sorted(base.items()):
        if path not in desired:
            deleted.append({
                "path": path,
                "oldSha256": cur.get("sha256"),
                "size": cur.get("size"),
            })

    return {
        "added": added,
        "replaced": replaced,
        "deleted": deleted,
        "unchanged": unchanged,
    }


# ===== manifest.py =====

"""Manifest JSON models, atomic IO and atomic file copy.

Spec sections: 3.1, 4.x
"""

SUPPORTED_SCHEMA_VERSION = 1

EXIT_IO_ERROR = 19

def atomic_write(path: str, data: bytes) -> None:
    """Temp file in same directory + flush + fsync + os.replace."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp-%d" % (os.path.basename(path), os.getpid()))
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def atomic_write_text(path: str, text: str) -> None:
    atomic_write(path, text.encode("utf-8"))

def read_json(path: str) -> Any:
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))

def write_json(path: str, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")

def atomic_copy(src: str, dst: str) -> None:
    """Copy via temp file in dst directory + fsync + os.replace."""
    d = os.path.dirname(os.path.abspath(dst))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp-%d" % (os.path.basename(dst), os.getpid()))
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        while True:
            block = fi.read(1024 * 1024)
            if not block:
                break
            fo.write(block)
        fo.flush()
        os.fsync(fo.fileno())
    os.replace(tmp, dst)

def check_schema(obj: Dict[str, Any], max_supported: int = SUPPORTED_SCHEMA_VERSION) -> None:
    v = obj.get("schemaVersion")
    if not isinstance(v, int):
        raise ValueError("缺少 schemaVersion")
    if v > max_supported:
        raise ValueError("schemaVersion %d 高于本程序支持值 %d" % (v, max_supported))

def make_state(pack_id: str, version: Optional[str], applied_at: str,
               files: List[dict]) -> Dict[str, Any]:
    return {
        "schemaVersion": SUPPORTED_SCHEMA_VERSION,
        "packId": pack_id,
        "lastAppliedVersion": version,
        "appliedAt": applied_at,
        "files": files,
    }

def make_desired(pack_id: str, version: str, files: List[dict]) -> Dict[str, Any]:
    return {
        "schemaVersion": SUPPORTED_SCHEMA_VERSION,
        "packId": pack_id,
        "version": version,
        "files": files,
    }


# ===== locking.py =====

"""Cross-platform file lock (POSIX fcntl / Windows msvcrt).

Spec sections: 3.5, 10.6
"""

EXIT_LOCK_HELD = 10

class LockHeld(Exception):
    """Raised when the lock file is already held by another process."""

    exit_code = EXIT_LOCK_HELD

class FileLock:
    """Exclusive non-blocking lock via a lock file; context manager."""

    def __init__(self, path: Union[str, os.PathLike]) -> None:
        self.path = os.fspath(path)
        self._fd = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            self._lock_fd(fd)
        except OSError:
            os.close(fd)
            raise LockHeld("锁被占用: %s" % self.path)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            self._unlock_fd(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    def _lock_fd(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fd(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


# ===== platform.py =====

"""Platform helpers: same-device check and game-running detection.

Spec sections: 5.2-2 (st_dev), 9.2-3 (game process detection)
"""

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


# ===== applier.py =====

"""Atomic apply / backup / rollback shared pure logic.

Used by C directly; stitched into the single-file B build (spec [6] applier,
[T-12] 白名单). B's apply/rollback orchestration lives in server/b_main.py and
calls into these helpers.

关键差异（与 [T-12] apply 步骤9 对齐）:
  B 端因步骤2 已保证 staging 与 mods/ 同分区，added/replaced 一律用
  os.replace(staging_blob, target) **原子移入，不产生复制**（move=True）。
  C 端 staging 位于 _updater/ 内，按 T-40 使用「临时文件 + os.replace」（move=False）。
"""

FaultHook = Optional[Callable[[str], None]]

class ApplyError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code

def backup_changes(entries: List[dict], backup_root: str, root: str,
                   log: Callable[[str], None]) -> List[dict]:
    """Copy replaced/deleted current disk files into backup_root; fill oldSha256/oldSize.

    entries: merged list of replaced+deleted change dicts with "path".
    Copies use atomic_copy; oldSha256/oldSize are recomputed from disk BEFORE copying.
    Returns the enriched entries (with backupPath + oldSha256 + oldSize filled).
    """
    out: List[dict] = []
    for e in entries:
        rel = e["path"]
        src = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(src):
            log("警告: 待备份文件不存在，跳过: %s" % rel)
            e = dict(e)
            e.setdefault("oldSha256", None)
            e.setdefault("oldSize", None)
            out.append(e)
            continue
        info = hash_file(src)
        dst = os.path.join(backup_root, rel.replace("/", os.sep))
        manifest.atomic_copy(src, dst)
        e = dict(e)
        e["oldSha256"] = str(info["sha256"])
        e["oldSize"] = int(info["size"])
        e["backupPath"] = os.path.relpath(dst, os.path.dirname(backup_root)).replace("\\", "/")
        log("已备份: %s (sha256=%s size=%d)" % (rel, e["oldSha256"], e["oldSize"]))
        out.append(e)
    return out

def apply_change_set(changes: Dict[str, List[dict]], staging: str, root: str,
                     log: Callable[[str], None],
                     fault: Optional[Callable[[str], None]] = None,
                     move: bool = False) -> Dict[str, int]:
    """Apply added/replaced/deleted; idempotent per file, tolerant of missing targets.

    - added/replaced: 目标已等于 newSha256 -> 跳过（幂等）；否则
      move=True  -> os.replace(staging blob, target)（B 端，同分区原子移入，不复制）
      move=False -> 临时文件 + os.replace 复制（C 端）
    - deleted: os.remove；缺失则记 warning 并跳过
    - fault: 每处理完一个文件后回调（测试注入崩溃点 "mid-apply"）
    """
    counts = {"added": 0, "replaced": 0, "deleted": 0, "skipped": 0, "unchanged": 0}
    for kind in ("added", "replaced"):
        for e in changes.get(kind, []):
            rel = e["path"]
            sha = e["newSha256"]
            blob = os.path.join(staging, "blobs", sha[0:2], sha[2:4], sha)
            dst = os.path.join(root, rel.replace("/", os.sep))
            if os.path.isfile(dst) and str(hash_file(dst)["sha256"]) == sha:
                counts["unchanged"] += 1
                log("已就位，跳过(%s): %s" % (kind, rel))
            elif not os.path.isfile(blob):
                raise ApplyError(11, "staging 缺件: %s" % rel)
            elif move:
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                os.replace(blob, dst)
                counts[kind] += 1
                log("已应用(移入, %s): %s" % (kind, rel))
            else:
                manifest.atomic_copy(blob, dst)
                counts[kind] += 1
                log("已应用(复制, %s): %s" % (kind, rel))
            if fault:
                fault("mid-apply")
    for e in changes.get("deleted", []):
        rel = e["path"]
        dst = os.path.join(root, rel.replace("/", os.sep))
        if os.path.isfile(dst):
            os.remove(dst)
            counts["deleted"] += 1
            log("已删除: %s" % rel)
        else:
            counts["skipped"] += 1
            log("警告: 删除目标不存在，跳过: %s" % rel)
        if fault:
            fault("mid-apply")
    return counts

def verify_against(root: str, entries: List[dict], sha_key: str,
                   log: Callable[[str], None]) -> None:
    """Recheck restored files hash; raises ApplyError(19) on mismatch."""
    for e in entries:
        rel = e["path"]
        f = os.path.join(root, rel.replace("/", os.sep))
        want = e.get(sha_key)
        if want is None or not os.path.isfile(f):
            raise ApplyError(19, "回滚复核失败，文件缺失: %s" % rel)
        got = hash_file(f)["sha256"]
        if got != want:
            raise ApplyError(19, "回滚复核失败，哈希不符: %s" % rel)
        log("复核通过: %s" % rel)


# ===== canonicaljson.py =====

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


# ===== 模块代理（供模块限定名访问，如 manifest.atomic_copy）=====
from types import SimpleNamespace as _SimpleNamespace

hashing = _SimpleNamespace(CHUNK=CHUNK, EXIT_IO_ERROR=EXIT_IO_ERROR, FileEntry=FileEntry, hash_bytes=hash_bytes, hash_file=hash_file, is_jar=is_jar, scan_tree=scan_tree, entry_map=entry_map, mtime_of=mtime_of)
paths = _SimpleNamespace(EXIT_UNSAFE_PATH=EXIT_UNSAFE_PATH, _DRIVE_RE=_DRIVE_RE, UnsafePath=UnsafePath, normalize_rel=normalize_rel, is_within=is_within, safe_join=safe_join)
planner = _SimpleNamespace(plan=plan)
manifest = _SimpleNamespace(SUPPORTED_SCHEMA_VERSION=SUPPORTED_SCHEMA_VERSION, EXIT_IO_ERROR=EXIT_IO_ERROR, atomic_write=atomic_write, atomic_write_text=atomic_write_text, read_json=read_json, write_json=write_json, atomic_copy=atomic_copy, check_schema=check_schema, make_state=make_state, make_desired=make_desired)
locking = _SimpleNamespace(EXIT_LOCK_HELD=EXIT_LOCK_HELD, LockHeld=LockHeld, FileLock=FileLock)
platform = _SimpleNamespace(GAME_PROC_KEYWORDS=GAME_PROC_KEYWORDS, same_device=same_device, disk_usage=disk_usage, is_game_running=is_game_running, _matches=_matches, _posix_game_running=_posix_game_running, _win_game_running=_win_game_running)
applier = _SimpleNamespace(FaultHook=FaultHook, ApplyError=ApplyError, backup_changes=backup_changes, apply_change_set=apply_change_set, verify_against=verify_against)
canonicaljson = _SimpleNamespace(canonical=canonical, _sort_keys=_sort_keys, strip_signature=strip_signature)

__MCMODSYNC_STITCHED__ = True

# ===== server/b_main.py =====

"""B main: single-file server script subcommand implementations.

Spec sections: [5] T-12（协议步骤为硬约束）, [3.1]-[3.4], [2.5]-[2.7]
This module is stitched together with whitelisted stdlib-only modules by
tools/build_server.py to produce server/mcmodsync-b.py (single file).
It must therefore use ONLY relative-import-free access: the build injects
shared modules as top-level names (hashing, paths, planner, manifest,
locking, platform, applier, canonicaljson 的 JSON 部分) into the same
namespace plus lightweight module proxies.

For the in-package version (tests import server.b_main directly), a small
shim maps those names from mcmodsync package modules.
"""


# --- package shim (removed/replaced by build stitching) ---------------------
# Single-file builds define __MCMODSYNC_STITCHED__ before this block; all
# shared module names (hashing/manifest/paths/planner/platform/FileLock/
# LockHeld and applier functions) are then already in globals().
if globals().get("__MCMODSYNC_STITCHED__"):
    _applier = sys.modules[__name__]
else:
    pass
    pass
    pass

PROTOCOL_VERSION = "MC-ModSync-B 2.0"
SUPPORTED_SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_DISK = 4
EXIT_LOCK = 10
EXIT_STAGING = 11
EXIT_UNSAFE_PATH = 13
EXIT_ROLLBACK_TARGET = 14
EXIT_ROLLED_BACK = 15
EXIT_CROSS_DEVICE = 18
EXIT_OTHER = 19
EXIT_FAULT = 99


class BError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _now_iso() -> str:
    tz = datetime.now().astimezone().tzinfo or timezone.utc
    return datetime.now(tz).isoformat(timespec="seconds")


def _stamp_ms() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _get_log(server_dir: str, subcommand: str, version: str):
    """Return (logfile_path, log_fn) per [2.6]:
    apply 用 version；manifest / rollback 无 version 时用扫描时间戳。
    """
    log_dir = os.path.join(server_dir, ".mcmodsync", "logs")
    os.makedirs(log_dir, exist_ok=True)
    tag = version or _stamp_ms()
    logfile = os.path.join(log_dir, "%s-%s.log" % (subcommand, tag))

    def log(msg: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(logfile, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (stamp, msg))

    return logfile, log


def _validate_rel(path_str: str) -> str:
    try:
        return paths.normalize_rel(path_str)
    except paths.UnsafePath as e:
        raise BError(EXIT_UNSAFE_PATH, "非法路径: %s" % e)


def _scan_mods(server_dir: str, mods_dir: str) -> dict:
    entries = hashing.scan_tree(server_dir, subdir=mods_dir, exts=(".jar",), recursive=False)
    return {e["path"]: e for e in entries}


def _load_json_file(path: str, err_code: int, what: str):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except FileNotFoundError:
        raise BError(err_code, "%s 不存在: %s" % (what, path))
    except ValueError as e:
        raise BError(err_code, "%s JSON 解析失败: %s" % (what, e))


def _check_desired(desired: dict, pack_id: str) -> None:
    if not isinstance(desired, dict):
        raise BError(EXIT_OTHER, "desired 必须是 JSON 对象")
    sv = desired.get("schemaVersion")
    if sv != SUPPORTED_SCHEMA_VERSION:
        if isinstance(sv, int) and sv > SUPPORTED_SCHEMA_VERSION:
            raise BError(EXIT_OTHER, "desired schemaVersion %s 过新" % sv)
        raise BError(EXIT_OTHER, "desired schemaVersion 无法识别: %r" % (sv,))
    if desired.get("packId") != pack_id:
        raise BError(EXIT_OTHER, "packId 不一致: desired=%r 参数=%r" % (desired.get("packId"), pack_id))
    files = desired.get("files")
    if not isinstance(files, list):
        raise BError(EXIT_OTHER, "desired.files 缺失")
    seen = set()
    for f in files:
        p = _validate_rel(f.get("path", ""))
        if p in seen:
            raise BError(EXIT_OTHER, "desired 中路径重复: %s" % p)
        seen.add(p)
        if not isinstance(f.get("sha256"), str) or len(f["sha256"]) != 64:
            raise BError(EXIT_OTHER, "sha256 非法: %s" % p)
        if not isinstance(f.get("size"), int) or f["size"] < 0:
            raise BError(EXIT_OTHER, "size 非法: %s" % p)


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def cmd_manifest(server_dir: str, mods_dir: str) -> int:
    if not os.path.isdir(server_dir):
        raise BError(EXIT_OTHER, "server-dir 不存在: %s" % server_dir)
    mods_path = os.path.join(server_dir, mods_dir)
    if not os.path.isdir(mods_path):
        os.makedirs(mods_path, exist_ok=True)
    os.makedirs(os.path.join(server_dir, ".mcmodsync"), exist_ok=True)
    _logfile, log = _get_log(server_dir, "manifest", None)
    log("=== manifest 扫描 %s ===" % server_dir)

    entries = hashing.scan_tree(server_dir, subdir=mods_dir, exts=(".jar",), recursive=False)
    state_path = os.path.join(server_dir, ".mcmodsync", "state.json")
    state_version = None
    if os.path.isfile(state_path):
        try:
            state_version = json.loads(open(state_path, "rb").read().decode("utf-8")).get("lastAppliedVersion")
        except (ValueError, OSError):
            state_version = None
    out = {
        "schemaVersion": SUPPORTED_SCHEMA_VERSION,
        "serverDir": server_dir,
        "scannedAt": _now_iso(),
        "stateFileExists": os.path.isfile(state_path),
        "stateVersion": state_version,
        "files": [dict(e, mtime=int(os.stat(os.path.join(server_dir, e["path"].replace("/", os.sep))).st_mtime))
                  for e in entries],
    }
    log("manifest 完成: %d 个 jar" % len(out["files"]))
    _emit(out)
    return EXIT_OK


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def _fault_point(name: str, inject: str) -> None:
    if inject == name:
        sys.stderr.write(json.dumps({"error": "fault-injected", "point": name}, ensure_ascii=False) + "\n")
        sys.exit(EXIT_FAULT)


def cmd_apply(args: dict) -> int:
    server_dir = args["server_dir"]
    staging = args["staging"]
    version = args["version"]
    pack_id = args["pack_id"]
    history_dir = args["history_dir"]
    history_keep = int(args.get("history_keep") or 3)
    inject = args.get("inject_fault") or ""
    mods_dir = args.get("mods_dir") or "mods"

    if not os.path.isdir(server_dir):
        raise BError(EXIT_OTHER, "server-dir 不存在: %s" % server_dir)
    mods_path = os.path.join(server_dir, mods_dir)
    if not os.path.isdir(mods_path):
        os.makedirs(mods_path, exist_ok=True)
    os.makedirs(os.path.join(server_dir, ".mcmodsync"), exist_ok=True)

    # 步骤1 加锁
    lock_path = os.path.join(server_dir, ".mcmodsync.lock")
    try:
        lock = FileLock(lock_path)
        lock.acquire()
    except LockHeld:
        raise BError(EXIT_LOCK, "锁被占用: %s" % lock_path)
    try:
        return _apply_locked(args, server_dir, staging, version, pack_id,
                             history_dir, history_keep, inject, mods_dir)
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _apply_locked(args, server_dir, staging, version, pack_id, history_dir,
                  history_keep, inject, mods_dir) -> int:
    logfile, log = _get_log(server_dir, "apply", version)
    log("=== apply %s (pack=%s) ===" % (version, pack_id))

    # 步骤2 staging 归属 + 同分区校验
    try:
        staging_norm = os.path.realpath(staging)
        server_norm = os.path.realpath(server_dir)
    except OSError as e:
        raise BError(EXIT_OTHER, "路径解析失败: %s" % e)
    if not paths.is_within(os.path.join(staging_norm, "x"), server_norm):
        raise BError(EXIT_UNSAFE_PATH, "staging 不在 server-dir 内: %s" % staging)
    mods_real = os.path.join(server_norm, mods_dir)
    if not platform.same_device(staging_norm, mods_real):
        raise BError(EXIT_CROSS_DEVICE, "staging 与 mods 不在同一分区")

    # 步骤3 加载并校验 desired
    desired_path = args.get("desired_path") or os.path.join(staging, "desired-%s.json" % version)
    if not paths.is_within(os.path.abspath(desired_path), os.path.abspath(staging)):
        raise BError(EXIT_UNSAFE_PATH, "desired 越界: %s" % desired_path)
    desired = _load_json_file(desired_path, EXIT_STAGING, "desired JSON")
    _check_desired(desired, pack_id)
    desired_map = {f["path"]: f for f in desired["files"]}

    # 步骤4 扫描磁盘
    disk_map = _scan_mods(server_dir, mods_dir)

    # 步骤5 幂等 + 漂移判断
    state_path = os.path.join(server_dir, ".mcmodsync", "state.json")
    state = None
    if os.path.isfile(state_path):
        state = _load_json_file(state_path, EXIT_OTHER, "state.json")
    if state and state.get("lastAppliedVersion") == version:
        disk_ok = (set(disk_map.keys()) == set(desired_map.keys())
                   and all(disk_map[p].get("sha256") == desired_map[p].get("sha256")
                           for p in disk_map))
        if disk_ok:
            _clear_dir(staging)
            log("no-op: 磁盘与 desired 一致")
            _emit({"result": "noop", "version": version})
            return EXIT_OK

    # 步骤6 确定变更集（只产生一次）
    ver_dir = os.path.join(history_dir, pack_id, version)
    changes_path = os.path.join(ver_dir, "changes.json")
    rerun = os.path.isfile(changes_path)
    if rerun:
        changes = _load_json_file(changes_path, EXIT_OTHER, "changes.json")
        _validate_changes(changes)
        log("重跑路径: 加载已有 changes.json")
    else:
        diff = planner.plan(disk_map, desired_map)
        changes = {
            "schemaVersion": SUPPORTED_SCHEMA_VERSION,
            "packId": pack_id,
            "version": version,
            "appliedAt": _now_iso(),
            "changes": {
                "added": diff["added"],
                "replaced": diff["replaced"],
                "deleted": diff["deleted"],
            },
        }
        log("首次路径: added=%d replaced=%d deleted=%d" % (
            len(diff["added"]), len(diff["replaced"]), len(diff["deleted"])))

    # 步骤7 staging 复核（首次与重跑都必须）
    _verify_staging(staging, changes, server_dir, log)

    # 步骤8 首次路径的备份阶段
    if not rerun:
        # 8a 磁盘空间预检（保守上界）
        need = 0
        for e in changes["changes"]["replaced"]:
            need += 2 * int(e.get("size") or 0)   # oldSize 与 new size 同量级，取保守值
        for e in changes["changes"]["deleted"]:
            need += int(e.get("size") or 0)       # deleted.size 即磁盘旧文件大小
        for e in changes["changes"]["added"]:
            need += int(e.get("size") or 0)
        try:
            usage = _disk_usage(server_dir)
            if usage and (usage.free * 1.05) < need:
                raise BError(EXIT_DISK, "磁盘空间不足: 需约 %d 字节，可用 %d 字节"
                             % (need, usage.free))
        except BError:
            raise
        except OSError:
            log("警告: 无法获取磁盘空间信息，跳过预检")

        # 8b/8c 备份目录 + 复制备份（记录 oldSha256/oldSize）
        backup_root = os.path.join(ver_dir, "backup")
        os.makedirs(backup_root, exist_ok=True)
        merged = changes["changes"]["replaced"] + changes["changes"]["deleted"]
        enriched = _applier.backup_changes(merged, backup_root, server_dir, log)
        n_repl = len(changes["changes"]["replaced"])
        changes["changes"]["replaced"] = enriched[:n_repl]
        changes["changes"]["deleted"] = enriched[n_repl:]
        # 8d 原子写 changes.json
        manifest.write_json(changes_path, changes)
        log("已写入 changes.json")
        _fault_point("after-backup", inject)
    else:
        log("重跑路径: 跳过备份")

    # 步骤9 应用（动作幂等，容忍目标缺失；同分区 os.replace 原子移入）
    counts = _applier.apply_change_set(
        changes["changes"], staging, server_dir, log,
        fault=_mid_apply_probe(changes, inject), move=True)

    # 步骤10 原子写 state
    new_state = {
        "schemaVersion": SUPPORTED_SCHEMA_VERSION,
        "packId": pack_id,
        "lastAppliedVersion": version,
        "appliedAt": _now_iso(),
        "files": [dict(e) for e in desired["files"]],
    }
    manifest.write_json(state_path, new_state)
    log("已写入 state.json")
    _fault_point("after-state", inject)

    # 步骤11 清空 staging
    _clear_dir(staging)

    # 步骤12 history 清理
    _cleanup_history(history_dir, pack_id, history_keep, log)

    # 步骤13 摘要
    log("apply 完成: %s" % counts)
    _emit({
        "result": "applied",
        "version": version,
        "added": len(changes["changes"]["added"]),
        "replaced": len(changes["changes"]["replaced"]),
        "deleted": len(changes["changes"]["deleted"]),
        "historyDir": ver_dir,
    })
    return EXIT_OK


def _mid_apply_probe(changes: dict, inject: str):
    """构造 mid-apply 故障注入回调: 累计字节过半时触发（[T-12] 步骤9）。

    回调按 applier 的处理顺序（added -> replaced -> deleted）逐个消费 sizes。
    """
    sizes = ([int(e.get("size") or 0) for e in changes["changes"]["added"]]
             + [int(e.get("size") or 0) for e in changes["changes"]["replaced"]]
             + [int(e.get("size") or 0) for e in changes["changes"]["deleted"]])
    total = sum(sizes)
    prog = {"done": 0, "idx": 0, "fired": False}

    def _cb(_point: str) -> None:
        if inject != "mid-apply" or prog["fired"]:
            return
        i = prog["idx"]
        if i < len(sizes):
            prog["done"] += sizes[i]
        prog["idx"] = i + 1
        if total == 0 or prog["done"] * 2 >= total:
            prog["fired"] = True
            _fault_point("mid-apply", inject)

    return _cb


def _disk_usage(path: str):
    import shutil
    try:
        return shutil.disk_usage(path)
    except OSError:
        return None


def _clear_dir(path: str) -> None:
    """Clear all contents of a directory but keep the directory itself."""
    if not os.path.isdir(path):
        return
    import shutil
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full, ignore_errors=True)
        else:
            try:
                os.remove(full)
            except OSError:
                pass


def _sha_of(path: str) -> str:
    from hashlib import sha256 as _sha
    h = _sha()
    with open(path, "rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _verify_staging(staging: str, changes: dict, root: str, log) -> None:
    """步骤7: 复核变更集中每个 added/replaced 的 staging 缺件/大小/哈希。

    因步骤9 采用 os.replace 原子移入（不复制），重跑时已应用的条目的 staging blob
    已被移走；此时以「目标文件已等于 newSha256」判定其已满足（CM-1 收敛所需）。
    真正需要用到的 blob 缺失/损坏/不一致 -> 码 11。
    """
    for e in changes["changes"]["added"] + changes["changes"]["replaced"]:
        sha = e["newSha256"]
        want_size = int(e.get("size") or -1)
        blob = os.path.join(staging, "blobs", sha[0:2], sha[2:4], sha)
        if not os.path.isfile(blob):
            target = os.path.join(root, e["path"].replace("/", os.sep))
            if os.path.isfile(target) and os.stat(target).st_size == want_size \
                    and _sha_of(target) == sha:
                log("staging 缺件但目标已就位，视为已应用: %s" % e["path"])
                continue
            raise BError(EXIT_STAGING, "staging 缺件: %s" % e["path"])
        if os.stat(blob).st_size != want_size:
            raise BError(EXIT_STAGING, "staging size 不一致: %s" % e["path"])
        if _sha_of(blob) != sha:
            raise BError(EXIT_STAGING, "staging 哈希不符: %s" % e["path"])
        log("staging 复核通过: %s" % e["path"])


def _validate_changes(changes: dict) -> None:
    c = changes.get("changes")
    if not isinstance(c, dict):
        raise BError(EXIT_OTHER, "changes.json 结构非法")
    for key in ("added", "replaced", "deleted"):
        if not isinstance(c.get(key), list):
            raise BError(EXIT_OTHER, "changes.json 缺少 %s" % key)
        for e in c[key]:
            _validate_rel(e.get("path", ""))
    for e in c["replaced"] + c["deleted"]:
        if not isinstance(e.get("backupPath"), str):
            raise BError(EXIT_OTHER, "changes.json 缺少 backupPath: %s" % e.get("path"))


def _cleanup_history(history_dir: str, pack_id: str, keep: int, log) -> None:
    """步骤12: 目录名字典序倒序，保留最新 keep 个，其余删除。"""
    root = os.path.join(history_dir, pack_id)
    if not os.path.isdir(root) or keep <= 0:
        return
    names = sorted((n for n in os.listdir(root)
                    if os.path.isdir(os.path.join(root, n))), reverse=True)
    for name in names[keep:]:
        import shutil
        target = os.path.join(root, name)
        shutil.rmtree(target, ignore_errors=True)
        log("history 清理: 删除 %s" % name)


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------

def cmd_rollback(args: dict) -> int:
    server_dir = args["server_dir"]
    history_dir = args["history_dir"]
    pack_id = args.get("pack_id") or ""
    want_version = args.get("version") or None
    mods_dir = args.get("mods_dir") or "mods"

    if not os.path.isdir(server_dir):
        raise BError(EXIT_OTHER, "server-dir 不存在: %s" % server_dir)

    lock_path = os.path.join(server_dir, ".mcmodsync.lock")
    try:
        lock = FileLock(lock_path)
        lock.acquire()
    except LockHeld:
        raise BError(EXIT_LOCK, "锁被占用: %s" % lock_path)
    try:
        return _rollback_locked(args, server_dir, history_dir, pack_id,
                                want_version, mods_dir)
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _rollback_locked(args, server_dir, history_dir, pack_id, want_version,
                     mods_dir) -> int:
    state_path = os.path.join(server_dir, ".mcmodsync", "state.json")
    logfile, log = _get_log(server_dir, "rollback", want_version)
    log("=== rollback (pack=%s, want=%s) ===" % (pack_id, want_version or "latest"))

    # 步骤2 packId 解析
    if not pack_id:
        if os.path.isfile(state_path):
            pack_id = (_load_json_file(state_path, EXIT_OTHER, "state.json").get("packId") or "")
        if not pack_id:
            raise BError(EXIT_OTHER, "packId 缺失（参数与 state 均未提供）")

    # 步骤3 最新 history 目录
    root = os.path.join(history_dir, pack_id)
    if not os.path.isdir(root):
        raise BError(EXIT_OTHER, "history 目录不存在: %s" % root)
    names = sorted((n for n in os.listdir(root)
                    if os.path.isdir(os.path.join(root, n))), reverse=True)
    if not names:
        raise BError(EXIT_OTHER, "history 为空: %s" % root)
    target_name = names[0]
    if want_version and want_version != target_name:
        raise BError(EXIT_ROLLBACK_TARGET,
                     "回滚目标 %s 不是最新 history 目录 %s" % (want_version, target_name))
    ver_dir = os.path.join(root, target_name)
    log("回滚目标: %s" % target_name)

    # 步骤4 changes 文件状态
    changes_path = os.path.join(ver_dir, "changes.json")
    rolled_path = changes_path + ".rolled-back"
    if os.path.isfile(rolled_path):
        raise BError(EXIT_ROLLED_BACK, "该版本已回滚过: %s" % target_name)
    if not os.path.isfile(changes_path):
        raise BError(EXIT_OTHER, "changes.json 不存在: %s" % target_name)
    changes = _load_json_file(changes_path, EXIT_OTHER, "changes.json")
    _validate_changes(changes)

    # 步骤5 反向操作 + 漂移保护
    drift_root = os.path.join(ver_dir, "rollback-drift")
    c = changes["changes"]
    restored: list = []

    for e in c["added"]:
        f = os.path.join(server_dir, e["path"].replace("/", os.sep))
        if not os.path.isfile(f):
            log("added 回滚跳过(不存在): %s" % e["path"])
            continue
        if hashing.hash_file(f)["sha256"] == e.get("newSha256"):
            os.remove(f)
            log("added 回滚删除: %s" % e["path"])
        else:
            # 手工漂移: 原文件整体移入 rollback-drift（移动即已清除 mods 下文件）
            _move_to_drift(f, drift_root, e["path"], log)
            log("added 漂移，已移入 rollback-drift 并清除 mods 下文件: %s" % e["path"])

    for e in c["replaced"]:
        f = os.path.join(server_dir, e["path"].replace("/", os.sep))
        bak = _backup_abs(ver_dir, e)
        if not os.path.isfile(bak):
            log("警告: 备份缺失，跳过还原: %s" % e["path"])
            continue
        if os.path.isfile(f) and hashing.hash_file(f)["sha256"] != e.get("newSha256"):
            _move_to_drift(f, drift_root, e["path"], log)
            log("replaced 漂移，已移入 rollback-drift: %s" % e["path"])
        manifest.atomic_copy(bak, f)
        restored.append({"path": e["path"], "oldSha256": e.get("oldSha256")})
        log("replaced 还原: %s" % e["path"])

    for e in c["deleted"]:
        f = os.path.join(server_dir, e["path"].replace("/", os.sep))
        bak = _backup_abs(ver_dir, e)
        if not os.path.isfile(bak):
            log("警告: 备份缺失，跳过还原: %s" % e["path"])
            continue
        if os.path.isfile(f):
            if hashing.hash_file(f)["sha256"] == e.get("oldSha256"):
                log("deleted 还原跳过(已存在且一致): %s" % e["path"])
                continue
            _move_to_drift(f, drift_root, e["path"], log)
            log("deleted 漂移，已移入 rollback-drift: %s" % e["path"])
        manifest.atomic_copy(bak, f)
        restored.append({"path": e["path"], "oldSha256": e.get("oldSha256")})
        log("deleted 还原: %s" % e["path"])

    # 步骤6 复核还原文件哈希
    _applier.verify_against(server_dir, restored, "oldSha256", log)

    # 步骤7 changes.json -> changes.json.rolled-back
    manifest.atomic_write(rolled_path, json.dumps(changes, ensure_ascii=False).encode("utf-8"))
    os.remove(changes_path)
    log("changes.json 已标记为 rolled-back")

    # 步骤8 state 重写（lastAppliedVersion=null，files=磁盘实时扫描）
    disk_entries = hashing.scan_tree(server_dir, subdir=mods_dir, exts=(".jar",), recursive=False)
    new_state = {
        "schemaVersion": SUPPORTED_SCHEMA_VERSION,
        "packId": pack_id,
        "lastAppliedVersion": None,
        "appliedAt": _now_iso(),
        "files": [dict(e) for e in disk_entries],
    }
    manifest.write_json(state_path, new_state)
    log("state 已重置（lastAppliedVersion=null）")

    # 步骤9 不清理 history；输出摘要
    _emit({
        "result": "rolled-back",
        "version": target_name,
        "added": len(c["added"]),
        "replaced": len(c["replaced"]),
        "deleted": len(c["deleted"]),
        "drift": len(os.listdir(drift_root)) if os.path.isdir(drift_root) else 0,
    })
    return EXIT_OK


def _backup_abs(ver_dir: str, e: dict) -> str:
    bp = e.get("backupPath") or ""
    return bp if os.path.isabs(bp) else os.path.join(ver_dir, bp.replace("/", os.sep))


def _move_to_drift(file_path: str, drift_root: str, rel: str, log) -> None:
    dst = os.path.join(drift_root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.replace(file_path, dst)
        log("漂移备份: %s -> rollback-drift/%s" % (rel, rel))
    except OSError as e:
        raise BError(EXIT_OTHER, "漂移备份失败 %s: %s" % (rel, e))


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def build_arg_parser():
    import argparse
    p = argparse.ArgumentParser(prog="mcmodsync-b", description="MC-ModSync 服务端脚本 B")
    sub = p.add_subparsers(dest="subcommand", required=True)

    m = sub.add_parser("manifest")
    m.add_argument("--server-dir", required=True)
    m.add_argument("--mods-dir", default="mods")

    a = sub.add_parser("apply")
    a.add_argument("--server-dir", required=True)
    a.add_argument("--staging", required=True)
    a.add_argument("--desired", required=True)
    a.add_argument("--version", required=True)
    a.add_argument("--pack-id", required=True)
    a.add_argument("--history-dir", required=True)
    a.add_argument("--history-keep", type=int, default=3)
    a.add_argument("--mods-dir", default="mods")
    a.add_argument("--inject-fault", default="")

    r = sub.add_parser("rollback")
    r.add_argument("--server-dir", required=True)
    r.add_argument("--history-dir", required=True)
    r.add_argument("--pack-id", default="")
    r.add_argument("--version", default=None)
    r.add_argument("--mods-dir", default="mods")
    r.add_argument("--inject-fault", default="")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--protocol-version" in argv:
        print(PROTOCOL_VERSION)
        return EXIT_OK

    if sys.version_info < (3, 8):
        sys.stderr.write(json.dumps({"error": "需要 Python 3.8+"}, ensure_ascii=False) + "\n")
        return EXIT_OTHER

    parser = build_arg_parser()
    try:
        ns = parser.parse_args(argv)
    except SystemExit:
        return EXIT_OTHER

    args = vars(ns)
    try:
        if ns.subcommand == "manifest":
            return cmd_manifest(args["server_dir"], args.get("mods_dir") or "mods")
        if ns.subcommand == "apply":
            args["desired_path"] = args["desired"]
            return cmd_apply(args)
        if ns.subcommand == "rollback":
            return cmd_rollback(args)
    except BError as e:
        sys.stderr.write(json.dumps({"error": str(e)}, ensure_ascii=False) + "\n")
        return e.exit_code
    except _applier.ApplyError as e:
        sys.stderr.write(json.dumps({"error": str(e)}, ensure_ascii=False) + "\n")
        return e.exit_code
    except Exception as e:  # unexpected; full stack to stderr for logs
        import traceback
        sys.stderr.write(json.dumps({"error": "内部错误: %s" % e}, ensure_ascii=False) + "\n")
        sys.stderr.write(traceback.format_exc())
        return EXIT_OTHER
    return EXIT_GENERIC


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
