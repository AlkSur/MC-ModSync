"""TL-2 (构建部分): 单文件 B 的产物、防漂移、纯标准库与 3.8 兼容。"""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from bharness import B_PATH, REPO_ROOT, run_b

BLOCKED_ROOTS = ("boto3", "botocore", "paramiko", "cryptography", "nacl", "requests")


def test_stitched_file_exists_with_header() -> None:
    assert B_PATH.is_file(), "缺少构建产物 server/mcmodsync-b.py"
    first = B_PATH.read_text(encoding="utf-8").splitlines()[0]
    assert first == "# GENERATED, do not edit"


def test_generation_is_reproducible() -> None:
    out = subprocess.run([sys.executable,
                          str(REPO_ROOT / "tools" / "build_server.py"), "--check"],
                         capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert out.returncode == 0, out.stdout + out.stderr


def test_protocol_version() -> None:
    out = run_b("--protocol-version")
    assert out.returncode == 0
    assert out.stdout.strip() == "MC-ModSync-B 2.0"


def test_no_third_party_imports() -> None:
    tree = ast.parse(B_PATH.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                roots.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and (node.level or 0) == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert not (roots & set(BLOCKED_ROOTS)), "产物含第三方 import: %s" % sorted(roots & set(BLOCKED_ROOTS))
    allowed = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    offending = sorted(r for r in roots if r not in allowed)
    assert offending == [], "产物含非标准库 import: %s" % offending


def test_python38_syntax_compatible() -> None:
    src = B_PATH.read_text(encoding="utf-8")
    ast.parse(src, filename=str(B_PATH), feature_version=(3, 8))


def test_contains_all_whitelisted_modules() -> None:
    src = B_PATH.read_text(encoding="utf-8")
    for marker in ["# ===== hashing.py =====", "# ===== paths.py =====", "# ===== planner.py =====",
                   "# ===== manifest.py =====", "# ===== locking.py =====", "# ===== platform.py =====",
                   "# ===== applier.py =====", "# ===== canonicaljson.py =====",
                   "# ===== server/b_main.py ====="]:
        assert marker in src, "缺少模块段: %s" % marker


def test_canonicaljson_json_part_only() -> None:
    """签名相关函数（依赖 cryptography）不得进入 B 产物。"""
    src = B_PATH.read_text(encoding="utf-8")
    assert "def canonical(" in src
    assert "def strip_signature(" in src
    assert "def sign(" not in src
    assert "def public_key_b64_from_private(" not in src
    assert "cryptography" not in src


def test_module_executes_without_third_party() -> None:
    """在屏蔽第三方库的环境内执行整个产物（不触发 __main__ 分支）。"""
    child = (
        "import sys, types\n"
        "class Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in %r:\n"
        "            raise ImportError('blocked: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "src = open(%r, encoding='utf-8').read()\n"
        "m = types.ModuleType('mcmodsync_b_under_test')\n"
        "sys.modules['mcmodsync_b_under_test'] = m\n"
        "exec(compile(src, 'mcmodsync-b.py', 'exec'), m.__dict__)\n"
        "assert m.PROTOCOL_VERSION == 'MC-ModSync-B 2.0'\n"
        "print('OK')\n" % (BLOCKED_ROOTS, str(B_PATH))
    )
    out = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "OK"
