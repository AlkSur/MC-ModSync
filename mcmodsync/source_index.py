"""下载源索引（C 端，纯标准库）。

`sources.json` 由 A 端每次发布时覆盖上传到 pack 根；本模块负责三件事：

1. 每次同步（且确实有文件要下）时拉取最新一份 —— 3 秒超时、验签、packId 校验；
   **任何失败都静默**（终端一行都不打），沿用本地缓存；
2. 原子覆盖本地缓存（路径由调用方给出，通常是 `<target>/_updater/sources.json`）；
3. 按 sha256 查直链，并在**读取侧再复检一次**白名单域名（与 A 端写入侧双重校验）。

设计红线：本模块是**纯优化层**。所有函数都不抛异常、不返回错误码 ——
拿不到索引就是"没有优化可用"，全部回落对象存储，行为与改造之前完全一致。
索引内容本身也不参与 sha256 校验与原子落位：直链下到的东西照样要过清单里的哈希。
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Callable, Optional, Tuple

from . import canonicaljson

USER_AGENT = "MC-ModSync-C/2.0"
SUPPORTED_INDEX_SCHEMA = 1

# 与 A 端 mcmodsync/source_resolve.py 的 ALLOWED_URL_PREFIXES 保持一致
# （tests/test_sources_index.py 有断言守住两者不漂移）。
ALLOWED_URL_PREFIXES: Tuple[str, ...] = (
    "https://cdn.modrinth.com/data/",
    "https://edge.forgecdn.net/files/",
)


def _trace(fn: Optional[Callable[[str], None]], msg: str) -> None:
    """只写日志文件的技术细节（终端不出现任何字样）。"""
    if fn:
        try:
            fn(msg)
        except Exception:
            pass


def is_allowed_url(url: str) -> bool:
    """只接受白名单域名下的 https 直链（与 A 端写入侧同一套规则）。"""
    if not isinstance(url, str):
        return False
    u = url.strip()
    if not u or not u.lower().startswith("https://"):
        return False
    if any(c in u for c in (" ", "\t", "\n", "\r")):
        return False
    low = u.lower()
    return any(low.startswith(p) for p in ALLOWED_URL_PREFIXES)


def index_url(pointer_url: str) -> str:
    """`<prefix>/manifest.json` -> `<prefix>/sources.json`（与 blob_base 同级）。"""
    u = urllib.parse.urlsplit(pointer_url or "")
    path = u.path
    base_path = path.rsplit("/", 1)[0] if "/" in path else ""
    return urllib.parse.urlunsplit((u.scheme, u.netloc, base_path + "/sources.json", "", ""))


def load_cached(path: str) -> dict:
    """读本地缓存索引；缺失/损坏一律空表（字段 'sources' 恒为 dict）。"""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "rb") as f:
            obj = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(obj, dict) or not isinstance(obj.get("sources"), dict):
        return {}
    return obj


def save_cached(path: str, obj: dict) -> None:
    """原子覆盖本地缓存（临时文件 + os.replace）；任何失败都静默。"""
    if not path or not isinstance(obj, dict):
        return
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = "%s.tmp-%d" % (path, os.getpid())
        with open(tmp, "wb") as f:
            f.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001  缓存写失败不影响同步（下次启动重试）
        pass


def fetch_index(pointer_url: str, public_key: str, pack_id: str,
                timeout: int = 3,
                trace: Optional[Callable[[str], None]] = None) -> Optional[dict]:
    """拉取并校验最新索引；任何异常/校验不过都返回 None（**静默**）。

    校验三道：验签（Ed25519，与清单同一把公钥）、packId 一致、schemaVersion 不高于支持值。
    """
    if not pointer_url or not public_key:
        return None
    url = index_url(pointer_url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001  404/超时/网络异常都视为"没有索引"
        _trace(trace, "来源索引拉取失败（沿用本地缓存，不影响同步）: %s" % e)
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("sources"), dict):
        _trace(trace, "来源索引结构非法（忽略）")
        return None
    try:
        if int(obj.get("schemaVersion") or 0) > SUPPORTED_INDEX_SCHEMA:
            _trace(trace, "来源索引 schemaVersion 高于本程序支持值（忽略）")
            return None
    except (TypeError, ValueError):
        _trace(trace, "来源索引 schemaVersion 非法（忽略）")
        return None
    if str(obj.get("packId") or "") != str(pack_id or ""):
        _trace(trace, "来源索引 packId 不一致（忽略）")
        return None
    try:
        ok = canonicaljson.verify(obj, public_key)
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        _trace(trace, "来源索引验签失败（忽略）")
        return None
    return obj


def lookup(index: dict, sha256: str) -> Optional[Tuple[str, str]]:
    """按 sha256 取 (source, downloadUrl)；未命中/不过白名单一律 None。"""
    try:
        sources = (index or {}).get("sources") or {}
        item = sources.get(str(sha256 or "").lower())
        if not isinstance(item, dict):
            return None
        url = str(item.get("downloadUrl") or "").strip()
        if not is_allowed_url(url):
            return None
        return (str(item.get("source") or "").strip().lower(), url)
    except Exception:  # noqa: BLE001
        return None


def count(index: dict) -> int:
    """索引条目数（仅用于日志；结构不对时算 0）。"""
    try:
        sources = (index or {}).get("sources") or {}
        return len(sources) if isinstance(sources, dict) else 0
    except Exception:  # noqa: BLE001
        return 0
