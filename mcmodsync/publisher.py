"""publish-client 编排（A 端，[T-22]）。

发布纪律: 只有服务端确认兼容（OP 手动重启看到 "Done ("）后才允许调用本模块。
顺序硬约束: 先上传 manifests/<ver>.json，最后上传指针 manifest.json（no-cache）。
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from . import canonicaljson, hashing, signing, source_resolve

POINTER_CC = "no-cache, no-store, must-revalidate"
MANIFEST_CC = "public, max-age=300"
BLOB_CC = "public, max-age=31536000, immutable"

# 下载源索引（C 端多源下载用）：与指针同级、覆盖式更新，所以只能用短缓存。
SOURCES_KEY = "sources.json"
SOURCES_CC = "public, max-age=300"
SOURCES_SCHEMA_VERSION = 1

SUPPORTED_SCHEMA_VERSION = 1

# 版本号规则: A.B.C —— A 大版本(1-9)、B 版本类(0-9)、C 小版本(0-6)。
# 自动递增: C 满 6 进位到 B、B 满 9 进位到 A（如 1.1.6 -> 1.2.0）。
_VERSION_RE = re.compile(r"^([1-9])\.([0-9])\.([0-6])$")

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_VERIFY = 2
EXIT_NETWORK = 5


class PublishError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def validate_version(version: str) -> str:
    """校验手动指定的版本号格式（A=1-9、B=0-9、C=0-6），非法立即报错。"""
    v = (version or "").strip()
    if not _VERSION_RE.match(v):
        raise PublishError(EXIT_GENERIC,
                           "版本号须为 A.B.C（A=1-9、B=0-9、C=0-6），收到: %r" % (version,))
    return v


def next_version(prev: str) -> str:
    """在上一版本基础上自动递增（C 满 6 进 B，B 满 9 进 A）。

    prev 为空视为首次发布，返回 1.0.0；prev 非法时要求手动指定 --version。
    """
    p = (prev or "").strip()
    m = _VERSION_RE.match(p)
    if not m:
        if p:
            raise PublishError(EXIT_GENERIC,
                               "上一版本号 %r 不符合 A.B.C 格式，请用 --version 手动指定新版本" % p)
        return "1.0.0"                              # 首次发布
    a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
    c += 1
    if c > 6:                                       # C 满 6 -> 进位到 B
        c, b = 0, b + 1
    if b > 9:                                       # B 满 9 -> 进位到 A
        b, a = 0, a + 1
    if a > 9:
        raise PublishError(EXIT_GENERIC,
                           "版本号已达上限 9.9.6，无更高版本可用（请更换 pack 或重置版本序列）")
    return "%d.%d.%d" % (a, b, c)


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


def gc_unreferenced(store, files: List[dict], version: str, log=print) -> dict:
    """清理对象存储：只删除未被当前清单引用的 blob；版本清单全部保留。

    - blobs/<sha> 为内容寻址；未被当前清单引用的（历史版本被替换掉的 jar）删除；
    - manifests/*.json 一律保留（每份仅几十 KB，保留后可追溯任意历史版本的
      releaseNotes 与文件列表，即"更新日志"）；
    - 返回 {"blobs": 删除数, "manifests": 0, "freed": 释放字节}。

    注意：本函数不做时间保护窗口——由调用方决定是否启用（默认每次发布后执行）。
    """
    referenced = {"blobs/%s/%s/%s" % (f["sha256"][0:2], f["sha256"][2:4], f["sha256"])
                  for f in files}
    removed_blobs = freed = 0
    for o in store.list_objects("blobs"):
        if o["key"] not in referenced:
            store.delete(o["key"])
            removed_blobs += 1
            freed += o["size"]

    if removed_blobs:
        log("已清理未引用对象: blob %d 个（版本清单全部保留）" % removed_blobs)
    else:
        log("无需清理：存储中已无未引用对象")
    return {"blobs": removed_blobs, "manifests": 0, "freed": freed}


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


# ---------------------------------------------------------------------------
# 下载源索引 sources.json（C 端多源下载的"从哪下"）
# ---------------------------------------------------------------------------

def build_sources_index(pack_id: str, version: str, files: List[dict],
                        prev: Optional[dict], resolved: Dict[str, dict],
                        created_at: Optional[str] = None) -> dict:
    """按当前清单裁剪并合并来源 -> sources.json 对象（未签名）。

    - 只保留 sha256 出现在本次 files 里的条目：删除的 mod、被替换掉的旧版本
      自动从索引中消失，不需要额外的删除逻辑；
    - 本次新解析的优先，未变更条目按 sha256 从上一版索引继承；
    - 继承来的条目也要过一遍白名单（旧索引可能来自更宽松的实现）。
    """
    before = ((prev or {}).get("sources") or {})
    if not isinstance(before, dict):
        before = {}
    out: Dict[str, dict] = {}
    for f in files:
        sha = str(f.get("sha256") or "").lower()
        if not sha:
            continue
        item = resolved.get(sha) or before.get(sha)
        if not isinstance(item, dict):
            continue
        src = str(item.get("source") or "").strip().lower()
        url = str(item.get("downloadUrl") or "").strip()
        if src not in source_resolve.ALLOWED_SOURCES:
            continue
        if not source_resolve.is_allowed_url(url):
            continue
        entry = {
            "source": src,
            "fileName": str(item.get("fileName")
                            or os.path.basename(str(f.get("path") or ""))),
            "size": int(item.get("size") or f.get("size") or 0),
            "downloadUrl": url,
        }
        slug = str(item.get("projectSlug") or "")
        ver = str(item.get("resolvedVersion") or "")
        if slug:
            entry["projectSlug"] = slug
        if ver:
            entry["resolvedVersion"] = ver
        out[sha] = entry
    return {
        "schemaVersion": SOURCES_SCHEMA_VERSION,
        "packId": pack_id,
        "generatedForVersion": version or "",
        "updatedAt": created_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "sources": {k: out[k] for k in sorted(out)},
    }


def fetch_sources_index(manifest_url: str, log: Callable[[str], None] = print) -> Optional[dict]:
    """从公网读上一版 sources.json 作合并基准；不存在/不可达一律 None（视为首次）。"""
    if not manifest_url:
        return None
    url = manifest_url.rstrip("/").rsplit("/", 1)[0] + "/" + SOURCES_KEY
    try:
        obj = _http_get_json(url, timeout=15)
    except PublishError as e:
        log("上一版来源索引不可用（视为首次生成）: %s" % e)
        return None
    return obj if isinstance(obj, dict) else None


def read_store_sources(store, log: Callable[[str], None] = print) -> Optional[dict]:
    """从对象存储读上一版 sources.json 作合并基准（A 端持 AK/SK，比走 CDN 更可靠）。"""
    try:
        if not store.head(SOURCES_KEY):
            return None
        obj = json.loads(store.get_text(SOURCES_KEY))
    except Exception as e:  # noqa: BLE001  基准缺失不该阻断发布
        log("读取线上来源索引失败（视为首次生成）: %s" % e)
        return None
    return obj if isinstance(obj, dict) else None


def _published_version(manifest_url: str, log: Callable[[str], None] = print) -> str:
    """只读探测线上当前版本号；失败返回空串（仅用于索引里的可读标注）。"""
    if not manifest_url:
        return ""
    try:
        pointer = _http_get_json(manifest_url, timeout=15)
    except PublishError as e:
        log("读取线上版本号失败（不影响本次操作）: %s" % e)
        return ""
    return str(pointer.get("latest") or "")


def resolve_index_for_publish(cfg, store, version: str, files: List[dict],
                              log: Callable[[str], None] = print,
                              backfill: bool = False, no_cf: bool = False,
                              lock_path: str = "mods.lock.json") -> Tuple[Optional[dict], dict]:
    """生成 sources.json 对象（未签名）；失败返回 (None, {}) 由调用方降级。

    只对「索引里还没有的文件」联网反查（新增/替换天然落在其中），
    backfill=True 时对全部文件重查（保底修复用）。
    """
    client_cfg = cfg["client"]
    source_dir = client_cfg["sourceModsDir"]
    prev = read_store_sources(store, log)
    known = set(((prev or {}).get("sources") or {}).keys())
    todo = [f for f in files
            if backfill or str(f.get("sha256") or "").lower() not in known]
    lock_index = source_resolve.load_lock_index(lock_path)
    resolved: Dict[str, dict] = {}
    stats = {"lock": 0, "modrinth": 0, "curseforge": 0, "miss": 0,
             "rejected": 0, "skipped": 0, "total": len(files)}
    if todo:
        resolved, stats = source_resolve.resolve_sources(
            source_dir, todo, lock_index, cfg, log, no_cf=no_cf)
    index_obj = build_sources_index(cfg["packId"], version, files, prev, resolved)
    log("来源索引: 共 %d 条，覆盖 %d/%d 个文件（本次反查 %d 个：lock %d / Modrinth %d / "
        "CurseForge %d / 未命中 %d / 白名单剔除 %d；继承 %d）"
        % (len(index_obj["sources"]), len(index_obj["sources"]), len(files), len(todo),
           stats["lock"], stats["modrinth"], stats["curseforge"], stats["miss"],
           stats["rejected"], len(files) - len(todo)))
    return index_obj, stats


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

def publish_client(cfg, store, version: str = "", conn=None, notes: str = "",
                   dry_run: bool = False, log: Callable[[str], None] = print,
                   server_files: Optional[List[dict]] = None,
                   check_server_client: bool = False,
                   local_manifest: Optional[dict] = None,
                   lock_path: str = "mods.lock.json", gc: bool = True,
                   resolve: bool = True, backfill: bool = False,
                   no_cf: bool = False) -> int:
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
    try:
        from . import fetcher as _fetcher
        _hint = _fetcher.lock_stale_hint(cfg, lock_path)
        if _hint:
            log("警告: " + _hint)
    except Exception:
        pass

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

    # 步骤2.5 版本号：缺省时在上一版本基础上自动递增；手动指定则校验格式
    version = (version or "").strip()
    if version:
        version = validate_version(version)
    else:
        prev = (old or {}).get("version", "")
        version = next_version(prev)
        log("版本号自动递增: %s -> %s" % (prev or "(首次发布)", version))

    # 步骤3 变更与累计 delete
    old_files = {f["path"]: f for f in (old or {}).get("files", [])}
    new_files = {f["path"]: f for f in files}
    added = sorted(p for p in new_files if p not in old_files)
    replaced = sorted(p for p in new_files if p in old_files
                      and old_files[p]["sha256"] != new_files[p]["sha256"])
    delete = compute_delete(old, files, version)
    # 终端只显示「本版 vs 上一版」的差异；历史继承的删除不重复打印（清单里仍完整保留）
    newly_deleted = sorted(p for p in old_files if p not in new_files)
    inherited = len(delete) - len(newly_deleted)
    log("本版变更: 新增 %d、替换 %d、删除 %d" % (len(added), len(replaced), len(newly_deleted)))
    for p in added:
        log("  新增: %s" % p)
    for p in replaced:
        log("  替换: %s" % p)
    for p in newly_deleted:
        log("  删除: %s" % p)
    if inherited > 0:
        log("  （另有历史删除 %d 条沿用旧清单，不再重复显示）" % inherited)

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

    # 步骤4.6 下载源索引（C 端多源下载用；纯优化层，失败不影响发布）
    index_obj: Optional[dict] = None
    if resolve:
        try:
            index_obj, _st = resolve_index_for_publish(
                cfg, store, version, files, log,
                backfill=backfill, no_cf=no_cf, lock_path=lock_path)
        except Exception as e:  # noqa: BLE001  索引只是加速层，绝不阻断发布
            log("警告: 来源索引生成失败（不影响本次发布，玩家将全部回落对象存储）: %s" % e)
            index_obj = None
    else:
        log("已指定 --no-resolve：跳过来源索引（线上旧索引保持不变）")

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
        if index_obj is not None:
            log("[dry-run] 将覆盖上传来源索引 sources.json（%d 条）"
                % len(index_obj["sources"]))
        else:
            log("[dry-run] 不上传来源索引（--no-resolve 或生成失败）")
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

    # 步骤7 先清单、再索引、最后指针（顺序硬约束）
    store.put_bytes("manifests/%s.json" % version,
                    json.dumps(signed, ensure_ascii=False).encode("utf-8"), MANIFEST_CC)
    log("已上传版本清单: manifests/%s.json（%s）" % (version, MANIFEST_CC))

    # 步骤7.5 下载源索引（在指针之前写入，保证"新指针可见时索引已就位"）
    if index_obj is not None:
        try:
            signed_index = canonicaljson.sign(index_obj, pem)
            store.put_bytes(SOURCES_KEY,
                            json.dumps(signed_index, ensure_ascii=False).encode("utf-8"),
                            SOURCES_CC)
            log("已上传来源索引: %s（%s，%d 条）"
                % (SOURCES_KEY, SOURCES_CC, len(index_obj["sources"])))
        except Exception as e:  # noqa: BLE001  见下：失败不阻断
            log("警告: sources.json 上传失败（不影响本次发布，玩家将回落对象存储）: %s" % e)

    pointer = canonicaljson.sign(build_pointer(pack_id, version), pem)
    store.put_bytes("manifest.json",
                    json.dumps(pointer, ensure_ascii=False).encode("utf-8"), POINTER_CC)
    log("已上传指针: manifest.json（%s）【最后写入】" % POINTER_CC)

    # 步骤8 写回本地发布状态
    if state_file:
        save_publish_state(state_file, signed)
        log("已写回本地发布状态: %s" % state_file)

    # 步骤9 清理：对象存储只保留「当前版本引用的 blob」，版本清单全部保留（可追溯历史更新说明）
    if gc:
        try:
            st = gc_unreferenced(store, files, version, log)
            log("存储清理: 删除旧 blob %d 个，释放 %.1f MiB（版本清单全部保留）"
                % (st["blobs"], st["freed"] / 1048576.0))
        except Exception as e:      # GC 失败不影响发布结果（指针已生效）
            log("警告: 存储清理失败（不影响本次发布）: %s" % e)

    log("publish-client 完成: version=%s files=%d 本版删除=%d 清单累计删除=%d"
        % (version, len(files), len(newly_deleted), len(delete)))
    return EXIT_OK


# ---------------------------------------------------------------------------
# 保底：全量重建来源索引（独立命令，不进日常发布流程）
# ---------------------------------------------------------------------------

def rebuild_sources(cfg, store, log: Callable[[str], None] = print,
                    dry_run: bool = False, lock_path: str = "mods.lock.json",
                    no_cf: bool = False) -> int:
    """对 client-mods 的**全部** mod 重新反查，整份重建并覆盖上传 sources.json。

    用途：索引损坏、大面积失效、平台侧换 CDN 域名等需要"彻底重来"的场合。
    与 publish-client 无关，不修改任何清单与 blob，只覆盖 sources.json 一个对象。
    """
    pack_id = cfg["packId"]
    client_cfg = cfg["client"]
    mods_dir = cfg["server"].get("modsDir", "mods")
    source_dir = client_cfg["sourceModsDir"]

    files = scan_client_mods(source_dir, mods_dir)
    if not files:
        raise PublishError(EXIT_GENERIC, "客户端源目录无 *.jar: %s" % source_dir)
    version = _published_version(client_cfg.get("manifestUrl") or "", log)
    log("全量重建来源索引: %d 个 jar（线上版本 %s）" % (len(files), version or "-"))

    lock_index = source_resolve.load_lock_index(lock_path)
    resolved, stats = source_resolve.resolve_sources(source_dir, files, lock_index, cfg, log,
                                                     no_cf=no_cf)
    index_obj = build_sources_index(pack_id, version, files, None, resolved)
    log("反查结果: lock %d / Modrinth %d / CurseForge %d / 未命中 %d / 白名单剔除 %d / 跳过 %d"
        % (stats["lock"], stats["modrinth"], stats["curseforge"], stats["miss"],
           stats["rejected"], stats["skipped"]))
    log("索引条目: %d / %d 个文件（未命中的走对象存储）"
        % (len(index_obj["sources"]), len(files)))
    missing = [f["path"] for f in files
               if str(f.get("sha256") or "").lower() not in index_obj["sources"]]
    for p in missing[:20]:
        log("  无平台直链: %s" % p)
    if len(missing) > 20:
        log("  ...（另有 %d 个，完整清单见日志）" % (len(missing) - 20))

    if dry_run:
        log("[dry-run] 未执行任何写操作（%s 保持不变）" % SOURCES_KEY)
        return EXIT_OK

    pem = _load_private_key_pem(cfg)
    signed = canonicaljson.sign(index_obj, pem)
    store.put_bytes(SOURCES_KEY, json.dumps(signed, ensure_ascii=False).encode("utf-8"),
                    SOURCES_CC)
    log("已覆盖上传来源索引: %s（%s，%d 条）"
        % (SOURCES_KEY, SOURCES_CC, len(index_obj["sources"])))
    return EXIT_OK
