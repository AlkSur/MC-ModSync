"""T-10 验收: 共享模块的纯标准库性。

- 运行时: 在屏蔽第三方库的子进程内仍能 import 6 个共享模块。
- 静态: 6 个模块的**顶层** import 只允许标准库与包内相对导入。

注: canonicaljson.py 的 sign() 通过惰性 import 使用 cryptography（A 端专用），
按计划 T-12「仅拼接 canonicaljson 的 JSON 部分」的说明，其顶层无第三方依赖，
不影响 B 端单文件构建；此处以「顶层 import」为判定口径。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    "mcmodsync.hashing",
    "mcmodsync.paths",
    "mcmodsync.planner",
    "mcmodsync.manifest",
    "mcmodsync.locking",
    "mcmodsync.canonicaljson",
    # C 端（T-40/T-41）同样要求纯标准库：
    "mcmodsync.http_download",
    "mcmodsync.ed25519_min",
    "mcmodsync.client",
]
FILES = {
    "mcmodsync.hashing": REPO_ROOT / "mcmodsync" / "hashing.py",
    "mcmodsync.paths": REPO_ROOT / "mcmodsync" / "paths.py",
    "mcmodsync.planner": REPO_ROOT / "mcmodsync" / "planner.py",
    "mcmodsync.manifest": REPO_ROOT / "mcmodsync" / "manifest.py",
    "mcmodsync.locking": REPO_ROOT / "mcmodsync" / "locking.py",
    "mcmodsync.canonicaljson": REPO_ROOT / "mcmodsync" / "canonicaljson.py",
    "mcmodsync.http_download": REPO_ROOT / "mcmodsync" / "http_download.py",
    "mcmodsync.ed25519_min": REPO_ROOT / "mcmodsync" / "ed25519_min.py",
    "mcmodsync.client": REPO_ROOT / "mcmodsync" / "client.py",
}
BLOCKED_ROOTS = ("boto3", "botocore", "paramiko", "cryptography", "nacl", "requests")


def test_import_works_without_third_party() -> None:
    child = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "class Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in %r:\n"
        "            raise ImportError('blocked third-party: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "import importlib\n"
        "for m in %r:\n"
        "    importlib.import_module(m)\n"
        "print('OK')\n" % (str(REPO_ROOT), BLOCKED_ROOTS, MODULES)
    )
    out = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "OK"


@pytest.mark.parametrize("mod", MODULES)
def test_python38_syntax_compatible(mod: str) -> None:
    src = FILES[mod].read_text(encoding="utf-8")
    try:
        ast.parse(src, filename=str(FILES[mod]), feature_version=(3, 8))
    except SyntaxError as e:  # pragma: no cover
        pytest.fail("%s 使用了 Python 3.8 不支持的语法: %s" % (mod, e))


@pytest.mark.parametrize("mod", MODULES)
def test_top_level_imports_are_stdlib(mod: str) -> None:
    tree = ast.parse(FILES[mod].read_text(encoding="utf-8"))

    def _roots(node) -> list:
        found = []
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.append(node.module)
        return found

    roots = []
    for node in tree.body:  # 仅顶层语句，排除函数/类体
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            roots += _roots(sub)

    allowed = set(getattr(sys, "stdlib_module_names", ())) | {"mcmodsync", "__future__"}
    offending = [r for r in roots if r.split(".")[0] not in allowed]
    assert offending == [], "%s 顶层第三方 import: %s" % (mod, offending)
