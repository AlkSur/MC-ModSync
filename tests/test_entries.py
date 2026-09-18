# -*- coding: utf-8 -*-
"""TL(T-42): 入口脚本 更新mod.bat / 更新mod.sh 的落档一致性与行为验收。

说明: 更新mod.bat 调用的 `_updater/mcmodsync.exe` 由 T-50 打包产出，故此处
分两层验收 ——
  (a) 与 [12.1]/[12.2] 原文逐字一致 + 换行符正确（.bat 必须 CRLF）；
  (b) 行为验收：.sh 用真实 shell 执行（含空格/中文路径、参数透传、退出码、暂停门控）；
      .bat 用「同语义替换」把 exe 换成桩程序后由 cmd.exe 实际执行其自身逻辑行。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRIES = os.path.join(REPO, "entries")
BAT = os.path.join(ENTRIES, "更新mod.bat")
SH = os.path.join(ENTRIES, "更新mod.sh")

BAT_SPEC = (
    "@echo off\n"
    "chcp 65001 >nul\n"
    'cd /d "%~dp0"\n'
    'set "TARGET=%CD%"\n'
    '"%~dp0_updater\\mcmodsync.exe" sync --target "%TARGET%" %*\n'
    'set "RC=%errorlevel%"\n'
    "echo.\n"
    'if "%RC%"=="0" (\n'
    "    echo === 更新完成，请手动启动 PCL2 或 HMCL ===\n"
    ") else (\n"
    "    echo === 更新失败，退出码 %RC%，日志见 _updater\\logs\\ ===\n"
    ")\n"
    'echo.%* | findstr /C:"--no-pause" >nul || pause\n'
    "exit /b %RC%\n"
)

SH_SPEC = (
    "#!/bin/sh\n"
    'cd "$(dirname "$0")" || exit 1\n'
    'DIR="$(pwd)"\n'
    'if [ -x "$DIR/_updater/mcmodsync-linux" ]; then\n'
    '  "$DIR/_updater/mcmodsync-linux" sync --target "$DIR" "$@"\n'
    "else\n"
    '  python3 "$DIR/_updater/mcmodsync.py" sync --target "$DIR" "$@"\n'
    "fi\n"
    "RC=$?\n"
    'echo ""\n'
    'if [ "$RC" = "0" ]; then echo "=== 更新完成，请手动启动 PCL2 或 HMCL ==="; '
    'else echo "=== 更新失败，退出码 $RC，日志见 _updater/logs/ ==="; fi\n'
    'echo "$@" | grep -q -- "--no-pause" || read -r -p "按回车退出..."\n'
    "exit $RC\n"
)


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


def _paused(text: str) -> bool:
    """pause 的提示语随系统语言而变（中文系统为「请按任意键继续」）。"""
    low = text.lower()
    return "请按任意键" in text or "press any key" in low


# --------------------------------------------------------------------------
# (a) 与 [12] 原文一致
# --------------------------------------------------------------------------

def test_bat_matches_spec_and_is_crlf() -> None:
    raw = open(BAT, "rb").read()
    assert b"\r\n" in raw, "Windows 批处理必须为 CRLF 换行"
    assert raw.count(b"\n") == raw.count(b"\r\n"), "不得存在裸 LF"
    assert _norm(raw.decode("utf-8")) == BAT_SPEC
    body = _norm(raw.decode("utf-8"))
    assert 'set "TARGET=%CD%"' in body
    assert '--target "%~dp0"' not in body, "禁止改回 --target \"%~dp0\"（末尾反斜杠+引号）"


def test_sh_matches_spec_and_is_lf() -> None:
    raw = open(SH, "rb").read()
    assert b"\r\n" not in raw, "shell 脚本必须为 LF"
    assert _norm(raw.decode("utf-8")) == SH_SPEC


# --------------------------------------------------------------------------
# (b1) .sh 真实执行
# --------------------------------------------------------------------------

SH_BIN = shutil.which("sh")


def _make_instance(tmp_path, name="实例 目录"):
    inst = tmp_path / name
    (inst / "_updater").mkdir(parents=True)
    shutil.copy2(SH, str(inst / "更新mod.sh"))
    return inst


def _stub(inst, rc, log_lines=False):
    """桩 mcmodsync-linux：打印收到的参数并以指定码退出。"""
    p = inst / "_updater" / "mcmodsync-linux"
    p.write_text("#!/bin/sh\n%s\necho \"GOT: $*\"\nexit %d\n"
                 % ("echo start" if log_lines else "", rc), encoding="utf-8")
    os.chmod(str(p), 0o755)
    return p


@pytest.mark.skipif(SH_BIN is None, reason="无 sh")
def test_sh_passes_args_and_exit_code(tmp_path) -> None:
    inst = _make_instance(tmp_path)
    _stub(inst, 7)
    out = subprocess.run([SH_BIN, str(inst / "更新mod.sh"), "--no-pause"],
                         capture_output=True, text=True, cwd=str(tmp_path))
    assert out.returncode == 7, out
    assert "更新失败，退出码 7，日志见 _updater/logs/" in out.stdout
    got = [l for l in out.stdout.splitlines() if l.startswith("GOT:")]
    assert got, out.stdout
    assert got[0].startswith("GOT: sync --target ") and got[0].endswith("--no-pause")
    assert "实例 目录" in got[0]
    assert "按回车退出" not in out.stdout, "--no-pause 时不应暂停"


@pytest.mark.skipif(SH_BIN is None, reason="无 sh")
def test_sh_success_message_and_pause_gate(tmp_path) -> None:
    inst = _make_instance(tmp_path, name="中文 目录")
    _stub(inst, 0)
    ok = subprocess.run([SH_BIN, str(inst / "更新mod.sh"), "--no-pause"],
                        capture_output=True, text=True, cwd=str(tmp_path))
    assert ok.returncode == 0
    assert "更新完成，请手动启动 PCL2 或 HMCL" in ok.stdout

    # 门控行为: 不带 --no-pause 时脚本停在 read 等待输入（父进程保持 stdin 写端 -> 不退出）
    import time
    p = subprocess.Popen([SH_BIN, str(inst / "更新mod.sh")], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         cwd=str(tmp_path))
    try:
        time.sleep(3)
        assert p.poll() is None, "未带 --no-pause 时脚本应停在 read 等待输入"
        p.stdin.write("\n")
        p.stdin.flush()
        out, _ = p.communicate(timeout=20)
        assert p.returncode == 0, out
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=20)

    # 带 --no-pause 时不得阻塞（3 秒内应已退出）
    q = subprocess.Popen([SH_BIN, str(inst / "更新mod.sh"), "--no-pause"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, cwd=str(tmp_path))
    try:
        out, _ = q.communicate(timeout=20)
        assert q.returncode == 0, out
    finally:
        if q.poll() is None:
            q.kill()
            q.wait(timeout=20)


@pytest.mark.skipif(SH_BIN is None, reason="无 sh")
def test_sh_falls_back_to_python_when_no_linux_binary(tmp_path) -> None:
    inst = _make_instance(tmp_path)
    py = inst / "_updater" / "mcmodsync.py"
    py.write_text("print('REAL')\n", encoding="utf-8")
    stubdir = tmp_path / "stub-bin"
    stubdir.mkdir()
    stub = stubdir / "python3"
    stub.write_text("#!/bin/sh\necho \"PYSTUB: $*\"\nexit 3\n", encoding="utf-8")
    os.chmod(str(stub), 0o755)
    env = dict(os.environ)
    env["PATH"] = str(stubdir) + os.pathsep + env.get("PATH", "")
    out = subprocess.run([SH_BIN, str(inst / "更新mod.sh"), "--no-pause"],
                         capture_output=True, text=True, cwd=str(tmp_path), env=env)
    assert out.returncode == 3, out
    got = [l for l in out.stdout.splitlines() if l.startswith("PYSTUB:")]
    assert got and got[0].startswith("PYSTUB: ") and "_updater/mcmodsync.py" in got[0] \
        and got[0].endswith("--no-pause"), out.stdout


# --------------------------------------------------------------------------
# (b2) .bat 自身逻辑（把 exe 换成同语义桩后由 cmd.exe 执行）
# --------------------------------------------------------------------------

CMD = shutil.which("cmd") or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                          "System32", "cmd.exe")


@pytest.mark.skipif(os.name != "nt" or not os.path.exists(CMD), reason="仅 Windows")
def test_bat_logic_with_stub_program(tmp_path) -> None:
    inst = tmp_path / "实例 目录"
    (inst / "_updater").mkdir(parents=True)
    body = open(BAT, "rb").read().decode("utf-8")
    body = body.replace('"%~dp0_updater\\mcmodsync.exe"', 'call "%~dp0_updater\\stub.cmd"')
    (inst / "更新mod.bat").write_bytes(body.encode("utf-8"))
    (inst / "_updater" / "stub.cmd").write_bytes(
        b"@echo off\r\necho STUB-ARGS=%*\r\nexit /b 7\r\n")

    out = subprocess.run([CMD, "/c", str(inst / "更新mod.bat"), "--no-pause"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert out.returncode == 7, out
    got = [l for l in out.stdout.splitlines() if l.startswith("STUB-ARGS=")]
    assert got, out.stdout
    assert got[0].startswith("STUB-ARGS=sync --target ") and got[0].endswith("--no-pause")
    assert "实例 目录" in got[0]
    assert "更新失败，退出码 7" in out.stdout
    assert not _paused(out.stdout), "--no-pause 时不应 pause"


@pytest.mark.skipif(os.name != "nt" or not os.path.exists(CMD), reason="仅 Windows")
def test_bat_pauses_without_no_pause_flag(tmp_path) -> None:
    inst = tmp_path / "dir with space"
    (inst / "_updater").mkdir(parents=True)
    body = open(BAT, "rb").read().decode("utf-8")
    body = body.replace('"%~dp0_updater\\mcmodsync.exe"', 'call "%~dp0_updater\\stub.cmd"')
    (inst / "更新mod.bat").write_bytes(body.encode("utf-8"))
    (inst / "_updater" / "stub.cmd").write_bytes(
        b"@echo off\r\necho STUB-ARGS=%*\r\nexit /b 0\r\n")
    out = subprocess.run([CMD, "/c", str(inst / "更新mod.bat")], capture_output=True,
                         text=True, stdin=subprocess.DEVNULL)
    assert out.returncode == 0
    assert "更新完成，请手动启动 PCL2 或 HMCL" in out.stdout
    assert _paused(out.stdout), "未带 --no-pause 时应暂停"
