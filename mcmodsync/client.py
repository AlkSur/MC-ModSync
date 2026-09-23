"""C 端 mod 同步客户端（纯标准库）—— [T-40]。

subcommand: sync / verify / rollback / doctor（由 client_main.py 分发）。

布局（`<target>` = 实例目录，即 `_updater/` 的父目录）:
  <target>/mods/*.jar                     游戏实际读取的平铺 mod 目录
  <target>/_updater/config.json           [3.6] 随分发包下发
  <target>/_updater/state.json            [3.5] 本地已同步状态
  <target>/_updater/sources.json          下载源索引本地缓存（A 端发布，每次启动覆盖）
  <target>/_updater/.lock                 运行锁（失败码 10）
  <target>/_updater/staging/              下载暂存（blobs/<xx>/<yy>/<sha>）
  <target>/_updater/backup/<时间戳>/      备份 + changes.json（[3.7]）

退出码: 0 成功 / 1 一般错误（含配置缺失、版本回退拒绝）/ 2 验签失败 /
        3 游戏运行中 / 4 磁盘不足 / 5 下载失败 / 6 清单 schema 过新 /
        7 packId 不符 / 10 锁被占用 / 13 路径不安全 / 99 故障注入。

本模块仅 import 标准库（C 端分发包要求）。
"""
from __future__ import annotations

import csv
import io
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

from . import canonicaljson, hashing, http_download, locking, manifest, paths, source_index

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


def sources_path(target: str) -> str:
    """下载源索引的本地缓存（A 端 sources.json 的副本；纯优化层，损坏无碍）。"""
    return os.path.join(updater_dir(target), "sources.json")


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
    # 秒级 + 毫秒后缀：同一秒内多次同步时目录名仍单调递增，
    # 保证 _prune_backups 的字典序排序 == 创建顺序（否则同秒内新备份反被当作最旧清理）。
    t = now if now is not None else time.time()
    return datetime.fromtimestamp(t).strftime("%Y%m%d-%H%M%S") + "-%03d" % (int(t * 1000) % 1000)


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


# 映像名含这些片段的进程 = Minecraft 本体/官方启动器（不含 java 宿主）
_MC_IMAGE_HINTS = ("minecraft",)
# java 宿主进程映像名（需要看命令行才能区分 MC 与任意 Java 程序）
_JAVA_IMAGES = ("java.exe", "javaw.exe", "java", "javaw")
# 命令行里出现任一即说明"与 MC 有关"
_MC_CMDLINE_HINTS = ("minecraft", "net.minecraftforge", "net.neoforged",
                     "cpw.mods", "net.fabricmc", "quiltmc", "launchwrapper")
# 专用服务端特征：命中且无客户端专有参数 -> 视为服务端，不拦截
_MC_SERVER_MARKERS = ("net.minecraft.server", "net/minecraft/server",
                      "nogui", "server.jar", "minecraft_server")
# 只有客户端才会带的启动参数
_MC_CLIENT_ONLY_MARKERS = ("net.minecraft.client", "net/minecraft/client",
                           "--username", "--accesstoken", "--uuid",
                           "--gamedir", "--assetsdir", "--assetindex", "--clientid")


def _is_mc_image(image: str) -> bool:
    """映像名是否明确属于 Minecraft 本体/启动器（java 等宿主进程不算）。"""
    low = (image or "").lower()
    return any(h in low for h in _MC_IMAGE_HINTS)


def _is_java_image(image: str) -> bool:
    return (image or "").strip().lower() in _JAVA_IMAGES


def _looks_like_mc_client(cmdline: str) -> bool:
    """命令行是否属于一个正在运行的 Minecraft **客户端**（而非专用服务端）。

    只看映像名无法区分 MC 与任意 Java 程序，所以对 java/javaw 必须看命令行：
    含 MC 相关特征、且不是纯服务端（nogui / net.minecraft.server / server.jar …）
    才判为客户端。
    """
    low = (cmdline or "").lower()
    if not low or not any(h in low for h in _MC_CMDLINE_HINTS):
        return False
    if any(m in low for m in _MC_SERVER_MARKERS) and \
            not any(m in low for m in _MC_CLIENT_ONLY_MARKERS):
        return False
    return True


