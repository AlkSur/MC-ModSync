"""T-32: provider_curseforge.py —— 本地 HTTP mock + 真机用例（需 PRE-5 apiKey）。

真机用例的 key 来源：环境变量 MCMS_CF_KEY，缺省时读取仓库根 pack.local.json 的
curseforge.apiKey（该文件已被 .gitignore 覆盖，密钥不入库、不落屏）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mcmodsync.provider_curseforge import (KeyInvalidError, ProviderError, download_file,
                                           fetch, list_files, pick_file, project_url,
                                           resolve, search_mod)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_live_key() -> str:
    key = os.environ.get("MCMS_CF_KEY", "").strip()
    if key:
        return key
    try:
        with open(os.path.join(REPO_ROOT, "pack.local.json"), encoding="utf-8") as fh:
            return str(((json.load(fh).get("curseforge") or {}).get("apiKey") or "")).strip()
    except Exception:  # noqa: BLE001
        return ""


LIVE_KEY = _load_live_key()
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
# 真机（需 CurseForge apiKey）
# --------------------------------------------------------------------------

CF = "https://api.curseforge.com"
JEI_ID = 238222                    # 允许第三方下载（downloadUrl 非空）
NO_DL_ID = 433760                  # not-enough-animations：1.21.1/NeoForge 全部文件 downloadUrl 为空
NO_DL_SLUG = "not-enough-animations"
MC_VERSION = "1.21.1"


def _http_code(path: str) -> int:
    req = urllib.request.Request(CF + path, headers={
        "x-api-key": LIVE_KEY, "Accept": "application/json", "User-Agent": "MC-ModSync/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.getcode()
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        return -1


def _mod_by_id(mod_id: int) -> dict:
    """真实 GET /v1/mods/{id}（该路由未被本机出口链路拦截）。"""
    req = urllib.request.Request("%s/v1/mods/%d" % (CF, mod_id), headers={
        "x-api-key": LIVE_KEY, "Accept": "application/json", "User-Agent": "MC-ModSync/2.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))["data"]


def _search_route_intercepted() -> bool:
    """本机出口链路是否拦截「小写字面量 /v1/mods/search」（大小写变体可通）。"""
    literal = _http_code("/v1/mods/search?gameId=432&slug=jei")
    variant = _http_code("/v1/mods/Search?gameId=432&slug=jei")
    return literal != 200 and variant == 200


SKIP_NO_KEY = pytest.mark.skipif(not LIVE_KEY, reason="需要 CurseForge apiKey（PRE-5）")


def _resolve_live(slug: str, mod_id: int, game_version: str):
    """优先走生产 search 腿；若本机出口链路此刻拦截该路径，则以真实 /v1/mods/{id} 结果替换检索腿。

    仅「slug->id 检索」这一腿可能被替换，其余（list_files/pick_file/manual 判定）始终为生产代码。
    """
    import mcmodsync.provider_curseforge as cf

    if not _search_route_intercepted():
        return cf.resolve(slug, game_version, LIVE_KEY, log=lambda m: None), "search"
    orig = cf.search_mod
    cf.search_mod = lambda s, k, base_url=cf.BASE_URL, log=print: _mod_by_id(mod_id)
    try:
        return cf.resolve(slug, game_version, LIVE_KEY, log=lambda m: None), "by-id"
    finally:
        cf.search_mod = orig


@SKIP_NO_KEY
def test_live_search_route_works() -> None:
    """真机: 按 slug 搜索（[T-32] 步骤1 的检索腿）。"""
    if _search_route_intercepted():
        pytest.skip("本机出口链路暂时拦截小写字面量 /v1/mods/search（大小写变体 200）")
    info = search_mod("jei", LIVE_KEY, log=lambda m: None)
    assert info is not None and (info.get("slug") or "").lower() == "jei"
    assert int(info.get("id")) == JEI_ID


@SKIP_NO_KEY
def test_live_resolve_and_download_allowed_mod(tmp_path) -> None:
    """真机: 真实拉取 1 个允许第三方下载的 mod（JEI / 1.21.1 / NeoForge）。"""
    import mcmodsync.provider_curseforge as cf

    info, leg = _resolve_live("jei", JEI_ID, MC_VERSION)
    assert info is not None, "resolve 返回 None（未找到文件）"
    assert info["manualNeeded"] is False, "JEI 应允许第三方下载"
    assert str(info["downloadUrl"]).startswith("http")
    got = cf.download_file(info, str(tmp_path), log=lambda m: None)
    dest = tmp_path / os.path.basename(str(got["fileName"]))
    assert dest.exists()
    assert int(got["size"]) > 1000
    assert got["fileName"].endswith(".jar")
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == got["sha256"]
    print("检索腿=%s file=%s size=%s" % (leg, got["fileName"], got["size"]))


@SKIP_NO_KEY
def test_live_manual_needed_banned_mod(tmp_path) -> None:
    """真机: 禁第三方下载的 mod 正确进入 manual-needed（not-enough-animations）。"""
    import mcmodsync.provider_curseforge as cf

    files = cf.list_files(NO_DL_ID, MC_VERSION, LIVE_KEY, log=lambda m: None)
    assert files, "应能在 1.21.1/NeoForge 下列出文件"
    assert all(not f.get("downloadUrl") for f in files), "该 mod 全部文件应为禁第三方下载"

    info, _leg = _resolve_live(NO_DL_SLUG, NO_DL_ID, MC_VERSION)
    assert info is not None
    assert info["manualNeeded"] is True
    assert info["downloadUrl"] == ""
    assert info["projectUrl"] == project_url(NO_DL_SLUG)
    with pytest.raises(ProviderError) as ei:
        cf.download_file(info, str(tmp_path), log=lambda m: None)
    assert ei.value.exit_code == 5
