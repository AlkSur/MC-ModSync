"""允许以模块方式调用 CLI：python -m mcmodsync <command>

这样即使没有执行 pip install（系统 PATH 里没有 mcmodsync.exe），
只要在仓库根目录运行，也能使用全部子命令。
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
