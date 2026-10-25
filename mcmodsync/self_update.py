"""Signed updater delivery; the launcher installs only after the old process exits.

No configuration, mod state, world, or mod backup is included in this transaction.
The existing mod-manifest public key also authenticates updater releases.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
from typing import Callable, Optional

from . import __version__, canonicaljson, client, hashing, http_download, locking, manifest, paths

EXIT_RESTART = 42
RELEASE_KEY = "updater.json"
FILES = {"windows": "mcmodsync.exe", "linux": "mcmodsync-linux", "python": "mcmodsync.py"}
MAX_SIZE = 128 * 1024 * 1024
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")


def stage_dir(target: str) -> str:
    return paths.safe_join(target, "_updater/self-update")


def _release_path(target: str) -> str:
    return paths.safe_join(target, "_updater/self-update/release.json")


def validate_release(doc: dict, cfg: dict) -> None:
    if not isinstance(doc, dict) or not canonicaljson.verify(doc, str(cfg["publicKey"])):
        raise client.ClientError(client.EXIT_SIGNATURE, "更新器发布清单验签失败，已停止")
    if doc.get("schemaVersion") != 1 or doc.get("packId") != cfg["packId"]:
        raise client.ClientError(client.EXIT_PACKID, "更新器发布清单不属于当前整合包或格式不支持")
    if not isinstance(doc.get("version"), str) or not _VERSION.fullmatch(doc["version"]):
        raise client.ClientError(client.EXIT_GENERIC, "更新器版本格式不合法")
    artifacts = doc.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts or set(artifacts).difference(FILES):
        raise client.ClientError(client.EXIT_GENERIC, "更新器文件清单不合法")
    for kind, row in artifacts.items():
        if not isinstance(row, dict):
            raise client.ClientError(client.EXIT_GENERIC, "更新器文件信息不合法")
        sha, size = row.get("sha256"), row.get("size")
        if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
                or type(size) is not int or not 0 < size <= MAX_SIZE
                or row.get("path") != "updater/files/%s/%s" % (sha, FILES[kind])):
            raise client.ClientError(client.EXIT_GENERIC, "更新器下载路径或校验信息不合法")


def _kinds(doc: dict, frozen: Optional[bool] = None) -> list:
    native = getattr(sys, "frozen", False) if frozen is None else frozen
    required = ("windows" if os.name == "nt" else "linux") if native else "python"
    if required not in doc["artifacts"]:
        raise client.ClientError(client.EXIT_GENERIC, "已发布更新器 %s，但缺少当前运行方式的程序（%s）"
                                 % (doc["version"], required))
    # 本地有备用 Python 入口时一起更新，防止两个入口版本不一致。
    return [required] + (["python"] if native and "python" in doc["artifacts"] else [])


def _verify(path: str, row: dict) -> bool:
    return os.path.isfile(path) and hashing.hash_file(path) == {"sha256": row["sha256"], "size": row["size"]}


def check(target: str, log: Callable[[str], None] = print) -> int:
    """0 = continue mod sync; 42 = verified candidate staged, launcher must install."""
    target = os.path.abspath(target)
    try:
        cfg = client.load_config(target)
        os.makedirs(client.updater_dir(target), exist_ok=True)
        with locking.FileLock(client.lock_path(target)):
            url = client.blob_base_of(str(cfg["manifestUrl"])) + "/" + RELEASE_KEY
            log("检查更新器版本（当前 %s）..." % __version__)
            try:
                doc = client.fetch_json(url, cache_bust=True, timeout=5)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    log("服主尚未发布更新器更新，继续检查 mod。")
                    return 0
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                log("暂时无法检查更新器版本，使用当前版本继续检查 mod（%s）。" % error)
                return 0
            validate_release(doc, cfg)
            if client._version_key(doc["version"]) <= client._version_key(__version__):
                log("更新器已是最新版本。")
                return 0
            kinds = _kinds(doc)
            log("发现更新器 %s，先更新程序，再更新 mod。" % doc["version"])
            # 不接受清单中的任意 URL；文件地址只能从已配置的整合包源构造。
            base = client.blob_base_of(str(cfg["manifestUrl"]))
            stage = stage_dir(target)
            os.makedirs(stage, exist_ok=True)
            # 防止上一次准备成功的清单在这次下载失败后被启动脚本误用。
            manifest.write_json(_release_path(target), {})
            for kind in kinds:
                row = doc["artifacts"][kind]
                dst = paths.safe_join(target, "_updater/self-update/" + FILES[kind])
                http_download.download(base + "/" + row["path"], dst, row["sha256"], row["size"],
                                       retries=2, segments=4, log=log)
                if kind == "linux":
                    os.chmod(dst, 0o755)
            manifest.write_json(_release_path(target), doc)
            log("新版更新器已下载并通过签名及 SHA-256 校验，正在切换版本。")
            return EXIT_RESTART
    except locking.LockHeld as error:
        log(str(error))
        return client.EXIT_LOCK
    except (client.ClientError, http_download.DownloadError) as error:
        log("更新器更新失败: %s" % error)
        return error.exit_code
    except Exception as error:
        log("更新器更新失败: %s" % error)
        return client.EXIT_NETWORK


def _copy_with_retry(src: str, dst: str) -> None:
    for attempt in range(20):
        try:
            manifest.atomic_copy(src, dst)
            return
        except OSError:
            if attempt == 19:
                raise
            time.sleep(0.2)


def apply(target: str, log: Callable[[str], None] = print) -> int:
    """Run from the staged NEW runtime, after launcher has waited for the old one."""
    target = os.path.abspath(target)
    try:
        cfg = client.load_config(target)
        with locking.FileLock(client.lock_path(target)):
            with open(_release_path(target), encoding="utf-8") as stream:
                doc = json.load(stream)
            validate_release(doc, cfg)
            if doc["version"] != __version__:
                raise client.ClientError(client.EXIT_GENERIC, "候选更新器版本与发布清单不一致，保留原程序")
            kinds = _kinds(doc)
            sources = []
            for kind in kinds:
                src = paths.safe_join(target, "_updater/self-update/" + FILES[kind])
                dst = paths.safe_join(target, "_updater/" + FILES[kind])
                if not _verify(src, doc["artifacts"][kind]):
                    raise client.ClientError(client.EXIT_SIGNATURE, "新版更新器文件校验失败，保留原程序")
                sources.append((kind, src, dst))
            # 每个文件先备份，再替换；失败恢复已经替换的文件。
            backups = []
            try:
                for kind, src, dst in sources:
                    backup = paths.safe_join(target, "_updater/self-update/backup/" + FILES[kind])
                    existed = os.path.isfile(dst)
                    if existed:
                        manifest.atomic_copy(dst, backup)
                    backups.append((dst, backup, existed))
                    _copy_with_retry(src, dst)
                    if kind == "linux":
                        os.chmod(dst, 0o755)
                    if not _verify(dst, doc["artifacts"][kind]):
                        raise OSError("替换后的更新器校验失败")
            except Exception:
                for dst, backup, existed in reversed(backups):
                    if existed:
                        _copy_with_retry(backup, dst)
                    elif os.path.isfile(dst):
                        os.remove(dst)
                raise
            log("更新器已升级至 %s，即将使用新版继续更新 mod。" % __version__)
            return 0
    except locking.LockHeld as error:
        log(str(error))
        return client.EXIT_LOCK
    except client.ClientError as error:
        log("更新器安装失败: %s" % error)
        return error.exit_code
    except Exception as error:
        log("更新器安装失败，已尝试恢复旧程序: %s" % error)
        return client.EXIT_GENERIC
