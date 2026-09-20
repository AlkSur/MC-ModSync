# -*- coding: utf-8 -*-
"""TL(T-41): client_main.py 打包入口的交互输出、日志与退出码。"""
from __future__ import annotations

import hashlib
import json
import os
import re

import pytest
from charness import PACK_ID, Cloud, Instance, make_keys

from mcmodsync import client, client_main

EMOJI_RANGES = ((0x1F300, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF),
                (0x1F000, 0x1F2FF), (0xFE0F, 0xFE0F))


def _has_emoji(text: str) -> bool:
    for ch in text:
        o = ord(ch)
        for lo, hi in EMOJI_RANGES:
            if lo <= o <= hi:
                return True
    return False


@pytest.fixture
def env(tmp_path):
    pem, pub = make_keys(tmp_path)
    cloud = Cloud(str(tmp_path / "cloud"), pem, PACK_ID)
    base = cloud.start()
    inst = Instance(str(tmp_path / "inst"), base + "/manifest.json", pub)
    yield (cloud, inst)
    cloud.stop()


# --------------------------------------------------------------------------
# 参数与分发
# --------------------------------------------------------------------------

def test_help_exits_0(capsys) -> None:
    with pytest.raises(SystemExit) as ei:
        client_main.main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("sync", "verify", "rollback", "doctor"):
        assert cmd in out


def test_unknown_subcommand_exits_2() -> None:
    with pytest.raises(SystemExit) as ei:
        client_main.main(["nope"])
    assert ei.value.code == 2


def test_no_pause_flag_accepted(tmp_path) -> None:
    # 空目录（无 config.json）-> 码 1（而非 argparse 的 2），证明 --no-pause 被识别
    empty = tmp_path / "empty"
    empty.mkdir()
    assert client_main.main(["sync", "--target", str(empty), "--no-pause"]) == 1


def test_exit_code_passthrough(env) -> None:
    cloud, inst = env
    cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    sha = hashlib.sha256(b"A").hexdigest()
    with open(cloud.blob_path(sha), "wb") as f:
        f.write(b"BAD")
    assert client_main.main(["sync", "--target", inst.root, "--no-pause"]) == 5


# --------------------------------------------------------------------------
# 中文输出 / 进度条 / 日志
# --------------------------------------------------------------------------

def test_sync_console_and_log_file(env, capsys) -> None:
    cloud, inst = env
    cloud.publish("1.0.0", {"mods/a.jar": b"A" * 4096, "mods/b.jar": b"B"})
    rc = client_main.main(["sync", "--target", inst.root, "--no-pause"])
    assert rc == 0
    out = capsys.readouterr().out

    assert "MC-ModSync 客户端  sync" in out
    assert "实例目录：" in out
    assert "日志文件：" in out
    assert "进度 [" in out and "个文件" in out
    assert "MiB" in out
    assert "同步完成：" in out
    assert "完成：成功（退出码 0）" in out
    assert not _has_emoji(out), "控制台不得输出 emoji"
    # 非终端（测试捕获）时不得出现 ANSI 转义码
    assert "\x1b[" not in out, "非 tty 环境下不得输出颜色转义码"

    lp = client_main.log_path_for(inst.root)
    assert os.path.isfile(lp)
    log = open(lp, encoding="utf-8").read()
    assert "\ufffd" not in log, "日志不得出现乱码替换符"
    assert "HTTP 200" in log                                   # HTTP 状态码
    assert hashlib.sha256(b"A" * 4096).hexdigest() in log      # 每个文件的 sha256
    assert "决策: 新增 mods/a.jar" in log
    assert "决策: 跳过" in log or "决策: 新增" in log
    assert "进度 [" in log                                     # 进度行入 DEBUG 日志


def test_failure_prints_log_path(env, capsys) -> None:
    cloud, inst = env
    cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    os.remove(os.path.join(inst.root, "_updater", "config.json"))
    rc = client_main.main(["sync", "--target", inst.root, "--no-pause"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "退出码 1" in out
    assert client_main.log_path_for(inst.root) in out


def test_verify_rollback_doctor_dispatch(env, capsys) -> None:
    cloud, inst = env
    cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    assert client_main.main(["sync", "--target", inst.root, "--no-pause"]) == 0
    capsys.readouterr()
    assert client_main.main(["verify", "--target", inst.root, "--no-pause"]) == 0
    assert "结论: 与云端一致" in capsys.readouterr().out
    assert client_main.main(["doctor", "--target", inst.root, "--no-pause"]) == 0
    assert client_main.main(["rollback", "--target", inst.root, "--no-pause"]) == 0
    assert "回滚完成" in capsys.readouterr().out


def test_default_target_is_parent_of_exe_dir(monkeypatch, tmp_path) -> None:
    """--target 缺省 -> exe 所在目录的上一级。"""
    fake_exe = tmp_path / "实例" / "_updater" / "mcmodsync.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_bytes(b"")
    monkeypatch.setattr(client_main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(client_main.sys, "executable", str(fake_exe), raising=False)
    assert os.path.abspath(client.default_target()) == os.path.abspath(str(tmp_path / "实例"))
