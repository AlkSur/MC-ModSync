"""A-side publishing for signed self-updater artifacts, separate from mod releases."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from . import canonicaljson, hashing, publisher, self_update


class UpdaterPublishError(RuntimeError):
    pass


def publish_updater(cfg, store, package_dir: str, dry_run: bool = False, log=print) -> int:
    package_dir = os.path.abspath(package_dir)
    try:
        with open(os.path.join(package_dir, "_updater", "build-info.json"), encoding="utf-8") as stream:
            info = json.load(stream)
        with open(os.path.join(package_dir, "_updater", "config.json"), encoding="utf-8") as stream:
            package_cfg = json.load(stream)
        if (package_cfg.get("packId") != cfg["packId"]
                or package_cfg.get("publicKey") != cfg["client"]["publicKey"]):
            raise UpdaterPublishError("分发包的 packId / 公钥与发布配置不一致")
        version = info.get("version", "")
        if not isinstance(version, str) or not self_update._VERSION.fullmatch(version):
            raise UpdaterPublishError("分发包版本不合法，请重新打包")
        artifacts, local = {}, {}
        for kind, name in self_update.FILES.items():
            path = os.path.join(package_dir, "_updater", name)
            if not os.path.isfile(path):
                continue
            # 不能把旧 exe 或旧备用脚本冠以新版本号发布。
            if kind == "python" or (kind == "windows") == (os.name == "nt"):
                command = ([sys.executable, path] if kind == "python" else [path]) + ["--version"]
                result = subprocess.run(command, capture_output=True, timeout=30,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode or result.stdout.decode("utf-8", "replace").strip() != "MC-ModSync " + version:
                    raise UpdaterPublishError("%s 的实际版本与 build-info.json 不一致，请重新构建" % name)
            row = hashing.hash_file(path)
            if not 0 < row["size"] <= self_update.MAX_SIZE:
                raise UpdaterPublishError("更新器文件大小不合法: " + name)
            row["path"] = "updater/files/%s/%s" % (row["sha256"], name)
            artifacts[kind] = row
            local[kind] = path
        if "python" not in artifacts:
            raise UpdaterPublishError("缺少 Python 备用更新器，请重新打包")
        doc = {"schemaVersion": 1, "packId": cfg["packId"], "version": version,
               "publishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "artifacts": artifacts}
        # 程序版本与内容清单版本相互独立；禁止同一程序版本静默覆盖不同的构建。
        if store.head(self_update.RELEASE_KEY):
            old = json.loads(store.get_text(self_update.RELEASE_KEY))
            self_update.validate_release(old, {"packId": cfg["packId"], "publicKey": cfg["client"]["publicKey"]})
            if publisher_version_key(old["version"]) > publisher_version_key(version):
                raise UpdaterPublishError("拒绝发布比线上更旧的更新器")
            if old["version"] == version:
                for kind, row in artifacts.items():
                    if kind in old["artifacts"] and old["artifacts"][kind] != row:
                        raise UpdaterPublishError("同一更新器版本已发布过不同文件，请先提升程序版本再构建")
                doc["artifacts"] = {**old["artifacts"], **artifacts}
        log("更新器发布预览: version=%s，文件 %d 个" % (version, len(artifacts)))
        for kind, row in artifacts.items():
            log("  %s: %.1f MiB" % (self_update.FILES[kind], row["size"] / 1048576.0))
        if dry_run:
            log("[dry-run] 未上传任何文件；正式发布会先上传程序，最后写入签名 updater.json。")
            return 0
        pem = publisher._load_private_key_pem(cfg)
        signed = canonicaljson.sign(doc, pem)
        self_update.validate_release(signed, {"packId": cfg["packId"], "publicKey": cfg["client"]["publicKey"]})
        for kind, path in local.items():
            store.put_file(artifacts[kind]["path"], path, "public,max-age=31536000,immutable")
        store.put_bytes(self_update.RELEASE_KEY, canonicaljson.canonical(signed), "no-cache,no-store,must-revalidate")
        log("更新器 %s 发布完成。玩家下次双击更新mod时会先升级程序。" % version)
        return 0
    except UpdaterPublishError:
        raise
    except Exception as error:
        raise UpdaterPublishError(str(error)) from error


def publisher_version_key(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))
