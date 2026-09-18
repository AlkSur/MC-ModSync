"""C 端 mod 同步客户端（纯标准库）—— [T-40]。

subcommand: sync / verify / rollback / doctor（由 client_main.py 分发）。

布局（`<target>` = 实例目录，即 `_updater/` 的父目录）:
  <target>/mods/*.jar                     游戏实际读取的平铺 mod 目录
  <target>/_updater/config.json           [3.6] 随分发包下发
  <target>/_updater/state.json            [3.5] 本地已同步状态
  <target>/_updater/.lock                 运行锁（失败码 10）
  <target>/_updater/staging/              下载暂存（blobs/<xx>/<yy>/<sha>）
  <target>/_updater/backup/<时间戳>/      备份 + changes.json（[3.7]）

退出码: 0 成功 / 1 一般错误（含配置缺失、版本回退拒绝）/ 2 验签失败 /
        3 游戏运行中 / 4 磁盘不足 / 5 下载失败 / 6 清单 schema 过新 /
        7 packId 不符 / 10 锁被占用 / 13 路径不安全 / 99 故障注入。

本模块仅 import 标准库（C 端分发包要求）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from . import canonicaljson, hashing, http_download, locking, manifest, paths

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_SIGNATURE = 2
EXIT_GAME_RUNNING = 3
EXIT_DISK = 4
EXIT_NETWORK = 5
EXIT_SCHEMA = 6
EXIT_PACKID = 7
EXIT_LOCK = locking.EXIT_LOCK_HELD          # 10
EXIT_UNSAFE_PATH = paths.EXIT_UNSAFE_PATH   # 13
EXIT_IO = manifest.EXIT_IO_ERROR            # 19
EXIT_INJECTED = 99

SUPPORTED_SCHEMA_VERSION = manifest.SUPPORTED_SCHEMA_VERSION
DEFAULT_KEEP_BACKUPS = 1
FAULT_ENV = "MCMODSYNC_FAULT"
USER_AGENT = "MC-ModSync-C/2.0"

UPDATER = "_updater"
MODS_DIR = "mods"


class ClientError(Exception):
    """C 端错误，携带退出码。"""

    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# --------------------------------------------------------------------------
# 路径与工具
# --------------------------------------------------------------------------

def updater_dir(target: str) -> str:
    return os.path.join(target, UPDATER)


def config_path(target: str) -> str:
    return os.path.join(updater_dir(target), "config.json")


def state_path(target: str) -> str:
    return os.path.join(updater_dir(target), "state.json")


def lock_path(target: str) -> str:
    return os.path.join(updater_dir(target), ".lock")


def staging_dir(target: str) -> str:
    return os.path.join(updater_dir(target), "staging")


def backup_root(target: str) -> str:
    return os.path.join(updater_dir(target), "backup")


def default_target() -> str:
    """--target 缺省时取 exe 所在目录的上一级（即 `_updater/` 的父目录）。"""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        exe_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(exe_dir) == UPDATER:
        return os.path.dirname(exe_dir)
    return os.path.dirname(exe_dir)


def _now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _stamp(now: Optional[float] = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time()).strftime("%Y%m%d-%H%M%S")


def _read_json(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def _fault(name: str, log: Callable[[str], None]) -> None:
    """故障注入点（仅测试用；MCMODSYNC_FAULT 显式分支）。"""
    if os.environ.get(FAULT_ENV) == name:
        log("故障注入: %s -> 强制中断（仅测试用）" % name)
        raise ClientError(EXIT_INJECTED, "fault-inject:%s" % name)


def _version_key(v: str) -> Tuple:
    """版本号比较键；无法解析时退化为字符串比较键。"""
    v = (v or "").strip()
    if not v:
        return ()
    parts: List[Tuple[int, object]] = []
    num = ""
    alpha = ""
    for ch in v:
        if ch.isdigit():
            if alpha:
                parts.append((0, alpha))
                alpha = ""
            num += ch
        else:
            if num:
                parts.append((1, int(num)))
                num = ""
            alpha += ch
    if num:
        parts.append((1, int(num)))
    if alpha:
        parts.append((0, alpha))
    return tuple(parts)


def _is_downgrade(new: str, old: str) -> bool:
    try:
        return bool(old) and bool(new) and _version_key(new) < _version_key(old)
    except TypeError:  # 混合类型不可比
        return new < old


def detect_game_running() -> bool:
    """检测 java/javaw/minecraft 相关进程（步骤3）。"""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=20, errors="ignore")
            text = (out.stdout or "") + (out.stderr or "")
        else:
            out = subprocess.run(["ps", "-eo", "comm,args"], capture_output=True,
                                 text=True, timeout=20, errors="ignore")
            text = (out.stdout or "") + (out.stderr or "")
    except Exception:  # noqa: BLE001  检测失败不阻断
        return False
    low = text.lower()
    for token in ("javaw.exe", "java.exe", "minecraft", "javaw", "java"):
        if token in low:
            return True
    return False


# --------------------------------------------------------------------------
# 网络读取（只读）
# --------------------------------------------------------------------------

def fetch_json(url: str, cache_bust: bool = False, timeout: int = 30) -> dict:
    """GET JSON；指针请求追加 ?t=<unix秒>（[T-40] 步骤4）。"""
    if cache_bust:
        sep = "&" if "?" in url else "?"
        url = "%s%st=%d" % (url, sep, int(time.time()))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                              "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _resolve_url(base_url: str, rel: str) -> str:
    if rel.startswith("http://") or rel.startswith("https://"):
        return rel
    return urllib.parse.urljoin(base_url, rel)


def blob_base_of(manifest_url: str) -> str:
    """[3.10] blob_base = dirname(manifestUrl)，去掉 query。

    注意：此处的 manifestUrl 指**配置里的指针 URL**（`<prefix>/manifest.json`），
    其 dirname 即前缀根 `<prefix>`；发布端（T-22）的键为 `blobs/xx/yy/<sha>`、
    `manifests/<ver>.json`，均相对该前缀根。
    """
    u = urllib.parse.urlsplit(manifest_url)
    path = u.path
    base_path = path.rsplit("/", 1)[0] if "/" in path else ""
    return urllib.parse.urlunsplit((u.scheme, u.netloc, base_path, "", ""))


# --------------------------------------------------------------------------
# 配置 / 状态
# --------------------------------------------------------------------------

_REQUIRED_CONFIG = ("schemaVersion", "packId", "manifestUrl", "publicKey")


def load_config(target: str) -> dict:
    path = config_path(target)
    cfg = _read_json(path)
    if cfg is None:
        raise ClientError(EXIT_GENERIC, "缺少配置文件: %s" % path)
    missing = [k for k in _REQUIRED_CONFIG if cfg.get(k) in (None, "")]
    if missing:
        raise ClientError(EXIT_GENERIC, "config.json 缺字段: %s（%s）" % (", ".join(missing), path))
    return cfg


def read_state(target: str) -> dict:
    return _read_json(state_path(target)) or {}


def write_state(target: str, pack_id: str, version: Optional[str],
                files: List[dict]) -> None:
    obj = {
        "schemaVersion": SUPPORTED_SCHEMA_VERSION,
        "packId": pack_id,
        "version": version,
        "syncedAt": _now_iso(),
        "files": [{"path": f["path"], "sha256": f["sha256"], "size": int(f["size"])}
                  for f in sorted(files, key=lambda e: e["path"])],
    }
    manifest.write_json(state_path(target), obj)


def _disk_files(target: str) -> List[dict]:
    """扫描 <target>/mods 平铺 *.jar（磁盘为基准）；.jar.disabled 不计入。"""
    return [{"path": e["path"], "sha256": e["sha256"], "size": int(e["size"])}
            for e in hashing.scan_tree(target, MODS_DIR, (".jar",))]


# --------------------------------------------------------------------------
# 规划（步骤8）
# --------------------------------------------------------------------------

def plan_changes(target: str, files: List[dict], delete: List[dict],
                 strict: bool = False) -> dict:
    """files 优先于 delete；strict 追加自装 jar；同一次同步内改名零下载。"""
    disk = {e["path"]: e for e in _disk_files(target)}
    desired = {f["path"]: {"path": f["path"], "sha256": f["sha256"], "size": int(f["size"])}
               for f in files}

    # 需下载/更新（磁盘缺失或哈希不一致）
    need: List[dict] = []
    for p in sorted(desired):
        d = disk.get(p)
        if d and d["sha256"] == desired[p]["sha256"]:
            continue
        need.append(desired[p])

    # 待删除：delete 列表且不在 files 中，且磁盘存在
    to_delete: List[dict] = []
    delete_paths = set()
    for entry in delete or []:
        p = entry.get("path") if isinstance(entry, dict) else str(entry)
        if not p or p in desired or p in delete_paths:
            continue
        delete_paths.add(p)
        d = disk.get(p)
        if d:
            to_delete.append({"path": p, "sha256": d["sha256"], "size": int(d["size"]),
                              "reason": "delete"})

    # --strict: 磁盘上所有不在 files 中的 *.jar -> 待删除（reason=strict）
    if strict:
        for p in sorted(disk):
            if p in desired or p in delete_paths:
                continue
            delete_paths.add(p)
            to_delete.append({"path": p, "sha256": disk[p]["sha256"],
                              "size": int(disk[p]["size"]), "reason": "strict"})

    # 同一次同步内改名零下载：待删除文件的 sha256 == 某待下载文件 sha256 -> 复制
    sha_to_delete = {}
    for e in to_delete:
        sha_to_delete.setdefault(e["sha256"], e["path"])
    copies: Dict[str, str] = {}
    downloads: List[dict] = []
    for e in need:
        src = sha_to_delete.get(e["sha256"])
        if src and src != e["path"]:
            copies[e["path"]] = src
        else:
            downloads.append(e)

    return {
        "disk": disk,
        "desired": desired,
        "downloads": downloads,
        "copies": copies,
        "to_delete": to_delete,
        "need": need,
    }


def self_installed_entries(disk: Dict[str, dict], desired: Dict[str, dict]) -> List[dict]:
    """磁盘多余（玩家自装）条目。"""
    return [disk[p] for p in sorted(disk) if p not in desired]


# --------------------------------------------------------------------------
# 安全与空间预检（步骤10）
# --------------------------------------------------------------------------

def check_paths(target: str, paths_to_check: List[str]) -> None:
    for p in paths_to_check:
        norm = paths.normalize_rel(p)          # 非法形式 -> UnsafePath
        paths.safe_join(target, norm)          # 越界 -> UnsafePath


def check_disk_space(target: str, download_bytes: int, backup_bytes: int) -> None:
    need = max(0, int(download_bytes)) + max(0, int(backup_bytes))
    if need <= 0:
        return
    free = shutil.disk_usage(target).free
    if free < need:
        raise ClientError(EXIT_DISK,
                          "磁盘空间不足: 需要约 %d 字节（下载 %d + 备份 %d），可用 %d"
                          % (need, download_bytes, backup_bytes, free))


# --------------------------------------------------------------------------
# 备份（步骤11）
# --------------------------------------------------------------------------

def _new_backup_dir(target: str) -> str:
    root = backup_root(target)
    os.makedirs(root, exist_ok=True)
    base = _stamp()
    cand = os.path.join(root, base)
    n = 0
    while os.path.exists(cand):
        n += 1
        cand = os.path.join(root, "%s-%d" % (base, n))
    os.makedirs(os.path.join(cand, "backup"), exist_ok=True)
    return cand


def _backup_files(target: str, bdir: str, rel_paths: List[str],
                  log: Callable[[str], None]) -> Dict[str, str]:
    """复制被替换/删除文件到 backup/；返回 {rel_path: backupPath}。"""
    out: Dict[str, str] = {}
    for rel in rel_paths:
        src = paths.safe_join(target, rel)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(bdir, "backup", rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        out[rel] = "backup/" + rel
    return out


def _prune_backups(target: str, keep: int, log: Callable[[str], None]) -> None:
    """按目录名排序保留最新 keep 个备份（.rolled-back 亦参与排序）。"""
    root = backup_root(target)
    if not os.path.isdir(root):
        return
    dirs = sorted([d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))])
    for d in dirs[:-keep] if keep > 0 else dirs:
        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
        log("清理旧备份: %s" % d)


# --------------------------------------------------------------------------
# sync（步骤1-15）
# --------------------------------------------------------------------------

def sync(target: str, strict: bool = False, no_downgrade: bool = False,
         keep_backups: int = DEFAULT_KEEP_BACKUPS, log: Callable[[str], None] = print,
         game_running_check: Optional[Callable[[], bool]] = None) -> int:
    target = os.path.abspath(target)
    os.makedirs(updater_dir(target), exist_ok=True)
    lock = locking.FileLock(lock_path(target))
    try:
        lock.acquire()                                  # 步骤1
    except locking.LockHeld as e:
        log("已有同步进程在运行（锁占用）: %s" % e)
        return EXIT_LOCK
    try:
        return _sync_locked(target, strict, no_downgrade, keep_backups, log,
                            game_running_check)
    except ClientError as e:
        log("错误(%d): %s" % (e.exit_code, e))
        return int(e.exit_code)
    except paths.UnsafePath as e:
        log("错误(13): %s" % e)
        return EXIT_UNSAFE_PATH
    except http_download.DownloadError as e:
        log("错误(%d): %s" % (e.exit_code, e))
        return int(e.exit_code)
    except urllib.error.HTTPError as e:
        log("错误(5): HTTP %s: %s" % (e.code, e.url))
        return EXIT_NETWORK
    except Exception as e:  # noqa: BLE001
        log("错误(1): %s: %s" % (type(e).__name__, e))
        return EXIT_GENERIC
    finally:
        lock.release()


def _sync_locked(target: str, strict: bool, no_downgrade: bool, keep_backups: int,
                 log: Callable[[str], None],
                 game_running_check: Optional[Callable[[], bool]]) -> int:
    # 步骤2 配置
    cfg = load_config(target)                            # 缺字段 -> 1
    # 步骤3 游戏运行检测
    check = game_running_check or detect_game_running
    if check():
        log("检测到 Minecraft 相关进程（java/javaw/minecraft）正在运行，请完全退出游戏后重试。")
        return EXIT_GAME_RUNNING

    # 步骤4 拉指针 + 版本清单（验签失败 -> 2）
    pointer_url = str(cfg["manifestUrl"])
    try:
        pointer = fetch_json(pointer_url, cache_bust=True)
    except urllib.error.HTTPError as e:
        log("指针拉取失败: HTTP %s (%s)" % (e.code, pointer_url))
        return EXIT_NETWORK
    if not canonicaljson.verify(pointer, str(cfg["publicKey"])):
        log("指针验签失败: %s" % pointer_url)
        return EXIT_SIGNATURE
    man_url = _resolve_url(pointer_url, str(pointer.get("manifestUrl") or ""))
    if not man_url:
        log("指针缺少 manifestUrl")
        return EXIT_GENERIC
    try:
        man = fetch_json(man_url)
    except urllib.error.HTTPError as e:
        log("版本清单拉取失败: HTTP %s (%s)" % (e.code, man_url))
        return EXIT_NETWORK
    if not canonicaljson.verify(man, str(cfg["publicKey"])):
        log("版本清单验签失败: %s" % man_url)
        return EXIT_SIGNATURE

    # 步骤5 兼容闸
    sv = int(man.get("schemaVersion") or 0)
    if sv > SUPPORTED_SCHEMA_VERSION:
        log("清单 schemaVersion=%d 高于本程序支持值 %d，请更新客户端。"
            % (sv, SUPPORTED_SCHEMA_VERSION))
        return EXIT_SCHEMA
    if str(man.get("packId") or "") != str(cfg["packId"]):
        log("packId 不一致: 清单=%r config=%r" % (man.get("packId"), cfg["packId"]))
        return EXIT_PACKID

    new_ver = str(man.get("version") or "")
    files = list(man.get("files") or [])
    delete = list(man.get("delete") or [])
    state = read_state(target)
    old_ver = str(state.get("version") or "")

    # 步骤6 版本回退
    if _is_downgrade(new_ver, old_ver):
        bar = "=" * 64
        log(bar)
        log("警告：检测到版本回退 —— 本地已同步 %s，云端最新为 %s" % (old_ver, new_ver))
        log("警告：这通常是发布端误操作；如非预期请立即联系管理员。")
        log(bar)
        log("WARN 版本回退: %s -> %s" % (old_ver, new_ver))
        if no_downgrade:
            log("已指定 --no-downgrade，终止本次同步。")
            return EXIT_GENERIC

    # 步骤7 + 步骤8 扫描与规划
    plan = plan_changes(target, files, delete, strict)
    downloads: List[dict] = plan["downloads"]
    copies: Dict[str, str] = plan["copies"]
    to_delete: List[dict] = plan["to_delete"]
    disk: Dict[str, dict] = plan["disk"]

    log("目标: %s" % target)
    log("云端版本: %s（本地 %s）" % (new_ver or "-", old_ver or "-"))
    log("变更预览: 新增/更新 %d（其中改名复用 %d，需下载 %d），删除 %d"
        % (len(plan["need"]), len(copies), len(downloads), len(to_delete)))

    # 步骤10 路径安全 + 磁盘预检（先于任何写操作）
    check_paths(target, sorted(set(list(plan["desired"].keys())
                                   + [e["path"] for e in to_delete])))
    download_bytes = sum(int(e["size"]) for e in downloads)
    backup_bytes = sum(int(e.get("size") or 0) for e in to_delete) + \
        sum(int(p["size"]) for p in plan["need"] if p["path"] in disk)
    check_disk_space(target, download_bytes, backup_bytes)

    # 步骤9 下载
    base = blob_base_of(pointer_url)
    staging = staging_dir(target)
    if downloads:
        log("开始下载 %d 个文件（并发 4，单个失败重试 3 次）..." % len(downloads))
        http_download.download_blobs([{"path": e["path"], "sha256": e["sha256"],
                                       "size": int(e["size"])} for e in downloads],
                                     base, staging, concurrency=4, retries=3, log=log)
    got_bytes = download_bytes

    # 步骤11 备份
    bdir = _new_backup_dir(target)
    backup_rel = {}
    replaced = [e for e in plan["need"] if e["path"] in disk]
    backup_srcs = [e["path"] for e in replaced] + [e["path"] for e in to_delete]
    backup_rel = _backup_files(target, bdir, sorted(set(backup_srcs)), log)
    log("备份目录: %s（%d 个文件）" % (os.path.relpath(bdir, target), len(backup_rel)))

    changes = {
        "schemaVersion": SUPPORTED_SCHEMA_VERSION,
        "packId": cfg["packId"],
        "version": new_ver,
        "appliedAt": _now_iso(),
        "changes": {
            "added": [{"path": e["path"], "newSha256": e["sha256"], "size": int(e["size"])}
                      for e in plan["need"] if e["path"] not in disk],
            "replaced": [{"path": e["path"],
                          "oldSha256": disk[e["path"]]["sha256"],
                          "oldSize": int(disk[e["path"]]["size"]),
                          "newSha256": e["sha256"], "size": int(e["size"]),
                          "backupPath": backup_rel.get(e["path"], "")}
                         for e in replaced],
            "deleted": [{"path": e["path"], "oldSha256": e["sha256"],
                         "oldSize": int(e["size"]),
                         "backupPath": backup_rel.get(e["path"], ""),
                         **({"reason": e["reason"]} if e.get("reason") == "strict" else {})}
                        for e in to_delete],
        },
    }
    manifest.write_json(os.path.join(bdir, "changes.json"), changes)
    _fault("after-backup", log)

    # 步骤12 应用
    half = max(0, sum(int(e["size"]) for e in plan["need"])) / 2.0
    done = 0
    for e in plan["need"]:
        dst = paths.safe_join(target, e["path"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if e["path"] in copies:
            src = paths.safe_join(target, copies[e["path"]])
        else:
            src = os.path.join(staging, "blobs", e["sha256"][0:2], e["sha256"][2:4],
                               e["sha256"])
        _apply_file(src, dst)
        done += int(e["size"])
        if half and done > half:
            _fault("mid-apply", log)
    for e in to_delete:
        p = paths.safe_join(target, e["path"])
        if os.path.isfile(p):
            os.remove(p)
    _fault("mid-apply", log)

    # 步骤13 原子写 state
    write_state(target, cfg["packId"], new_ver, files)
    _fault("after-state", log)

    # 步骤14 清理 staging + 旧备份
    shutil.rmtree(staging, ignore_errors=True)
    _prune_backups(target, max(0, int(keep_backups)), log)

    # 步骤15 摘要
    added = len(changes["changes"]["added"])
    repl = len(changes["changes"]["replaced"])
    dele = len(changes["changes"]["deleted"])
    log("-" * 64)
    log("同步完成：更新 %d，新增 %d，删除 %d；下载 %d 个文件，共 %.1f MiB"
        % (repl, added, dele, len(downloads), got_bytes / 1024.0 / 1024.0))
    if not plan["need"] and not to_delete:
        log("本地已是最新，无需变更。")
    log("版本: %s" % (new_ver or "-"))
    return EXIT_OK


def _apply_file(src: str, dst: str) -> None:
    """临时文件 + os.replace 原子落位。"""
    d = os.path.dirname(dst)
    tmp = os.path.join(d, ".%s.tmp-%d" % (os.path.basename(dst), os.getpid()))
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        shutil.copyfileobj(fi, fo, 1024 * 1024)
    os.replace(tmp, dst)


# --------------------------------------------------------------------------
# verify（只读）
# --------------------------------------------------------------------------

def verify(target: str, strict: bool = False, log: Callable[[str], None] = print) -> int:
    """只扫描不修改；完全一致码 0，有差异码 1。"""
    target = os.path.abspath(target)
    try:
        cfg = load_config(target)
    except ClientError as e:
        log("错误(%d): %s" % (e.exit_code, e))
        return int(e.exit_code)
    try:
        pointer = fetch_json(str(cfg["manifestUrl"]), cache_bust=True)
        if not canonicaljson.verify(pointer, str(cfg["publicKey"])):
            log("指针验签失败")
            return EXIT_SIGNATURE
        man_url = _resolve_url(str(cfg["manifestUrl"]), str(pointer.get("manifestUrl") or ""))
        man = fetch_json(man_url)
        if not canonicaljson.verify(man, str(cfg["publicKey"])):
            log("版本清单验签失败")
            return EXIT_SIGNATURE
    except urllib.error.HTTPError as e:
        log("拉取失败: HTTP %s" % e.code)
        return EXIT_NETWORK
    except ClientError as e:
        log("错误(%d): %s" % (e.exit_code, e))
        return int(e.exit_code)

    files = list(man.get("files") or [])
    delete = list(man.get("delete") or [])
    plan = plan_changes(target, files, delete, strict)
    state = read_state(target)
    new_ver = str(man.get("version") or "")

    missing = [p for p in sorted(plan["desired"]) if p not in plan["disk"]]
    corrupt = [e["path"] for e in sorted(plan["need"], key=lambda x: x["path"])
               if e["path"] in plan["disk"]]
    extra = [e["path"] for e in self_installed_entries(plan["disk"], plan["desired"])]
    pending_delete = [e["path"] for e in plan["to_delete"]]
    downgrade = _is_downgrade(new_ver, str(state.get("version") or ""))

    for title, items in (("缺失", missing), ("损坏", corrupt),
                         ("多余（玩家自装）", extra), ("待删除", pending_delete)):
        log("%s: %d" % (title, len(items)))
        for p in items:
            log("  - %s" % p)
    log("版本回退: %s" % ("是（%s -> %s）" % (state.get("version"), new_ver) if downgrade else "否"))

    dirty = bool(missing or corrupt or extra or pending_delete or downgrade)
    log("结论: %s" % ("存在差异（码 1）" if dirty else "与云端一致（码 0）"))
    return EXIT_GENERIC if dirty else EXIT_OK


# --------------------------------------------------------------------------
# rollback
# --------------------------------------------------------------------------

def list_backups(target: str) -> List[str]:
    root = backup_root(target)
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))


def _latest_rollbackable(target: str, log: Callable[[str], None]) -> Optional[str]:
    """最新且未 rolled-back 的备份目录。"""
    for name in reversed(list_backups(target)):
        d = os.path.join(backup_root(target), name)
        if os.path.isfile(os.path.join(d, "changes.json")):
            return d
    return None


def rollback(target: str, log: Callable[[str], None] = print) -> int:
    target = os.path.abspath(target)
    lock = locking.FileLock(lock_path(target))
    try:
        lock.acquire()
    except locking.LockHeld as e:
        log("已有进程在运行（锁占用）: %s" % e)
        return EXIT_LOCK
    try:
        return _rollback_locked(target, log)
    except paths.UnsafePath as e:
        log("错误(13): %s" % e)
        return EXIT_UNSAFE_PATH
    except ClientError as e:
        log("错误(%d): %s" % (e.exit_code, e))
        return int(e.exit_code)
    except Exception as e:  # noqa: BLE001
        log("错误(1): %s: %s" % (type(e).__name__, e))
        return EXIT_GENERIC
    finally:
        lock.release()


def _rollback_locked(target: str, log: Callable[[str], None]) -> int:
    bdir = _latest_rollbackable(target, log)
    if bdir is None:
        log("没有可回滚的备份（或全部已回滚）。")
        return EXIT_GENERIC
    cf = os.path.join(bdir, "changes.json")
    if not os.path.isfile(cf):
        log("缺少 changes.json: %s" % cf)
        return EXIT_IO
    data = _read_json(cf)
    if data is None:
        log("changes.json 解析失败: %s" % cf)
        return EXIT_IO
    changes = (data.get("changes") or {})
    drift_root = os.path.join(bdir, "rollback-drift")

    def _hash(rel: str) -> Optional[str]:
        p = paths.safe_join(target, rel)
        if not os.path.isfile(p):
            return None
        return str(hashing.hash_file(p)["sha256"])

    def _to_drift(rel: str) -> None:
        src = paths.safe_join(target, rel)
        dst = os.path.join(drift_root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        log("手工漂移，已移入 rollback-drift: %s" % rel)

    def _restore(entry: dict) -> bool:
        rel = entry.get("path")
        bp = entry.get("backupPath") or ("backup/" + str(rel))
        src = os.path.join(bdir, bp.replace("/", os.sep))
        if not os.path.isfile(src):
            log("备份缺失，跳过: %s" % rel)
            return False
        dst = paths.safe_join(target, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _apply_file(src, dst)
        return True

    # added: 磁盘有且哈希==newSha256 -> 删除；漂移 -> 先移入 rollback-drift 再删
    for e in changes.get("added") or []:
        rel = e["path"]
        h = _hash(rel)
        if h is None:
            continue
        if h != e.get("newSha256"):
            _to_drift(rel)
        p = paths.safe_join(target, rel)
        if os.path.isfile(p):
            os.remove(p)
        log("回滚删除: %s" % rel)

    # replaced: 哈希==new -> 还原；漂移 -> 先移入 rollback-drift 再还原；无 -> 直接还原
    for e in changes.get("replaced") or []:
        rel = e["path"]
        h = _hash(rel)
        if h is not None and h != e.get("newSha256"):
            _to_drift(rel)
        if _restore(e):
            log("回滚还原: %s" % rel)

    # deleted: 无 -> 还原；有但哈希 != old -> 先移入 rollback-drift 再还原；== old -> 跳过
    for e in changes.get("deleted") or []:
        rel = e["path"]
        h = _hash(rel)
        if h is not None and h == e.get("oldSha256"):
            continue
        if h is not None:
            _to_drift(rel)
        if _restore(e):
            log("回滚恢复: %s" % rel)

    # 复核还原文件哈希
    for key, want in (("replaced", "oldSha256"), ("deleted", "oldSha256")):
        for e in changes.get(key) or []:
            p = paths.safe_join(target, e["path"])
            if not os.path.isfile(p):
                log("复核失败：文件不存在 %s" % e["path"])
                return EXIT_IO
            if str(hashing.hash_file(p)["sha256"]) != e.get(want):
                log("复核失败：哈希不符 %s" % e["path"])
                return EXIT_IO

    os.replace(cf, cf + ".rolled-back")
    write_state(target, str(data.get("packId") or ""), None, _disk_files(target))
    log("-" * 64)
    log("回滚完成：备份目录 %s（版本 %s）" % (os.path.relpath(bdir, target), data.get("version")))
    log("已标记 changes.json.rolled-back；本地 state 版本置空。")
    return EXIT_OK


# --------------------------------------------------------------------------
# doctor（C 端）
# --------------------------------------------------------------------------

def doctor(target: str, log: Callable[[str], None] = print) -> int:
    target = os.path.abspath(target)
    rows: List[Tuple[str, Optional[bool], str]] = []

    def add(name: str, ok: Optional[bool], detail: str = "") -> None:
        rows.append((name, ok, detail))
        log("[%s] %s%s" % ("PASS" if ok else ("SKIP" if ok is None else "FAIL"),
                           name, (" — " + detail) if detail else ""))

    cfg: Optional[dict] = None
    try:
        cfg = load_config(target)
        add("配置完整", True, "%s" % config_path(target))
    except ClientError as e:
        add("配置完整", False, str(e))

    up = updater_dir(target)
    try:
        os.makedirs(up, exist_ok=True)
        probe = os.path.join(up, ".doctor-write-probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        add("目录可写", True, up)
    except OSError as e:
        add("目录可写", False, str(e))

    try:
        free = shutil.disk_usage(target).free
        add("磁盘余量", free > 100 * 1024 * 1024, "%.1f GiB 可用" % (free / 1024 ** 3))
    except OSError as e:
        add("磁盘余量", False, str(e))

    if cfg is not None:
        try:
            pointer = fetch_json(str(cfg["manifestUrl"]), cache_bust=True)
            add("指针可达", True, str(cfg["manifestUrl"]))
        except Exception as e:  # noqa: BLE001
            pointer = None
            add("指针可达", False, str(e)[:120])
        if pointer is not None:
            ok = canonicaljson.verify(pointer, str(cfg["publicKey"]))
            add("指针验签", ok)
            if ok:
                try:
                    man_url = _resolve_url(str(cfg["manifestUrl"]),
                                           str(pointer.get("manifestUrl") or ""))
                    man = fetch_json(man_url)
                    add("版本清单验签", canonicaljson.verify(man, str(cfg["publicKey"])),
                        "版本 %s" % man.get("version"))
                except Exception as e:  # noqa: BLE001
                    add("版本清单验签", False, str(e)[:120])

    bad = [r for r in rows if r[1] is False]
    log("结论: %s" % ("存在问题（%d 项）" % len(bad) if bad else "全部通过"))
    return EXIT_GENERIC if bad else EXIT_OK
