# -*- coding: utf-8 -*-
"""生成三端发布包（解压即用）。

三端包名统一使用**系统版本**（与 pyproject.toml 一致）。
已发布的 mod 清单版本只写在包内说明里，不参与包名——两者语义不同。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "dist", "release")
VER = "2.1.0"          # 系统版本（三端统一），与 pyproject.toml 保持一致


def current_pack_version():
    """已发布的 mod 清单版本（内容版本，与系统版本无关）。"""
    p = os.path.join(REPO, "client-publish-state.json")
    try:
        return json.load(open(p, encoding="utf-8")).get("version") or "未知"
    except Exception:
        return "未知"

# ---- A 端（OP 工具）----
A_ROOT = "MC-ModSync-A端-OP工具"
A_FILES = [
    "pyproject.toml",
    "pack.example.json",
    "mods.lock.json",
    "README.md",
    ".gitignore",
    "server/mcmodsync-b.py",
    "client/mcmodsync.py",
    "packaging/mcmodsync.spec",
    "entries/更新mod.bat",
    "entries/更新mod.sh",
]
A_DIRS = ["mcmodsync", "docs"]
A_TOOLS = ["gen_lock.py", "build_server.py", "build_client.py",
           "package_client.py", "check_c_deps.py", "check_config.py"]

A_README = """MC-ModSync · A 端（OP 工具）
================================

【这是什么】
  服主/OP 用的命令行工具：从平台拉 mod、推送服务端、发布客户端到对象存储。

【第一步：看教程】
  docs\\OP端使用教程.md      ← 从这里开始，讲清每一步怎么敲

【第二步：装环境】（需要 Python 3.10 以上，只需做一次）
  python -m venv .venv
  .\\.venv\\Scripts\\python.exe -m pip install -e .

【第三步：配置】
  1) 把 pack.example.json 复制一份，改名 pack.local.json，填好里面的
     服务器地址 / SSH / 七彩云 AK-SK（详见 docs\\管理员部署指南.md）
  2) 生成签名密钥：mcmodsync keygen
     把输出的公钥填进 pack.local.json 的 client.publicKey
  3) 自检：mcmodsync doctor

【目录里都是什么】
  mcmodsync\\            A 端程序本体（不要删）
  server\\               B 端脚本，推送时会自动传到服务器（不要删）
  client\\               玩家端兜底源码，打包玩家端时用（不要删）
  packaging\\ entries\\  打包玩家端时用（不要删）
  tools\\                gen_lock.py 等运维脚本
  docs\\                 全部文档
  pack.example.json     配置模板
  mods.lock.json        客户端 mod 清单（初始为空）

【打包三端发布包】（一条命令）
  1) 先装一次 dev 依赖（含 PyInstaller，打包玩家端 exe 需要）：
       .\\.venv\\Scripts\\python.exe -m pip install -e ".[dev]"
  2) 生成三端包：
       .\\.venv\\Scripts\\python.exe tools\\make_release.py --build
     产物在 dist\\release\\ 下（A 端 / B 端 / C 端 三个 zip）。
     只改了教程、没动 C 端代码时可省掉 --build。

【包里没有的东西】
  你的 pack.local.json、私钥、server-mods\\、client-mods\\ —— 这些是你自己的数据，
  不随包分发。换电脑时记得单独带走 pack.local.json 和 ~/.mcmodsync/private.key。
"""

# ---- B 端（服务端脚本）----
B_ROOT = "MC-ModSync-B端-服务端脚本"
B_README = """MC-ModSync · B 端（服务端脚本）
================================

【这是什么】
  住在 MC 服务器上的一个小脚本，单文件、只用 Python 标准库、不常驻。

【你需要做什么】
  基本上什么都不用做——OP 执行 mcmodsync push-server 时，会自动把它上传到
  服务器并自动升级。服务器上只要装了 python3 就行（不需要 pip 装任何东西）。

  只有这两种情况才需要手工放：
    1) 服务器没装 python3        -> 先 apt install python3 / yum install python3
    2) OP 的 SSH 账号没写权限     -> 手工复制到有权限的目录，让 OP 改 remoteBPath

  手工放置命令示例：
    scp mcmodsync-b.py root@你的服务器:/www/mcmodsync-b.py

【看详细说明】
  docs\\B端使用教程.md

【包里就一个文件】
  mcmodsync-b.py      复制到服务器即可，不用建目录、不用改内容、不用配置
