"""CurseForge provider（纯 urllib + x-api-key 头）—— [T-32]。

API 约定:
  搜索:     GET https://api.curseforge.com/v1/mods/search?gameId=432&slug=<slug>
            （gameId=432 = Minecraft）
  文件列表: GET /v1/mods/{modId}/files?gameVersion=<mc版本>&modLoaderType=6
            （modLoaderType=6 = NeoForge）
  鉴权:     所有请求带 x-api-key: <curseforge.apiKey>（来自配置；日志中打码）
  下载:     取 file.downloadUrl；为 null 即「禁第三方下载」-> 不报错中断，
            记入 manual-needed 清单（交由 T-34 人工闭环）。
  401/403:  提示 key 失效并给出重新申请指引，跳过 CurseForge 源，其余继续。
"""
from __future__ import annotations

import concurrent.futures as _fut  # noqa: F401  (占位导入，保持与其它 provider 结构一致)
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional

from .logutil import mask

BASE_URL = "https://api.curseforge.com/v1"
USER_AGENT = "MC-ModSync/2.0 (neoforge mods sync; contact: op@example.com)"
GAME_ID_MINECRAFT = 432
MOD_LOADER_NEOFORGE = 6
TIMEOUT = 30
RETRIES = 3

KEY_HELP = ("CurseForge apiKey 失效（401/403）。请到 https://console.curseforge.com/ 重新申请"
            " 并更新配置 curseforge.apiKey（PRE-5）。本轮将跳过 CurseForge 源，其余 mod 继续。")


class ProviderError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class KeyInvalidError(ProviderError):
    """apiKey 失效（401/403）。"""

    def __init__(self, message: str = KEY_HELP) -> None:
        super().__init__(5, message)