def _parse_image_pid_csv(text: str) -> List[Tuple[str, str]]:
    """解析 `tasklist /FO CSV /NH` -> [(image, pid)]。"""
    rows: List[Tuple[str, str]] = []
    for row in csv.reader(io.StringIO(text or "")):
        if len(row) >= 2 and row[1].strip().isdigit():
            rows.append((row[0].strip(), row[1].strip()))
    return rows


def _parse_wmic_csv(text: str) -> Dict[str, str]:
    """解析 `wmic process get ProcessId,CommandLine /format:csv` -> {pid: cmdline}。"""
    res: Dict[str, str] = {}
    for row in csv.reader(io.StringIO(text or "")):
        if len(row) >= 3 and row[2].strip().isdigit():
            res[row[2].strip()] = row[1]
    return res


def _parse_cim_csv(text: str) -> Dict[str, str]:
    """解析 PowerShell `ConvertTo-Csv`（Name,ProcessId,CommandLine）-> {pid: cmdline}。"""
    res: Dict[str, str] = {}
    rows = list(csv.reader(io.StringIO(text or "")))
    if not rows:
        return res
    try:
        hdr = [h.strip().lower() for h in rows[0]]
        i_name, i_pid, i_cl = (hdr.index("name"), hdr.index("processid"),
                               hdr.index("commandline"))
    except ValueError:
        return res
    for row in rows[1:]:
        if len(row) <= max(i_name, i_pid, i_cl):
            continue
        name, pid, cl = row[i_name].strip().lower(), row[i_pid].strip(), row[i_cl]
        if pid.isdigit() and cl and (_is_java_image(name) or _is_mc_image(name)):
            res[pid] = cl
    return res


def _java_command_lines() -> Dict[str, str]:
    """尽力而为地取 java 进程命令行（{pid: cmdline}）；取不到返回空 dict。

    新系统已移除 wmic，故优先 wmic、回退 PowerShell CIM。两条路都失败就不下结论
    （宁可漏判，也不误判卡住玩家更新）。
    """
    try:
        out = subprocess.run(["wmic", "process", "get", "ProcessId,CommandLine",
                              "/format:csv"], capture_output=True, text=True,
                             timeout=25, errors="ignore")
        got = _parse_wmic_csv(out.stdout or "")
        if got:
            return got
    except Exception:  # noqa: BLE001
        pass
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return {}
    try:
        out = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | Select-Object Name,ProcessId,CommandLine"
             " | ConvertTo-Csv -NoTypeInformation"],
            capture_output=True, text=True, timeout=40, errors="ignore")
        return _parse_cim_csv(out.stdout or "")
    except Exception:  # noqa: BLE001
        return {}


def detect_game_running() -> bool:
    """检测本机是否正在运行 Minecraft **客户端**（步骤3）。

    仅当确有客户端在跑时返回 True（-> 退出码 3）。机上有任意 Java 程序
    （IDEA / 其他 Java 应用 / MC 服务端 / 其他整合包）不再误判。
    """
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=20, errors="ignore")
            procs = _parse_image_pid_csv(out.stdout or "")
        else:
            out = subprocess.run(["ps", "-eo", "comm,args"], capture_output=True,
                                 text=True, timeout=20, errors="ignore")
            procs = []
            for line in (out.stdout or "").splitlines()[1:]:
                parts = line.strip().split(None, 1)
                if parts:
                    procs.append((parts[0], parts[1] if len(parts) > 1 else ""))
    except Exception:  # noqa: BLE001  检测失败不阻断
        return False

    if not procs:
        return False
    # ① 明确的 MC 本体/启动器映像名
    if any(_is_mc_image(img) for img, _ in procs):
        return True

    if os.name != "nt":
        # POSIX: ps 的 args 第二列就是命令行，直接判定
        for img, args in procs:
            if _is_java_image(img) and _looks_like_mc_client(args):
                return True
        return False

    # ② Windows: 只有存在 java/java(w) 时才值得额外花代价取命令行
    java_pids = [pid for img, pid in procs if _is_java_image(img)]
    if not java_pids:
        return False
    cmdlines = _java_command_lines()
    for pid in java_pids:
        cl = cmdlines.get(pid)
        if cl and _looks_like_mc_client(cl):
            return True
    return False


