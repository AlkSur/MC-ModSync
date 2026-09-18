# -*- coding: utf-8 -*-
"""TL(T-50): 打包分发包验收 —— 分发包结构、SHA256SUMS、exe 内模块、双击链路端到端。

前置: 先运行 `python tools/package_client.py` 生成 dist/client-package/。
未构建时本文件整体 skip（CI 里由构建步骤保证存在）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest
from charness import PACK_ID, Cloud, make_keys

from mcmodsync import client

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "dist", "client-package")
EXE_NAME = "mcmodsync.exe" if os.name == "nt" else "mcmodsync-linux"
EXE = os.path.join(PKG, "_updater", EXE_NAME)
FALLBACK = os.path.join(PKG, "_updater", "mcmodsync.py")
CMD = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")

pytestmark = pytest.mark.skipif(
    not os.path.isfile(EXE),
    reason="未构建分发包（先运行 tools/package_client.py）")


def _load_package_client():
    path = os.path.join(REPO, "tools", "package_client.py")
    spec = importlib.util.spec_from_file_location("_pkgclient", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(argv, cwd=None, env=None):
    out = subprocess.run(argv, capture_output=True, cwd=cwd, env=env)
    return out.returncode, out.stdout.decode("utf-8", "replace"), out.stderr.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# 1) 分发包结构 + 校验和
# --------------------------------------------------------------------------

def test_package_layout() -> None:
    for rel in ("更新mod.bat", "更新mod.sh", "_updater/config.json",
                "_updater/mcmodsync.py", "_updater/%s" % EXE_NAME, "SHA256SUMS.txt"):
        assert os.path.isfile(os.path.join(PKG, rel)), "缺少 %s" % rel
    cfg = json.load(open(os.path.join(PKG, "_updater", "config.json"), encoding="utf-8"))
    assert cfg["schemaVersion"] == 1
    assert cfg["packId"] and cfg["manifestUrl"].startswith("https://") and cfg["publicKey"]


def test_sha256sums_matches_files() -> None:
    rows = 0
    with open(os.path.join(PKG, "SHA256SUMS.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            sha, rel = line.split("  ", 1)
            full = os.path.join(PKG, rel.replace("/", os.sep))
            assert os.path.isfile(full), "SHA256SUMS 指向不存在的文件: %s" % rel
            with open(full, "rb") as fh:
                assert hashlib.sha256(fh.read()).hexdigest() == sha, "哈希不符: %s" % rel
            rows += 1
    assert rows >= 5


def test_bat_and_sh_in_package_still_match_spec() -> None:
    for name in ("更新mod.bat", "更新mod.sh"):
        src = open(os.path.join(REPO, "entries", name), "rb").read()
        dst = open(os.path.join(PKG, name), "rb").read()
        assert src == dst, "%s 与 entries/ 原文不一致" % name


# --------------------------------------------------------------------------
# 2) exe: 可运行 + 不含应排除模块
# --------------------------------------------------------------------------

def test_exe_runs_and_lists_commands() -> None:
    rc, out, err = _run([EXE, "--help"])
    assert rc == 0, err
    for cmd in ("sync", "verify", "rollback", "doctor"):
        assert cmd in out


def test_exe_bundles_no_banned_modules() -> None:
    pc = _load_package_client()
    names, how = pc.bundled_modules(EXE)
    if names is None:
        pytest.skip("无法读取 exe 归档: %s" % how)
    roots = {n.split(".")[0].lower() for n in names}
    bad = sorted(r for r in roots if r in pc.BANNED_MODULES)
    assert not bad, "exe 内检出应排除模块: %s" % bad


# --------------------------------------------------------------------------
# 3) exe / .py 兜底 端到端同步（本地对象存储替身）
# --------------------------------------------------------------------------

@pytest.fixture
def cloud(tmp_path):
    pem, pub = make_keys(tmp_path)
    c = Cloud(str(tmp_path / "cloud"), pem, PACK_ID)
    base = c.start()
    yield c, pub, base
    c.stop()


def _make_instance(tmp_path, base, pub, files):
    inst = tmp_path / "我的 整合包"
    shutil.copytree(PKG, str(inst))
    cfg = {"schemaVersion": 1, "packId": PACK_ID, "manifestUrl": base + "/manifest.json",
           "publicKey": pub}
    with open(inst / "_updater" / "config.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, ensure_ascii=False))
    return inst


def _assert_synced(inst, files):
    for name, data in files.items():
        p = os.path.join(str(inst), name)
        assert os.path.isfile(p), "缺少 %s" % name
        with open(p, "rb") as f:
            assert f.read() == data
    st = json.load(open(os.path.join(str(inst), "_updater", "state.json"), encoding="utf-8"))
    assert st["version"] == "1.0.0"
    logs = os.path.join(str(inst), "_updater", "logs")
    text = "".join(open(os.path.join(logs, n), encoding="utf-8").read()
                   for n in os.listdir(logs))
    assert "HTTP 200" in text


FILES = {"mods/中文 模组.jar": b"ZH-BYTES", "mods/a b.jar": b"SP-BYTES"}


def test_exe_end_to_end(tmp_path, cloud) -> None:
    c, pub, base = cloud
    c.publish("1.0.0", FILES)
    inst = _make_instance(tmp_path, base, pub, FILES)
    # exe 自带运行时：清掉 Python 相关环境变量仍应可用
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    rc, out, err = _run([EXE, "sync", "--target", str(inst), "--no-pause"], env=env)
    assert rc == 0, out + err
    assert "同步完成" in out
    _assert_synced(inst, FILES)


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows")
def test_real_bat_double_click_end_to_end(tmp_path, cloud) -> None:
    if client.detect_game_running():
        pytest.skip("本机当前有 java/javaw/minecraft 进程，会被游戏运行检测拦截")
    c, pub, base = cloud
    c.publish("1.0.0", FILES)
    inst = _make_instance(tmp_path, base, pub, FILES)
    rc, out, err = _run([CMD, "/c", str(inst / "更新mod.bat"), "--no-pause"],
                        cwd=str(tmp_path))
    assert rc == 0, out + err
    assert "更新完成，请手动启动 PCL2 或 HMCL" in out
    _assert_synced(inst, FILES)


def test_python_fallback_end_to_end(tmp_path, cloud) -> None:
    c, pub, base = cloud
    c.publish("1.0.0", FILES)
    inst = _make_instance(tmp_path, base, pub, FILES)
    rc, out, err = _run([sys.executable, FALLBACK, "sync", "--target", str(inst), "--no-pause"])
    assert rc == 0, out + err
    _assert_synced(inst, FILES)


def test_python_fallback_on_system_interpreter(tmp_path, cloud) -> None:
    """用另一套系统解释器运行 .py 兜底，验证「纯标准库 + 系统 Python 可运行」。"""
    sys_py = r"D:\Software\Python\python.exe"
    if not os.path.isfile(sys_py):
        pytest.skip("未找到系统 Python: %s" % sys_py)
    c, pub, base = cloud
    c.publish("1.0.0", FILES)
    inst = _make_instance(tmp_path, base, pub, FILES)
    rc, out, err = _run([sys_py, FALLBACK, "sync", "--target", str(inst), "--no-pause"])
    assert rc == 0, out + err
    _assert_synced(inst, FILES)
