"""publish-client 编排（A 端，[T-22]）。

发布纪律: 只有服务端确认兼容（OP 手动重启看到 "Done ("）后才允许调用本模块。
顺序硬约束: 先上传 manifests/<ver>.json，最后上传指针 manifest.json（no-cache）。
"""
from __future__ import annotations

import json
import os
import posixpath
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from . import canonicaljson, hashing, signing

POINTER_CC = "no-cache, no-store, must-revalidate"
MANIFEST_CC = "public, max-age=300"
BLOB_CC = "public, max-age=31536000, immutable"

SUPPORTED_SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_VERIFY = 2
EXIT_NETWORK = 5


class PublishError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# 纯逻辑
# ---------------------------------------------------------------------------

def scan_client_mods(source_dir: str, mods_dir: str = "mods") -> List[dict]:
    """扫描 client.sourceModsDir（平铺 *.jar）-> [{path, sha256, size}]，path 带 mods/ 前缀。"""
    entries = hashing.scan_tree(source_dir, subdir=".", exts=(".jar",), recursive=False)
    out = []
    for e in entries:
        out.append({"path": "%s/%s" % (mods_dir, e["path"].split("/")[-1]),
                    "sha256": e["sha256"], "size": e["size"]})
    out.sort(key=lambda x: x["path"])
    return out


def compute_delete(old_manifest: Optional[dict], new_files: List[dict], version: str) -> List[dict]:
    """累计 delete = 旧 delete ∪ 本次消失路径，再剔除重新出现在新 files 中的路径。"""
    new_paths = {f["path"] for f in new_files}
    acc: Dict[str, str] = {}
    if old_manifest:
        for d in old_manifest.get("delete", []) or []:
            acc[d["path"]] = d.get("deletedInVersion") or version
        old_paths = {f["path"] for f in old_manifest.get("files", []) or []}
        for p in old_paths - new_paths:
            acc[p] = version
    for p in list(acc):
        if p in new_paths:            # 重新加回 -> 从 delete 移除
            acc.pop(p)
    return [{"path": p, "deletedInVersion": acc[p]} for p in sorted(acc)]


def build_version_manifest(pack_id: str, version: str, notes: str, files: List[dict],
                           delete: List[dict], created_at: Optional[str] = None) -> dict:
    return {
        "schemaVersion": SUPPORTED_SCHEMA_VERSION,
        "packId": pack_id,
        "version": version,
        "createdAt": created_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "releaseNotes": notes or "",
        "files": files,
        "delete": delete,
    }


def build_pointer(pack_id: str, version: str) -> dict:
    return {
        "schemaVersion": SUPPORTED_SCHEMA_VERSION,
        "packId": pack_id,
        "latest": version,
        "manifestUrl": "manifests/%s.json" % version,
    }


def verify_manifest(obj: dict, public_key_b64: str) -> bool:
    return canonicaljson.verify(obj, public_key_b64)


def _load_private_key_pem(cfg) -> bytes:
    path = cfg.get("signing", {}).get("privateKeyFile") or "~/.mcmodsync/private.key"
    return signing.load_private_key(path)


