#!/bin/sh
cd -- "$(dirname -- "$0")" || exit 1
export PYTHONUTF8=1
if [ ! -x .venv/bin/python ]; then
    echo '请先按 docs/OP端使用教程.md 安装环境：'
    echo 'python3 -m venv .venv'
    echo '.venv/bin/python -m pip install -e .'
    exit 1
fi
exec .venv/bin/python -m mcmodsync menu "$@"
