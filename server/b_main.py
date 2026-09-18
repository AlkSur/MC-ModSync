"""B main: single-file server script subcommand implementations.

Spec sections: 5 (protocol), 8.2
This module is stitched together with whitelisted stdlib-only modules by
tools/build_server.py to produce server/mcmodsync-b.py (single file).
It must therefore use ONLY relative-import-free access: the build injects
shared modules as top-level names (hashing, paths, planner, manifest,
locking, applier, canonicaljson helpers) into the same namespace.

For the in-package version (tests import server.b_main directly), a small
shim maps those names from mcmodsync package modules.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

# --- package shim (removed/replaced by build stitching) ---------------------
# Single-file builds define __MCMODSYNC_STITCHED__ before this block; all
# shared module names (hashing/manifest/paths/planner/FileLock/LockHeld and
# applier functions) are then already in globals().
if globals().get("__MCMODSYNC_STITCHED__"):
    _applier = sys.modules[__name__]
else:
    from mcmodsync import hashing, manifest, paths, planner  # noqa: F401
    from mcmodsync.locking import FileLock, LockHeld  # noqa: F401
    from mcmodsync import applier as _applier  # noqa: F401

# In single-file builds these aliases come from the stitched modules;
# in package mode they alias the imported modules.
PROTOCOL_VERSION = "MC-ModSync-B 0.7"
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

# In single-file builds these aliases come from the stitched modules;
# in package mode they alias the imported modules.

class BError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _now_iso() -> str:
    # local time with offset; servers usually run UTC or CST, either is fine
    tz = datetime.now().astimezone().tzinfo or timezone.utc
    return datetime.now(tz).isoformat(timespec="seconds")


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _get_log(server_dir: str, subcommand: str, version: str):
    """Return (logfile_path, log_fn); keeps B logging self-contained."""
    log_dir = os.path.join(server_dir, ".mcmodsync", "logs")
    os.makedirs(log_dir, exist_ok=True)
    logfile = os.path.join(log_dir, "%s-%s.log" % (subcommand, version))

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

    # 1. lock
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

    # 2. staging containment + same-device checks
    try:
        staging_norm = os.path.realpath(staging)
        server_norm = os.path.realpath(server_dir)
    except OSError as e:
        raise BError(EXIT_OTHER, "路径解析失败: %s" % e)
    if not paths.is_within(os.path.join(staging_norm, "x"), server_norm):
        raise BError(EXIT_UNSAFE_PATH, "staging 不在 server-dir 内: %s" % staging)
    mods_real = os.path.join(server_norm, mods_dir)
    if not paths.same_device(staging_norm, mods_real):
        raise BError(EXIT_CROSS_DEVICE, "staging 与 mods 不在同一分区")

    # 3. load + validate desired
    desired_path = args.get("desired_path") or os.path.join(staging, "desired-%s.json" % version)
    if not paths.is_within(os.path.abspath(desired_path), os.path.abspath(staging)):
        raise BError(EXIT_UNSAFE_PATH, "desired 越界: %s" % desired_path)
    desired = _load_json_file(desired_path, EXIT_STAGING, "desired JSON")
    _check_desired(desired, pack_id)
    desired_map = {f["path"]: f for f in desired["files"]}

    # 4. scan disk
    disk_map = _scan_mods(server_dir, mods_dir)

    # 5. idempotent no-op check (live disk vs desired; state only for version)
    state_path = os.path.join(server_dir, ".mcmodsync", "state.json")
    state = None
    if os.path.isfile(state_path):
        state = _load_json_file(state_path, EXIT_OTHER, "state.json")
    if state and state.get("lastAppliedVersion") == version:
        disk_ok = (set(disk_map.keys()) == set(desired_map.keys())
                   and all(disk_map[p].get("sha256") == desired_map[p].get("sha256")
                           for p in disk_map))
        if disk_ok:
            # clear entire staging; noop even if staging already empty
            _clear_dir(staging)
            log("no-op: 磁盘与 desired 一致")
            _emit({"result": "noop", "version": version})
            return EXIT_OK

    # 6. change set: rerun path (changes.json exists) vs first path
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

    # 7. staging verification (both paths)
    _verify_staging(staging, changes, log)

    # 8. backup phase (first path only)
    if not rerun:
        # 8a. disk space precheck
        need = 0
        for e in changes["changes"]["replaced"]:
            need += int(e.get("size") or 0)
        for e in changes["changes"]["deleted"]:
            need += int(e.get("size") or 0)
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

        backup_root = os.path.join(ver_dir, "backup")
        os.makedirs(backup_root, exist_ok=True)
        # 8c. copy backups (replaced + deleted), recompute oldSha256 from disk
        merged = changes["changes"]["replaced"] + changes["changes"]["deleted"]
        enriched = _applier.backup_changes(merged, backup_root, server_dir, log)
        n_repl = len(changes["changes"]["replaced"])
        changes["changes"]["replaced"] = enriched[:n_repl]
        changes["changes"]["deleted"] = enriched[n_repl:]
        # 8d. atomic changes.json
        manifest.write_json(changes_path, changes)
        log("已写入 changes.json")
        _fault_point("after-backup", inject)
    else:
        log("重跑路径: 跳过备份")

    # 9. apply
    counts = _applier.apply_change_set(changes["changes"], staging, server_dir, log,
                                       fault=lambda pt: _fault_point(pt, inject))

    # 10. atomic state write
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

    # 11. clear staging
    _clear_dir(staging)

    # 12. history cleanup
    _cleanup_history(history_dir, pack_id, history_keep, log)

    # 13. summary
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


def _verify_staging(staging: str, changes: dict, log) -> None:
    from hashlib import sha256 as _sha
    for e in changes["changes"]["added"] + changes["changes"]["replaced"]:
        sha = e["newSha256"]
        blob = os.path.join(staging, "blobs", sha[0:2], sha[2:4], sha)
        if not os.path.isfile(blob):
            raise BError(EXIT_STAGING, "staging 缺件: %s" % e["path"])
        st = os.stat(blob)
        if st.st_size != int(e.get("size") or -1):
            raise BError(EXIT_STAGING, "staging size 不一致: %s" % e["path"])
        h = _sha()
        with open(blob, "rb") as f:
            while True:
                block = f.read(1024 * 1024)
                if not block:
                    break
                h.update(block)
        if h.hexdigest() != sha:
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
    logfile, log = _get_log(server_dir, "rollback", want_version or "latest")
    log("=== rollback (pack=%s, want=%s) ===" % (pack_id, want_version or "latest"))

    # 2. packId resolution
    if not pack_id:
        if state_path and os.path.isfile(state_path):
            pack_id = (_load_json_file(state_path, EXIT_OTHER, "state.json").get("packId") or "")
        if not pack_id:
            raise BError(EXIT_OTHER, "packId 缺失（参数与 state 均未提供）")

    # 3. newest history dir
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

    # 4. changes file state
    changes_path = os.path.join(ver_dir, "changes.json")
    rolled_path = changes_path + ".rolled-back"
    if os.path.isfile(rolled_path):
        raise BError(EXIT_ROLLED_BACK, "该版本已回滚过: %s" % target_name)
    if not os.path.isfile(changes_path):
        raise BError(EXIT_OTHER, "changes.json 不存在: %s" % target_name)
    changes = _load_json_file(changes_path, EXIT_OTHER, "changes.json")
    _validate_changes(changes)

    # 5. reverse operations with drift protection
    drift_root = os.path.join(ver_dir, "rollback-drift")
    c = changes["changes"]
    restored: list = []

    # added: remove (drift -> move aside first)
    for e in c["added"]:
        f = os.path.join(server_dir, e["path"].replace("/", os.sep))
        if not os.path.isfile(f):
            log("added 回滚跳过(不存在): %s" % e["path"])
            continue
        cur = hashing.hash_file(f)["sha256"]
        if cur == e.get("newSha256"):
            os.remove(f)
            log("added 回滚删除: %s" % e["path"])
        else:
            _move_to_drift(f, drift_root, e["path"], log)
            os.remove(f)
            log("added 漂移，已移入 rollback-drift 并删除: %s" % e["path"])

    # replaced: restore backup (drift handled)
    for e in c["replaced"]:
        f = os.path.join(server_dir, e["path"].replace("/", os.sep))
        bak = os.path.join(ver_dir, e["backupPath"].replace("/", os.sep)) \
            if not os.path.isabs(e["backupPath"]) else e["backupPath"]
        if not os.path.isfile(bak):
            log("警告: 备份缺失，跳过还原: %s" % e["path"])
            continue
        if os.path.isfile(f):
            cur = hashing.hash_file(f)["sha256"]
            if cur != e.get("newSha256"):
                _move_to_drift(f, drift_root, e["path"], log)
                log("replaced 漂移，已移入 rollback-drift: %s" % e["path"])
        manifest.atomic_copy(bak, f)
        restored.append({"path": e["path"], "oldSha256": e.get("oldSha256")})
        log("replaced 还原: %s" % e["path"])

    # deleted: restore backup
    for e in c["deleted"]:
        f = os.path.join(server_dir, e["path"].replace("/", os.sep))
        bak = os.path.join(ver_dir, e["backupPath"].replace("/", os.sep)) \
            if not os.path.isabs(e["backupPath"]) else e["backupPath"]
        if not os.path.isfile(bak):
            log("警告: 备份缺失，跳过还原: %s" % e["path"])
            continue
        if os.path.isfile(f):
            cur = hashing.hash_file(f)["sha256"]
            if cur == e.get("oldSha256"):
                log("deleted 还原跳过(已存在且一致): %s" % e["path"])
                continue
            _move_to_drift(f, drift_root, e["path"], log)
            log("deleted 漂移，已移入 rollback-drift: %s" % e["path"])
        manifest.atomic_copy(bak, f)
        restored.append({"path": e["path"], "oldSha256": e.get("oldSha256")})
        log("deleted 还原: %s" % e["path"])

    # 6. recheck restored hashes (only files actually restored)
    _applier.verify_against(server_dir, restored, "oldSha256", log)

    # 7. rename changes.json -> changes.json.rolled-back
    manifest.atomic_write(rolled_path, json.dumps(changes, ensure_ascii=False).encode("utf-8"))
    os.remove(changes_path)
    log("changes.json 已标记为 rolled-back")

    # 8. rewrite state from live disk
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

    # 9. summary; history NOT cleaned
    _emit({
        "result": "rolled-back",
        "version": target_name,
        "added": len(c["added"]),
        "replaced": len(c["replaced"]),
        "deleted": len(c["deleted"]),
        "drift": len(os.listdir(drift_root)) if os.path.isdir(drift_root) else 0,
    })
    return EXIT_OK


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
    except Exception as e:  # unexpected; full stack to stderr for logs
        import traceback
        sys.stderr.write(json.dumps({"error": "内部错误: %s" % e}, ensure_ascii=False) + "\n")
        sys.stderr.write(traceback.format_exc())
        return EXIT_OTHER
    return EXIT_GENERIC


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
