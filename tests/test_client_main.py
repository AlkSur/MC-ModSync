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


# --------------------------------------------------------------------------
# 控制台排版：双行进度 / ASCII 进度条 / 来源标识 / 哈希屏蔽
# --------------------------------------------------------------------------

class TtyStream:
    """isatty()=True 的文本流替身（capsys 的流 isatty 为假，测不了动态刷新）。"""

    def __init__(self) -> None:
        self.buf = []

    def write(self, s: str) -> None:
        self.buf.append(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True

    @property
    def text(self) -> str:
        return "".join(self.buf)


class FakeLogger:
    def __init__(self) -> None:
        self.lines = []

    def info(self, msg: str) -> None:
        self.lines.append(msg)

    def debug(self, msg: str) -> None:
        self.lines.append(msg)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


SHA_A = "a" * 64
SHA_B = "b" * 64


def _entry(sha: str, name: str, size: int, source: str = "") -> dict:
    return {"path": "mods/" + name, "sha256": sha, "size": size, "source": source}


def test_source_label_mapping() -> None:
    assert client_main.source_label({"source": "modrinth"}) == "Modrinth"
    assert client_main.source_label({"source": "CurseForge"}) == "CurseForge"
    assert client_main.source_label({"source": "storage"}) == "对象存储"
    assert client_main.source_label({"source": ""}) == "对象存储"      # 清单无 source
    assert client_main.source_label({}) == "对象存储"
    assert client_main.source_label(None) == "对象存储"
    assert client_main.source_label({"source": "unknown"}) == "对象存储"


def test_file_progress_two_line_render() -> None:
    """下载中：第 1 行固定、第 2 行 \\r 原地覆写并带 ANSI 清行；100% 即结束。"""
    stream = TtyStream()
    fp = client_main.FileProgress(stream, interactive=True, ansi=True, bar_width=20)
    fp.paint_interval = 0
    fp.start(_entry(SHA_A, "ModernUI-NeoForge-1.21.1.jar", 1048576, "modrinth"))
    fp.add_bytes(SHA_A, 512 * 1024)
    fp.done(_entry(SHA_A, "ModernUI-NeoForge-1.21.1.jar", 1048576, "modrinth"))
    text = stream.text

    assert "正在下载 |来源:Modrinth| 文件名:ModernUI-NeoForge-1.21.1.jar\n" in text
    assert "\r[##########----------] 50% | 0.5MB / 1.0MB | " in text
    assert "\r[####################] 100% | 1.0MB / 1.0MB | " in text
    assert "\x1b[K" in text
    assert "下载完成" not in text, "进度条到 100% 即代表完成，不得再打印完成行"
    # 进度条只用 ASCII：无 Unicode 方块/半宽方块（规避中文字体的双宽错位）
    for ch in "█░▓▒▐■◧▁▏▊":
        assert ch not in text
    bars = [seg for seg in text.split("\r") if seg.startswith("[")]
    assert bars and all(all(ord(c) < 128 for c in seg) for seg in bars)


def test_file_progress_switches_to_next_file() -> None:
    """文件 100% 后直接进入下一个文件的双行输出。"""
    stream = TtyStream()
    fp = client_main.FileProgress(stream, interactive=True, ansi=True)
    fp.start(_entry(SHA_A, "a.jar", 1024))
    fp.start(_entry(SHA_B, "b.jar", 2048))
    fp.add_bytes(SHA_B, 1024)
    fp.done(_entry(SHA_A, "a.jar", 1024))
    text = stream.text

    assert text.count("正在下载 |来源:对象存储| 文件名:a.jar") == 1
    assert "正在下载 |来源:对象存储| 文件名:b.jar" in text
    # b.jar 的第一行出现在 a.jar 的 100% 之后
    assert text.index("文件名:b.jar") > text.index("] 100% |")


def test_file_progress_non_interactive_is_plain_lines() -> None:
    """管道/重定向：不做动态刷新（无 \\r、无 ANSI），只逐行输出。"""
    stream = TtyStream()
    fp = client_main.FileProgress(stream, interactive=False, ansi=False)
    fp.start(_entry(SHA_A, "a.jar", 1024))
    fp.add_bytes(SHA_A, 512)
    fp.done(_entry(SHA_A, "a.jar", 1024))
    text = stream.text
    assert text == "正在下载 |来源:对象存储| 文件名:a.jar\n"
    assert "\r" not in text and "\x1b" not in text


def test_file_progress_reset_zeroes_counting() -> None:
    """重试/回退：reset 后本文件计数归零（不残留上一轮的百分比）。"""
    stream = TtyStream()
    fp = client_main.FileProgress(stream, interactive=True, ansi=True, bar_width=20)
    fp.paint_interval = 0
    fp.start(_entry(SHA_A, "a.jar", 1024 * 1024))
    fp.add_bytes(SHA_A, 1024 * 1024)
    fp.reset(SHA_A)
    fp.add_bytes(SHA_A, 256 * 1024)
    assert "[#####---------------] 25% | " in stream.text


def test_console_masks_hash_on_terminal_but_keeps_it_in_log() -> None:
    """终端不展示 sha256；日志文件保留完整哈希（需求 5）。"""
    stream = TtyStream()
    logger = FakeLogger()
    con = client_main.Console(logger, stream=stream, files=client_main.FileProgress(stream))
    con.log("决策: 新增 mods/a.jar (sha256=%s, size=3)" % SHA_A)
    assert SHA_A not in stream.text
    assert "sha256=见日志" in stream.text
    assert SHA_A in logger.text


def test_console_log_does_not_break_progress_block() -> None:
    """失败/重试信息单独成行，不插进进度行；之后进度块自动续画（含第 1 行重绘）。"""
    stream = TtyStream()
    files = client_main.FileProgress(stream, interactive=True, ansi=True)
    files.paint_interval = 0
    con = client_main.Console(FakeLogger(), stream=stream, files=files)
    con.files.start(_entry(SHA_A, "a.jar", 1024 * 1024))
    con.files.add_bytes(SHA_A, 256 * 1024)
    con.log("下载失败(第 1 次): http://x/blobs: timeout")
    con.files.add_bytes(SHA_A, 256 * 1024)
    text = stream.text

    assert "\n下载失败(第 1 次): http://x/blobs: timeout\n" in text
    assert text.count("正在下载 |来源:对象存储| 文件名:a.jar\n") == 2   # 被打断后重绘
    assert "[#####---------------] 25% | " in text
    assert "[##########----------] 50% | " in text


def test_interactive_sync_end_to_end(monkeypatch, tmp_path) -> None:
    """交互终端下跑完整 sync：双行进度 + 原有头部/汇总统计全部保留。"""
    pem, pub = make_keys(tmp_path)
    cloud = Cloud(str(tmp_path / "cloud"), pem, PACK_ID)
    base = cloud.start()
    try:
        cloud.publish("1.0.0", {"mods/a.jar": b"A" * 1048576, "mods/b.jar": b"B"})
        inst = Instance(str(tmp_path / "inst"), base + "/manifest.json", pub)
        stream = TtyStream()
        monkeypatch.setattr(client_main.sys, "stdout", stream)
        rc = client_main.main(["sync", "--target", inst.root, "--no-pause"])
        assert rc == 0
        text = stream.text
        assert "MC-ModSync 客户端  sync" in text          # 头部实例信息保留
        assert "实例目录：" in text and "日志文件：" in text
        assert "\r[" in text and "#" in text              # 双行动态刷新
        assert "正在下载 |来源:对象存储| 文件名:" in text
        assert "同步完成：" in text                        # 末尾汇总统计保留
        assert "完成：成功（退出码 0）" in text
        assert text.count("\n") >= 6
        assert not _has_emoji(text)
    finally:
        cloud.stop()


def test_skipped_file_prints_one_line(env, capsys) -> None:
    """本地哈希已一致的文件：单独一行「跳过：<文件名>」。"""
    cloud, inst = env
    cloud.publish("1.0.0", {"mods/CustomSkinLoader_Universal-15.0.1.jar": b"X"})
    assert client_main.main(["sync", "--target", inst.root, "--no-pause"]) == 0
    capsys.readouterr()
    assert client_main.main(["sync", "--target", inst.root, "--no-pause"]) == 0
    out = capsys.readouterr().out
    assert "跳过：CustomSkinLoader_Universal-15.0.1.jar" in out
    assert "本地已是最新，无需变更。" in out


def test_non_tty_output_has_no_control_chars(env, capsys) -> None:
    """非终端（管道/重定向）不得出现 \\r 或 ANSI 转义码；哈希不上终端。"""
    cloud, inst = env
    cloud.publish("1.0.0", {"mods/big.jar": b"B" * 4096})
    assert client_main.main(["sync", "--target", inst.root, "--no-pause"]) == 0
    out = capsys.readouterr().out
    assert "\r" not in out
    assert "\x1b" not in out
    assert "正在下载 |来源:对象存储| 文件名:big.jar" in out
    assert hashlib.sha256(b"B" * 4096).hexdigest() not in out