"""

# ---- C 端（玩家更新器）----
C_README = """MC-ModSync · C 端（玩家更新器）
================================

【怎么用】
  1. 关掉游戏
  2. 双击「更新mod.bat」
  3. 看到「=== 更新完成，请手动启动 PCL2 或 HMCL ===」后，自己打开启动器进游戏

【放哪里】（重要）
  把本压缩包里的全部内容解压到【游戏实例目录】，也就是有 mods\\ 文件夹的那一层：

      <游戏实例目录>\\
      ├── mods\\              ← 你原有的 mod 文件夹
      ├── 更新mod.bat         ← 放这里
      └── _updater\\           ← 整个文件夹放这里

  常见位置：.minecraft\\ ，或开了版本隔离时的 .minecraft\\versions\\<版本号>\\
  判断标准：哪一层有 mods\\，就放哪一层。不要放进 mods\\ 里面。

【不用配置】
  _updater\\config.json 已由服主填好（下载地址、包标识、公钥），你不需要改任何东西，
  也不需要手动下载任何 mod。

【出问题了】
  详细说明和退出码对照表见 C端使用教程.md

【版本说明】（两个版本是两回事，别搞混）
  更新器版本   ：2.1.0   —— 本包（程序）的版本，三端统一
  内容清单版本 ：{{PACKVER}}   —— 服主已发布的 mod 清单版本，由服主发布时决定
  更新器只看内容清单版本决定要不要更新 mod，两者互不影响。
