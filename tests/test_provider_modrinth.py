"""T-31: provider_modrinth.py —— 本地 HTTP mock + 真机拉取。"""
from __future__ import annotations

import hashlib
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mcmodsync import hashing
from mcmodsync.provider_modrinth import (ProviderError, download_file, list_versions,
                                         pick_file, pick_version, project_url, resolve)

LIVE = __import__("os").environ.get("MCMS_LIVE") == "1"


# --------------------------------------------------------------------------
# mock 服务器
# --------------------------------------------------------------------------

class _State:
    routes = {}          # slug -> (status, payload) | callable(attempt)->(status,payload)
    counts = {}
    paths = []
    download_body = b"JAR-BYTES" * 64


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默
        pass

    def _send(self, status: int, payload, raw: bytes = None):
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        _State.paths.append(self.path)
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.path.startswith("/download/"):
            self._send(200, None, _State.download_body)
            return
        if len(parts) == 4 and parts[0] == "v2" and parts[1] == "project" and parts[3] == "version":
            slug = parts[2]
            route = _State.routes.get(slug)
            if route is None:
                self._send(404, {"error": "not_found"})
                return
            n = _State.counts.get(slug, 0)
            _State.counts[slug] = n + 1
            status, payload = route(n) if callable(route) else route
            self._send(status, payload)
            return
        self._send(404, {"error": "not_found"})


@pytest.fixture()
def mock():
    _State.routes = {}
    _State.counts = {}
    _State.paths = []
    _State.download_body = b"JAR-BYTES" * 64
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/v2" % srv.server_address[1]
    dl = "http://127.0.0.1:%d/download" % srv.server_address[1]
    try:
        yield dict(base=base, dl=dl, state=_State)
    finally:
        srv.shutdown()
        srv.server_close()


def _version(ver_number: str, date: str, primary_url: str, size: int, primary=True):
    return {
        "version_number": ver_number,
        "date_published": date,
        "files": [
            {"filename": "extra-%s.jar" % ver_number, "url": primary_url, "size": 1,
             "primary": not primary, "hashes": {"sha1": "aa", "sha512": "bb"}},
            {"filename": "main-%s.jar" % ver_number, "url": primary_url, "size": size,
             "primary": primary, "hashes": {"sha1": "cc", "sha512": "dd"}},
        ],
    }


# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------

def test_build_query_and_filtering(mock) -> None:
    slug = "jei"
    _State.routes[slug] = (200, [_version("15.0.0", "2024-09-01T00:00:00Z",
                                          mock["dl"] + "/jei.jar", 100)])
    versions = list_versions(slug, "1.21.1", "neoforge", mock["base"], log=lambda m: None)
    assert isinstance(versions, list) and len(versions) == 1
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(_State.paths[-1]).query)
    assert json.loads(qs["loaders"][0]) == ["neoforge"]
    assert json.loads(qs["game_versions"][0]) == ["1.21.1"]


def test_project_not_found(mock) -> None:
    assert list_versions("nope", "1.21.1", base_url=mock["base"], log=lambda m: None) is None
    assert resolve("nope", "1.21.1", base_url=mock["base"], log=lambda m: None) is None


def test_pick_version_semantics() -> None:
    vs = [{"version_number": "2.0.0"}, {"version_number": "1.0.0"}]
    assert pick_version(vs, "latest")["version_number"] == "2.0.0"
    assert pick_version(vs, "")["version_number"] == "2.0.0"
    assert pick_version(vs, "1.0.0")["version_number"] == "1.0.0"
    assert pick_version(vs, "9.9.9") is None
    assert pick_version([], "latest") is None


def test_pick_file_prefers_primary() -> None:
    v = {"files": [{"filename": "a", "primary": False}, {"filename": "b", "primary": True}]}
    assert pick_file(v)["filename"] == "b"
    v2 = {"files": [{"filename": "a", "primary": False}]}
    assert pick_file(v2)["filename"] == "a"
    assert pick_file({"files": []}) is None


def test_resolve_fields(mock) -> None:
    _State.routes["jei"] = (200, [
        _version("15.3.0.4", "2024-10-01T00:00:00Z", mock["dl"] + "/jei-15.3.0.4.jar", 2048),
        _version("15.0.0", "2024-09-01T00:00:00Z", mock["dl"] + "/jei-15.0.0.jar", 1024),
    ])
    info = resolve("jei", "1.21.1", base_url=mock["base"], log=lambda m: None)
    assert info["resolvedVersion"] == "15.3.0.4"          # latest -> 列表首个
    assert info["fileName"] == "main-15.3.0.4.jar"        # primary=true
    assert info["size"] == 2048 and info["sha1"] == "cc" and info["source"] == "modrinth"

    pinned = resolve("jei", "1.21.1", version_pin="15.0.0", base_url=mock["base"],
                     log=lambda m: None)
    assert pinned["resolvedVersion"] == "15.0.0"
    assert pinned["size"] == 1024


def test_download_computes_sha256(mock, tmp_path) -> None:
    info = {"fileName": "x.jar", "downloadUrl": mock["dl"] + "/x.jar", "size": 0}
    out = download_file(info, str(tmp_path), log=lambda m: None)
    data = _State.download_body
    assert out["sha256"] == hashlib.sha256(data).hexdigest()
    assert out["size"] == len(data)
    assert hashing.hash_file(str(tmp_path / "x.jar"))["sha256"] == out["sha256"]


def test_rate_limit_retries_then_succeeds(mock) -> None:
    def route(n):
        return (429, {"error": "rate"}) if n < 2 else (200, [
            _version("1.0.0", "2024-01-01T00:00:00Z", mock["dl"] + "/a.jar", 10)])

    _State.routes["flaky"] = route
    logs = []
    vs = list_versions("flaky", "1.21.1", base_url=mock["base"], log=logs.append)
    assert len(vs) == 1
    assert any("429" in m for m in logs)


def test_rate_limit_exhausted_raises_code_5(mock) -> None:
    _State.routes["always429"] = (429, {"error": "rate"})
    with pytest.raises(ProviderError) as ei:
        list_versions("always429", "1.21.1", base_url=mock["base"], log=lambda m: None)
    assert ei.value.exit_code == 5


def test_server_error_raises_code_5(mock) -> None:
    _State.routes["boom"] = (500, {"error": "boom"})
    with pytest.raises(ProviderError) as ei:
        list_versions("boom", "1.21.1", base_url=mock["base"], log=lambda m: None)
    assert ei.value.exit_code == 5


def test_project_url() -> None:
    assert project_url("jei") == "https://modrinth.com/mod/jei"


# --------------------------------------------------------------------------
# 真机拉取
# --------------------------------------------------------------------------

@pytest.mark.skipif(not LIVE, reason="需要 MCMS_LIVE=1")
def test_live_fetch_jei_for_1_21_1(tmp_path) -> None:
    from mcmodsync.provider_modrinth import fetch

    out = fetch("jei", str(tmp_path), "1.21.1", "neoforge", "latest", log=lambda m: None)
    assert out is not None, "Modrinth 未找到 jei/1.21.1/neoforge"
    assert out["fileName"].endswith(".jar")
    assert out["size"] > 1000
    # 落盘可被 hashing 计算 sha256，且与实算一致
    again = hashing.hash_file(str(tmp_path / out["fileName"]))
    assert again["sha256"] == out["sha256"] and again["size"] == out["size"]
    with open(str(tmp_path / out["fileName"]), "rb") as f:
        assert f.read(2) == b"PK"      # jar = zip
