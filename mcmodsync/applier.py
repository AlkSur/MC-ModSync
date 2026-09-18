"""Atomic apply / backup / rollback shared pure logic.

Used by C directly; stitched into the single-file B build (spec [6] applier,
[T-12] 白名单). B's apply/rollback orchestration lives in server/b_main.py and
calls into these helpers.

关键差异（与 [T-12] apply 步骤9 对齐）:
  B 端因步骤2 已保证 staging 与 mods/ 同分区，added/replaced 一律用
  os.replace(staging_blob, target) **原子移入，不产生复制**（move=True）。
  C 端 staging 位于 _updater/ 内，按 T-40 使用「临时文件 + os.replace」（move=False）。
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
