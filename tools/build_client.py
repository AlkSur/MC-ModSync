# -*- coding: utf-8 -*-
"""生成 C 端「纯 Python 兜底」单文件 —— [T-50] 步骤2 的 _updater/mcmodsync.py。

原理: 把 C 端依赖链（mcmodsync 包内的纯标准库模块）打进一个确定性 zip，
base64 内嵌到单个 .py 运行器中；运行时自解压到临时目录并调用 C 端 CLI。
不使用任何第三方库，系统 Python 3.10+ 可直接执行:
    python3 _updater/mcmodsync.py sync --target "<实例目录>"

用法:
  python tools/build_client.py             # 生成 client/mcmodsync.py
  python tools/build_client.py --check     # 防漂移: 校验仓库版本与重新生成一致
  python tools/build_client.py --print-modules
退出码: 0 成功；1 失败（--check 不一致）。
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import io
import os
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(REPO, "mcmodsync")
PKG = "mcmodsync"
ENTRY = "%s.client_main" % PKG
OUT = os.path.join(REPO, "client", "mcmodsync.py")

HEADER = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# GENERATED, do not edit. 由 tools/build_client.py 生成（[T-50]）。
"""MC-ModSync C 端「纯 Python 兜底」运行器。

无 exe 或 exe 被安全软件拦截时，系统 Python 3.10+ 可直接运行本文件：

    python3 _updater/mcmodsync.py sync --target "<实例目录>" [--strict] [--no-pause]

内置 zip 载荷（纯标准库的 mcmodsync 包）在运行时自解压至临时目录后调用 C 端 CLI。
本文件及其载荷不依赖任何第三方库。
"""
import base64
import os
import shutil
import sys
import tempfile
import zipfile

PAYLOAD_B64 = (
'''

FOOTER = '''
)


def _extract() -> str:
    """把内嵌载荷解压到临时目录，返回 zip 路径（供 sys.path 使用）。"""
    d = tempfile.mkdtemp(prefix="mcmodsync-")
    zp = os.path.join(d, "mcmodsync.zip")
    with open(zp, "wb") as f:
        f.write(base64.b64decode("".join(PAYLOAD_B64)))
    return zp


def main() -> int:
    zp = _extract()
    sys.path.insert(0, zp)
    try:
        from mcmodsync.client_main import main as cli_main
        return int(cli_main())
    finally:
        shutil.rmtree(os.path.dirname(zp), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
'''


def module_path(dotted: str):
    if dotted != PKG and not dotted.startswith(PKG + "."):
        return None
    rel = dotted[len(PKG):].lstrip(".").replace(".", os.sep)
    base = os.path.join(PKG_DIR, rel) if rel else PKG_DIR
    for cand in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.isfile(cand):
            return cand
    return None


def collect(entry: str):
    """按导入图收集包内模块；返回有序的 (dotted, path) 列表（依赖在前）。"""
    order, seen, stack = [], set(), [entry]

    def visit(dotted: str) -> None:
        if dotted in seen:
            return
        seen.add(dotted)
        path = module_path(dotted)
        if path is None:
            return
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        pkg_ctx = dotted if os.path.basename(path) == "__init__.py" else dotted.rsplit(".", 1)[0]
        deps = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if module_path(a.name):
                        deps.append(a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = pkg_ctx.split(".")
                    base = parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts
                    target = ".".join([p for p in base if p]
                                      + ([node.module] if node.module else []))
                    if module_path(target):
                        deps.append(target)
                    for a in node.names:
                        if a.name != "*" and module_path("%s.%s" % (target, a.name)):
                            deps.append("%s.%s" % (target, a.name))
                elif module_path(node.module or ""):
                    deps.append(node.module)
        for d in sorted(set(deps)):
            visit(d)
        order.append(dotted)

    visit("%s.__init__" % PKG)
    visit(entry)
    out, seen_name = [], set()
    for dotted in order:
        p = module_path(dotted)
        if not p:
            continue
        rel = dotted[len(PKG):].lstrip(".")
        name = "mcmodsync/%s" % ((rel.replace(".", "/") + ".py") if rel else "__init__.py")
        if name in seen_name:
            continue
        seen_name.add(name)
        out.append((dotted, p))
    return out


def build_zip(modules) -> bytes:
    """确定性 zip（固定时间戳/属性；成员按名称排序）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for dotted, path in sorted(modules, key=lambda x: x[0]):
            rel = dotted[len(PKG):].lstrip(".")
            name = "%s/%s" % (PKG, (rel.replace(".", "/") + ".py") if rel else "__init__.py")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as f:
                z.writestr(info, f.read())
    return buf.getvalue()


def render(payload: bytes) -> str:
    b64 = base64.b64encode(payload).decode("ascii")
    lines = [b64[i:i + 96] for i in range(0, len(b64), 96)]
    body = "".join('    "%s"\n' % ln for ln in lines)
    return HEADER + body + FOOTER


def payload_of(text: str) -> bytes:
    """从生成文件里取回 zip 载荷（供 --check 用）。"""
    start = text.index("PAYLOAD_B64 = (") + len("PAYLOAD_B64 = (")
    end = text.index("\n)", start)
    b64 = "".join(ast.literal_eval("[" + text[start:end] + "]"))
    return base64.b64decode(b64)


def members(payload: bytes):
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        return {i.filename: hashlib.sha256(z.read(i.filename)).hexdigest()
                for i in z.infolist()}


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="生成 C 端纯 Python 兜底单文件")
    ap.add_argument("--check", action="store_true", help="防漂移校验")
    ap.add_argument("--print-modules", action="store_true")
    args = ap.parse_args(argv[1:])

    modules = collect(ENTRY)
    if args.print_modules:
        for dotted, path in modules:
            print("%-34s %s" % (dotted, os.path.relpath(path, REPO)))
    payload = build_zip(modules)
    text = render(payload)

    if args.check:
        if not os.path.isfile(OUT):
            print("[FAIL] 缺少 %s，请先运行 tools/build_client.py" % os.path.relpath(OUT, REPO))
            return 1
        with open(OUT, encoding="utf-8") as f:
            cur = f.read()
        if payload_of(cur) != payload and members(payload_of(cur)) != members(payload):
            print("[FAIL] 产物与重新生成不一致（防漂移失败）")
            return 1
        if cur[:len(HEADER)] != HEADER or not cur.rstrip().endswith(FOOTER.strip()):
            print("[FAIL] 运行器模板部分与生成器不一致")
            return 1
        print("[OK] 产物与重新生成一致（%d 个模块）" % len(modules))
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("已生成 %s（%d 个模块，zip %d 字节，文件 %d 字节）"
          % (os.path.relpath(OUT, REPO), len(modules), len(payload), len(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
