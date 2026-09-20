"""TL-4 (T-22): publisher.py —— publish-client 全流程。

离线: 内存 Store + 本地 http.server（用于云端恢复/验签失败路径）。
联网: MCMS_LIVE=1 时对七彩云真实桶的 t22-live/ 前缀发布 3 个版本，
      并通过 CDN 域名验证缓存头与「本地 state 丢失后从云端恢复」。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mcmodsync import canonicaljson, publisher, signing
from mcmodsync.config import Config
from mcmodsync.storage.s3 import S3Store

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE = os.environ.get("MCMS_LIVE") == "1"


# --------------------------------------------------------------------------
# 内存 Store
# --------------------------------------------------------------------------

class MemStore:
    def __init__(self):
        self.objs: dict = {}
        self.order: list = []

    def head(self, key: str) -> bool:
        return key in self.objs

    def put_bytes(self, key: str, data: bytes, cc: str) -> None:
        self.objs[key] = (bytes(data), cc)
        self.order.append(key)

    def put_file(self, key: str, path: str, cc: str) -> None:
        with open(path, "rb") as f:
            self.objs[key] = (f.read(), cc)
        self.order.append(key)

    def get_text(self, key: str) -> str:
        return self.objs[key][0].decode("utf-8")

    def get_object(self, key: str) -> bytes:
        return self.objs[key][0]

    def delete(self, key: str) -> None:
        self.objs.pop(key, None)

    def get_cache_control(self, key: str):
        return self.objs.get(key, (None, None))[1]

    def list_objects(self, sub_prefix: str = "") -> list:
        base = sub_prefix.lstrip("/").rstrip("/") + "/" if sub_prefix else ""
        out = []
        for k, (data, _cc) in sorted(self.objs.items()):
            if base and not k.startswith(base):
                continue
            out.append({"key": k, "size": len(data), "last_modified": None})
        return out


def _blob_key(sha: str) -> str:
    return "blobs/%s/%s/%s" % (sha[:2], sha[2:4], sha)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture()
def pub(tmp_path):
    src = tmp_path / "client-mods"
    src.mkdir()
    key_path = tmp_path / "private.key"
    pub_b64, key_path = signing.keygen(str(key_path))
    cfg = Config({
        "packId": "t22-pack",
        "server": {"modsDir": "mods", "sourceModsDir": str(src)},
        "client": {"sourceModsDir": str(src), "manifestUrl": "", "publicKey": pub_b64,
                   "clientStateFile": str(tmp_path / "client-publish-state.json")},
        "signing": {"privateKeyFile": key_path},
        "concurrency": {"upload": 2, "download": 2},
    })
    store = MemStore()
    return dict(cfg=cfg, store=store, src=src, tmp=tmp_path, pub=pub_b64,
                state=Path(cfg["client"]["clientStateFile"]))


def _put(pub, mapping: dict) -> None:
    for name, data in mapping.items():
        (pub["src"] / name).write_bytes(data)


def _publish(pub, version, notes="", **kw):
    return publisher.publish_client(pub["cfg"], pub["store"], version, notes=notes,
                                    log=kw.pop("log", lambda m: None), **kw)


# --------------------------------------------------------------------------
# 纯逻辑
# --------------------------------------------------------------------------

def test_scan_client_mods_prefixes_mods_dir(tmp_path) -> None:
    (tmp_path / "a.jar").write_bytes(b"a")
    (tmp_path / "b.JAR").write_bytes(b"b")
    (tmp_path / "x.txt").write_bytes(b"x")
    files = publisher.scan_client_mods(str(tmp_path), "mods")
    assert [f["path"] for f in files] == ["mods/a.jar", "mods/b.JAR"]


def test_compute_delete_accumulates_and_removes_readded() -> None:
    old = {"files": [{"path": "mods/a.jar"}, {"path": "mods/b.jar"}, {"path": "mods/c.jar"}],
           "delete": [{"path": "mods/x.jar", "deletedInVersion": "1.0.0"}]}
    files = [{"path": "mods/a.jar"}, {"path": "mods/c.jar"}]
    d = publisher.compute_delete(old, files, "1.1.0")
    assert {e["path"]: e["deletedInVersion"] for e in d} == {
        "mods/x.jar": "1.0.0", "mods/b.jar": "1.1.0"}

    # b 重新加回 -> 从 delete 移除
    files2 = [{"path": "mods/a.jar"}, {"path": "mods/b.jar"}, {"path": "mods/c.jar"}]
    d2 = publisher.compute_delete({"files": files, "delete": d}, files2, "1.2.0")
    assert [e["path"] for e in d2] == ["mods/x.jar"]


# --------------------------------------------------------------------------
# 发布 3 个版本
# --------------------------------------------------------------------------

def test_publish_three_versions_blob_dedup_and_order(pub) -> None:
    _put(pub, {"a.jar": b"A1", "b.jar": b"B1"})
    assert _publish(pub, "1.0.0", "首版") == 0
    assert pub["store"].get_cache_control("manifest.json") == publisher.POINTER_CC
    assert pub["store"].get_cache_control("manifests/1.0.0.json") == publisher.MANIFEST_CC
    assert pub["store"].get_cache_control(_blob_key(_sha(b"A1"))) == publisher.BLOB_CC
    assert pub["store"].order[-1] == "manifest.json"          # 指针最后写入

    # v2: 仅 b 变化 -> a 的 blob 不再上传
    _put(pub, {"b.jar": b"B2"})
    before = list(pub["store"].order)
    assert _publish(pub, "1.1.0") == 0
    new_keys = pub["store"].order[len(before):]
    assert _blob_key(_sha(b"A1")) not in new_keys
    assert _blob_key(_sha(b"B2")) in new_keys
    assert pub["store"].order[-1] == "manifest.json"

    # v3: 删除 b -> 累计 delete 含 b
    (pub["src"] / "b.jar").unlink()
    assert _publish(pub, "1.2.0") == 0
    v3 = json.loads(pub["store"].get_text("manifests/1.2.0.json"))
    assert [d["path"] for d in v3["delete"]] == ["mods/b.jar"]
    assert v3["delete"][0]["deletedInVersion"] == "1.2.0"
    ptr = json.loads(pub["store"].get_text("manifest.json"))
    assert ptr["latest"] == "1.2.0" and ptr["manifestUrl"] == "manifests/1.2.0.json"

    # 签名可被公钥验证
    assert canonicaljson.verify(v3, pub["pub"]) is True
    assert canonicaljson.verify(ptr, pub["pub"]) is True

    # 本地状态为最新版本清单（含累计 delete）
    st = json.loads(pub["state"].read_text(encoding="utf-8"))
    assert st["version"] == "1.2.0" and [d["path"] for d in st["delete"]] == ["mods/b.jar"]


def test_readd_removes_from_delete(pub) -> None:
    _put(pub, {"a.jar": b"A1", "b.jar": b"B1"})
    _publish(pub, "1.0.0")
    (pub["src"] / "b.jar").unlink()
    _publish(pub, "1.1.0")
    (pub["src"] / "b.jar").write_bytes(b"B3")     # 重新加回（内容不同）
    _publish(pub, "1.2.0")
    v = json.loads(pub["store"].get_text("manifests/1.2.0.json"))
    assert v["delete"] == []
    assert "mods/b.jar" in [f["path"] for f in v["files"]]


def test_missing_version_fails(pub) -> None:
    _put(pub, {"a.jar": b"A1"})
    with pytest.raises(publisher.PublishError):
        _publish(pub, "")


def test_dry_run_no_writes(pub) -> None:
    _put(pub, {"a.jar": b"A1"})
    logs = []
    assert _publish(pub, "1.0.0", dry_run=True, log=logs.append) == 0
    assert pub["store"].objs == {}
    assert any("[dry-run]" in m for m in logs)


def test_no_source_mods_fails(pub) -> None:
    with pytest.raises(publisher.PublishError):
        _publish(pub, "1.0.0")


# --------------------------------------------------------------------------
# 验签失败 / 云端恢复（本地 http.server）
# --------------------------------------------------------------------------

@pytest.fixture()
def http_site(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % srv.server_address[1], root
    finally:
        srv.shutdown()
        srv.server_close()


def test_fetch_manifest_tampered_signature_exit_2(pub, http_site) -> None:
    base, root = http_site
    _put(pub, {"a.jar": b"A1"})
    store = pub["store"]
    _publish(pub, "1.0.0")
    (root / "manifests").mkdir()
    (root / "manifest.json").write_text(store.get_text("manifest.json"), encoding="utf-8")

    ver = json.loads(store.get_text("manifests/1.0.0.json"))
    ver["files"][0]["sha256"] = "0" * 64          # 篡改载荷（签名不再匹配）
    (root / "manifests" / "1.0.0.json").write_text(
        json.dumps(ver, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(publisher.PublishError) as ei:
        publisher.fetch_published_manifest(base + "/manifest.json", pub["pub"],
                                           log=lambda m: None)
    assert ei.value.exit_code == 2


def test_recover_base_from_cloud_keeps_delete(pub, http_site) -> None:
    base, root = http_site
    store = pub["store"]
    _put(pub, {"a.jar": b"A1", "b.jar": b"B1"})
    _publish(pub, "1.0.0")
    (pub["src"] / "b.jar").unlink()
    _publish(pub, "1.1.0")                        # 累计 delete={mods/b.jar}

    # 把云端内容放成静态站点
    (root / "manifests").mkdir()
    (root / "manifest.json").write_text(store.get_text("manifest.json"), encoding="utf-8")
    (root / "manifests" / "1.1.0.json").write_text(
        store.get_text("manifests/1.1.0.json"), encoding="utf-8")

    pub["cfg"]["client"]["manifestUrl"] = base + "/manifest.json"
    pub["state"].unlink()                         # 本地状态丢失

    (pub["src"] / "c.jar").write_bytes(b"C1")
    assert _publish(pub, "1.2.0") == 0
    v2 = json.loads(store.get_text("manifests/1.2.0.json"))
    # 从云端恢复的基准保留了 mods/b.jar 的累计 delete
    assert [d["path"] for d in v2["delete"]] == ["mods/b.jar"]


def test_recover_from_cloud_via_urllib_reads_pointer(pub, http_site) -> None:
    base, root = http_site
    _put(pub, {"a.jar": b"A1"})
    _publish(pub, "1.0.0")
    (root / "manifests").mkdir()
    (root / "manifest.json").write_text(pub["store"].get_text("manifest.json"), encoding="utf-8")
    (root / "manifests" / "1.0.0.json").write_text(
        pub["store"].get_text("manifests/1.0.0.json"), encoding="utf-8")
    pointer, ver = publisher.fetch_published_manifest(base + "/manifest.json", pub["pub"],
                                                      log=lambda m: None)
    assert pointer["latest"] == "1.0.0" and ver["version"] == "1.0.0"


# --------------------------------------------------------------------------
# 联网: 七彩云真实桶（t22-live/ 前缀）+ CDN
# --------------------------------------------------------------------------

@pytest.mark.skipif(not LIVE, reason="需要 MCMS_LIVE=1")
def test_live_publish_three_versions_with_cdn(tmp_path) -> None:
    cfg_local = Config(json.loads((REPO_ROOT / "pack.local.json").read_text(encoding="utf-8")))
    st = cfg_local["storage"]
    prefix = "t22-live/"
    store = S3Store(st["endpointUrl"], st["region"], st["bucket"], prefix,
                    st["accessKey"], st["secretKey"], st["pathStyle"])
    cdn = st["publicBaseUrl"].rstrip("/") + "/" + prefix

    src = tmp_path / "client-mods"
    src.mkdir()
    pub_b64, key_path = signing.keygen(str(tmp_path / "private.key"))
    cfg = Config({
        "packId": "t22-live-pack",
        "server": {"modsDir": "mods", "sourceModsDir": str(src)},
        "client": {"sourceModsDir": str(src), "manifestUrl": cdn + "manifest.json",
                   "publicKey": pub_b64, "clientStateFile": str(tmp_path / "state.json")},
        "signing": {"privateKeyFile": key_path},
        "concurrency": {"upload": 4, "download": 4},
    })

    (src / "a.jar").write_bytes(b"A1")
    (src / "b.jar").write_bytes(b"B1")
    assert publisher.publish_client(cfg, store, "1.0.0", notes="首版", log=lambda m: None) == 0

    (src / "b.jar").write_bytes(b"B2")
    assert publisher.publish_client(cfg, store, "1.1.0", notes="换 B", log=lambda m: None) == 0

    (src / "b.jar").unlink()
    (src / "c.jar").write_bytes(b"C1")
    assert publisher.publish_client(cfg, store, "1.2.0", log=lambda m: None) == 0

    # 缓存头（经 CDN 匿名读 + HEAD 语义）
    def _head_cc(key):
        req = urllib.request.Request(cdn + key, method="GET")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.headers.get("Cache-Control", "")
    assert "no-cache" in _head_cc("manifest.json")
    assert "max-age=300" in _head_cc("manifests/1.2.0.json")

    ptr = json.loads(urllib.request.urlopen(cdn + "manifest.json", timeout=20).read())
    assert ptr["latest"] == "1.2.0"
    assert publisher.verify_manifest(ptr, pub_b64) is True

    # 本地 state 丢失 -> 从 CDN 云端恢复后发布
    os.remove(str(tmp_path / "state.json"))
    (src / "d.jar").write_bytes(b"D1")
    assert publisher.publish_client(cfg, store, "1.3.0", log=lambda m: None) == 0
    v = json.loads(urllib.request.urlopen(cdn + "manifests/1.3.0.json", timeout=20).read())
    assert [d["path"] for d in v["delete"]] == ["mods/b.jar"]

    # 清理测试前缀
    import boto3
    from botocore.config import Config as BotoConfig
    cli = boto3.client("s3", endpoint_url=st["endpointUrl"], region_name=st["region"],
                       aws_access_key_id=st["accessKey"], aws_secret_access_key=st["secretKey"],
                       config=BotoConfig(s3={"addressing_style": "path"}))
    resp = cli.list_objects_v2(Bucket=st["bucket"], Prefix=prefix)
    for o in resp.get("Contents", []):
        cli.delete_object(Bucket=st["bucket"], Key=o["Key"])


# --------------------------------------------------------------------------
# 存储清理（GC）：对象存储只保留「当前版本引用的 blob + 最新一份版本清单」
# --------------------------------------------------------------------------

def test_gc_removes_unreferenced_blobs_and_old_manifests(pub) -> None:
    _put(pub, {"a.jar": b"A1", "b.jar": b"B1"})
    assert _publish(pub, "1.0.0") == 0
    store = pub["store"]
    old_blob = _blob_key(_sha(b"A1"))
    assert old_blob in store.objs
    assert "manifests/1.0.0.json" in store.objs

    _put(pub, {"a.jar": b"A2"})                 # a.jar 内容变了
    assert _publish(pub, "1.0.1") == 0

    assert old_blob not in store.objs, "不再被引用的旧 blob 应被删除"
    assert _blob_key(_sha(b"A2")) in store.objs, "新 blob 必须保留"
    assert _blob_key(_sha(b"B1")) in store.objs, "仍被引用的 blob 必须保留"
    assert "manifests/1.0.0.json" not in store.objs, "旧版本清单应被删除"
    assert "manifests/1.0.1.json" in store.objs
    assert "manifest.json" in store.objs, "指针必须保留"


def test_gc_can_be_disabled(pub) -> None:
    _put(pub, {"a.jar": b"A1"})
    assert _publish(pub, "1.0.0") == 0
    old_blob = _blob_key(_sha(b"A1"))
    _put(pub, {"a.jar": b"A2"})
    assert _publish(pub, "1.0.1", gc=False) == 0
    assert old_blob in pub["store"].objs, "gc=False 时应保留旧 blob"
    assert "manifests/1.0.0.json" in pub["store"].objs
