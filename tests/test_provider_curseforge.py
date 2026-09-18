"""T-32: provider_curseforge.py —— 本地 HTTP mock（真机拉取需 PRE-5 apiKey）。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mcmodsync.provider_curseforge import (KeyInvalidError, ProviderError, download_file,
                                           fetch, list_files, pick_file, project_url,
                                           resolve, search_mod)

LIVE_KEY = os.environ.get("MCMS_CF_KEY", "")
BAD_KEY = "UNAUTHORIZED"
FORBIDDEN_KEY = "FORBIDDEN"


# --------------------------------------------------------------------------
# mock 服务器
# --------------------------------------------------------------------------

class _State:
    mods = {}          # slug -> mod dict
    files = {}         # modId -> [file dict]
    counts = {}
    paths = []
    download_body = b"CF-JAR" * 128


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, status: int, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        _State.paths.append(self.path)
        key = self.headers.get("x-api-key") or ""
        if key == BAD_KEY:
            self._json(401, {"error": "unauthorized"})
            return
        if key == FORBIDDEN_KEY:
            self._json(403, {"error": "forbidden"})
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/download/"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(_State.download_body)))
            self.end_headers()
            self.wfile.write(_State.download_body)
            return
        if parsed.path == "/v1/mods/search":
            slug = urllib.parse.parse_qs(parsed.query).get("slug", [""])[0]
            mod = _State.mods.get(slug)
            self._json(200, {"data": [mod] if mod else []})
            return
        m = re.match(r"^/v1/mods/(\d+)/files$", parsed.path)
        if m:
            mod_id = int(m.group(1))
            n = _State.counts.get(mod_id, 0)
            _State.counts[mod_id] = n + 1
            files = _State.files.get(mod_id, [])
            if os.environ.get("_CF_FLAKY") and n < 1:
                self._json(429, {"error": "rate"})
                return
            self._json(200, {"data": files})
            return
        self._json(404, {"error": "not_found"})


@pytest.fixture()
def mock(monkeypatch):
    _State.mods = {}
    _State.files = {}
    _State.counts = {}
    _State.paths = []
    _State.download_body = b"CF-JAR" * 128
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/v1" % srv.server_address[1]
    dl = "http://127.0.0.1:%d/download" % srv.server_address[1]
    try:
        yield dict(base=base, dl=dl)
    finally:
        srv.shutdown()
        srv.server_close()


def _file(name: str, url, size: int, date: str):
    return {"id": abs(hash(name)) % 10000, "displayName": name, "fileName": name,
            "downloadUrl": url, "fileLength": size, "fileDate": date}


# --------------------------------------------------------------------------
# 搜索 / 文件 / 选取
# --------------------------------------------------------------------------

def test_search_and_resolve(mock) -> None:
    _State.mods["jei"] = {"id": 238222, "slug": "jei", "name": "JEI"}
    _State.files[238222] = [
        _file("jei-1.21.1-19.0.0.jar", mock["dl"] + "/a.jar", 2048, "2024-10-01T00:00:00Z"),
        _file("jei-1.21.1-18.0.0.jar", mock["dl"] + "/b.jar", 1024, "2024-09-01T00:00:00Z"),
    ]
    mod = search_mod("jei", "KEY-1234567890", mock["base"], log=lambda m: None)
    assert mod["id"] == 238222

    files = list_files(238222, "1.21.1", "KEY-1234567890", base_url=mock["base"],
                       log=lambda m: None)
    assert [f["fileName"] for f in files] == ["jei-1.21.1-19.0.0.jar", "jei-1.21.1-18.0.0.jar"]

    info = resolve("jei", "1.21.1", "KEY-1234567890", base_url=mock["base"], log=lambda m: None)
    assert info["fileName"] == "jei-1.21.1-19.0.0.jar"
    assert info["size"] == 2048 and info["manualNeeded"] is False
    assert info["projectUrl"] == "https://www.curseforge.com/minecraft/mc-mods/jei"

    pinned = resolve("jei", "1.21.1", "KEY-1234567890", version_pin="18.0.0",
                     base_url=mock["base"], log=lambda m: None)
    assert pinned["fileName"] == "jei-1.21.1-18.0.0.jar"

    # 查询串: gameId=432 / modLoaderType=6
    qs1 = urllib.parse.parse_qs(urllib.parse.urlparse(_State.paths[0]).query)
    assert qs1["gameId"][0] == "432"
    qs2 = urllib.parse.parse_qs(urllib.parse.urlparse(_State.paths[1]).query)
    assert qs2["modLoaderType"][0] == "6" and qs2["gameVersion"][0] == "1.21.1"


def test_project_not_found(mock) -> None:
    assert search_mod("nope", "KEY-1234567890", mock["base"], log=lambda m: None) is None
    assert resolve("nope", "1.21.1", "KEY-1234567890", base_url=mock["base"],
                   log=lambda m: None) is None


def test_version_pin_not_found(mock) -> None:
    _State.mods["x"] = {"id": 1, "slug": "x"}
    _State.files[1] = [_file("x-1.0.jar", "http://h/x.jar", 10, "2024-01-01T00:00:00Z")]
    assert resolve("x", "1.21.1", "KEY-1234567890", version_pin="9.9.9",
                   base_url=mock["base"], log=lambda m: None) is None


def test_pick_file_semantics() -> None:
    fs = [_file("a-2.0.jar", "u", 1, "2024-02-01T00:00:00Z"),
          _file("a-1.0.jar", "u", 1, "2024-01-01T00:00:00Z")]
    assert pick_file(fs, "latest")["fileName"] == "a-2.0.jar"
    assert pick_file(fs, "1.0")["fileName"] == "a-1.0.jar"
    assert pick_file(fs, "9.9") is None
    assert pick_file([], "latest") is None


# --------------------------------------------------------------------------
# 禁第三方下载 -> manual-needed
# --------------------------------------------------------------------------

def test_download_url_null_marks_manual(mock) -> None:
    _State.mods["no-dl"] = {"id": 42, "slug": "no-dl"}
    _State.files[42] = [_file("no-dl-1.0.jar", None, 0, "2024-01-01T00:00:00Z")]
    info = resolve("no-dl", "1.21.1", "KEY-1234567890", base_url=mock["base"],
                   log=lambda m: None)
    assert info is not None and info["manualNeeded"] is True and info["downloadUrl"] == ""
    # fetch 不抛错，返回 manual 条目
    out = fetch("no-dl", str(mock), "1.21.1", "KEY-1234567890", base_url=mock["base"],
                log=lambda m: None)
    assert out["manualNeeded"] is True
    # 直接下载 manual 条目 -> 码 5
    with pytest.raises(ProviderError) as ei:
        download_file(info, str(mock), log=lambda m: None)
    assert ei.value.exit_code == 5


# --------------------------------------------------------------------------
# 401 / 403 / 缺 key
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key", [BAD_KEY, FORBIDDEN_KEY])
def test_key_invalid_401_403(mock, key) -> None:
    logs = []
    with pytest.raises(KeyInvalidError) as ei:
        search_mod("jei", key, mock["base"], log=logs.append)
    assert ei.value.exit_code == 5
    assert any("console.curseforge.com" in m for m in logs)     # 重新申请指引


def test_missing_key_raises(mock) -> None:
    with pytest.raises(KeyInvalidError):
        search_mod("jei", "", mock["base"], log=lambda m: None)


def test_key_masked_in_logs(mock, monkeypatch) -> None:
    """429 重试日志中的 apiKey 必须打码（只留前后各 2 位）。"""
    from mcmodsync.logutil import mask

    _State.mods["x"] = {"id": 7, "slug": "x"}
    _State.files[7] = [_file("x.jar", "u", 1, "2024-01-01T00:00:00Z")]
    monkeypatch.setenv("_CF_FLAKY", "1")
    logs = []
    secret = "SECRETKEY12345"
    assert resolve("x", "1.21.1", secret, base_url=mock["base"], log=logs.append) is not None
    assert any("429" in m for m in logs)
    assert all(secret not in m for m in logs)
    assert any(mask(secret) in m for m in logs)


# --------------------------------------------------------------------------
# 限流 / 下载
# --------------------------------------------------------------------------

def test_rate_limit_retry_then_success(mock, monkeypatch) -> None:
    _State.mods["x"] = {"id": 9, "slug": "x"}
    _State.files[9] = [_file("x-1.0.jar", "u", 1, "2024-01-01T00:00:00Z")]
    monkeypatch.setenv("_CF_FLAKY", "1")
    logs = []
    info = resolve("x", "1.21.1", "KEY-1234567890", base_url=mock["base"], log=logs.append)
    assert info is not None
    assert any("429" in m for m in logs)


def test_download_computes_sha256(mock, tmp_path) -> None:
    out = download_file({"fileName": "c.jar", "downloadUrl": mock["dl"] + "/c.jar"},
                        str(tmp_path), log=lambda m: None)
    data = _State.download_body
    assert out["sha256"] == hashlib.sha256(data).hexdigest()
    assert out["size"] == len(data)


def test_project_url() -> None:
    assert project_url("jei") == "https://www.curseforge.com/minecraft/mc-mods/jei"


# --------------------------------------------------------------------------
# 真机（需 MCMS_CF_KEY）
# --------------------------------------------------------------------------

@pytest.mark.skipif(not LIVE_KEY, reason="需要 MCMS_CF_KEY（PRE-5）")
def test_live_curseforge_fetch(tmp_path) -> None:
    info = resolve("jei", "1.21.1", LIVE_KEY, base_url="https://api.curseforge.com/v1",
                   log=lambda m: None)
    assert info is not None
    if info.get("manualNeeded"):
        pytest.skip("该项目禁第三方下载，已正确标记 manualNeeded")
    got = download_file(info, str(tmp_path), log=lambda m: None)
    assert got["size"] > 1000
