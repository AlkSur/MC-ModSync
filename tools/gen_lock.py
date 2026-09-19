"""按已有 jar 反查平台来源，生成/补全 mods.lock.json。

原理（不靠文件名猜）：
  Modrinth   GET /v2/version_file/{sha1}?algorithm=sha1  -> version -> project_id
             POST /v2/projects?ids=[...]                 -> slug
  CurseForge POST /v1/fingerprints {fingerprints:[murmur2]}（需 --cf 与 apiKey）

命中  -> {"source":"modrinth"/"curseforge", "projectSlug":..., "versionPin":"latest", ...}
未命中 -> {"source":"manual", "versionPin":"manual", "fileName":...}（保留在本地，不会丢）

默认只写 mods.lock.generated.json，确认无误后再用 --write 覆盖 mods.lock.json。

用法:
  python tools/gen_lock.py --limit 5                 # 先试 5 个看看命中率
  python tools/gen_lock.py --cf                      # 未命中的再试 CurseForge
  python tools/gen_lock.py --cf --write              # 确认后写回 mods.lock.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

MR_BASE = "https://api.modrinth.com/v2"
CF_BASE = "https://api.curseforge.com/v1"
UA = "MC-ModSync/2.0 (neoforge mods sync; contact: op@example.com)"
TIMEOUT = 30


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# 哈希
# --------------------------------------------------------------------------

def file_hashes(path: str) -> dict:
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
    """CurseForge fingerprint: 去掉空白字节后的 murmur2(seed=1)，无符号 32 位。"""
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
# HTTP
# --------------------------------------------------------------------------

def _get(url: str, headers: dict) -> object:
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA,
                                                    "Accept": "application/json"}, **headers))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, payload: object, headers: dict) -> object:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=dict(
        {"User-Agent": UA, "Accept": "application/json",
         "Content-Type": "application/json"}, **headers))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def mr_lookup(sha1: str, sha512: str) -> dict:
    """单个 jar 反查：sha1 优先，失败再试 sha512；返回 version 对象或 {}。"""
    for algo, h in (("sha1", sha1), ("sha512", sha512)):
        url = "%s/version_file/%s?algorithm=%s" % (MR_BASE, h, algo)
        try:
            v = _get(url, {})
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                continue
            raise
        if isinstance(v, dict) and v.get("project_id"):
            return v
    return {}


def mr_lookup_batch(sha1_list: list, chunk: int = 800,
                    log_fn: Callable[[str], None] = log) -> dict:
    """批量反查 sha1 -> version（参考 scex portable-publish.ps1）。

    POST /v2/version_files  body: {"hashes":[...], "algorithm":"sha1"}
    响应: { sha1: <version 对象> }（只包含命中的）。比逐个 GET 快几十倍。
    """
    out: dict = {}
    uniq = sorted({h.lower() for h in sha1_list if h})
    if not uniq:
        return out
    for i in range(0, len(uniq), chunk):
        part = uniq[i:i + chunk]
        try:
            res = _post("%s/version_files" % MR_BASE,
                        {"hashes": part, "algorithm": "sha1"}, {})
        except urllib.error.HTTPError as e:
            log_fn("  [warn] Modrinth 批量反查失败（HTTP %s），逐个回退" % e.code)
            return None     # 交给调用方回退到逐��查询
        except Exception as e:
            log_fn("  [warn] Modrinth 批量反查失败（%s），逐个回退" % e)
            return None
        if isinstance(res, dict):
            for k, v in res.items():
                if isinstance(v, dict) and v.get("project_id"):
                    out[k.lower()] = v
        time.sleep(0.2)
    return out


def mr_projects(ids: list) -> dict:
    """POST /v2/projects 批量取 slug。"""
    out = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            arr = _get("%s/projects?ids=%s" % (MR_BASE,
                                               urllib.parse.quote(json.dumps(chunk))), {})
        except Exception as e:
            log("  [warn] 批量取项目信息失败: %s" % e)
            continue
        if isinstance(arr, list):
            for p in arr:
                out[p.get("id")] = p.get("slug") or p.get("id")
    return out


def cf_lookup(fps: list, api_key: str) -> dict:
    """fingerprint -> (modId, fileId)。"""
    out = {}
    if not api_key or not fps:
        return out
    try:
        res = _post("%s/fingerprints" % CF_BASE, {"fingerprints": fps},
                    {"x-api-key": api_key})
    except urllib.error.HTTPError as e:
        log("  [warn] CurseForge 指纹查询失败（HTTP %s）" % e.code)
        return out
    for m in ((res or {}).get("data") or {}).get("exactMatches") or []:
        fp = str((m.get("file") or {}).get("fileFingerprint"))
        out[fp] = (m.get("id"), (m.get("file") or {}).get("id"))
    return out


# --------------------------------------------------------------------------
# 兜底：文件名搜索 + 哈希校验（不允许"猜名字就直接采信"）
# --------------------------------------------------------------------------

_DROP = {"neoforge", "forge", "fabric", "quilt", "mc", "minecraft", "release",
         "beta", "alpha", "universal", "neoforged", "mod"}


def guess_query(file_name: str) -> str:
    """jar 文件名 -> 搜索关键词：去掉版本号、加载器名、中文前缀。"""
    n = os.path.splitext(file_name)[0]
    n = n.replace("[", " ").replace("]", " ").replace("(", " ").replace(")", " ")
    toks = []
    for t in n.replace("_", "-").split("-"):
        t = t.strip()
        if not t:
            continue
        low = t.lower()
        # 纯版本号段（1.21.1 / 2.9.1 / 21.1.3）、mc1.21 之类
        if all(c.isdigit() or c == "." for c in low) and any(c.isdigit() for c in low):
            continue
        if low.startswith("mc") and any(c.isdigit() for c in low):
            continue
        if low in _DROP:
            continue
        if low.startswith("v") and any(c.isdigit() for c in low):
            continue
        toks.append(low)
    return " ".join(toks[:3])


def mr_search(query: str, limit: int = 5) -> list:
    url = "%s/search?query=%s&limit=%d" % (MR_BASE, urllib.parse.quote(query), limit)
    try:
        d = _get(url, {})
    except Exception:
        return []
    return [h.get("slug") for h in (d.get("hits") or []) if h.get("slug")]


def mr_verify(slug: str, sha1: str) -> tuple:
    """列出该项目所有版本的文件哈希，逐个比对 sha1；命中返回 (version, file)。"""
    url = "%s/project/%s/version" % (MR_BASE, urllib.parse.quote(slug, safe=""))
    try:
        arr = _get(url, {})
    except Exception:
        return (None, None)
    for v in arr or []:
        for f in v.get("files") or []:
            if (f.get("hashes") or {}).get("sha1") == sha1:
                return (v, f)
    return (None, None)


def cf_search(query: str, api_key: str, limit: int = 5) -> list:
    url = "%s/mods/search?gameId=432&searchFilter=%s&pageSize=%d" % (
        CF_BASE, urllib.parse.quote(query), limit)
    try:
        d = _get(url, {"x-api-key": api_key})
    except Exception:
        return []
    return [m.get("id") for m in (d.get("data") or []) if m.get("id")]


def cf_mod_slug(mod_id: str, api_key: str) -> str:
    """modId -> slug（fetcher 按 slug 搜索，数字 id 查不到）。"""
    try:
        d = _get("%s/mods/%s" % (CF_BASE, mod_id), {"x-api-key": api_key})
    except Exception:
        return ""
    return ((d or {}).get("data") or {}).get("slug") or ""


def cf_verify(mod_id: str, sha1: str, api_key: str) -> tuple:
    """列出该 mod 的文件哈希，比对 sha1（algo 1 = sha1）。"""
    url = "%s/mods/%s/files?pageSize=50" % (CF_BASE, mod_id)
    try:
        d = _get(url, {"x-api-key": api_key})
    except Exception:
        return (None, None)
    for f in (d.get("data") or []):
        for h in f.get("hashes") or []:
            if h.get("algo") == 1 and str(h.get("value") or "").lower() == sha1.lower():
                return (f, f.get("downloadUrl") or "")
    return (None, None)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def scan(cfg: dict, repo: str, only: str = "both") -> dict:
    """返回 fileName -> {side, path}；两边都有 -> both。

    server-mods 是直接 SSH 推送的（push-server），不需要知道平台来源；
    只有走对象存储分发的 client-mods 才需要登记 -> 用 --only client。
    """
    found = {}

    def add(d: str, side: str) -> None:
        if not os.path.isdir(d):
            return
        for n in sorted(os.listdir(d)):
            if n.lower().endswith(".jar"):
                e = found.setdefault(n, {"side": side, "path": os.path.join(d, n)})
                if e["side"] != side:
                    e["side"] = "both"

    srv = ((cfg.get("server") or {}).get("sourceModsDir") or "./server-mods")
    cli = ((cfg.get("client") or {}).get("sourceModsDir") or "./client-mods")
    if only in ("server", "both"):
        add(os.path.join(repo, srv), "server")
    if only in ("client", "both"):
        add(os.path.join(repo, cli), "client")
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="由已有 jar 生成 mods.lock.json")
    ap.add_argument("--repo", default=".", help="仓库根目录（含 pack.local.json）")
    ap.add_argument("--config", default="", help="配置文件（默认 <repo>/pack.local.json）")
    ap.add_argument("--out", default="", help="输出文件（默认 <repo>/mods.lock.generated.json）")
    ap.add_argument("--write", action="store_true",
                    help="生成后直接覆盖 mods.lock.json（会重新联网跑一遍）")
    ap.add_argument("--apply", action="store_true",
                    help="把上次生成的 mods.lock.generated.json 应用为正式文件："
                         "不联网、秒完成，并自动备份原文件为 mods.lock.json.bak")
    ap.add_argument("--only", choices=("client", "server", "both"), default="client",
                    help="登记哪一侧的源目录（默认 client：只有玩家端需要平台来源，"
                         "服务端走 SSH 直推无需登记；要带上服务端用 --only both）")
    ap.add_argument("--no-cf", action="store_true", help="跳过 CurseForge 反查（默认会查）")
    ap.add_argument("--no-name-search", action="store_true",
                    help="跳过「文件名搜索 + 哈希校验」兜底（默认会做）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（试跑用）")
    ap.add_argument("--sleep", type=float, default=0.15, help="每次请求间隔秒（平台限流）")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)

    # --apply: 离线把上次生成结果应用到正式文件，不重新联网
    if args.apply:
        src = args.out or os.path.join(repo, "mods.lock.generated.json")
        dst = os.path.join(repo, "mods.lock.json")
        if not os.path.isfile(src):
            log("找不到 %s —— 先跑一次不带参数的生成" % os.path.basename(src))
            return 1
        with open(src, encoding="utf-8") as f:
            doc = json.load(f)
        if not isinstance(doc, dict) or doc.get("schemaVersion") != 1 \
                or not isinstance(doc.get("mods"), list):
            log("生成文件结构非法（schemaVersion 必须为 1 且 mods 为数组），拒绝应用")
            return 1
        if os.path.isfile(dst):
            shutil.copy2(dst, dst + ".bak")
            log("已备份原文件: mods.lock.json.bak")
        shutil.copy2(src, dst)
        log("已应用: %s -> mods.lock.json（共 %d 条）" % (os.path.basename(src), len(doc["mods"])))
        return 0

    cfg_path = args.config or os.path.join(repo, "pack.local.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    pack_meta = {"minecraftVersion": "1.21.1", "loader": "neoforge"}

    found = scan(cfg, repo, args.only)
    names = sorted(found)
    if args.limit:
        names = names[:args.limit]
    log("扫描到 jar: %d 个（本次处理 %d）" % (len(found), len(names)))

    # 第一步：先算全部哈希（本地 IO）
    meta = {}
    for n in names:
        meta[n] = file_hashes(found[n]["path"])
    log("哈希计算完成: %d 个" % len(meta))

    # 第二步：Modrinth 批量反查（POST /v2/version_files，一次数百个）
    batch = mr_lookup_batch([meta[n]["sha1"] for n in names])
    if batch is None:       # 批量失败则逐个回退
        batch = {}
        for n in names:
            try:
                v = mr_lookup(meta[n]["sha1"], meta[n]["sha512"])
            except Exception as e:
                log("  [warn] %s 查询失败 %s" % (n, e))
                v = {}
            time.sleep(args.sleep)
            if v:
                batch[meta[n]["sha1"].lower()] = v
    log("Modrinth 哈希反查命中: %d / %d" % (
        sum(1 for n in names if meta[n]["sha1"].lower() in batch), len(names)))

    entries = []
    pending_cf = []
    for n in names:
        info = found[n]
        h = meta[n]
        v = batch.get(h["sha1"].lower()) or {}

        if v:
            files = v.get("files") or []
            primary = next((f for f in files if f.get("primary")), files[0] if files else {})
            entries.append({
                "name": os.path.splitext(n)[0],
                "projectId": v.get("project_id"),
                "source": "modrinth",
                "side": info["side"],
                "versionPin": "latest",
                "fileName": n,
                "resolvedVersion": v.get("version_number") or "",
                "downloadUrl": primary.get("url") or "",
                "sha256": h["sha256"],
                "size": h["size"],
            })
            log("  [MR]  %-58s -> %s" % (n[:58], v.get("version_number") or "?"))
        else:
            e = {
                "name": os.path.splitext(n)[0],
                "source": "manual",
                "side": info["side"],
                "versionPin": "manual",
                "fileName": n,
                "sha256": h["sha256"],
                "size": h["size"],
                "note": "平台未识别（自建/改名/CurseForge 独占），保持本地文件",
            }
            entries.append(e)
            pending_cf.append((n, e, h))
            log("  [--]  %-58s -> 未识别" % n[:58])

    # 补全 slug
    ids = [e["projectId"] for e in entries if e.get("projectId")]
    if ids:
        slugs = mr_projects(sorted(set(ids)))
        for e in entries:
            if e.get("projectId"):
                e["projectSlug"] = slugs.get(e["projectId"], "")
                e.pop("projectId", None)

    # CurseForge 兜底（默认启用；指纹是哈希级匹配，可直接采信）
    cf_key = (cfg.get("curseforge") or {}).get("apiKey") or ""
    if args.no_cf:
        pass
    elif not pending_cf:
        pass
    elif not cf_key:
        log("未配置 curseforge.apiKey，跳过 CurseForge 反查")
    else:
            fps = {}
            for n, e, h in pending_cf:
                try:
                    fps[str(cf_murmur2(found[n]["path"]))] = (n, e)
                except Exception as ex:
                    log("  [warn] %s 指纹计算失败 %s" % (n, ex))
            hits = cf_lookup(list(fps), cf_key)
            for fp, (n, e) in fps.items():
                if fp in hits:
                    mod_id, _file_id = hits[fp]
                    e.pop("note", None)
                    slug = cf_mod_slug(str(mod_id), cf_key)
                    e["source"] = "curseforge"
                    e["projectSlug"] = slug or str(mod_id)
                    e["versionPin"] = "latest"
                    log("  [CF]  %-58s -> %s (modId %s)" % (n[:58], e["projectSlug"], mod_id))
            time.sleep(args.sleep)

    # 最后兜底：按文件名搜索候选，但必须哈希校验一致才采信
    if not args.no_name_search:
        still = [(n, e, h) for (n, e, h) in pending_cf if e.get("source") == "manual"]
        if still:
            log("文件名搜索兜底（仅当候选版本文件哈希与你本地 jar 完全一致才采信）: %d 个" % len(still))
        for n, e, h in still:
            q = guess_query(n)
            if not q:
                continue
            hit = False
            for slug in mr_search(q):
                time.sleep(args.sleep)
                v, f = mr_verify(slug, h["sha1"])
                if v:
                    e.pop("note", None)
                    e["source"] = "modrinth"
                    e["projectSlug"] = slug
                    e["versionPin"] = "latest"
                    e["resolvedVersion"] = v.get("version_number") or ""
                    e["downloadUrl"] = (f or {}).get("url") or ""
                    e["note"] = "经文件名搜索定位 + 文件哈希校验一致"
                    log("  [MR?] %-58s -> %s (%s)" % (n[:58], slug, e["resolvedVersion"]))
                    hit = True
                    break
            if hit or not cf_key:
                continue
            for mod_id in cf_search(q, cf_key):
                time.sleep(args.sleep)
                f, url = cf_verify(str(mod_id), h["sha1"], cf_key)
                if f:
                    e.pop("note", None)
                    e["source"] = "curseforge"
                    e["projectSlug"] = cf_mod_slug(str(mod_id), cf_key) or str(mod_id)
                    e["versionPin"] = "latest"
                    e["resolvedVersion"] = (f.get("displayName") or f.get("fileName") or "")
                    e["note"] = "经文件名搜索定位 + 文件哈希校验一致"
                    log("  [CF?] %-58s -> modId %s" % (n[:58], mod_id))
                    hit = True
                    break
            if not hit:
                e["note"] = "已查 Modrinth/CurseForge（哈希 + 文件名搜索）均无匹配，按本地文件处理"

    # 同一份 jar 被复制成多个文件名（如 xxx.jar 与 xxx.jar-备注.jar）只登记一条
    seen = set()
    deduped = []
    for e in entries:
        key = e.get("sha256") or e["fileName"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    if len(deduped) != len(entries):
        log("去重: %d -> %d（内容完全相同的重复文件只保留一条）" % (len(entries), len(deduped)))
    entries = deduped

    doc = {"schemaVersion": 1, "packMeta": pack_meta, "mods": entries}
    out = args.out or os.path.join(repo, "mods.lock.generated.json")
    if args.write:
        out = os.path.join(repo, "mods.lock.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")

    n_mr = sum(1 for e in entries if e["source"] == "modrinth")
    n_cf = sum(1 for e in entries if e["source"] == "curseforge")
    n_man = sum(1 for e in entries if e["source"] == "manual")
    log("-" * 62)
    log("已写出: %s" % out)
    log("Modrinth %d / CurseForge %d / 需人工 %d （共 %d）" % (n_mr, n_cf, n_man, len(entries)))
    if not args.write:
        log("确认无误后加 --write 覆盖 mods.lock.json（当前写的是独立文件，未动原文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
