"""push-server / rollback-server 编排（A 端，[T-21]）。

要点:
  - B 引导（bootstrap）: 上传/升级服务端单文件 B，校验 --protocol-version，
    失败可回退 .bak；远端目录自动创建。
  - push-server 11 步、rollback-server 3 步。
  - 所有远端操作经 `conn`（SSHClient 兼容对象）进行，便于注入测试替身。
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shlex
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from . import hashing, manifest, planner

PROTOCOL_VERSION = "MC-ModSync-B 2.0"
SUPPORTED_SCHEMA_VERSION = 1

B_REL_PATH = os.path.join("server", "mcmodsync-b.py")

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_VERIFY = 2
EXIT_NETWORK = 5
EXIT_LOCK = 10
EXIT_STAGING = 11

# [2.7] 退出码 -> 中文处理建议
EXIT_ADVICE = {
    1: "通用失败（含交集检查阻断）：按提示修正后重试",
    2: "验签失败/公钥不匹配：检查 client.publicKey 与签名私钥是否配对",
    3: "游戏运行中（仅 C 端）",
    4: "服务端磁盘空间不足：清理或降低 historyKeep",
    5: "网络不可达/重试耗尽：检查网络或对象存储连通性",
    6: "清单 schemaVersion 过新：升级更新器",
    7: "packId 不匹配：核对 pack.local.json 与客户端 config.json",
    10: "锁冲突：另一实例正在执行 B，稍后重试",
    11: "staging 缺件或哈希不符：已自动重传 desired 与全部 blob 后重试",
    13: "路径非法/穿越/staging 越界：检查 mods 内文件名",
    14: "回滚目标不是最新 history 目录",
    15: "该版本已回滚过（changes.json.rolled-back 已存在）",
    18: "staging 与 mods 不在同一分区",
    19: "服务端其他错误：查看 B 日志",
    99: "故障注入（仅测试用）",
}


class PushError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# 纯逻辑（可离线单测）
# ---------------------------------------------------------------------------

def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def b_local_path() -> str:
    """A 包内内置的构建产物路径。"""
    return os.path.join(repo_root(), B_REL_PATH)


def b_local_sha256() -> str:
    with open(b_local_path(), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def make_version(now: Optional[datetime] = None) -> str:
    """srv-YYYYMMDD-HHMMSS-mmm（毫秒定长，字典序=时间序）。"""
    now = now or datetime.now()
    return "srv-%s-%03d" % (now.strftime("%Y%m%d-%H%M%S"), now.microsecond // 1000)


def scan_source_mods(source_dir: str, mods_dir: str = "mods") -> List[dict]:
    """扫描 server.sourceModsDir（平铺 *.jar）-> [FileEntry]。"""
    return hashing.scan_tree(source_dir, subdir=".", exts=(".jar",), recursive=False)


def _entry_map(entries: List[dict]) -> Dict[str, dict]:
    return {e["path"]: e for e in entries}


def to_desired_entries(entries: List[dict], mods_dir: str = "mods") -> List[dict]:
    """把源目录扫描结果规范为 desired.files（path 以 "<modsDir>/" 前缀）。"""
    out = []
    for e in entries:
        name = e["path"].split("/")[-1]
        out.append({"path": "%s/%s" % (mods_dir, name), "sha256": e["sha256"], "size": e["size"]})
    out.sort(key=lambda x: x["path"])
    return out


def build_desired(pack_id: str, version: str, files: List[dict]) -> dict:
    return {"schemaVersion": SUPPORTED_SCHEMA_VERSION, "packId": pack_id,
            "version": version, "files": files}


def intersection_check(server_files: List[dict], client_source_dir: str,
                       mods_dir: str = "mods") -> List[str]:
    """服务端有、客户端源目录无的文件名（按文件名比较）。"""
    client_names = set()
    if client_source_dir and os.path.isdir(client_source_dir):
        for name in os.listdir(client_source_dir):
            if name.lower().endswith(".jar") and os.path.isfile(os.path.join(client_source_dir, name)):
                client_names.add(name)
    missing = []
    for e in server_files:
        name = e["path"].split("/")[-1]
        if name not in client_names:
            missing.append(name)
    return sorted(missing)


def report_missing_mods(missing: List[str], log, blocking: bool = False) -> None:
    """列出「服务端有、客户端源目录没有」的 mod，一行一个便于逐条核对。

    整条消息一次输出，所以时间戳只出现一次，列表项保持对齐缩进。
    """
    if not missing:
        return
    detail = "\n".join("    - %s" % name for name in missing)
    msg = ("服务端存在而客户端源目录缺少的 mod（%d 个）:\n%s"
           % (len(missing), detail))
    if blocking:
        raise PushError(EXIT_GENERIC, "交集检查阻断: " + msg)
    log("警告: " + msg)


def changes_summary(diff: Dict[str, List[dict]]) -> str:
    return "新增 %d、替换 %d、删除 %d、未变 %d" % (
        len(diff["added"]), len(diff["replaced"]), len(diff["deleted"]), len(diff["unchanged"]))


def staging_paths(server_dir: str, version: str, mods_dir: str = "mods") -> Dict[str, str]:
    staging = posixpath.join(server_dir, ".mcmodsync-staging")
    return {
        "staging": staging,
        "desired": posixpath.join(staging, "desired-%s.json" % version),
        "blobs": posixpath.join(staging, "blobs"),
    }


def blob_rel(sha: str) -> str:
    return "blobs/%s/%s/%s" % (sha[0:2], sha[2:4], sha)


def write_push_log(log_dir: str, record: dict) -> str:
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "push-log.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


# ---------------------------------------------------------------------------
# 远端操作
# ---------------------------------------------------------------------------

def _q(s: str) -> str:
    return shlex.quote(str(s))


def _remote_sha256(conn, path: str) -> Optional[str]:
    py = ("import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())")
    rc, out, _err = conn.run("python3 -c %s %s" % (_q(py), _q(path)))
    if rc != 0:
        return None
    return out.strip() or None


def ensure_remote_dirs(conn, server_dir: str, history_dir: str, log) -> None:
    conn.mkdirs(posixpath.join(server_dir, ".mcmodsync"))
    conn.mkdirs(history_dir)
    conn.mkdirs(posixpath.join(server_dir, ".mcmodsync", "logs"))
    log("远端目录就绪: .mcmodsync/ 与 history 根目录")


def bootstrap_b(conn, server_dir: str, remote_b_path: str, log,
                b_local: Optional[str] = None) -> bool:
    """确保远端 B 可用；返回是否发生了上传。"""
    local = b_local or b_local_path()
    with open(local, "rb") as f:
        local_sha = hashlib.sha256(f.read()).hexdigest()

    remote_sha = _remote_sha256(conn, remote_b_path)
    uploaded = False
    if remote_sha is None:
        log("B 引导: 远端不存在 %s，上传" % remote_b_path)
        conn.put(local, remote_b_path)
        uploaded = True
    elif remote_sha != local_sha:
        bak = remote_b_path + ".bak"
        log("B 引导: 远端版本不同，先备份为 %s" % bak)
        rc, _out, _err = conn.run("cp -f %s %s" % (_q(remote_b_path), _q(bak)))
        if rc != 0:
            raise PushError(EXIT_GENERIC, "B 引导失败：无法备份旧版 B")
        conn.put(local, remote_b_path)
        uploaded = True
    else:
        log("B 引导: 远端版本一致，跳过上传")

    rc, out, err = conn.run("python3 %s --protocol-version" % _q(remote_b_path))
    got = out.strip()
    if rc != 0 or got != PROTOCOL_VERSION:
        hint = ("；可用 SFTP 将 %s.bak 还原" % remote_b_path) if uploaded else ""
        raise PushError(EXIT_GENERIC,
                        "B 协议版本不匹配: 期望 %s，实际 %r%s" % (PROTOCOL_VERSION, got or err, hint))
    log("B 引导: 协议版本校验通过 (%s)" % got)
    return uploaded


def b_manifest(conn, server_dir: str, remote_b_path: str, mods_dir: str = "mods") -> dict:
    cmd = "cd %s && python3 %s manifest --server-dir %s --mods-dir %s" % (
        _q(server_dir), _q(remote_b_path), _q(server_dir), _q(mods_dir))
    rc, out, err = conn.run(cmd)
    if rc != 0:
        raise PushError(rc or EXIT_GENERIC, "B manifest 失败: %s" % (err.strip() or out.strip()))
    try:
        return json.loads(out)
    except ValueError as e:
        raise PushError(EXIT_GENERIC, "B manifest 输出非 JSON: %s" % e)


def remote_size(conn, path: str) -> Optional[int]:
    try:
        st = conn.stat(path)
    except Exception:
        return None
    return None if st is None else int(getattr(st, "st_size", -1))


def upload_desired(conn, paths: Dict[str, str], doc: dict, tmp_dir: str, log) -> None:
    os.makedirs(tmp_dir, exist_ok=True)
    local = os.path.join(tmp_dir, "desired.json")
    manifest.write_json(local, doc)
    conn.mkdirs(paths["staging"])
    conn.put(local, paths["desired"])
    log("已上传 desired: %s" % paths["desired"])


def upload_blobs(conn, source_dir: str, entries: List[dict], paths: Dict[str, str],
                 log, skip_existing: bool = True, concurrency_unused: int = 4) -> Tuple[int, int]:
    """上传源目录中的 jar 到 staging/blobs；返回 (上传数, 跳过数)。"""
    up = skip = 0
    for e in entries:
        name = e["path"].split("/")[-1]
        local = os.path.join(source_dir, name)
        rel = blob_rel(e["sha256"])
        remote = posixpath.join(paths["staging"], rel)
        if skip_existing:
            size = remote_size(conn, remote)
            if size is not None and size == e["size"]:
                skip += 1
                continue
        conn.mkdirs(posixpath.dirname(remote))
        conn.put(local, remote)
        up += 1
    log("blob 上传完成: 新上传 %d，跳过（远端已存在且 size 一致）%d" % (up, skip))
    return up, skip


def b_apply(conn, server_dir: str, remote_b_path: str, paths: Dict[str, str],
            version: str, pack_id: str, history_dir: str, history_keep: int) -> Tuple[int, str, str]:
    cmd = ("cd %s && python3 %s apply --server-dir %s --staging %s --desired %s "
           "--version %s --pack-id %s --history-dir %s --history-keep %d" % (
               _q(server_dir), _q(remote_b_path), _q(server_dir), _q(paths["staging"]),
               _q(paths["desired"]), _q(version), _q(pack_id), _q(history_dir), int(history_keep)))
    return conn.run(cmd)


def b_rollback(conn, server_dir: str, remote_b_path: str, pack_id: str,
               history_dir: str, version: str = "") -> Tuple[int, str, str]:
    cmd = ("cd %s && python3 %s rollback --server-dir %s --history-dir %s --pack-id %s %s" % (
        _q(server_dir), _q(remote_b_path), _q(server_dir), _q(history_dir), _q(pack_id),
        ("--version " + _q(version)) if version else ""))
    return conn.run(cmd)


def apply_with_retry(run_apply: Callable[[], Tuple[int, str, str]],
                     reupload: Callable[[], None],
                     log) -> Tuple[int, str, str]:
    """码 11 -> 无条件重传 desired + 全部 blob 后自动重试一次。"""
    rc, out, err = run_apply()
    if rc != EXIT_STAGING:
        return rc, out, err
    log("B apply 返回 11（staging 缺件/哈希不符），重传 desired 与全部 blob 后重试一次")
    reupload()
    return run_apply()


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def _server_conf(cfg) -> dict:
    s = cfg["server"]
    return {
        "server_dir": s["serverDir"],
        "mods_dir": s.get("modsDir", "mods"),
        "remote_b": s["remoteBPath"],
        "history_dir": s["historyDir"],
        "history_keep": int(s.get("historyKeep", 3)),
        "source_mods": s["sourceModsDir"],
    }


def connect_ssh(cfg, accept_new_host: bool = False, timeout: int = 20):
    from .ssh import SSHClient

    s = cfg["server"]
    cli = SSHClient()
    cli.connect(s["host"], int(s["port"]), s["user"],
                key_file=s.get("keyFile", ""), password=s.get("password", ""),
                known_hosts=s.get("knownHosts", ""), accept_new_host=accept_new_host,
                timeout=timeout)
    return cli


def rollback_server(cfg, conn, log=print) -> int:
    c = _server_conf(cfg)
    connect = conn is None
    if connect:
        conn = connect_ssh(cfg, accept_new_host=True)
    try:
        ensure_remote_dirs(conn, c["server_dir"], c["history_dir"], log)
        bootstrap_b(conn, c["server_dir"], c["remote_b"], log)
        rc, out, err = b_rollback(conn, c["server_dir"], c["remote_b"],
                                  cfg["packId"], c["history_dir"])
        if rc != 0:
            raise PushError(rc, "rollback 失败: %s（%s）" % (err.strip() or out.strip(),
                                                           EXIT_ADVICE.get(rc, "未知错误")))
        log("rollback 结果: %s" % out.strip())
        log("请手动重启 MC 服务器，确认日志出现 \"Done (\"")
        return EXIT_OK
    finally:
        if connect and conn is not None:
            conn.close()


def lock_mtime_hint(cfg, lock_path: str = "mods.lock.json") -> Optional[str]:
    """[T-35] 联动提示（warning，不阻断）。"""
    try:
        from . import fetcher
        return fetcher.lock_stale_hint(cfg, lock_path)
    except Exception:
        return None


def push_server(cfg, conn, dry_run: bool = False, check_server_client: bool = False,
                accept_new_host: bool = True, log=print,
                tmp_dir: Optional[str] = None, log_dir: str = ".mcmodsync/logs",
                lock_path: str = "mods.lock.json") -> int:
    c = _server_conf(cfg)
    connect = conn is None
    if connect:
        conn = connect_ssh(cfg, accept_new_host=accept_new_host)
    tmp_dir = tmp_dir or os.path.join(os.getcwd(), ".mcmodsync", "tmp")
    started = time.time()
    try:
        # 步骤1 扫描源目录
        entries = scan_source_mods(c["source_mods"], c["mods_dir"])
        if not entries:
            raise PushError(EXIT_GENERIC, "源目录无 *.jar: %s" % c["source_mods"])
        desired_files = to_desired_entries(entries, c["mods_dir"])
        log("源目录扫描: %d 个 jar" % len(desired_files))
        hint = lock_mtime_hint(cfg, lock_path)
        if hint:
            log("警告: " + hint)

        desired_map = {e["path"]: e for e in desired_files}

        # 步骤11 --dry-run: 零写操作（不 bootstrap、不建目录、不上传、不 apply）
        if dry_run:
            server_files: List[dict] = []
            if _remote_sha256(conn, c["remote_b"]) is not None:
                rc, out, _err = conn.run(
                    "cd %s && python3 %s manifest --server-dir %s --mods-dir %s" % (
                        _q(c["server_dir"]), _q(c["remote_b"]), _q(c["server_dir"]), _q(c["mods_dir"])))
                if rc == 0:
                    try:
                        server_files = json.loads(out).get("files", [])
                    except ValueError:
                        server_files = []
            else:
                log("[dry-run] 远端未安装 B，跳过服务端清单读取（零写操作）")

            diff = planner.plan({e["path"]: e for e in server_files}, desired_map)
            log("[dry-run] 将新增 %d、替换 %d、删除 %d；将上传 blob %d 个（约 %d 字节）"
                % (len(diff["added"]), len(diff["replaced"]), len(diff["deleted"]),
                   len(diff["added"]) + len(diff["replaced"]),
                   sum(e["size"] for e in diff["added"] + diff["replaced"])))
            for e in diff["added"]:
                log("[dry-run] 新增: %s" % e["path"])
            for e in diff["replaced"]:
                log("[dry-run] 替换: %s" % e["path"])
            for e in diff["deleted"]:
                log("[dry-run] 删除: %s" % e["path"])
            missing = intersection_check(server_files, cfg["client"]["sourceModsDir"],
                                         c["mods_dir"]) if server_files else []
            report_missing_mods(missing, log, blocking=check_server_client)
            log("[dry-run] 未执行任何写操作")
            return EXIT_OK

        # 步骤2 bootstrap B + 远端目录
        ensure_remote_dirs(conn, c["server_dir"], c["history_dir"], log)
        bootstrap_b(conn, c["server_dir"], c["remote_b"], log)

        # 步骤3 服务器实时清单
        mf = b_manifest(conn, c["server_dir"], c["remote_b"], c["mods_dir"])
        server_files = mf.get("files", [])
        log("服务端清单: %d 个 jar（stateVersion=%s）" % (len(server_files), mf.get("stateVersion")))

        # 步骤4 变更集
        diff = planner.plan({e["path"]: e for e in server_files}, desired_map)
        log("变更预览: %s" % changes_summary(diff))

        # 步骤5 双端交集检查
        missing = intersection_check(server_files, cfg["client"]["sourceModsDir"], c["mods_dir"])
        report_missing_mods(missing, log, blocking=check_server_client)

        # 步骤6 版本号
        version = make_version()
        # 步骤7 desired
        paths = staging_paths(c["server_dir"], version, c["mods_dir"])
        doc = build_desired(cfg["packId"], version, desired_files)
        upload_desired(conn, paths, doc, tmp_dir, log)

        # 步骤8 blob —— 只上传变更集真正需要的文件（added + replaced）
        #
        # B 端 _verify_staging 只校验 added/replaced 的 staging blob；未变文件沿用
        # 远端 mods/ 里的现有文件（blob 缺失但目标 sha256 一致时判定「已应用」），
        # 不需要重新上传。原先这里传的是全部 desired_files，会对每个 jar 做一次
        # SFTP stat，blobs 池为空时（例如首次推送、或上次 apply 用 os.replace 把
        # blob 移走后）就会把全部 jar 重传一遍 —— 即使一个变更都没有。
        need_paths = {e["path"] for e in (diff["added"] + diff["replaced"])}
        needed = [e for e in desired_files if e["path"] in need_paths]
        if needed:
            upload_blobs(conn, c["source_mods"], needed, paths, log)
        else:
            log("blob 上传: 无新增/替换，跳过（未变 %d 个沿用远端现有文件）"
                % len(diff["unchanged"]))

        # 步骤9 apply（码 11 自动重传并重试一次）
        def _reupload():
            upload_desired(conn, paths, doc, tmp_dir, log)
            upload_blobs(conn, c["source_mods"], needed, paths, log, skip_existing=False)

        rc, out, err = apply_with_retry(
            lambda: b_apply(conn, c["server_dir"], c["remote_b"], paths, version,
                            cfg["packId"], c["history_dir"], c["history_keep"]),
            _reupload, log)

        record = {"at": datetime.now().isoformat(timespec="seconds"), "version": version,
                  "result": "ok" if rc == 0 else "failed", "exitCode": rc,
                  "added": len(diff["added"]), "replaced": len(diff["replaced"]),
                  "deleted": len(diff["deleted"]), "seconds": round(time.time() - started, 2)}
        try:
            write_push_log(log_dir, record)
        except OSError:
            pass

        if rc != 0:
            blog = posixpath.join(c["server_dir"], ".mcmodsync", "logs", "apply-%s.log" % version)
            raise PushError(rc, "apply 失败（码 %d: %s）: %s；B 日志: %s"
                            % (rc, EXIT_ADVICE.get(rc, "未知错误"), err.strip() or out.strip(), blog))

        log("apply 成功: %s" % out.strip())
        log("下一步: 手动重启 MC 服务器，确认日志出现 \"Done (\" 后再执行 publish-client")
        return EXIT_OK
    finally:
        if connect and conn is not None:
            conn.close()