# --------------------------------------------------------------------------
# 网络读取（只读）
# --------------------------------------------------------------------------

def fetch_json(url: str, cache_bust: bool = False, timeout: int = 30,
               log: Optional[Callable[[str], None]] = None) -> dict:
    """GET JSON；指针请求追加 ?t=<unix秒>（[T-40] 步骤4）。"""
    if cache_bust:
        sep = "&" if "?" in url else "?"
        url = "%s%st=%d" % (url, sep, int(time.time()))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                              "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if log:
                log("HTTP %d %s" % (resp.status, url))
            return data
    except urllib.error.URLError as e:
        # 补上 URL：诊断 HTTPS 证书失败时需要知道连的是哪台主机
        if not getattr(e, "filename", ""):
            e.filename = url
        raise


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


def load_source_index(target: str, pointer_url: str, cfg: dict,
                      enabled: bool = True,
                      trace: Optional[Callable[[str], None]] = None) -> Dict[str, tuple]:
    """拉取最新「下载源索引」并覆盖本地缓存，返回 {sha256: (来源, 直链)}。

    纯优化层（[T-52] 多源下载）：
    - enabled=False（config `client.preferPlatform=false` 或 --no-platform）时
      完全不请求、不读缓存，直接返回空表 -> 全部走对象存储；
    - 拉取/验签/落盘任何失败都**静默**（终端零输出，只在 trace 留一行），
      并沿用本地缓存兜底；
    - 本函数不参与同步成败判定，拿不到就是"没有优化可用"。
    """
    if not enabled:
        return {}
    path = sources_path(target)
    cached = source_index.load_cached(path)
    fresh = source_index.fetch_index(pointer_url, str(cfg.get("publicKey") or ""),
                                     str(cfg.get("packId") or ""), timeout=3, trace=trace)
    if fresh is not None:
        source_index.save_cached(path, fresh)
    idx = fresh if fresh is not None else cached
    out: Dict[str, tuple] = {}
    for sha in list((idx or {}).get("sources") or {}):
        hit = source_index.lookup(idx, sha)
        if hit:
            out[str(sha).lower()] = hit
    if trace:
        state = ("已更新" if fresh is not None
                 else ("沿用本地缓存" if cached else "无（全部走对象存储）"))
        trace("下载源索引: %s，可用直链 %d 条" % (state, len(out)))
    return out


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
# 网络失败诊断（HTTPS 证书问题在玩家机器上最常见，输出可自证的信息）
# --------------------------------------------------------------------------

def _flatten_name(seq) -> str:
    """把 _test_decode_cert 的 subject/issuer 嵌套元组拍平成 "k=v, k=v"。"""
    out = []
    for rdn in seq or ():
        for k, v in rdn:
            out.append("%s=%s" % (k, v))
    return ", ".join(out)


def _san_covers(info: dict, host: str) -> bool:
    """证书的 subjectAltName 是否覆盖 host（判断是不是发给我们这台主机的证书）。"""
    import fnmatch
    for typ, val in info.get("subjectAltName") or ():
        if typ == "DNS" and (val == host or fnmatch.fnmatch(host, val)):
            return True
    return False


