# -*- coding: utf-8 -*-
"""PyInstaller 入口脚本（[T-50]）。

以**绝对导入**引入 C 端 CLI，避免把 client_main.py 当裸脚本执行时
`from . import client` 失败（打包态与源码态均可运行）。
"""
from __future__ import annotations

import os
import sys


def _bootstrap_path() -> None:
    if getattr(sys, "frozen", False):
        return
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    if repo not in sys.path:
        sys.path.insert(0, repo)


def main() -> int:
    _bootstrap_path()
    from mcmodsync.client_main import main as cli_main
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
