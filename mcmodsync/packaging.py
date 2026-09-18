# -*- coding: utf-8 -*-
"""C 端分发包组装与校验 —— [T-50] 步骤1~3（A 端 OP 使用，非 C 端运行时依赖）。

产物结构（默认 dist/client-package/）:
  更新mod.bat                     [12.1] 原文（CRLF）
  更新mod.sh                      [12.2] 原文（LF）
  _updater/mcmodsync.exe          PyInstaller 单文件（Linux 为 mcmodsync-linux）
  _updater/mcmodsync.py           纯 Python 兜底（tools/build_client.py 产物）
  _updater/config.json            [3.6]（由 pack.local.json 渲染）
  SHA256SUMS.txt                  各文件 sha256（sha256sum 格式，按路径排序）

本模块仅使用标准库；被 A 端 CLI `mcmodsync package-client` 与
tools/package_client.py 共用（单一实现来源）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Dict, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# [T-50] 步骤1: 产物中不得出现的第三方/无用模块（与 packaging/mcmodsync.spec 的 excludes 对齐）
BANNED_MODULES = (
    "boto3", "botocore", "s3transfer", "jmespath", "paramiko", "cryptography",
    "cffi", "pycparser", "nacl", "bcrypt", "requests", "urllib3",
    "charset_normalizer", "idna", "certifi", "dateutil", "yaml", "numpy", "PIL",
    "tkinter", "pytest", "setuptools", "pip",
)


class PackagingError(Exception):
    """打包/组装失败。"""


def _die(msg: str) -> None:
    raise PackagingError(msg)


def repo_assets(repo: str = REPO) -> Dict[str, str]:
    return {
        "entries": os.path.join(repo, "entries"),
        "bat": os.path.join(repo, "entries", "更新mod.bat"),
        "sh": os.path.join(repo, "entries", "更新mod.sh"),
        "spec": os.path.join(repo, "packaging", "mcmodsync.spec"),
        "fallback": os.path.join(repo, "client", "mcmodsync.py"),
        "default_out": os.path.join(repo, "dist", "client-package"),
        "config": os.path.join(repo, "pack.local.json"),
        "exe_name": "mcmodsync.exe" if os.name == "nt" else "mcmodsync-linux",
    }


# --------------------------------------------------------------------------
# exe 构建与产物核验
# --------------------------------------------------------------------------

def build_exe(spec: str, workroot: str, name: str,
              log: Callable[[str], None] = print) -> str:
    """用 PyInstaller spec 构建单文件可执行文件，返回其路径。"""
    dist = os.path.join(workroot, "dist")
    work = os.path.join(workroot, "build")
    cmd = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
           "--distpath", dist, "--workpath", work, spec]
    log("构建 exe: %s" % " ".join(cmd))
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(spec))
    if out.returncode != 0:
        tail = (out.stdout or "")[-1500:] + (out.stderr or "")[-1500:]
        _die("PyInstaller 构建失败（退出码 %d）\n%s" % (out.returncode, tail))
    produced = os.path.join(dist, name)
    if not os.path.isfile(produced):
        _die("未找到构建产物: %s" % produced)
    return produced


def bundled_modules(exe_path: str):
    """列出 PyInstaller 单文件产物中打包的模块名；返回 (names, how)，失败时 names 为 None。"""
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception:  # noqa: BLE001
        return None, "PyInstaller 不可用"
    try:
        reader = CArchiveReader(exe_path)
    except Exception as e:  # noqa: BLE001
        return None, "归档读取失败: %s" % e
    names = set(reader.toc.keys())
    for entry in list(reader.toc.keys()):
        fn = getattr(reader, "open_embedded_archive", None)
        if not fn or not entry.startswith("PYZ"):
            continue
        try:
            names |= set(fn(entry).toc.keys())
        except Exception:  # noqa: BLE001
            pass
    return names, "CArchiveReader+PYZ"


def check_bundle(exe_path: str, log: Callable[[str], None] = print) -> List[str]:
    """返回 exe 内检出的应排除模块（排序）；无法读取归档时返回 []。"""
    names, how = bundled_modules(exe_path)
    if names is None:
        log("[WARN] 无法读取 exe 归档（%s），跳过产物内模块核验" % how)
        return []
    roots = {n.split(".")[0].lower() for n in names}
    bad = sorted(r for r in roots if r in BANNED_MODULES)
    log("产物核验（%s）: 归档条目 %d 个，顶层模块 %d 个" % (how, len(names), len(roots)))
    if bad:
        log("  [FAIL] exe 内检出应排除模块: %s" % ", ".join(bad))
    else:
        log("  [OK] exe 内无 boto3/botocore/paramiko/cryptography 等应排除模块")
    return bad


# --------------------------------------------------------------------------
# 配置渲染
# --------------------------------------------------------------------------

def render_config(pack_file: str) -> Dict[str, object]:
    """由 pack.local.json 渲染 [3.6] 的 C 端 config.json。"""
    if not os.path.isfile(pack_file):
        _die("缺少配置文件: %s" % pack_file)
    with open(pack_file, encoding="utf-8") as f:
        pack = json.load(f)
    cli = pack.get("client") or {}
    manifest_url = str(cli.get("manifestUrl") or "").strip()
    public_key = str(cli.get("publicKey") or "").strip()
    pack_id = str(pack.get("packId") or "").strip()
    missing = [n for n, v in (("packId", pack_id), ("client.manifestUrl", manifest_url),
                              ("client.publicKey", public_key)) if not v]
    if missing:
        _die("pack.local.json 缺少字段: %s" % ", ".join(missing))
    return {"schemaVersion": 1, "packId": pack_id,
            "manifestUrl": manifest_url, "publicKey": public_key}


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------

def _clear_dir(path: str, log: Callable[[str], None]) -> None:
    """尽力清空输出目录；个别文件不可删（杀软/沙箱拦截）时改为逐文件覆盖。"""
    try:
        shutil.rmtree(path)
        return
    except OSError as e:
        log("[WARN] 目录清理受限（%s），改为逐文件覆盖" % e)
    for dirpath, dirs, files in os.walk(path, topdown=False):
        for n in files:
            try:
                os.remove(os.path.join(dirpath, n))
            except OSError:
                pass
        for n in dirs:
            try:
                os.rmdir(os.path.join(dirpath, n))
            except OSError:
                pass


def write_sha256sums(root: str) -> str:
    rows = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel == "SHA256SUMS.txt":
                continue
            with open(full, "rb") as f:
                rows.append((rel, hashlib.sha256(f.read()).hexdigest()))
    rows.sort()
    path = os.path.join(root, "SHA256SUMS.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for rel, sha in rows:
            f.write("%s  %s\n" % (sha, rel))
    return path


def assemble(config_path: str, out: str, repo: str = REPO,
             exe: Optional[str] = None, no_exe: bool = False,
             fallback: Optional[str] = None, skip_verify: bool = False,
             log: Callable[[str], None] = print) -> Dict[str, object]:
    """组装分发包；返回 {"out", "files", "exe", "banned", "sums"}。"""
    a = repo_assets(repo)
    out = os.path.abspath(out)

    # 先完成全部前置校验，再做任何破坏性动作/构建（避免「清空目录并构建 exe 后才发现配置缺失」）
    cfg = render_config(config_path)
    fb = fallback or a["fallback"]
    for label, path in (("入口脚本", a["bat"]), ("入口脚本", a["sh"]), (".py 兜底", fb)):
        if not os.path.isfile(path):
            _die("缺少%s: %s" % (label, path))
    if exe and not os.path.isfile(os.path.abspath(exe)):
        _die("指定的 exe 不存在: %s" % exe)
    if not no_exe and not exe and not os.path.isfile(a["spec"]):
        _die("缺少 PyInstaller spec: %s" % a["spec"])

    up = os.path.join(out, "_updater")
    if os.path.isdir(out):
        _clear_dir(out, log)
    os.makedirs(up, exist_ok=True)

    for src in (a["bat"], a["sh"]):
        shutil.copy2(src, os.path.join(out, os.path.basename(src)))

    tmp = None
    exe_src = None
    try:
        if exe:
            exe_src = os.path.abspath(exe)
            log("复用已有 exe: %s" % exe_src)
        elif no_exe:
            log("已指定 no_exe：跳过 exe 构建（仅 .py 兜底）")
        else:
            tmp = tempfile.mkdtemp(prefix="mcmodsync-pkg-")
            exe_src = build_exe(a["spec"], tmp, a["exe_name"], log)
        if exe_src:
            shutil.copy2(exe_src, os.path.join(up, a["exe_name"]))
            log("已放入 _updater/%s（%.1f MiB）"
                % (a["exe_name"],
                   os.path.getsize(os.path.join(up, a["exe_name"])) / 1048576.0))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    shutil.copy2(fb, os.path.join(up, "mcmodsync.py"))

    with open(os.path.join(up, "config.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    sums = write_sha256sums(out)

    banned: List[str] = []
    packed = os.path.join(up, a["exe_name"])
    if os.path.isfile(packed) and not skip_verify:
        banned = check_bundle(packed, log)

    files = []
    for dirpath, _dirs, names in os.walk(out):
        for n in sorted(names):
            full = os.path.join(dirpath, n)
            files.append((os.path.relpath(full, out).replace(os.sep, "/"),
                          os.path.getsize(full)))
    return {"out": out, "files": sorted(files), "exe": packed if os.path.isfile(packed) else "",
            "banned": banned, "sums": sums, "config": cfg}