def _http_get_json(url: str, timeout: int = 20) -> dict:
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise PublishError(EXIT_GENERIC, "manifestUrl 非法或未配置: %r" % (url,))
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request("%s%st=%d" % (url, sep, int(time.time())),
                                 headers={"User-Agent": "MC-ModSync-A/2.0",
                                          "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise PublishError(EXIT_NETWORK, "拉取失败 HTTP %s: %s" % (e.code, url))
    except Exception as e:
        raise PublishError(EXIT_NETWORK, "拉取失败 %s: %s" % (url, e))


def fetch_published_manifest(manifest_url: str, public_key_b64: str = "",
                             log: Callable[[str], None] = print) -> Tuple[dict, dict]:
    """从云端拉指针与版本清单（用于本地 state 丢失时恢复）；有公钥时验签。"""
    pointer = _http_get_json(manifest_url)
    rel = pointer.get("manifestUrl") or ""
    if not rel:
        raise PublishError(EXIT_GENERIC, "指针缺少 manifestUrl: %s" % manifest_url)
    base = manifest_url.rsplit("/", 1)[0]
    version_url = rel if rel.startswith("http") else base + "/" + rel.lstrip("/")
    ver = _http_get_json(version_url)
    if public_key_b64:
        if not verify_manifest(pointer, public_key_b64):
            raise PublishError(EXIT_VERIFY, "指针验签失败: %s" % manifest_url)
        if not verify_manifest(ver, public_key_b64):
            raise PublishError(EXIT_VERIFY, "版本清单验签失败: %s" % version_url)
        log("指针与版本清单验签通过")
    return pointer, ver


def load_publish_state(path: str) -> Optional[dict]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except (ValueError, OSError):
        return None


def save_publish_state(path: str, manifest_obj: dict) -> None:
    from . import manifest as _m
    _m.write_json(path, manifest_obj)


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def publish_client(cfg, store, version: str, conn=None, notes: str = "",
                   dry_run: bool = False, log: Callable[[str], None] = print,
                   server_files: Optional[List[dict]] = None,
                   check_server_client: bool = False,
                   local_manifest: Optional[dict] = None) -> int:
    if not version:
        raise PublishError(EXIT_GENERIC, "publish-client 必须提供 --version（缺失立即报错）")

    pack_id = cfg["packId"]
    client_cfg = cfg["client"]
    mods_dir = cfg["server"].get("modsDir", "mods")
    source_dir = client_cfg["sourceModsDir"]
    state_file = client_cfg.get("clientStateFile") or "./client-publish-state.json"
    concurrency = int(cfg.get("concurrency", {}).get("upload", 4))

    # 步骤1 扫描客户端源目录
    files = scan_client_mods(source_dir, mods_dir)
    if not files:
        raise PublishError(EXIT_GENERIC, "客户端源目录无 *.jar: %s" % source_dir)
    log("客户端源目录扫描: %d 个 jar" % len(files))

    # 步骤2 基准清单（本地 state，或从云端恢复）
    old = local_manifest if local_manifest is not None else load_publish_state(state_file)
    if old is None and client_cfg.get("manifestUrl"):
        log("本地发布状态缺失，尝试从云端恢复（%s）" % client_cfg.get("manifestUrl", ""))
        try:
            _pointer, old = fetch_published_manifest(client_cfg.get("manifestUrl", ""),
                                                     client_cfg.get("publicKey", ""), log)
            log("已从云端恢复基准清单: version=%s files=%d delete=%d"
                % (old.get("version"), len(old.get("files", [])), len(old.get("delete", []))))
        except PublishError as e:
            log("云端恢复失败（视为首次发布）: %s" % e)
            old = None

    # 步骤3 变更与累计 delete
    old_files = {f["path"]: f for f in (old or {}).get("files", [])}
    new_files = {f["path"]: f for f in files}
    added = sorted(p for p in new_files if p not in old_files)
    replaced = sorted(p for p in new_files if p in old_files
                      and old_files[p]["sha256"] != new_files[p]["sha256"])
    delete = compute_delete(old, files, version)
    log("本版变更: 新增 %d、替换 %d、累计 delete %d" % (len(added), len(replaced), len(delete)))
    for p in added:
        log("  新增: %s" % p)
    for p in replaced:
        log("  替换: %s" % p)
    for d in delete:
        log("  删除: %s (自 %s)" % (d["path"], d["deletedInVersion"]))

    # 步骤4 双端交集检查（服务端有、客户端无）
    if server_files is None and conn is not None:
        try:
            from . import pusher as _p
            c = _p._server_conf(cfg)
            mf = _p.b_manifest(conn, c["server_dir"], c["remote_b"], c["mods_dir"])
            server_files = mf.get("files", [])
        except Exception as e:                      # 非致命：仅提示
            log("警告: 无法获取服务端清单，跳过双端交集检查: %s" % e)
    if server_files:
        from . import pusher as _p
        missing = _p.intersection_check(server_files, source_dir, mods_dir)
        if missing:
            msg = "服务端存在而客户端源目录缺少的 mod（%d 个）: %s" % (len(missing), ", ".join(missing))
            if check_server_client:
                raise PublishError(EXIT_GENERIC, "交集检查阻断: " + msg)
            log("警告: " + msg)

    # 步骤5 blob 上传（HEAD 存在则跳过）
    needed = [new_files[p] for p in added + replaced]
    to_upload = []
    for f in needed:
        key = "blobs/%s/%s/%s" % (f["sha256"][0:2], f["sha256"][2:4], f["sha256"])
        if store.head(key):
            continue
        to_upload.append((key, f))

    if dry_run:
        total = sum(f["size"] for _k, f in to_upload)
        log("[dry-run] 将上传版本清单 manifests/%s.json、指针 manifest.json" % version)
        log("[dry-run] 将上传 blob %d 个（%d 字节），已存在跳过 %d 个"
            % (len(to_upload), total, len(needed) - len(to_upload)))
        log("[dry-run] 未执行任何写操作")
        return EXIT_OK

    def _up(item):
        key, f = item
        local = os.path.join(source_dir, f["path"].split("/")[-1])
        store.put_file(key, local, BLOB_CC)
        return key

    if to_upload:
        delay = 1.0
        pending = list(to_upload)
        for attempt in range(1, 4):
            try:
                with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
                    list(pool.map(_up, pending))
                break
            except Exception as e:
                if attempt == 3:
                    raise PublishError(EXIT_NETWORK, "blob 上传失败（已重试 3 次）: %s" % e)
                log("blob 上传失败（第 %d 次），%.1fs 后重试: %s" % (attempt, delay, e))
                time.sleep(delay)
                delay *= 2
    log("blob 就绪: 新上传 %d，已存在跳过 %d" % (len(to_upload), len(needed) - len(to_upload)))

    # 步骤6 版本清单 + 签名
    pem = _load_private_key_pem(cfg)
    manifest_obj = build_version_manifest(pack_id, version, notes, files, delete)
    signed = canonicaljson.sign(manifest_obj, pem)

    # 步骤7 先清单、后指针（顺序硬约束）
    store.put_bytes("manifests/%s.json" % version,
                    json.dumps(signed, ensure_ascii=False).encode("utf-8"), MANIFEST_CC)
    log("已上传版本清单: manifests/%s.json（%s）" % (version, MANIFEST_CC))

    pointer = canonicaljson.sign(build_pointer(pack_id, version), pem)
    store.put_bytes("manifest.json",
                    json.dumps(pointer, ensure_ascii=False).encode("utf-8"), POINTER_CC)
    log("已上传指针: manifest.json（%s）【最后写入】" % POINTER_CC)

    # 步骤8 写回本地发布状态
    if state_file:
        save_publish_state(state_file, signed)
        log("已写回本地发布状态: %s" % state_file)

    log("publish-client 完成: version=%s files=%d delete=%d" % (version, len(files), len(delete)))
    return EXIT_OK
