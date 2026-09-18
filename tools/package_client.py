# -*- coding: utf-8 -*-
"""组装 C 端分发包（[T-50] 步骤1~3）—— mcmodsync.packaging 的命令行薄封装。

等价于 A 端 CLI: `mcmodsync package-client -c pack.local.json [--out DIR] [--no-exe]`

用法:
  python tools/package_client.py                       # 构建 exe 并组装
  python tools/package_client.py --no-exe              # 跳过 exe（无 PyInstaller 时）
  python tools/package_client.py --exe <已有 exe 路径>   # 复用已构建的 exe
退出码: 0 成功；1 失败。
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from mcmodsync import packaging  # noqa: E402


def main(argv) -> int:
    a = packaging.repo_assets(REPO)
    ap = argparse.ArgumentParser(description="组装 C 端分发包")
    ap.add_argument("--out", default=a["default_out"])
    ap.add_argument("--config", default=a["config"])
    ap.add_argument("--exe", default="")
    ap.add_argument("--no-exe", action="store_true")
    ap.add_argument("--fallback", default=a["fallback"])
    ap.add_argument("--skip-verify", action="store_true", help="跳过产物内模块核验")
    args = ap.parse_args(argv[1:])

    try:
        res = packaging.assemble(args.config, args.out, repo=REPO,
                                 exe=args.exe or None, no_exe=args.no_exe,
                                 fallback=args.fallback, skip_verify=args.skip_verify)
    except packaging.PackagingError as e:
        sys.stderr.write("错误: %s\n" % e)
        return 1

    print("\n分发包: %s" % res["out"])
    for rel, size in res["files"]:
        print("  %-28s %9d 字节" % (rel, size))
    print("校验和: %s" % os.path.relpath(res["sums"], REPO))
    return 1 if res["banned"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