"""


def open_zip(path):
    """打开待写 zip；文件被占用（预览/杀软句柄未释放）时自动换名，不中断打包。"""
    try:
        return path, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9)
    except PermissionError:
        alt = path[:-4] + "-" + time.strftime("%H%M%S") + ".zip"
        print("  [警告] %s 被占用，改写到 %s" % (os.path.basename(path),
                                                os.path.basename(alt)))
        return alt, zipfile.ZipFile(alt, "w", zipfile.ZIP_DEFLATED, compresslevel=9)


# 确定性打包：条目时间戳固定，内容不变则 zip 字节与 SHA256 不变（便于发布校验）
FIXED_DT = (1980, 1, 1, 0, 0, 0)


def _write(zf, full_path, arc_name, data=None):
    zi = zipfile.ZipInfo(arc_name, date_time=FIXED_DT)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = 0o644 << 16
    if data is None:
        with open(full_path, "rb") as f:
            data = f.read()
    zf.writestr(zi, data)


def add_dir(zf, root, rel_dir, arc_prefix, exclude=()):
    """递归添加目录（跳过 __pycache__ / .pyc / exclude 中的相对路径）。"""
    base = os.path.join(root, rel_dir)
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", ".idea")]
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel in exclude:
                continue
            _write(zf, full, arc_prefix + rel)


def add_file(zf, root, rel, arc_prefix=""):
    full = os.path.join(root, rel)
    _write(zf, full, arc_prefix + rel.replace(os.sep, "/"))


def add_text(zf, arc_name, text):
    _write(zf, None, arc_name, text.replace("\n", "\r\n").encode("utf-8"))


def _client_watched_files():
    """真正影响 C 端成品的文件：C 端载荷模块 + spec + 入口模板 + 配置。

    模块清单向 build_client.py 询问（--print-modules），所以以后新增 C 端模块
    会自动被纳入监视，不需要改这里。
    """
    watched = [
        os.path.join(REPO, "packaging", "mcmodsync.spec"),
        os.path.join(REPO, "pack.local.json"),
        os.path.join(REPO, "entries", "更新mod.bat"),
        os.path.join(REPO, "entries", "更新mod.sh"),
    ]
    bc = os.path.join(REPO, "tools", "build_client.py")
    try:
        r = subprocess.run([sys.executable, bc, "--print-modules"],
                           cwd=REPO, capture_output=True, text=True)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].endswith(".py"):
                watched.append(os.path.join(REPO, parts[1]))
    except Exception:
        pass
    return [p for p in watched if os.path.isfile(p)]


def client_package_stale():
    """玩家端产物是否已过期（C 端相关源文件比已构建的 exe 新）。

    只打 zip 不会重新编译 exe，所以这些文件更新后必须重建，否则 C 端 zip 里
    装的仍是旧程序。仅改文档 / A 端代码不会触发。
    """
    exe = os.path.join(REPO, "dist", "client-package", "_updater", "mcmodsync.exe")
    if not os.path.isfile(exe):
        return True, "尚未构建"
    built = os.path.getmtime(exe)
    newest, newest_file = built, ""
    for p in _client_watched_files():
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if m > newest:
            newest, newest_file = m, os.path.relpath(p, REPO)
    if newest > built:
        return True, "较新的源文件: %s" % newest_file
    return False, ""


def main():
    ap = argparse.ArgumentParser(
        description="生成 A/B/C 三端发布包（解压即用）")
    ap.add_argument("--build", action="store_true",
                    help="强制先构建玩家端（等价于 mcmodsync package-client），再打三包")
    ap.add_argument("--no-build", action="store_true",
                    help="即使检测到玩家端产物过期也不重新构建（只打 zip，可能打进旧产物）")
    args = ap.parse_args()

    stale, why = client_package_stale()
    if stale and args.no_build:
        print("[警告] 玩家端产物已过期（%s），本次按 --no-build 跳过构建，"
              "C 端 zip 里可能是旧程序。" % why)
    elif args.build or stale:
        if args.build:
            print("=== 先构建玩家端：python -m mcmodsync package-client ===")
        else:
            print("=== 检测到玩家端产物已过期（%s），自动重新构建 ===" % why)
            print("    （只改文档/A 端代码时不会触发；要跳过可用 --no-build）")
        r = subprocess.run([sys.executable, "-m", "mcmodsync", "package-client"], cwd=REPO)
        if r.returncode != 0:
            print("player build failed (exit %d). "
                  "if it says 'No module named PyInstaller', run: "
                  'pip install -e ".[dev]"' % r.returncode)
            return r.returncode
        print()
    else:
        print("玩家端产物是最新的，跳过构建（--build 可强制重建）")

    src_check = os.path.join(REPO, "dist", "client-package")
    if not os.path.isdir(src_check):
        print("missing %s -- run 'mcmodsync package-client' first, "
              "or use: python tools/make_release.py --build" % src_check)
        return 1

    os.makedirs(OUT, exist_ok=True)
    # 清掉旧版本包，避免新旧版本混在同一个目录里
    for old in os.listdir(OUT):
        if old.endswith(".zip"):
            try:
                os.remove(os.path.join(OUT, old))
            except OSError:
                pass   # 被占用/沙箱限制时跳过，不中断打包

    # ---------- A 端 ----------
    p, _z = open_zip(os.path.join(OUT, "MC-ModSync-A端-OP工具-v%s.zip" % VER))
    with _z as zf:
        add_text(zf, A_ROOT + "/先看这里.txt", A_README)
        for rel in A_FILES:
            add_file(zf, REPO, rel, A_ROOT + "/")
        for d in A_DIRS:
            add_dir(zf, REPO, d, A_ROOT + "/")
        for t in A_TOOLS:
            add_file(zf, REPO, os.path.join("tools", t), A_ROOT + "/")
    print("A:", p, os.path.getsize(p), "bytes")

    # ---------- B 端 ----------
    p, _z = open_zip(os.path.join(OUT, "MC-ModSync-B端-服务端脚本-v%s.zip" % VER))
    with _z as zf:
        add_text(zf, B_ROOT + "/先看这里.txt", B_README)
        zf.write(os.path.join(REPO, "server", "mcmodsync-b.py"),
                 B_ROOT + "/mcmodsync-b.py")
        zf.write(os.path.join(REPO, "docs", "B端使用教程.md"),
                 B_ROOT + "/B端使用教程.md")
    print("B:", p, os.path.getsize(p), "bytes")

    # ---------- C 端（裸文件，便于直接解压到游戏目录）----------
    p, _z = open_zip(os.path.join(OUT, "MC-ModSync-C端-玩家更新器-v%s.zip" % VER))
    src = os.path.join(REPO, "dist", "client-package")
    with _z as zf:
        add_text(zf, "先看这里.txt", C_README.replace("{{PACKVER}}", current_pack_version()))
        for name in ("更新mod.bat", "更新mod.sh", "SHA256SUMS.txt"):
            add_file(zf, src, name)
        add_dir(zf, src, "_updater", "")   # rel 已含 "_updater/" 前缀
        zf.write(os.path.join(REPO, "docs", "C端使用教程.md"), "C端使用教程.md")
    print("C:", p, os.path.getsize(p), "bytes")
    print()
    print("三端发布包已生成到: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
