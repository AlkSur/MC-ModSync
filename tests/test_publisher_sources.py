# -*- coding: utf-8 -*-
"""T-52: A 端来源索引 —— 生成/裁剪/继承/上传与保底重建（离线，反查全 mock）。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_publisher_minio import MemStore

from mcmodsync import publisher, signing
from mcmodsync.config import Config

A_URL = "https://cdn.modrinth.com/data/pid/versions/vid/a.jar"
B_URL = "https://edge.forgecdn.net/files/1234/567/b.jar"
DIRTY = "https://modrinth.com/mod/ https://www.curseforge.com/minecraft/mc-mods/"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stats(**kw) -> dict:
    out = {"total": 0, "lock": 0, "modrinth": 0, "curseforge": 0, "miss": 0,
           "rejected": 0, "skipped": 0}
    out.update(kw)
    return out


@pytest.fixture()
def pub(tmp_path):
    src = tmp_path / "client-mods"
    src.mkdir()
    pub_b64, key_path = signing.keygen(str(tmp_path / "private.key"))
    cfg = Config({
        "packId": "t52-pack",
        "server": {"modsDir": "mods", "sourceModsDir": str(src)},
        "client": {"sourceModsDir": str(src), "manifestUrl": "", "publicKey": pub_b64,
                   "clientStateFile": str(tmp_path / "client-publish-state.json")},
        "signing": {"privateKeyFile": key_path},
        "concurrency": {"upload": 2, "download": 2},
    })
    return dict(cfg=cfg, store=MemStore(), src=src, tmp=tmp_path,
                state=Path(cfg["client"]["clientStateFile"]))


def _publish(pub, version, **kw):
    kw.setdefault("log", lambda m: None)
    return publisher.publish_client(pub["cfg"], pub["store"], version, **kw)


def _index_of(store) -> dict:
    return json.loads(store.get_text(publisher.SOURCES_KEY))


# --------------------------------------------------------------------------
# 纯函数：build_sources_index
# --------------------------------------------------------------------------

def test_build_sources_index_prunes_and_merges() -> None:
    files = [{"path": "mods/a.jar", "sha256": sha(b"A"), "size": 1},
             {"path": "mods/b.jar", "sha256": sha(b"B"), "size": 1}]
    prev = {"sources": {
        sha(b"A"): {"source": "modrinth", "downloadUrl": A_URL, "fileName": "a.jar"},
        "deadbeef" + "0" * 56: {"source": "modrinth", "downloadUrl": A_URL},   # 已删除
    }}
    resolved = {sha(b"B"): {"source": "curseforge", "downloadUrl": B_URL,
                            "fileName": "b.jar", "size": 1}}
    idx = publisher.build_sources_index("p", "1.0.1", files, prev, resolved)
    assert set(idx["sources"]) == {sha(b"A"), sha(b"B")}         # 继承 + 新增，删除的消失
    assert idx["sources"][sha(b"A")]["downloadUrl"] == A_URL
    assert idx["sources"][sha(b"B")]["source"] == "curseforge"
    assert idx["packId"] == "p" and idx["generatedForVersion"] == "1.0.1"
    assert idx["schemaVersion"] == publisher.SOURCES_SCHEMA_VERSION


def test_build_sources_index_rejects_non_whitelist_and_manual() -> None:
    files = [{"path": "mods/a.jar", "sha256": sha(b"A"), "size": 1},
             {"path": "mods/b.jar", "sha256": sha(b"B"), "size": 1},
             {"path": "mods/c.jar", "sha256": sha(b"C"), "size": 1}]
    resolved = {
        sha(b"A"): {"source": "modrinth", "downloadUrl": DIRTY},           # 脏 URL
        sha(b"B"): {"source": "manual", "downloadUrl": A_URL},             # 非平台来源
        sha(b"C"): {"source": "modrinth", "downloadUrl": A_URL},           # 正常
    }
    idx = publisher.build_sources_index("p", "1.0.0", files, None, resolved)
    assert set(idx["sources"]) == {sha(b"C")}


def test_build_sources_index_empty_without_sources() -> None:
    idx = publisher.build_sources_index("p", "1.0.0",
                                        [{"path": "mods/a.jar", "sha256": sha(b"A"), "size": 1}],
                                        None, {})
    assert idx["sources"] == {}


# --------------------------------------------------------------------------
# publish_client 集成
# --------------------------------------------------------------------------

def test_publish_uploads_index_between_manifest_and_pointer(pub, monkeypatch) -> None:
    (pub["src"] / "a.jar").write_bytes(b"A")
    monkeypatch.setattr(publisher.source_resolve, "resolve_sources",
                        lambda *a, **kw: ({sha(b"A"): {"source": "modrinth",
                                                       "downloadUrl": A_URL, "fileName": "a.jar"}},
                                          _stats(lock=1, total=1)))
    assert _publish(pub, "1.0.0") == 0
    store = pub["store"]
    idx = _index_of(store)
    assert idx["sources"][sha(b"A")]["downloadUrl"] == A_URL
    assert store.get_cache_control(publisher.SOURCES_KEY) == publisher.SOURCES_CC
    order = store.order
    assert order.index("manifests/1.0.0.json") < order.index("sources.json") \
        < order.index("manifest.json")
    assert order[-1] == "manifest.json"


def test_index_pruned_and_inherited_across_versions(pub, monkeypatch) -> None:
    """v2 删除 b.jar -> 索引里 b 的条目消失；未变更的 a 从上一版继承（不再联网）。"""
    (pub["src"] / "a.jar").write_bytes(b"A")
    (pub["src"] / "b.jar").write_bytes(b"B")
    calls = []

    def fake(source_dir, entries, lock_index, cfg, log, **kw):
        calls.append([e["sha256"] for e in entries])
        return ({e["sha256"]: {"source": "modrinth", "downloadUrl": A_URL,
                               "fileName": e["path"].split("/")[-1]}
                 for e in entries}, _stats(modrinth=len(entries)))

    monkeypatch.setattr(publisher.source_resolve, "resolve_sources", fake)
    assert _publish(pub, "1.0.0") == 0
    assert set(_index_of(pub["store"])["sources"]) == {sha(b"A"), sha(b"B")}
    assert len(calls[0]) == 2

    (pub["src"] / "b.jar").unlink()
    assert _publish(pub, "1.0.1") == 0
    assert set(_index_of(pub["store"])["sources"]) == {sha(b"A")}
    assert len(calls) == 1                     # a 未变更 -> 全部继承，压根不触发反查


def test_backfill_resolves_everything(pub, monkeypatch) -> None:
    (pub["src"] / "a.jar").write_bytes(b"A")
    calls = []

    def fake(source_dir, entries, lock_index, cfg, log, **kw):
        calls.append(len(entries))
        return ({e["sha256"]: {"source": "modrinth", "downloadUrl": A_URL}
                 for e in entries}, _stats(lock=len(entries)))

    monkeypatch.setattr(publisher.source_resolve, "resolve_sources", fake)
    assert _publish(pub, "1.0.0") == 0
    assert calls == [1]
    assert _publish(pub, "1.0.1") == 0
    assert calls == [1]                        # 无变更 -> 全部继承，不触发反查
    assert _publish(pub, "1.0.2", backfill=True) == 0
    assert calls == [1, 1]                     # --backfill 强制重查全部


def test_no_resolve_leaves_existing_index_untouched(pub, monkeypatch) -> None:
    (pub["src"] / "a.jar").write_bytes(b"A")
    monkeypatch.setattr(publisher.source_resolve, "resolve_sources",
                        lambda *a, **kw: ({sha(b"A"): {"source": "modrinth",
                                                       "downloadUrl": A_URL}},
                                          _stats(lock=1)))
    assert _publish(pub, "1.0.0") == 0
    before = pub["store"].get_text(publisher.SOURCES_KEY)
    n_before = len(pub["store"].order)
    (pub["src"] / "a.jar").write_bytes(b"A2")
    assert _publish(pub, "1.0.1", resolve=False) == 0
    assert pub["store"].get_text(publisher.SOURCES_KEY) == before
    assert publisher.SOURCES_KEY not in pub["store"].order[n_before:]   # 索引一个字都没动


def test_index_upload_failure_does_not_block_publish(pub, monkeypatch) -> None:
    class FlakyStore(MemStore):
        def put_bytes(self, key, data, cc):
            if key == publisher.SOURCES_KEY:
                raise RuntimeError("模拟上传失败")
            return super().put_bytes(key, data, cc)

    (pub["src"] / "a.jar").write_bytes(b"A")
    monkeypatch.setattr(publisher.source_resolve, "resolve_sources",
                        lambda *a, **kw: ({sha(b"A"): {"source": "modrinth",
                                                       "downloadUrl": A_URL}},
                                          _stats(lock=1)))
    pub["store"] = FlakyStore()
    logs = []
    assert _publish(pub, "1.0.0", log=logs.append) == 0
    assert pub["store"].order[-1] == "manifest.json"
    assert not pub["store"].head(publisher.SOURCES_KEY)
    assert any("sources.json 上传失败" in m for m in logs)


def test_resolve_failure_does_not_block_publish(pub, monkeypatch) -> None:
    def boom(*a, **kw):
        raise RuntimeError("模拟反查崩溃")

    (pub["src"] / "a.jar").write_bytes(b"A")
    monkeypatch.setattr(publisher.source_resolve, "resolve_sources", boom)
    logs = []
    assert _publish(pub, "1.0.0", log=logs.append) == 0
    assert not pub["store"].head(publisher.SOURCES_KEY)
    assert any("来源索引生成失败" in m for m in logs)


def test_dry_run_reports_index_without_writing(pub, monkeypatch) -> None:
    (pub["src"] / "a.jar").write_bytes(b"A")
    monkeypatch.setattr(publisher.source_resolve, "resolve_sources",
                        lambda *a, **kw: ({sha(b"A"): {"source": "modrinth",
                                                       "downloadUrl": A_URL}},
                                          _stats(lock=1)))
    logs = []
    assert _publish(pub, "1.0.0", dry_run=True, log=logs.append) == 0
    assert pub["store"].order == []
    assert any("sources.json" in m and "1 条" in m for m in logs)


# --------------------------------------------------------------------------
# 保底命令 rebuild-sources
# --------------------------------------------------------------------------

def test_rebuild_sources_overwrites_dirty_index(pub, monkeypatch) -> None:
    (pub["src"] / "a.jar").write_bytes(b"A")
    (pub["src"] / "b.jar").write_bytes(b"B")
    pub["store"].put_bytes(publisher.SOURCES_KEY, b"{broken", "public, max-age=300")
    monkeypatch.setattr(publisher.source_resolve, "resolve_sources",
                        lambda *a, **kw: ({sha(b"A"): {"source": "modrinth",
                                                       "downloadUrl": A_URL},
                                           sha(b"B"): {"source": "curseforge",
                                                       "downloadUrl": B_URL}},
                                          _stats(lock=2, total=2)))
    logs = []
    assert publisher.rebuild_sources(pub["cfg"], pub["store"], log=logs.append) == 0
    idx = _index_of(pub["store"])
    assert set(idx["sources"]) == {sha(b"A"), sha(b"B")}
    assert pub["store"].get_cache_control(publisher.SOURCES_KEY) == publisher.SOURCES_CC
    assert any("已覆盖上传来源索引" in m for m in logs)


def test_rebuild_sources_dry_run_writes_nothing(pub, monkeypatch) -> None:
    (pub["src"] / "a.jar").write_bytes(b"A")
    monkeypatch.setattr(publisher.source_resolve, "resolve_sources",
                        lambda *a, **kw: ({}, _stats(miss=1)))
    logs = []
    assert publisher.rebuild_sources(pub["cfg"], pub["store"], log=logs.append,
                                     dry_run=True) == 0
    assert pub["store"].order == []
    assert any("dry-run" in m for m in logs)
    assert any("无平台直链" in m for m in logs)      # 未命中项在日志里列出


def test_rebuild_sources_empty_dir_raises(pub) -> None:
    with pytest.raises(publisher.PublishError):
        publisher.rebuild_sources(pub["cfg"], pub["store"], log=lambda m: None)