def _ssl_cert_info(host: str, port: int = 443) -> dict:
    """取该主机实际出示的证书信息（不校验，仅用于诊断）。失败返回 {}。"""
    import socket
    import ssl as _ssl
    import tempfile
    if not host or host == "?":
        return {}
    try:
        ctx = _ssl._create_unverified_context()
        with socket.create_connection((host, port), timeout=8) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                pem = _ssl.DER_cert_to_PEM_cert(ss.getpeercert(binary_form=True))
        fd, path = tempfile.mkstemp(suffix=".pem")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(pem)
            return _ssl._ssl._test_decode_cert(path)     # CPython 内置解析
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    except Exception:                                    # noqa: BLE001
        return {}


def _looks_like_ssl_failure(text: str) -> bool:
    up = text.upper()
    return ("CERTIFICATE_VERIFY_FAILED" in up or "SSLCERTVERIFICATIONERROR" in up
            or "SSL: " in up or "SSLEOFERROR" in up)


def report_ssl_failure(err: Exception, log: Callable[[str], None]) -> bool:
    """若 err 属于 HTTPS 证书类失败，打印可自检的诊断信息；返回是否命中。"""
    import re
    text = str(err)
    typed_ssl = False
    try:
        import ssl as _ssl
        typed_ssl = isinstance(getattr(err, "reason", None), _ssl.SSLError)
    except Exception:                                    # noqa: BLE001
        pass
    if not typed_ssl and not _looks_like_ssl_failure(text):
        return False
    url = str(getattr(err, "filename", "") or "")
    if not url:
        m = re.search(r"https?://[^\s)]+", text)
        url = m.group(0) if m else ""
    host = urllib.parse.urlsplit(url).hostname or "?"
    log("无法建立安全连接（HTTPS 证书校验失败）")
    log("    目标主机: %s" % host)
    log("    失败原因: %s" % text)
    if host == "?":
        log("    提示: 未能识别目标主机，请把本窗口内容发给服主")
    log("    本机时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    info = _ssl_cert_info(host)
    if info:
        subj = _flatten_name(info.get("subject"))
        iss = _flatten_name(info.get("issuer"))
        log("    实际收到的证书: %s" % (subj or "?"))
        log("    该证书签发者  : %s" % (iss or "?"))
        log("    该证书有效期  : %s ~ %s"
            % (info.get("notBefore") or "?", info.get("notAfter") or "?"))
        if host != "?" and not _san_covers(info, host):
            log("    [注意] 这张证书不是颁发给 %s 的 —— 连接很可能被安全软件或"
                "公司代理做了 HTTPS 拦截" % host)
        else:
            log("    若本机时间不在上面的有效期内，说明系统时间不准确"
                "（请开启自动设置时间后重试）")
    else:
        log("    无法获取服务器证书（网络不通，或 HTTPS 被安全软件/代理拦截）")
    log("    排查顺序: 1) 核对本机时间是否准确（时间偏差会让证书被判定为过期）；"
        "2) 运行 Windows Update 更新系统根证书；"
        "3) 暂时关闭杀毒软件/代理的 HTTPS 扫描后重试")
    if url:
        log("    自测方法: 用浏览器打开 %s" % url)
        log("              若浏览器也报证书错误，说明是本机环境问题，与更新器无关")
    return True


# --------------------------------------------------------------------------
# sync（步骤1-15）
# --------------------------------------------------------------------------

def sync(target: str, strict: bool = False, no_downgrade: bool = False,
         keep_backups: int = DEFAULT_KEEP_BACKUPS, log: Callable[[str], None] = print,
         game_running_check: Optional[Callable[[], bool]] = None,
         progress: Optional[Callable[[int, int, int, int], None]] = None,
         on_file_start: Optional[Callable[[dict], None]] = None,
         on_file_bytes: Optional[Callable[[str, int], None]] = None,
         on_file_reset: Optional[Callable[[str], None]] = None,
         on_file_done: Optional[Callable[[dict], None]] = None,
         trace: Optional[Callable[[str], None]] = None,
         prefer_platform: bool = True) -> int:
    """同步一次。

    progress / on_file_* / trace 均为**控制台渲染钩子**（可为 None）：
      progress(已完成文件数, 总文件数, 已完成字节, 总字节)   —— 聚合进度
      on_file_start(entry) / on_file_bytes(sha, n)          —— 文件级双行进度
      on_file_reset(sha) / on_file_done(entry)              —— 重试与完成
      trace(msg)                                            —— 只写日志文件的技术细节
    这些钩子只读、不参与任何判定：下载 / 分片 / 重试 / 校验 / 备份逻辑不受影响。

    prefer_platform（多源下载，默认开）：命中下载源索引时优先走 Modrinth /
    CurseForge 官方 CDN 直链，失败静默回落对象存储；关掉则完全不请求该索引。
    """
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
                            game_running_check, progress, on_file_start,
                            on_file_bytes, on_file_reset, on_file_done, trace,
                            prefer_platform)
    except ClientError as e:
        log("错误(%d): %s" % (e.exit_code, e))
        return int(e.exit_code)
    except paths.UnsafePath as e:
        log("错误(13): %s" % e)
        return EXIT_UNSAFE_PATH
    except http_download.DownloadError as e:
        log("错误(%d): %s" % (e.exit_code, e))
        report_ssl_failure(e, log)          # 证书类失败给出排查指引
        return int(e.exit_code)
    except urllib.error.HTTPError as e:
        log("错误(5): HTTP %s: %s" % (e.code, e.url))
        return EXIT_NETWORK
    except urllib.error.URLError as e:
        # URLError 可能是 SSL 证书失败（玩家机器时间不准/根证书过旧/HTTPS 拦截）
        if not report_ssl_failure(e, log):
            log("错误(5): 网络请求失败: %s" % e)
        else:
            log("错误(5): 网络请求失败（详见上方排查指引）")
        return EXIT_NETWORK
    except Exception as e:  # noqa: BLE001
        log("错误(1): %s: %s" % (type(e).__name__, e))
        return EXIT_GENERIC
    finally:
        lock.release()


