"""Atomic apply / backup / rollback shared pure logic.

Used by C directly; stitched into the single-file B build (spec 7.2 applier,
8.1 whitelist). B's apply/rollback orchestration lives in server/b_main.py and
calls into these helpers.
"""
from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional

from . import manifest
from .hashing import hash_file

FaultHook = Optional[Callable[[str], None]]


class ApplyError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def backup_changes(entries: List[dict], backup_root: str, root: str,
                   log: Callable[[str], None]) -> List[dict]:
    """Copy replaced/deleted current disk files into backup_root; fill oldSha256.

    entries: merged list of replaced+deleted change dicts with "path".
    Copies use atomic_copy; oldSha256 is recomputed from disk BEFORE copying.
    Returns the enriched entries (with backupPath + oldSha256 filled).
    """
    out: List[dict] = []
    for e in entries:
        rel = e["path"]
        src = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(src):
            log("警告: 待备份文件不存在，跳过: %s" % rel)
            e = dict(e)
            e.setdefault("oldSha256", None)
            out.append(e)
            continue
        info = hash_file(src)
        old_sha = str(info["sha256"])
        dst = os.path.join(backup_root, rel.replace("/", os.sep))
        manifest.atomic_copy(src, dst)
        log("已备份: %s (sha256=%s)" % (rel, old_sha))
        e = dict(e)
        e["oldSha256"] = old_sha
        sep = "/" if not os.name == "nt" else "/"
        rel_backup = os.path.relpath(dst, os.path.dirname(backup_root)).replace("\\", "/")
        e["backupPath"] = rel_backup
        out.append(e)
        _ = sep
    return out


def apply_change_set(changes: Dict[str, List[dict]], staging: str, root: str,
                     log: Callable[[str], None],
                     fault: Optional[Callable[[str]], None] = None) -> Dict[str, int]:
    """Apply added/replaced/deleted with temp+replace; idempotent per file.

    - added/replaced: copy staging blob -> mods/<path>.tmp-<pid> -> fsync -> replace
    - deleted: os.remove; missing file logs a warning and is skipped
    fault: called with "mid-apply" after each file (test hook decides when to die)
    """
    counts = {"added": 0, "replaced": 0, "deleted": 0, "skipped": 0}
    pid = os.getpid()
    for e in changes.get("added", []) + changes.get("replaced", []):
        rel = e["path"]
        sha = e["newSha256"]
        blob = os.path.join(staging, "blobs", sha[0:2], sha[2:4], sha)
        dst = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(blob):
            raise ApplyError(11, "staging 缺件: %s" % rel)
        manifest.atomic_copy(blob, dst)
        kind = "added" if e in changes.get("added", []) else "replaced"
        counts[kind] += 1
        log("已应用(%s): %s" % (kind, rel))
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
