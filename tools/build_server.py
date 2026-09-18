"""T-12: 将白名单纯标准库共享模块与 server/b_main.py 拼接为单文件 B。

产物: server/mcmodsync-b.py（头部带 "# GENERATED, do not edit"，内容可复现、
不含时间戳，便于 CI 防漂移 diff）。

白名单（[T-12] 步骤1）:
  hashing.py  paths.py  planner.py  manifest.py  locking.py  platform.py
  applier.py（apply/backup/rollback 共享纯逻辑）
  canonicaljson.py 的 **JSON 部分**（仅 canonical / _sort_keys / strip_signature；
  签名相关函数依赖 cryptography，属 A 端专用，不进入 B）

用法:
  python tools/build_server.py            # 生成 server/mcmodsync-b.py
  python tools/build_server.py --check    # 仅校验仓库版本与重新生成一致（CI 用）
  python tools/build_server.py -o <path>  # 指定输出路径
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(REPO_ROOT, "mcmodsync")
SERVER_DIR = os.path.join(REPO_ROOT, "server")
DEFAULT_OUT = os.path.join(SERVER_DIR, "mcmodsync-b.py")

# (模块名, 文件名, 允许保留的顶层定义名集合 或 None=全部)
WHITELIST: List[Tuple[str, str, Optional[set]]] = [
    ("hashing", "hashing.py", None),
    ("paths", "paths.py", None),
    ("planner", "planner.py", None),
    ("manifest", "manifest.py", None),
    ("locking", "locking.py", None),
    ("platform", "platform.py", None),
    ("applier", "applier.py", None),
    ("canonicaljson", "canonicaljson.py", {"canonical", "_sort_keys", "strip_signature"}),
]

HEADER = (
    "# GENERATED, do not edit\n"
    "# 由 tools/build_server.py 拼接生成（白名单纯标准库共享模块 + server/b_main.py）。\n"
    "# 请勿手工修改：CI 会重新生成并做防漂移 diff。\n"
)

_IMPORT_RE = re.compile(r"^(\s*)(?:import|from)\s+([A-Za-z_][\w.]*)")


def _is_relative(node) -> bool:
    return isinstance(node, ast.ImportFrom) and (node.level or 0) > 0


def _root_module(node) -> str:
    if isinstance(node, ast.Import):
        return node.names[0].name.split(".")[0]
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0]
    return ""


def _defined_names(tree: ast.Module) -> List[str]:
    names: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.append(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.append(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.append((a.asname or a.name).split(".")[0])
    # 去重保序
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def build_module_source(name: str, fname: str,
                        keep_defs: Optional[set]) -> Tuple[str, List[str], List[str]]:
    path = os.path.join(PKG_DIR, fname)
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=path)

    segments: List[str] = []
    imports: List[str] = []
    kept: List[ast.AST] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if _is_relative(node):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if _root_module(node) == "mcmodsync":
                continue
            imports.append(ast.get_source_segment(src, node).strip())
            continue
        if keep_defs is not None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                    and node.name in keep_defs:
                segments.append(ast.get_source_segment(src, node))
                kept.append(node)
            continue
        segments.append(ast.get_source_segment(src, node))
        kept.append(node)

    # 用注释分隔，便于人工定位
    body = "\n\n".join(s for s in segments if s)
    chunk = ("\n# ===== %s.py =====\n\n" % name) + body + "\n"
    return chunk, _defined_names(ast.Module(body=kept, type_ignores=[])), imports


def rewrite_b_main() -> Tuple[str, List[str]]:
    path = os.path.join(SERVER_DIR, "b_main.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    out: List[str] = []
    imports: List[str] = []
    for line in src.splitlines():
        m = _IMPORT_RE.match(line)
        if m:
            indent, mod = m.group(1), m.group(2)
            if mod == "__future__":
                continue
            if mod == "mcmodsync" or mod.startswith("mcmodsync."):
                out.append(indent + "pass" if indent else "")
                continue
            if not indent:
                imports.append(line.strip())
                continue
        out.append(line)
    while out and out[0] == "":
        out.pop(0)
    return "\n".join(out) + "\n", imports


def generate() -> str:
    all_imports: List[str] = []
    chunks: List[str] = []
    proxy_specs: List[Tuple[str, List[str]]] = []

    for name, fname, keep in WHITELIST:
        chunk, names, imps = build_module_source(name, fname, keep)
        chunks.append(chunk)
        proxy_specs.append((name, names))
        all_imports.extend(imps)

    b_src, b_imports = rewrite_b_main()
    all_imports.extend(b_imports)

    # 去重保序（按整行文本）
    seen = set()
    imports_final = []
    for line in all_imports:
        if line in seen:
            continue
        seen.add(line)
        imports_final.append(line)

    proxies: List[str] = ["", "# ===== 模块代理（供模块限定名访问，如 manifest.atomic_copy）=====",
                          "from types import SimpleNamespace as _SimpleNamespace", ""]
    for name, names in proxy_specs:
        args = ", ".join("%s=%s" % (n, n) for n in names)
        proxies.append("%s = _SimpleNamespace(%s)" % (name, args))
    proxies.append("")
    proxies.append("__MCMODSYNC_STITCHED__ = True")
    proxies.append("")

    parts = [
        HEADER,
        "from __future__ import annotations",
        "",
        "\n".join(imports_final),
        "",
        "\n".join(chunks),
        "\n".join(proxies),
        "# ===== server/b_main.py =====",
        "",
        b_src,
    ]
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="拼接生成单文件 B")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="仅校验与仓库版本一致")
    args = ap.parse_args()

    content = generate()

    if args.check:
        if not os.path.isfile(args.out):
            print("[FAIL] 产物不存在: %s" % args.out)
            return 1
        with open(args.out, "r", encoding="utf-8", newline="") as f:
            existing = f.read()
        if existing != content:
            print("[FAIL] 产物与重新生成不一致（防漂移）: %s" % args.out)
            return 1
        print("[OK] 产物与重新生成一致")
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    print("[OK] 已生成 %s（%d 字节）" % (args.out, len(content.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
