"""TL-1: locking.py —— 排他锁与码 10。"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from mcmodsync.locking import FileLock, LockHeld

REPO_ROOT = str(Path(__file__).resolve().parents[1])


def test_exit_code_is_10() -> None:
    assert LockHeld.exit_code == 10


def test_acquire_release_roundtrip(tmp_path) -> None:
    p = str(tmp_path / "l.lock")
    lock = FileLock(p)
    lock.acquire()
    lock.acquire()  # 重入同一实例无副作用
    lock.release()

    again = FileLock(p)
    again.acquire()
    again.release()


def test_context_manager(tmp_path) -> None:
    p = str(tmp_path / "l.lock")
    with FileLock(p) as lk:
        assert lk is not None
    with FileLock(p):
        pass


def test_second_process_cannot_lock(tmp_path) -> None:
    """同机第二个进程持锁时，本进程获取应失败（码 10）。"""
    p = str(tmp_path / "l.lock")
    child_code = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from mcmodsync.locking import FileLock\n"
        "l = FileLock(%r)\n"
        "l.acquire()\n"
        "sys.stdout.write('LOCKED\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n" % (REPO_ROOT, p)
    )
    proc = subprocess.Popen([sys.executable, "-c", child_code],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 15
        line = ""
        while time.time() < deadline:
            line = (proc.stdout.readline() or "").strip()  # type: ignore[union-attr]
            if line:
                break
        assert line == "LOCKED", "子进程未能获取锁"

        with pytest.raises(LockHeld):
            FileLock(p).acquire()
    finally:
        proc.kill()
        proc.wait(timeout=10)