def _get_json(url: str, api_key: str, retries: int = RETRIES,
              log: Callable[[str], None] = print) -> Optional[dict]:
    """GET JSON；404 -> None；401/403 -> KeyInvalidError；429 -> 指数退避重试。"""
    if not api_key:
        raise KeyInvalidError("未配置 curseforge.apiKey（PRE-5）")
    delay = 1.0
    last = ""
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={
            "x-api-key": api_key,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (401, 403):
                log(KEY_HELP)
                raise KeyInvalidError()
            if e.code == 429 and attempt < retries:
                log("CurseForge 429 限流（key=%s），%.1fs 后重试（第 %d 次）"
                    % (mask(api_key), delay, attempt))
                time.sleep(delay)
                delay *= 2
                last = "HTTP 429"
                continue
            last = "HTTP %s" % e.code
            break
        except Exception as e:
            last = str(e)
            if attempt < retries:
                log("CurseForge 请求失败（第 %d 次）: %s" % (attempt, e))
                time.sleep(delay)
                delay *= 2
                continue
            break
    raise ProviderError(5, "CurseForge 请求失败（已重试 %d 次）: %s" % (retries, last))


def search_mod(slug: str, api_key: str, base_url: str = BASE_URL,
               log: Callable[[str], None] = print) -> Optional[dict]:
    """按 slug 搜索 Minecraft mod；无结果 -> None。"""
    url = "%s/mods/search?%s" % (base_url.rstrip("/"),
                                 urllib.parse.urlencode({"gameId": GAME_ID_MINECRAFT,
                                                         "slug": slug}))
    data = _get_json(url, api_key, log=log)
    if not data:
        return None
    items = data.get("data") or []
    if not items:
        return None
    for it in items:                       # slug 精确匹配优先
        if (it.get("slug") or "").lower() == slug.lower():
            return it
    return items[0]


def list_files(mod_id: int, game_version: str, api_key: str,
               loader_type: int = MOD_LOADER_NEOFORGE, base_url: str = BASE_URL,
               log: Callable[[str], None] = print) -> List[dict]:
    """列出该 mod 在指定 MC 版本 + NeoForge 下的文件（按 fileDate 倒序）。"""
    qs = urllib.parse.urlencode({"gameVersion": game_version, "modLoaderType": loader_type})
    url = "%s/mods/%s/files?%s" % (base_url.rstrip("/"), int(mod_id), qs)
    data = _get_json(url, api_key, log=log)
    if not data:
        return []
    files = list(data.get("data") or [])
    files.sort(key=lambda f: f.get("fileDate") or "", reverse=True)
    return files


def pick_file(files: List[dict], version_pin: str = "latest") -> Optional[dict]:
    """versionPin 为具体版本时按 displayName/fileName 包含匹配；否则取最新。"""
    if not files:
        return None
    if not version_pin or version_pin in ("latest", "manual"):
        return files[0]
    for f in files:
        if version_pin in (f.get("displayName") or "") or version_pin in (f.get("fileName") or ""):
            return f
    return None


def project_url(slug: str) -> str:
    return "https://www.curseforge.com/minecraft/mc-mods/%s" % slug


def resolve(slug: str, game_version: str, api_key: str, version_pin: str = "latest",
            base_url: str = BASE_URL, log: Callable[[str], None] = print) -> Optional[Dict[str, object]]:
    """解析下载信息；项目不存在 -> None。

    downloadUrl 为空 -> 返回 manualNeeded=True（不抛错），由 fetcher 记入 manual-needed。
    """
    mod = search_mod(slug, api_key, base_url, log)
    if mod is None:
        log("CurseForge: 项目不存在 slug=%s" % slug)
        return None
    mod_id = mod.get("id")
    files = list_files(mod_id, game_version, api_key, base_url=base_url, log=log)
    f = pick_file(files, version_pin)
    if f is None:
        log("CurseForge: 未找到指定版本 %s（slug=%s）" % (version_pin, slug))
        return None
    url = f.get("downloadUrl")
    manual = not url
    return {
        "resolvedVersion": f.get("displayName") or f.get("fileName"),
        "fileName": f.get("fileName"),
        "downloadUrl": url or "",
        "size": int(f.get("fileLength") or 0),
        "modId": mod_id,
        "fileId": f.get("id"),
        "source": "curseforge",
        "manualNeeded": manual,
        "projectUrl": project_url(slug),
    }


def download_file(info: Dict[str, object], dest_dir: str,
                  log: Callable[[str], None] = print) -> Dict[str, object]:
    """下载到 dest_dir/<fileName> 并实算 sha256/size；downloadUrl 为空 -> ProviderError(5)。"""
    from . import hashing

    url = str(info.get("downloadUrl") or "")
    name = str(info.get("fileName") or "")
    if not url:
        raise ProviderError(5, "CurseForge 禁第三方下载（downloadUrl 为空）: %s" % name)
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
            got = hashing.hash_file(dst)
            log("CurseForge 下载完成: %s (sha256=%s size=%d)"
                % (name, got["sha256"], got["size"]))
            return {"fileName": name, "sha256": str(got["sha256"]), "size": int(got["size"]),
                    "downloadUrl": url}
        except Exception as e:
            last = str(e)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if attempt < RETRIES:
                log("CurseForge 下载失败（第 %d 次）: %s" % (attempt, e))
                time.sleep(delay)
                delay *= 2
    raise ProviderError(5, "CurseForge 下载失败（已重试 %d 次）: %s" % (RETRIES, last))


def fetch(slug: str, dest_dir: str, game_version: str, api_key: str,
          version_pin: str = "latest", base_url: str = BASE_URL,
          log: Callable[[str], None] = print) -> Optional[Dict[str, object]]:
    """解析 + 下载；项目不存在 -> None；禁第三方下载 -> 返回 manualNeeded=True 的条目。"""
    info = resolve(slug, game_version, api_key, version_pin, base_url, log)
    if info is None:
        return None
    if info.get("manualNeeded"):
        return info
    got = download_file(info, dest_dir, log)
    out = dict(info)
    out.update(got)
    out["path"] = os.path.join(dest_dir, got["fileName"])
    return out
