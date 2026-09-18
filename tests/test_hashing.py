"""TL-1: hashing.py —— 流式 SHA-256 与平铺扫描。"""
from __future__ import annotations

import hashlib
import os

import pytest

from mcmodsync import hashing


def _write(path, data: bytes) -> str:
    with open(path, "wb") as f:
        f.write(data)
    return str(path)


class TestHashFile:
    @pytest.mark.parametrize("size", [0, 1, 1024, 1024 * 1024 + 7, 3 * 1024 * 1024 + 12345])
    def test_matches_hashlib(self, tmp_path, size: int) -> None:
        data = os.urandom(size)
        p = _write(tmp_path / ("f-%d.bin" % size), data)
        got = hashing.hash_file(p)
        assert got["sha256"] == hashlib.sha256(data).hexdigest()
        assert got["size"] == size

    def test_empty_file(self, tmp_path) -> None:
        p = _write(tmp_path / "empty.bin", b"")
        got = hashing.hash_file(p)
        assert got["sha256"] == hashlib.sha256(b"").hexdigest()
        assert got["size"] == 0

    def test_one_byte(self, tmp_path) -> None:
        p = _write(tmp_path / "one.bin", b"\x00")
        assert hashing.hash_file(p)["sha256"] == hashlib.sha256(b"\x00").hexdigest()


def test_hash_bytes() -> None:
    assert hashing.hash_bytes(b"abc") == hashlib.sha256(b"abc").hexdigest()


class TestScanTree:
    def test_flat_only_and_case_insensitive(self, tmp_path) -> None:
        mods = tmp_path / "mods"
        mods.mkdir()
        (mods / "b.JAR").write_bytes(b"b")
        (mods / "a.jar").write_bytes(b"a")
        (mods / "notes.txt").write_bytes(b"t")
        (mods / "c.jar.disabled").write_bytes(b"d")
        sub = mods / "nested"
        sub.mkdir()
        (sub / "inner.jar").write_bytes(b"i")

        names = [e["path"] for e in hashing.scan_tree(str(tmp_path))]
        assert names == ["mods/a.jar", "mods/b.JAR"]

    def test_missing_mods_dir_returns_empty(self, tmp_path) -> None:
        assert hashing.scan_tree(str(tmp_path)) == []

    def test_recursive_mode_includes_subdirs(self, tmp_path) -> None:
        mods = tmp_path / "mods"
        mods.mkdir()
        (mods / "a.jar").write_bytes(b"a")
        sub = mods / "nested"
        sub.mkdir()
        (sub / "inner.jar").write_bytes(b"i")
        names = [e["path"] for e in hashing.scan_tree(str(tmp_path), recursive=True)]
        assert names == ["mods/a.jar", "mods/nested/inner.jar"]

    def test_entries_are_file_entry_dicts(self, tmp_path) -> None:
        mods = tmp_path / "mods"
        mods.mkdir()
        (mods / "a.jar").write_bytes(b"a")
        e = hashing.scan_tree(str(tmp_path))[0]
        assert isinstance(e, dict)
        assert set(e.keys()) >= {"path", "sha256", "size"}


def test_entry_map() -> None:
    entries = [{"path": "mods/a.jar", "sha256": "x", "size": 1}]
    assert hashing.entry_map(entries) == {"mods/a.jar": entries[0]}
