"""Cross-platform file lock (POSIX fcntl / Windows msvcrt).

Spec sections: 3.5, 10.6
"""
from __future__ import annotations

import os
from typing import Union

EXIT_LOCK_HELD = 10


class LockHeld(Exception):
    """Raised when the lock file is already held by another process."""

    exit_code = EXIT_LOCK_HELD


class FileLock:
    """Exclusive non-blocking lock via a lock file; context manager."""

    def __init__(self, path: Union[str, os.PathLike]) -> None:
        self.path = os.fspath(path)
        self._fd = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            self._lock_fd(fd)
        except OSError:
            os.close(fd)
            raise LockHeld("锁被占用: %s" % self.path)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            self._unlock_fd(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    def _lock_fd(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fd(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
