"""Modrinth provider（纯 urllib，无第三方依赖）—— [T-31]。

API 约定:
  版本列表: GET https://api.modrinth.com/v2/project/{slug}/version
            ?loaders=["neoforge"]&game_versions=["1.21.1"]   （参数为 URL 编码的 JSON 数组）
  选版本:   versionPin="latest" 取列表首个（发布日期倒序）；否则按 version_number 匹配。
  选文件:   files 中 primary=true 者，无则取第一个。
  完整性:   平台只给 sha1/sha512；本项目统一下载后实算 sha256 与 size。
  限流:     HTTP 429 -> 指数退避重试 3 次；仍失败 -> 抛 ProviderError(5)，跳过该 mod 继续。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional

BASE_URL = "https://api.modrinth.com/v2"
USER_AGENT = "MC-ModSync/2.0 (neoforge mods sync; contact: op@example.com)"
TIMEOUT = 30
RETRIES = 3


class ProviderError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _build_url(slug: str, loaders: List[str], game_versions: List[str],
               base_url: str = BASE_URL) -> str:
    q = urllib.parse.urlencode({
        "loaders": json.dumps(loaders, separators=(",", ":")),
        "game_versions": json.dumps(game_versions, separators=(",", ":")),
    })
    return "%s/project/%s/version?%s" % (base_url.rstrip("/"),
                                         urllib.parse.quote(slug, safe=""), q)


def _http_get_json(url: str, retries: int = RETRIES,
                   log: Callable[[str], None] = print) -> Optional[object]:
    """GET JSON；404 -> None；429 -> 指数退避重试；其余错误抛 ProviderError(5)。"""
    delay = 1.0
    last = ""
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429 and attempt < retries:
                log("Modrinth 429 限流，%.1fs 后重试（第 %d 次）" % (delay, attempt))
                time.sleep(delay)
                delay *= 2
                last = "HTTP 429"
                continue
            last = "HTTP %s" % e.code
            break
        except Exception as e:
            last = str(e)
            if attempt < retries:
                log("Modrinth 请求失败（第 %d 次）: %s" % (attempt, e))
                time.sleep(delay)
                delay *= 2
                continue
            break
    raise ProviderError(5, "Modrinth 请求失败（已重试 %d 次）: %s" % (retries, last))


def list_versions(slug: str, game_version: str, loader: str = "neoforge",
                  base_url: str = BASE_URL, log: Callable[[str], None] = print) -> Optional[List[dict]]:
    """返回该项目在该 MC 版本/加载器下的版本列表（发布日期倒序）；项目不存在 -> None。"""
    url = _build_url(slug, [loader], [game_version], base_url)
    data = _http_get_json(url, log=log)
    if data is None:
        return None
    if not isinstance(data, list):
        raise ProviderError(5, "Modrinth 版本列表结构异常: %s" % type(data).__name__)
    return data


def pick_version(versions: List[dict], version_pin: str) -> Optional[dict]:
    """versionPin="latest" 取首个；否则按 version_number 精确匹配。"""
    if not versions:
        return None
    if not version_pin or version_pin == "latest":
        return versions[0]
    for v in versions:
        if v.get("version_number") == version_pin:
            return v
    return None


def pick_file(version: dict) -> Optional[dict]:
    """优先 primary=true 的文件，否则取第一个。"""
    files = version.get("files") or []
    if not files:
        return None
    for f in files:
        if f.get("primary"):
            return f
    return files[0]


def project_url(slug: str) -> str:
    return "https://modrinth.com/mod/%s" % slug


def resolve(slug: str, game_version: str, loader: str = "neoforge",
            version_pin: str = "latest", base_url: str = BASE_URL,
            log: Callable[[str], None] = print) -> Optional[Dict[str, object]]:
    """解析出下载信息；项目不存在或版本不匹配 -> None。

    返回: {resolvedVersion, fileName, downloadUrl, size, sha1, sha512}
    """
    versions = list_versions(slug, game_version, loader, base_url, log)
    if versions is None:
        log("Modrinth: 项目不存在 slug=%s" % slug)
        return None
    ver = pick_version(versions, version_pin)
    if ver is None:
        log("Modrinth: 未找到指定版本 %s（slug=%s）" % (version_pin, slug))
        return None
    f = pick_file(ver)
    if f is None:
        log("Modrinth: 版本 %s 无可下载文件" % ver.get("version_number"))
        return None
    hashes = f.get("hashes") or {}
    return {
        "resolvedVersion": ver.get("version_number"),
        "fileName": f.get("filename"),
        "downloadUrl": f.get("url"),
        "size": int(f.get("size") or 0),
        "sha1": hashes.get("sha1"),
        "sha512": hashes.get("sha512"),
        "source": "modrinth",
    }


def download_file(info: Dict[str, object], dest_dir: str,
                  log: Callable[[str], None] = print) -> Dict[str, object]:
    """下载到 dest_dir/<fileName>，实算 sha256 与 size（[2.3]）。"""
    from . import hashing

    url = str(info.get("downloadUrl") or "")
    name = str(info.get("fileName") or "")
    if not url or not name:
        raise ProviderError(5, "Modrinth 下载信息不完整: %r" % (info,))
    os.makedirs(dest_dir, exist_ok=True)
    dst = os.path.join(dest_dir, name)
    tmp = dst + ".part"

    delay = 1.0
    last = ""
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp, open(tmp, "wb") as out:
                while True:
                    block = resp.read(1024 * 1024)
                    if not block:
                        break
                    out.write(block)
            os.replace(tmp, dst)
            info_out = hashing.hash_file(dst)
            log("Modrinth 下载完成: %s (sha256=%s size=%d)"
                % (name, info_out["sha256"], info_out["size"]))
            return {"fileName": name, "sha256": str(info_out["sha256"]),
                    "size": int(info_out["size"]), "downloadUrl": url}
        except Exception as e:
            last = str(e)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if attempt < RETRIES:
                log("Modrinth 下载失败（第 %d 次）: %s" % (attempt, e))
                time.sleep(delay)
                delay *= 2
    raise ProviderError(5, "Modrinth 下载失败（已重试 %d 次）: %s" % (RETRIES, last))


def fetch(slug: str, dest_dir: str, game_version: str, loader: str = "neoforge",
          version_pin: str = "latest", base_url: str = BASE_URL,
          log: Callable[[str], None] = print) -> Optional[Dict[str, object]]:
    """解析 + 下载一体；项目不存在 -> None。返回锁字段（[3.12]）与落盘路径。"""
    info = resolve(slug, game_version, loader, version_pin, base_url, log)
    if info is None:
        return None
    got = download_file(info, dest_dir, log)
    out = dict(info)
    out.update(got)
    out["path"] = os.path.join(dest_dir, got["fileName"])
    return out