def _sync_locked(target: str, strict: bool, no_downgrade: bool, keep_backups: int,
                 log: Callable[[str], None],
                 game_running_check: Optional[Callable[[], bool]],
                 progress: Optional[Callable[[int, int, int, int], None]] = None,
                 on_file_start: Optional[Callable[[dict], None]] = None,
                 on_file_bytes: Optional[Callable[[str, int], None]] = None,
                 on_file_reset: Optional[Callable[[str], None]] = None,
                 on_file_done: Optional[Callable[[dict], None]] = None,
                 trace: Optional[Callable[[str], None]] = None,
                 prefer_platform: bool = True) -> int:
    # 步骤2 配置
    cfg = load_config(target)                            # 缺字段 -> 1
    # 步骤3 游戏运行检测
    check = game_running_check or detect_game_running
    if check():
        log("检测到 Minecraft 客户端正在运行，请完全退出游戏后重试。")
        return EXIT_GAME_RUNNING

    # 步骤4 拉指针 + 版本清单（验签失败 -> 2）
    pointer_url = str(cfg["manifestUrl"])
    try:
        pointer = fetch_json(pointer_url, cache_bust=True, log=log)
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
        man = fetch_json(man_url, log=log)
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
    for e in plan["need"]:
        kind = "改名复用" if e["path"] in copies else ("更新" if e["path"] in disk else "新增")
        log("决策: %s %s (sha256=%s, size=%d)" % (kind, e["path"], e["sha256"], int(e["size"])))
    for e in to_delete:
        log("决策: 删除 %s (sha256=%s, reason=%s)"
            % (e["path"], e["sha256"], e["reason"]))
    skipped = len(plan["desired"]) - len(plan["need"])
    if skipped:
        log("决策: 跳过 %d 个（磁盘哈希已与清单一致）" % skipped)
    # 渲染层：本地哈希已与清单一致的文件逐行精简为「跳过：<文件名>」（不打印哈希）
    need_paths = set(e["path"] for e in plan["need"])
    for p in sorted(plan["desired"]):
        if p not in need_paths:
            log("跳过：%s" % os.path.basename(p))

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
    total_files = len(plan["need"])
    total_bytes = sum(int(e["size"]) for e in plan["need"])
    tick = {"files": 0, "bytes": 0}

    def _emit() -> None:
        if progress:
            progress(tick["files"], total_files, tick["bytes"], total_bytes)

    for e in plan["need"]:                       # 改名复用无需网络，直接计入完成
        if e["path"] in copies:
            tick["files"] += 1
            tick["bytes"] += int(e["size"])
            _emit()
    if downloads:
        log("开始下载 %d 个文件（并发 4，单个失败重试 3 次）..." % len(downloads))

        def _on_done(entry: dict) -> None:
            tick["files"] += 1
            tick["bytes"] += int(entry["size"])
            if on_file_done:                     # 渲染：文件进度打满 100%
                on_file_done(entry)
            _emit()

        # 下载源索引：只有真要下东西时才拉一次（纯优化层，失败静默、不阻断）
        alt = load_source_index(target, pointer_url, cfg,
                                bool(cfg.get("preferPlatform", True)) and prefer_platform,
                                trace)
        tasks = []
        for e in downloads:
            hit = alt.get(str(e["sha256"]).lower())
            tasks.append({
                "path": e["path"], "sha256": e["sha256"], "size": int(e["size"]),
                # source 仅用于控制台“来源”标识；altUrl 命中时优先走平台 CDN 直链
                "source": (hit[0] if hit else ""),
                "altUrl": (hit[1] if hit else ""),
            })
        http_download.download_blobs(tasks, base, staging, concurrency=4, retries=3, log=log,
                                     on_done=_on_done, on_start=on_file_start,
                                     on_bytes=on_file_bytes, on_reset=on_file_reset,
                                     trace=trace)
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
        pointer = fetch_json(str(cfg["manifestUrl"]), cache_bust=True, log=log)
        if not canonicaljson.verify(pointer, str(cfg["publicKey"])):
            log("指针验签失败")
            return EXIT_SIGNATURE
        man_url = _resolve_url(str(cfg["manifestUrl"]), str(pointer.get("manifestUrl") or ""))
        man = fetch_json(man_url, log=log)
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
    probe = os.path.join(up, ".doctor-write-probe-%d" % os.getpid())
    try:
        os.makedirs(up, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        note = up
        try:
            os.remove(probe)
        except OSError as e:      # 清理失败不代表目录不可写（如杀软/策略拦截删除）
            note = "%s（探针文件清理失败: %s）" % (up, e)
        add("目录可写", True, note)
    except OSError as e:
        add("目录可写", False, str(e))

    try:
        free = shutil.disk_usage(target).free
        add("磁盘余量", free > 100 * 1024 * 1024, "%.1f GiB 可用" % (free / 1024 ** 3))
    except OSError as e:
        add("磁盘余量", False, str(e))

    if cfg is not None:
        try:
            pointer = fetch_json(str(cfg["manifestUrl"]), cache_bust=True, log=log)
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
                    man = fetch_json(man_url, log=log)
                    add("版本清单验签", canonicaljson.verify(man, str(cfg["publicKey"])),
                        "版本 %s" % man.get("version"))
                except Exception as e:  # noqa: BLE001
                    add("版本清单验签", False, str(e)[:120])

    bad = [r for r in rows if r[1] is False]
    log("结论: %s" % ("存在问题（%d 项）" % len(bad) if bad else "全部通过"))
    return EXIT_GENERIC if bad else EXIT_OK
