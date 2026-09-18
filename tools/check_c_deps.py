# -*- coding: utf-8 -*-
"""C 端依赖检查（[T-41]）：确保客户端分发包**不使用**第三方库。

三重判定（全部通过才 OK）:
  1. 静态·顶层: 从入口（缺省 mcmodsync.client_main）遍历包内导入图，
     任一模块的**顶层** import 出现禁用库 -> FAIL。
     （口径与 T-10 一致: 函数内的惰性/受保护引用不算顶层依赖。）
  2. 静态·报告: 函数内引用记 WARN 并列出，供 T-50 的 PyInstaller .spec
     的 excludes 处理（canonicaljson 对 cryptography 的引用是受保护惰性引用）。
  3. 功能性: 在**屏蔽**第三方库的子进程内运行 C 端入口 doctor，
     若仍能正常运行（不抛 ImportError）-> PASS（最强证据）。

用法: python tools/check_c_deps.py [入口模块...]
退出码: 0 通过；1 失败。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(REPO, "mcmodsync")
PKG = "mcmodsync"

BANNED = ("boto3", "botocore", "paramiko", "cryptography", "nacl", "requests", "urllib3")


def module_path(dotted: str):
    if dotted != PKG and not dotted.startswith(PKG + "."):
        return None
    rel = dotted[len(PKG):].lstrip(".").replace(".", os.sep)
    base = os.path.join(PKG_DIR, rel) if rel else PKG_DIR
    for cand in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.isfile(cand):
            return cand
    return None


def _targets(node, pkg_ctx):
    """(导入的包内模块列表, 外部顶层名列表)"""
    inner, outer = [], []
    if isinstance(node, ast.Import):
        for a in node.names:
            (inner if module_path(a.name) else outer).append(a.name)
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            parts = pkg_ctx.split(".")
            base = parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts
            target = ".".join([p for p in base if p] + ([node.module] if node.module else []))
            if module_path(target):
                inner.append(target)
            for a in node.names:
                sub = "%s.%s" % (target, a.name)
                if a.name != "*" and module_path(sub):
                    inner.append(sub)
        else:
            mod = node.module or ""
            if module_path(mod):
                inner.append(mod)
            elif mod.split(".")[0]:
                outer.append(mod.split(".")[0])
    return inner, outer


def walk(entry: str):
    """遍历导入图；返回 (包内模块集合, 顶层外部依赖集合, 惰性外部依赖映射)。"""
    seen, top_ext, lazy_ext, queue = set(), set(), {}, [entry]
    while queue:
        dotted = queue.pop()
        if dotted in seen:
            continue
        seen.add(dotted)
        path = module_path(dotted)
        if path is None:
            continue
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        pkg_ctx = dotted if os.path.basename(path) == "__init__.py" else dotted.rsplit(".", 1)[0]

        top_nodes = set()
        for node in tree.body:                      # 仅顶层（不下钻函数/类体）
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for n in ast.walk(node):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    top_nodes.add(id(n))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            inner, outer = _targets(node, pkg_ctx)
            queue.extend(inner)
            if id(node) in top_nodes:
                top_ext.update(outer)
            else:
                for name in outer:
                    lazy_ext.setdefault(name, set()).add(dotted)
    return seen, top_ext, lazy_ext


def functional_check(entry: str) -> tuple:
    """屏蔽第三方库后运行 C 端入口 doctor。"""
    tmp = tempfile.mkdtemp(prefix="cdeps-")
    child = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "class Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in %r:\n"
        "            raise ImportError('blocked third-party: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "from mcmodsync import client_main\n"
        "rc = client_main.main(['doctor', '--target', %r])\n"
        "print('C-ENTRY-OK', rc)\n"
    ) % (REPO, BANNED, tmp)
    out = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True,
                         cwd=REPO)
    return out.returncode, out.stdout, out.stderr


def main(argv) -> int:
    entries = argv[1:] or ["%s.client_main" % PKG]
    ok = True
    for entry in entries:
        seen, top_ext, lazy_ext = walk(entry)
        banned_top = sorted(e for e in top_ext if e.split(".")[0] in BANNED)
        print("入口 %s: 包内模块 %d 个；顶层外部依赖: %s"
              % (entry, len(seen), ", ".join(sorted(top_ext)) or "(无)"))
        if banned_top:
            print("  [FAIL] 顶层 import 引入第三方库: %s" % ", ".join(banned_top))
            ok = False
        else:
            print("  [OK] 顶层无第三方 import")

        lazy_banned = {k: sorted(v) for k, v in lazy_ext.items() if k.split(".")[0] in BANNED}
        for name, mods in sorted(lazy_banned.items()):
            print("  [WARN] 函数内惰性/受保护引用 %s（来自 %s）——需 T-50 .spec excludes 排除"
                  % (name, ", ".join(mods)))

    rc, out, err = functional_check(entries[0])
    if "C-ENTRY-OK" in out:
        print("[OK] 功能性: 屏蔽第三方库后 C 端入口可正常加载并运行（%s）"
              % [l for l in out.splitlines() if l.startswith("C-ENTRY-OK")][0])
    else:
        print("[FAIL] 功能性: 屏蔽第三方库后 C 端入口失败（rc=%s）" % rc)
        print((err or "").strip()[-800:])
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
