# -*- coding: utf-8 -*-
"""T-52: source_resolve.py —— 平台反查 + 直链白名单（全离线，网络全 mock）。"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

from mcmodsync import source_resolve as sr

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# 白名单
# --------------------------------------------------------------------------

def test_allowed_url_whitelist() -> None:
    assert sr.is_allowed_url("https://cdn.modrinth.com/data/abc/versions/v/f.jar")
    assert sr.is_allowed_url("https://edge.forgecdn.net/files/1234/567/x.jar")
    assert sr.is_allowed_url("https://CDN.Modrinth.com/data/x")            # 域名大小写不敏感


def test_allowed_url_rejects_dirty_and_foreign() -> None:
    # lock 里的真实脏数据：两个项目主页 URL 还粘在一起
    dirty = "https://modrinth.com/mod/ https://www.curseforge.com/minecraft/mc-mods/"
    assert not sr.is_allowed_url(dirty)
    assert not sr.is_allowed_url("https://modrinth.com/mod/lift-n-load")   # 主页而非文件
    assert not sr.is_allowed_url("http://cdn.modrinth.com/data/x")         # 非 https
    assert not sr.is_allowed_url("https://evil.example.com/cdn.modrinth.com/data/x")
    assert not sr.is_allowed_url("")
    assert not sr.is_allowed_url(None)                                     # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 哈希算法与 tools/gen_lock.py 完全一致（防止搬迁时改错）
# --------------------------------------------------------------------------

def _load_gen_lock():
    spec = importlib.util.spec_from_file_location("_gen_lock_probe",
                                                  REPO_ROOT / "tools" / "gen_lock.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                                            # type: ignore[union-attr]
    return mod


def test_hash_algorithms_match_tool(tmp_path) -> None:
    f = tmp_path / "x.jar"
    f.write_bytes(b"PK\x03\x04" + bytes(range(256)) + b"\n \t\r some payload")
    tool = _load_gen_lock()
    assert sr.cf_murmur2(str(f)) == tool.cf_murmur2(str(f))
    assert sr.file_hashes(str(f)) == tool.file_hashes(str(f))
    # 空白字节要被忽略（CF 指纹的语义）
    assert sr.cf_murmur2(str(f)) == sr.cf_murmur2(str(f))


def test_murmur2_ignores_whitespace_bytes(tmp_path) -> None:
    plain = tmp_path / "a.jar"
    plain.write_bytes(b"ABC")
    spaced = tmp_path / "b.jar"
    spaced.write_bytes(b"A\x09B\x0aC\x0d \x20")
    assert sr.cf_murmur2(str(plain)) == sr.cf_murmur2(str(spaced))


def test_file_hashes_values(tmp_path) -> None:
    f = tmp_path / "a.jar"
    data = b"hello"
    f.write_bytes(data)
    h = sr.file_hashes(str(f))
    assert h["sha256"] == hashlib.sha256(data).hexdigest()
    assert h["sha1"] == hashlib.sha1(data).hexdigest()
    assert h["size"] == len(data)


# --------------------------------------------------------------------------
# lock 索引
# --------------------------------------------------------------------------

def test_load_lock_index_missing_and_broken(tmp_path) -> None:
    assert sr.load_lock_index(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert sr.load_lock_index(str(bad)) == {}


def test_load_lock_index_by_sha256(tmp_path) -> None:
    p = tmp_path / "mods.lock.json"
    p.write_text('{"schemaVersion":1,"mods":[{"sha256":"AB12","source":"modrinth"}]}',
                 encoding="utf-8")
    idx = sr.load_lock_index(str(p))
    assert "ab12" in idx          # 键统一小写


# --------------------------------------------------------------------------
# resolve_sources
# --------------------------------------------------------------------------

def _entry(tmp_path, name: str = "a.jar", data: bytes = b"A") -> dict:
    (tmp_path / name).write_bytes(data)
    return {"path": "mods/%s" % name,
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def test_lock_hit_uses_no_network(tmp_path, monkeypatch) -> None:
    e = _entry(tmp_path)
    url = "https://cdn.modrinth.com/data/pid/versions/vid/a.jar"
    lock = {e["sha256"]: {"source": "modrinth", "downloadUrl": url,
                          "projectSlug": "lift-n-load", "resolvedVersion": "1.2.3"}}

    def boom(*a, **kw):
        raise AssertionError("lock 命中时不应联网")

    monkeypatch.setattr(sr, "mr_lookup_batch", boom)
    monkeypatch.setattr(sr, "cf_lookup", boom)
    resolved, stats = sr.resolve_sources(str(tmp_path), [e], lock, {}, log=lambda m: None)
    assert resolved[e["sha256"]]["downloadUrl"] == url
    assert resolved[e["sha256"]]["source"] == "modrinth"
    assert stats["lock"] == 1 and stats["miss"] == 0


def test_dirty_lock_url_rejected_then_resolved_by_modrinth(tmp_path, monkeypatch) -> None:
    e = _entry(tmp_path)
    dirty = "https://modrinth.com/mod/ https://www.curseforge.com/minecraft/mc-mods/"
    lock = {e["sha256"]: {"source": "manual", "downloadUrl": dirty}}
    good = "https://cdn.modrinth.com/data/pid/versions/vid/a.jar"
    monkeypatch.setattr(sr, "mr_lookup_batch",
                        lambda hashes, **kw: {h.lower(): {
                            "project_id": "pid", "version_number": "1.2.3",
                            "files": [{"primary": True, "url": good}]} for h in hashes})
    monkeypatch.setattr(sr, "mr_projects", lambda ids: {"pid": "lift-n-load"})
    resolved, stats = sr.resolve_sources(str(tmp_path), [e], lock, {}, log=lambda m: None)
    assert resolved[e["sha256"]]["downloadUrl"] == good
    assert resolved[e["sha256"]]["projectSlug"] == "lift-n-load"
    assert stats["rejected"] == 1 and stats["modrinth"] == 1


def test_modrinth_primary_file_preferred(tmp_path, monkeypatch) -> None:
    e = _entry(tmp_path)
    monkeypatch.setattr(sr, "mr_lookup_batch", lambda hashes, **kw: {
        h.lower(): {"project_id": "pid", "version_number": "2.0",
                    "files": [{"primary": False, "url": "https://cdn.modrinth.com/data/x/secondary.jar"},
                              {"primary": True, "url": "https://cdn.modrinth.com/data/x/primary.jar"}]}
        for h in hashes})
    monkeypatch.setattr(sr, "mr_projects", lambda ids: {})
    resolved, _ = sr.resolve_sources(str(tmp_path), [e], {}, {}, log=lambda m: None)
    assert resolved[e["sha256"]]["downloadUrl"].endswith("primary.jar")


def test_curseforge_fingerprint_path(tmp_path, monkeypatch) -> None:
    e = _entry(tmp_path, "cf.jar", b"CF-PAYLOAD")
    monkeypatch.setattr(sr, "mr_lookup_batch", lambda hashes, **kw: {})
    url = "https://edge.forgecdn.net/files/1234/567/cf.jar"
    monkeypatch.setattr(sr, "cf_lookup",
                        lambda fps, key: {fps[0]: (99, 555, url)})
    monkeypatch.setattr(sr, "cf_mod_slug", lambda mod_id, key: "some-mod")
    cfg = {"curseforge": {"apiKey": "KEY"}}
    resolved, stats = sr.resolve_sources(str(tmp_path), [e], {}, cfg, log=lambda m: None)
    assert resolved[e["sha256"]]["source"] == "curseforge"
    assert resolved[e["sha256"]]["downloadUrl"] == url
    assert resolved[e["sha256"]]["projectSlug"] == "some-mod"


def test_curseforge_null_download_url_not_written(tmp_path, monkeypatch) -> None:
    e = _entry(tmp_path, "nodl.jar", b"NODL")
    monkeypatch.setattr(sr, "mr_lookup_batch", lambda hashes, **kw: {})
    monkeypatch.setattr(sr, "cf_lookup", lambda fps, key: {fps[0]: (7, 8, None)})
    cfg = {"curseforge": {"apiKey": "KEY"}}
    resolved, stats = sr.resolve_sources(str(tmp_path), [e], {}, cfg, log=lambda m: None)
    assert resolved == {}
    assert stats["miss"] == 1 and stats["rejected"] == 1


def test_cf_skipped_without_api_key(tmp_path, monkeypatch) -> None:
    e = _entry(tmp_path)
    monkeypatch.setattr(sr, "mr_lookup_batch", lambda hashes, **kw: {})

    def boom(*a, **kw):
        raise AssertionError("无 apiKey 时不应调用 CurseForge")

    monkeypatch.setattr(sr, "cf_lookup", boom)
    resolved, stats = sr.resolve_sources(str(tmp_path), [e], {}, {}, log=lambda m: None)
    assert resolved == {} and stats["miss"] == 1


def test_allow_network_false_marks_all_miss(tmp_path) -> None:
    e = _entry(tmp_path)
    url = "https://cdn.modrinth.com/data/pid/versions/vid/a.jar"
    lock = {e["sha256"]: {"source": "modrinth", "downloadUrl": url}}
    resolved, stats = sr.resolve_sources(str(tmp_path), [e], lock, {}, log=lambda m: None,
                                         allow_network=False)
    assert resolved[e["sha256"]]["downloadUrl"] == url     # lock 命中照旧
    e2 = _entry(tmp_path, "b.jar", b"B")
    resolved2, stats2 = sr.resolve_sources(str(tmp_path), [e2], {}, {}, log=lambda m: None,
                                           allow_network=False)
    assert resolved2 == {} and stats2["miss"] == 1


def test_unreadable_file_is_skipped(tmp_path, monkeypatch) -> None:
    e = {"path": "mods/ghost.jar", "sha256": "0" * 64, "size": 1}
    monkeypatch.setattr(sr, "mr_lookup_batch", lambda hashes, **kw: {})
    resolved, stats = sr.resolve_sources(str(tmp_path), [e], {}, {}, log=lambda m: None)
    assert resolved == {} and stats["skipped"] == 1


def test_modrinth_batch_failure_is_tolerated(tmp_path, monkeypatch) -> None:
    e = _entry(tmp_path)
    monkeypatch.setattr(sr, "mr_lookup_batch", lambda hashes, **kw: None)
    resolved, stats = sr.resolve_sources(str(tmp_path), [e], {}, {}, log=lambda m: None)
    assert resolved == {} and stats["miss"] == 1


def test_empty_entries(tmp_path) -> None:
    resolved, stats = sr.resolve_sources(str(tmp_path), [], {}, {}, log=lambda m: None)
    assert resolved == {} and stats["total"] == 0
