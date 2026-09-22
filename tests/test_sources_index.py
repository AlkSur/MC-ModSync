# -*- coding: utf-8 -*-
"""T-52: C 端 source_index.py —— 下载源索引（拉取/缓存/查表），纯标准库、静默失败。"""
from __future__ import annotations

import json
import os

from charness import PACK_ID, Cloud, make_keys

from mcmodsync import canonicaljson, source_index
from mcmodsync import source_resolve as sr

GOOD = "https://cdn.modrinth.com/data/pid/versions/vid/a.jar"


def _index(sources: dict, pack_id: str = PACK_ID, schema: int = 1) -> dict:
    return {"schemaVersion": schema, "packId": pack_id, "generatedForVersion": "1.0.0",
            "updatedAt": "2026-09-22T15:00:00+08:00", "sources": dict(sources)}


# --------------------------------------------------------------------------
# 常量与工具
# --------------------------------------------------------------------------

def test_whitelist_matches_a_side() -> None:
    """A 端写入侧与 C 端读取侧的白名单必须完全一致（防漂移）。"""
    assert sr.ALLOWED_URL_PREFIXES == source_index.ALLOWED_URL_PREFIXES


def test_index_url_derived_from_pointer() -> None:
    assert source_index.index_url("https://h/packs/abc/manifest.json") == \
        "https://h/packs/abc/sources.json"
    assert source_index.index_url("https://h/manifest.json") == "https://h/sources.json"
    assert source_index.index_url("") == "/sources.json"


def test_is_allowed_url() -> None:
    assert source_index.is_allowed_url(GOOD)
    assert not source_index.is_allowed_url("https://modrinth.com/mod/x")
    assert not source_index.is_allowed_url("http://cdn.modrinth.com/data/x")
    assert not source_index.is_allowed_url(None)          # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 本地缓存
# --------------------------------------------------------------------------

def test_load_save_roundtrip(tmp_path) -> None:
    p = str(tmp_path / "updater" / "sources.json")
    source_index.save_cached(p, _index({"a" * 64: {"source": "modrinth",
                                                   "downloadUrl": GOOD}}))
    got = source_index.load_cached(p)
    assert source_index.count(got) == 1
    assert got["sources"]["a" * 64]["downloadUrl"] == GOOD


def test_load_cached_missing_broken_and_wrong_shape(tmp_path) -> None:
    assert source_index.load_cached(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    assert source_index.load_cached(str(bad)) == {}
    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"sources": []}', encoding="utf-8")
    assert source_index.load_cached(str(wrong)) == {}
    assert source_index.load_cached("") == {}


def test_save_cached_never_raises_on_bad_path(tmp_path) -> None:
    source_index.save_cached("", {})                       # 空路径
    source_index.save_cached("\x00bad.json", {})           # 非法路径名（ValueError 也要吞掉）
    source_index.save_cached(str(tmp_path / "ok.json"), {"sources": {}})


# --------------------------------------------------------------------------
# 查表
# --------------------------------------------------------------------------

def test_lookup_hit_and_miss() -> None:
    idx = _index({"a" * 64: {"source": "modrinth", "downloadUrl": GOOD},
                  "b" * 64: {"source": "curseforge",
                             "downloadUrl": "https://modrinth.com/mod/x"}})   # 非白名单
    assert source_index.lookup(idx, "A" * 64) == ("modrinth", GOOD)          # 大写也能命中
    assert source_index.lookup(idx, "b" * 64) is None
    assert source_index.lookup(idx, "c" * 64) is None
    assert source_index.lookup({}, "a" * 64) is None
    assert source_index.lookup(None, "a" * 64) is None                       # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 拉取（本地 HTTP 替身）
# --------------------------------------------------------------------------

def _write_index(cloud: Cloud, obj: dict) -> None:
    with open(os.path.join(cloud.root, "sources.json"), "wb") as f:
        f.write(canonicaljson.canonical(canonicaljson.sign(obj, cloud.pem)))


