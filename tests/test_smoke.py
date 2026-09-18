"""骨架冒烟测试（T-03 空套件跑通）。

目的: 证明包结构可安装、共享模块可导入、基础契约可调用。
后续各任务按 [10] 增补各自的 tests/test_*.py。
"""
from __future__ import annotations

import importlib

import pytest


def test_package_importable() -> None:
    for name in [
        "mcmodsync",
        "mcmodsync.hashing",
        "mcmodsync.paths",
        "mcmodsync.planner",
        "mcmodsync.manifest",
        "mcmodsync.locking",
        "mcmodsync.canonicaljson",
        "mcmodsync.ed25519_min",
        "mcmodsync.config",
        "mcmodsync.logutil",
        "mcmodsync.platform",
        "mcmodsync.storage.s3",
    ]:
        importlib.import_module(name)


def test_paths_rejects_escape() -> None:
    from mcmodsync import paths

    with pytest.raises(paths.UnsafePath):
        paths.normalize_rel("../etc/passwd")
    with pytest.raises(paths.UnsafePath):
        paths.normalize_rel("/abs/path")
    assert paths.normalize_rel("mods/a.jar") == "mods/a.jar"


def test_hashing_and_planner_roundtrip() -> None:
    from mcmodsync import planner

    base = {"mods/a.jar": {"sha256": "aa", "size": 1}}
    desired = {"mods/a.jar": {"sha256": "bb", "size": 2}, "mods/b.jar": {"sha256": "cc", "size": 3}}
    diff = planner.plan(base, desired)
    assert [e["path"] for e in diff["added"]] == ["mods/b.jar"]
    assert [e["path"] for e in diff["replaced"]] == ["mods/a.jar"]
    assert diff["deleted"] == []
