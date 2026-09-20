# -*- coding: utf-8 -*-
"""生成三端发布包（解压即用）。临时脚本，跑完可删。"""
import os
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "dist", "release")
VER = "2.0.0"
CVER = "1.0.1"

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
  详细说明和退出码对照表见 docs\\C端使用教程.md
"""


def add_dir(zf, root, rel_dir, arc_prefix):
    """递归添加目录（跳过 __pycache__ / .pyc）。"""
    base = os.path.join(root, rel_dir)
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", ".idea")]
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            zf.write(full, arc_prefix + rel)


def add_file(zf, root, rel, arc_prefix=""):
    full = os.path.join(root, rel)
    zf.write(full, arc_prefix + rel.replace(os.sep, "/"))


def add_text(zf, arc_name, text):
    zf.writestr(arc_name, text.replace("\n", "\r\n"))


def main():
    os.makedirs(OUT, exist_ok=True)

    # ---------- A 端 ----------
    p = os.path.join(OUT, "MC-ModSync-A端-OP工具-v%s.zip" % VER)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        add_text(zf, A_ROOT + "/先看这里.txt", A_README)
        for rel in A_FILES:
            add_file(zf, REPO, rel, A_ROOT + "/")
        for d in A_DIRS:
            add_dir(zf, REPO, d, A_ROOT + "/")
        for t in A_TOOLS:
            add_file(zf, REPO, os.path.join("tools", t), A_ROOT + "/")
    print("A:", p, os.path.getsize(p), "bytes")

    # ---------- B 端 ----------
    p = os.path.join(OUT, "MC-ModSync-B端-服务端脚本-v%s.zip" % VER)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        add_text(zf, B_ROOT + "/先看这里.txt", B_README)
        zf.write(os.path.join(REPO, "server", "mcmodsync-b.py"),
                 B_ROOT + "/mcmodsync-b.py")
        zf.write(os.path.join(REPO, "docs", "B端使用教程.md"),
                 B_ROOT + "/B端使用教程.md")
    print("B:", p, os.path.getsize(p), "bytes")

    # ---------- C 端（裸文件，便于直接解压到游戏目录）----------
    p = os.path.join(OUT, "MC-ModSync-C端-玩家更新器-v%s.zip" % CVER)
    src = os.path.join(REPO, "dist", "client-package")
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        add_text(zf, "先看这里.txt", C_README)
        for name in ("更新mod.bat", "更新mod.sh", "SHA256SUMS.txt"):
            add_file(zf, src, name)
        add_dir(zf, src, "_updater", "")   # rel 已含 "_updater/" 前缀
        zf.write(os.path.join(REPO, "docs", "C端使用教程.md"), "C端使用教程.md")
    print("C:", p, os.path.getsize(p), "bytes")


if __name__ == "__main__":
    main()
