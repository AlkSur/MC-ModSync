"""按本地 jar 反查平台来源，产出「下载源索引」用的 sha256 -> 直链映射（A 端）。

原理（不靠文件名猜，算法与 tools/gen_lock.py 完全一致，此处只是搬到包内复用）：
  1. 本地 mods.lock.json 命中      -> 0 次 API
  2. Modrinth  POST /v2/version_files {hashes:[sha1], algorithm:"sha1"} -> version -> files[primary].url
  3. CurseForge POST /v1/fingerprints {fingerprints:[murmur2]}          -> file.downloadUrl

多出来的一层是**直链白名单**：索引里的 URL 会被玩家端直接拿去下载，
绝不能把 lock 里那种「两个项目主页拼在一起」的脏数据写进去
（见 docs/C端多源下载改造方案.md §2.1）。白名单外的条目一律不写入，宁可走对象存储。

本模块只用标准库。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

MR_BASE = "https://api.modrinth.com/v2"
CF_BASE = "https://api.curseforge.com/v1"
UA = "MC-ModSync/2.0 (neoforge mods sync; contact: op@example.com)"
TIMEOUT = 30
BATCH = 800          # Modrinth version_files 单批上限（实测可用）

# 允许写入索引的直链前缀。**改这里必须同步改 C 端 source_index.py**，
# 两侧各自持有同一份白名单，做「写入 + 读取」双重校验（tests 有断言守住一致性）。
ALLOWED_URL_PREFIXES: Tuple[str, ...] = (
    "https://cdn.modrinth.com/data/",
    "https://edge.forgecdn.net/files/",
)

ALLOWED_SOURCES = ("modrinth", "curseforge")


# --------------------------------------------------------------------------
# 哈希
# --------------------------------------------------------------------------

def file_hashes(path: str) -> dict:
    """一次读完算出 sha1 / sha256 / sha512 / size（Modrinth 认 sha1，CF 认 murmur2）。"""
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    sha512 = hashlib.sha512()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha1.update(chunk)
            sha256.update(chunk)
            sha512.update(chunk)
            size += len(chunk)
    return {"sha1": sha1.hexdigest(), "sha256": sha256.hexdigest(),
            "sha512": sha512.hexdigest(), "size": size}


def cf_murmur2(path: str) -> int:
    """CurseForge 指纹：去掉空白字节后的 murmur2(seed=1)，无符号 32 位。"""
    with open(path, "rb") as f:
        data = f.read()
    data = bytes(b for b in data if b not in (0x09, 0x0A, 0x0D, 0x20))
    length = len(data)
    m = 0x5BD1E995
    r = 24
    h = (1 ^ length) & 0xFFFFFFFF
    n = length // 4
    for i in range(n):
        i4 = i * 4
        k = (data[i4] | (data[i4 + 1] << 8) | (data[i4 + 2] << 16) | (data[i4 + 3] << 24))
        k = (k * m) & 0xFFFFFFFF
        k ^= (k >> r) & 0xFFFFFFFF
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        h ^= k
        h &= 0xFFFFFFFF
    tail = length & 3
    if tail:
        i4 = n * 4
        k = 0
        if tail == 3:
            k ^= data[i4 + 2] << 16
        if tail >= 2:
            k ^= data[i4 + 1] << 8
        if tail >= 1:
            k ^= data[i4]
            k = (k * m) & 0xFFFFFFFF
            k ^= (k >> r) & 0xFFFFFFFF
            k = (k * m) & 0xFFFFFFFF
            h ^= k
            h &= 0xFFFFFFFF
    h ^= (h >> 13) & 0xFFFFFFFF
    h = (h * m) & 0xFFFFFFFF
    h ^= (h >> 15) & 0xFFFFFFFF
    return h & 0xFFFFFFFF


# --------------------------------------------------------------------------
# 直链白名单
# --------------------------------------------------------------------------

def is_allowed_url(url: str) -> bool:
    """只有白名单域名下的 https 直链才允许写进索引（挡住项目主页/拼接脏数据）。"""
    if not isinstance(url, str):
        return False
    u = url.strip()
    if not u or not u.lower().startswith("https://"):
        return False
    if any(c in u for c in (" ", "\t", "\n", "\r")):
        return False
    low = u.lower()
    return any(low.startswith(p) for p in ALLOWED_URL_PREFIXES)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _get(url: str, headers: dict, timeout: int = TIMEOUT) -> object:
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA,
                                                    "Accept": "application/json"}, **headers))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, payload: object, headers: dict, timeout: int = TIMEOUT) -> object:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=dict(
        {"User-Agent": UA, "Accept": "application/json",
         "Content-Type": "application/json"}, **headers))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def mr_lookup_batch(sha1_list: List[str], chunk: int = BATCH,
                    log_fn: Callable[[str], None] = print) -> Optional[dict]:
    """批量 sha1 反查 -> {sha1: version}；批量接口失败返回 None（调用方放弃这一家）。"""
    out: Dict[str, dict] = {}
    uniq = sorted({str(h).lower() for h in sha1_list if h})
    if not uniq:
        return out
    for i in range(0, len(uniq), chunk):
        part = uniq[i:i + chunk]
        try:
            res = _post("%s/version_files" % MR_BASE,
                        {"hashes": part, "algorithm": "sha1"}, {})
        except Exception as e:  # noqa: BLE001  网络问题不该中断发布
            log_fn("  [warn] Modrinth 批量反查失败（%s），本次跳过 Modrinth" % e)
            return None
        if isinstance(res, dict):
            for k, v in res.items():
                if isinstance(v, dict) and v.get("project_id"):
                    out[str(k).lower()] = v
        if i + chunk < len(uniq):
            time.sleep(0.2)
    return out


def mr_projects(ids: List[str]) -> dict:
    """POST /v2/projects 批量取 slug（失败只是少了可读的 slug，不影响直链）。"""
    out: Dict[str, str] = {}
    uniq = sorted({i for i in ids if i})
    for i in range(0, len(uniq), 100):
        chunk = uniq[i:i + 100]
        try:
            arr = _get("%s/projects?ids=%s" % (MR_BASE,
                                               urllib.parse.quote(json.dumps(chunk))), {})
        except Exception:  # noqa: BLE001
            continue
        if isinstance(arr, list):
            for p in arr:
                out[p.get("id")] = p.get("slug") or p.get("id") or ""
    return out


def cf_lookup(fps: List[str], api_key: str) -> dict:
    """指纹 -> (modId, fileId, downloadUrl)；downloadUrl 可能为 None（作者禁第三方下载）。"""
    out: Dict[str, tuple] = {}
    if not api_key or not fps:
        return out
    try:
        res = _post("%s/fingerprints" % CF_BASE, {"fingerprints": fps},
                    {"x-api-key": api_key})
    except Exception:  # noqa: BLE001
        return out
    for m in ((res or {}).get("data") or {}).get("exactMatches") or []:
        f = m.get("file") or {}
        fp = str(f.get("fileFingerprint"))
        out[fp] = (m.get("id"), f.get("id"), f.get("downloadUrl") or "")
    return out


def cf_mod_slug(mod_id: str, api_key: str) -> str:
    """modId -> slug（仅用于日志可读性）。"""
    try:
        d = _get("%s/mods/%s" % (CF_BASE, mod_id), {"x-api-key": api_key})
    except Exception:  # noqa: BLE001
        return ""
    return ((d or {}).get("data") or {}).get("slug") or ""


# --------------------------------------------------------------------------
# lock 索引
# --------------------------------------------------------------------------

def load_lock_index(lock_path: str) -> Dict[str, dict]:
    """mods.lock.json -> {sha256: 条目}；文件缺失/损坏返回空表（不抛）。"""
    if not lock_path or not os.path.isfile(lock_path):
        return {}
    try:
        with open(lock_path, "rb") as f:
            doc = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return {}
    idx: Dict[str, dict] = {}
    for e in (doc.get("mods") or []):
        if not isinstance(e, dict):
            continue
        sha = str(e.get("sha256") or "").lower()
        if sha:
            idx.setdefault(sha, e)
    return idx


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def _entry_info(source: str, url: str, entry: dict,
                slug: str = "", resolved: str = "") -> dict:
    return {
        "source": source,
        "fileName": os.path.basename(str(entry.get("path") or "")),
        "size": int(entry.get("size") or 0),
        "downloadUrl": url,
        "projectSlug": str(slug or ""),
        "resolvedVersion": str(resolved or ""),
    }


def _from_lock(le: Optional[dict], entry: dict) -> Optional[dict]:
    if not isinstance(le, dict):
        return None
    src = str(le.get("source") or "").strip().lower()
    if src not in ALLOWED_SOURCES:
        return None
    url = str(le.get("downloadUrl") or "").strip()
    if not is_allowed_url(url):
        return None
    return _entry_info(src, url, entry, le.get("projectSlug") or "",
                       le.get("resolvedVersion") or "")


def resolve_sources(source_dir: str, entries: List[dict],
                    lock_index: Optional[Dict[str, dict]] = None,
                    cfg: Optional[dict] = None,
                    log: Callable[[str], None] = print,
                    allow_network: bool = True,
                    no_cf: bool = False) -> Tuple[Dict[str, dict], dict]:
    """把 [{path, sha256, size}] 解析成 {sha256: 来源信息}。

    返回 (resolved, stats)；stats = {total, lock, modrinth, curseforge,
    miss, rejected, skipped}。任何条目未命中就不写入 —— 该文件走对象存储。
    """
    lock_index = lock_index or {}
    resolved: Dict[str, dict] = {}
    stats = {"total": len(entries), "lock": 0, "modrinth": 0, "curseforge": 0,
             "miss": 0, "rejected": 0, "skipped": 0}
    pending: List[dict] = []

    for e in entries:
        sha = str(e.get("sha256") or "").lower()
        if not sha:
            stats["skipped"] += 1
            continue
        le = lock_index.get(sha)
        info = _from_lock(le, e)
        if info:
            resolved[sha] = info
            stats["lock"] += 1
            continue
        if isinstance(le, dict) and str(le.get("downloadUrl") or "").strip():
            stats["rejected"] += 1        # lock 有 URL 但不过白名单（脏数据）
        pending.append(e)

    if not pending:
        return resolved, stats
    if not allow_network:
        stats["miss"] += len(pending)
        return resolved, stats

    # 本地算哈希（只读；只为未命中项付这份 IO 代价）
    hashes: Dict[str, dict] = {}
    paths: Dict[str, str] = {}
    for e in pending:
        sha = str(e.get("sha256") or "").lower()
        p = os.path.join(source_dir, os.path.basename(str(e.get("path") or "")))
        try:
            hashes[sha] = file_hashes(p)
            paths[sha] = p
        except OSError as ex:
            log("  [warn] 读取失败，跳过反查: %s（%s）" % (p, ex))
            stats["skipped"] += 1

    # 2) Modrinth 批量 sha1 反查
    if hashes:
        batch = mr_lookup_batch([h["sha1"] for h in hashes.values()], log_fn=log) or {}
        slugs = mr_projects([v.get("project_id") for v in batch.values()
                             if v.get("project_id")])
        for e in pending:
            sha = str(e.get("sha256") or "").lower()
            h = hashes.get(sha)
            if not h:
                continue
            v = batch.get(h["sha1"].lower())
            if not v:
                continue
            files = v.get("files") or []
            primary = next((f for f in files if f.get("primary")),
                           files[0] if files else {})
            url = str((primary or {}).get("url") or "")
            if not is_allowed_url(url):
                stats["rejected"] += 1
                continue
            resolved[sha] = _entry_info(
                "modrinth", url, e,
                slugs.get(v.get("project_id"), ""), v.get("version_number") or "")
            stats["modrinth"] += 1

    # 3) CurseForge 指纹反查
    cf_key = ((cfg or {}).get("curseforge") or {}).get("apiKey") or ""
    rest = [e for e in pending if str(e.get("sha256") or "").lower() not in resolved]
    if rest and cf_key and not no_cf:
        fps: Dict[str, dict] = {}
        for e in rest:
            sha = str(e.get("sha256") or "").lower()
            p = paths.get(sha)
            if not p:
                continue
            try:
                fps[str(cf_murmur2(p))] = e
            except OSError as ex:
                log("  [warn] 指纹计算失败: %s（%s）" % (p, ex))
        hits = cf_lookup(list(fps), cf_key)
        for fp, e in fps.items():
            hit = hits.get(fp)
            if not hit:
                continue
            mod_id, _file_id, url = hit
            if not is_allowed_url(str(url or "")):
                stats["rejected"] += 1
                continue
            sha = str(e.get("sha256") or "").lower()
            resolved[sha] = _entry_info("curseforge", str(url), e,
                                        cf_mod_slug(str(mod_id), cf_key), "")
            stats["curseforge"] += 1

    stats["miss"] = len([e for e in pending
                         if str(e.get("sha256") or "").lower() not in resolved])
    return resolved, stats
