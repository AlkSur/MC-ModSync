# -*- coding: utf-8 -*-
"""组装 C 端分发包 —— [T-50] 步骤1~3。

产物结构（dist/client-package/）:
  更新mod.bat                     [12.1] 原文（CRLF）
  更新mod.sh                      [12.2] 原文（LF）
  _updater/mcmodsync.exe          PyInstaller 单文件（Linux 为 mcmodsync-linux）
  _updater/mcmodsync.py           纯 Python 兜底（tools/build_client.py 产物）
  _updater/config.json            [3.6]（由 pack.local.json 渲染）
  SHA256SUMS.txt                  各文件 sha256（sha256sum 格式，按路径排序）

用法:
  python tools/package_client.py                       # 构建 exe 并组装
  python tools/package_client.py --no-exe              # 跳过 exe（无 PyInstaller 时）
  python tools/package_client.py --exe <已有 exe 路径>   # 复用已构建的 exe
退出码: 0 成功；1 失败。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRIES = os.path.join(REPO, "entries")
SPEC = os.path.join(REPO, "packaging", "mcmodsync.spec")
FALLBACK = os.path.join(REPO, "client", "mcmodsync.py")
BAT = os.path.join(ENTRIES, "更新mod.bat")
SH = os.path.join(ENTRIES, "更新mod.sh")
DEFAULT_OUT = os.path.join(REPO, "dist", "client-package")
EXE_NAME = "mcmodsync.exe" if os.name == "nt" else "mcmodsync-linux"

# [T-50] 步骤1: 产物中不得出现的第三方/无用模块（与 spec 的 excludes 对齐）
BANNED_MODULES = (
    "boto3", "botocore", "s3transfer", "jmespath", "paramiko", "cryptography",
    "cffi", "pycparser", "nacl", "bcrypt", "requests", "urllib3",
    "charset_normalizer", "idna", "certifi", "dateutil", "yaml", "numpy", "PIL",
    "tkinter", "pytest", "setuptools", "pip",
)


def die(msg: str) -> None:
    raise SystemExit("错误: " + msg)


def bundled_modules(exe_path: str):
    """列出 PyInstaller 单文件产物中打包的模块名（含内嵌 PYZ）。

    返回 (names:set, how:str)；读取失败时 names 为 None。
    """
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
        if not entry.startswith("PYZ"):
            continue
        for api in ("open_embedded_archive",):
            fn = getattr(reader, api, None)
            if fn is None:
                continue
            try:
                inner = fn(entry)
                names |= set(inner.toc.keys())
            except Exception:  # noqa: BLE001
                pass
    return names, "CArchiveReader+PYZ"


def build_exe(workroot: str) -> str:
    """用 packaging/mcmodsync.spec 构建单文件可执行文件，返回其路径。"""
    dist = os.path.join(workroot, "dist")
    work = os.path.join(workroot, "build")
    cmd = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
           "--distpath", dist, "--workpath", work, SPEC]
    print("构建 exe: %s" % " ".join(cmd))
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)
    if out.returncode != 0:
        sys.stderr.write(out.stdout[-2000:])
        sys.stderr.write(out.stderr[-2000:])
        die("PyInstaller 构建失败（退出码 %d）" % out.returncode)
    produced = os.path.join(dist, EXE_NAME)
    if not os.path.isfile(produced):
        die("未找到构建产物: %s" % produced)
    return produced


def render_config(pack_file: str) -> dict:
    """由 pack.local.json 渲染 [3.6] config.json。"""
    if not os.path.isfile(pack_file):
        die("缺少配置文件: %s" % pack_file)
    with open(pack_file, encoding="utf-8") as f:
        pack = json.load(f)
    cli = pack.get("client") or {}
    manifest_url = (cli.get("manifestUrl") or "").strip()
    public_key = (cli.get("publicKey") or "").strip()
    pack_id = (pack.get("packId") or "").strip()
    if not manifest_url or not public_key or not pack_id:
        die("pack.local.json 缺少 packId / client.manifestUrl / client.publicKey")
    return {"schemaVersion": 1, "packId": pack_id,
            "manifestUrl": manifest_url, "publicKey": public_key}


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


def _clear_dir(path: str) -> None:
    """尽力清空输出目录；个别文件不可删（杀软/沙箱拦截）时继续并提示。"""
    try:
        shutil.rmtree(path)
        return
    except OSError as e:
        print("[WARN] 目录清理受限（%s），改为逐文件覆盖" % e)
    for dirpath, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(dirpath, name))
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(dirpath, name))
            except OSError:
                pass


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="组装 C 端分发包")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--config", default=os.path.join(REPO, "pack.local.json"))
    ap.add_argument("--exe", default="")
    ap.add_argument("--no-exe", action="store_true")
    ap.add_argument("--fallback", default=FALLBACK)
    ap.add_argument("--skip-verify", action="store_true", help="跳过产物内模块核验")
    args = ap.parse_args(argv[1:])

    out = os.path.abspath(args.out)
    up = os.path.join(out, "_updater")
    if os.path.isdir(out):
        _clear_dir(out)
    os.makedirs(up, exist_ok=True)

    # 1) 入口脚本
    for src in (BAT, SH):
        if not os.path.isfile(src):
            die("缺少入口脚本: %s" % src)
        shutil.copy2(src, os.path.join(out, os.path.basename(src)))

    # 2) exe
    tmp = None
    exe_dst = None
    try:
        if args.exe:
            exe_dst = os.path.abspath(args.exe)
            print("复用已有 exe: %s" % exe_dst)
        elif args.no_exe:
            print("已指定 --no-exe：跳过 exe 构建（仅 .py 兜底）")
        else:
            tmp = tempfile.mkdtemp(prefix="mcmodsync-pkg-")
            exe_dst = build_exe(tmp)
        if exe_dst:
            shutil.copy2(exe_dst, os.path.join(up, EXE_NAME))
            print("已放入 _updater/%s（%.1f MiB）"
                  % (EXE_NAME, os.path.getsize(os.path.join(up, EXE_NAME)) / 1048576.0))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    # 3) .py 兜底
    if not os.path.isfile(args.fallback):
        die("缺少 .py 兜底（先运行 tools/build_client.py）: %s" % args.fallback)
    shutil.copy2(args.fallback, os.path.join(up, "mcmodsync.py"))

    # 4) config.json（[3.6]）
    cfg = render_config(args.config)
    with open(os.path.join(up, "config.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    # 5) SHA256SUMS.txt
    sums = write_sha256sums(out)

    # 6) 产物核验: exe 内不得含 A/B 端第三方依赖
    bad = []
    packed = os.path.join(up, EXE_NAME)
    if os.path.isfile(packed) and not args.skip_verify:
        names, how = bundled_modules(packed)
        if names is None:
            print("\n[WARN] 无法读取 exe 归档（%s），跳过产物内模块核验" % how)
        else:
            roots = {n.split(".")[0].lower() for n in names}
            bad = sorted(r for r in roots if r in BANNED_MODULES)
            print("\n产物核验（%s）: 归档条目 %d 个，顶层模块 %d 个" % (how, len(names), len(roots)))
            if bad:
                print("  [FAIL] exe 内检出应排除模块: %s" % ", ".join(bad))
            else:
                print("  [OK] exe 内无 boto3/botocore/paramiko/cryptography 等应排除模块")

    print("\n分发包: %s" % out)
    for dirpath, _dirs, files in os.walk(out):
        for name in sorted(files):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, out).replace(os.sep, "/")
            print("  %-28s %9d 字节" % (rel, os.path.getsize(full)))
    print("校验和: %s" % os.path.relpath(sums, REPO))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