def test_fetch_index_ok(tmp_path) -> None:
    pem, pub = make_keys(tmp_path)
    cloud = Cloud(str(tmp_path / "cloud"), pem)
    base = cloud.start()
    try:
        _write_index(cloud, _index({"a" * 64: {"source": "modrinth", "downloadUrl": GOOD}}))
        got = source_index.fetch_index(base + "/manifest.json", pub, PACK_ID)
        assert got is not None and source_index.count(got) == 1
    finally:
        cloud.stop()


def test_fetch_index_missing_returns_none(tmp_path) -> None:
    pem, pub = make_keys(tmp_path)
    cloud = Cloud(str(tmp_path / "cloud"), pem)
    base = cloud.start()
    try:
        assert source_index.fetch_index(base + "/manifest.json", pub, PACK_ID) is None
    finally:
        cloud.stop()


def test_fetch_index_rejects_unsigned_wrong_pack_and_future_schema(tmp_path) -> None:
    pem, pub = make_keys(tmp_path)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_pem, _ = make_keys(other_dir)
    cloud = Cloud(str(tmp_path / "cloud"), pem)
    base = cloud.start()
    try:
        url = base + "/manifest.json"
        # 未签名
        with open(os.path.join(cloud.root, "sources.json"), "wb") as f:
            f.write(canonicaljson.canonical(_index({"a" * 64: {"source": "modrinth",
                                                               "downloadUrl": GOOD}})))
        assert source_index.fetch_index(url, pub, PACK_ID) is None
        # 别的私钥签的
        _write_index(cloud, _index({"a" * 64: {"source": "modrinth", "downloadUrl": GOOD}}))
        assert source_index.fetch_index(url, pub, PACK_ID) is not None
        with open(os.path.join(cloud.root, "sources.json"), "wb") as f:
            f.write(canonicaljson.canonical(canonicaljson.sign(
                _index({"a" * 64: {"source": "modrinth", "downloadUrl": GOOD}}), other_pem)))
        assert source_index.fetch_index(url, pub, PACK_ID) is None
        # packId 不符
        _write_index(cloud, _index({"a" * 64: {"source": "modrinth", "downloadUrl": GOOD}},
                                   pack_id="someone-else"))
        assert source_index.fetch_index(url, pub, PACK_ID) is None
        # schemaVersion 过新
        _write_index(cloud, _index({"a" * 64: {"source": "modrinth", "downloadUrl": GOOD}},
                                   schema=99))
        assert source_index.fetch_index(url, pub, PACK_ID) is None
    finally:
        cloud.stop()


def test_fetch_index_unreachable_returns_none_and_is_silent(tmp_path) -> None:
    traces = []
    # 127.0.0.1:1 必然连不上（测试端口）
    got = source_index.fetch_index("http://127.0.0.1:1/manifest.json", "AAAA", PACK_ID,
                                   timeout=1, trace=traces.append)
    assert got is None
    assert len(traces) == 1 and "拉取失败" in traces[0]


def test_fetch_index_without_key_or_url_returns_none() -> None:
    assert source_index.fetch_index("", "AAAA", PACK_ID) is None
    assert source_index.fetch_index("http://x/manifest.json", "", PACK_ID) is None


def test_count_tolerates_garbage() -> None:
    assert source_index.count({}) == 0
    assert source_index.count({"sources": {"x": 1, "y": 2}}) == 2
    assert source_index.count(None) == 0                  # type: ignore[arg-type]
    assert source_index.count({"sources": "not-a-dict"}) == 0


def test_saved_index_json_is_utf8_human_readable(tmp_path) -> None:
    p = str(tmp_path / "sources.json")
    source_index.save_cached(p, _index({"a" * 64: {"source": "modrinth",
                                                   "downloadUrl": GOOD,
                                                   "projectSlug": "lift-n-load"}}))
    raw = open(p, "rb").read().decode("utf-8")
    assert json.loads(raw)["sources"]["a" * 64]["projectSlug"] == "lift-n-load"
    assert "\\u" not in raw            # ensure_ascii=False
