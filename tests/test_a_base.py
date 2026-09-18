"""T-20: A 端基础模块（config / logutil / ssh / storage.s3 / http_download）。

联网用例默认跳过，设 MCMS_LIVE=1 时启用（凭证取自 pack.local.json / ssh.json）。
"""
from __future__ import annotations

import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mcmodsync import config as mconfig
from mcmodsync import http_download, logutil
from mcmodsync.ssh import SSHClient, SSHError
from mcmodsync.storage.s3 import S3Error, S3Store

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE = os.environ.get("MCMS_LIVE") == "1"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def test_load_example_config_is_config_type() -> None:
    cfg = mconfig.load_config(str(REPO_ROOT / "pack.example.json"))
    assert isinstance(cfg, mconfig.Config)
    assert cfg["packId"] == "my-server-pack"
    assert cfg.packId == "my-server-pack"          # 属性访问
    assert cfg.server["port"] == 22                # 下标访问
    assert cfg.concurrency["upload"] == 4          # 默认值
    with pytest.raises(AttributeError):
        _ = cfg.nope


def test_load_local_config_when_present() -> None:
    p = REPO_ROOT / "pack.local.json"
    if not p.is_file():
        pytest.skip("无 pack.local.json")
    cfg = mconfig.load_config(str(p))
    assert isinstance(cfg, mconfig.Config)
    assert cfg.storage["bucket"] and cfg.client["publicKey"]


def test_config_missing_file() -> None:
    with pytest.raises(mconfig.ConfigError):
        mconfig.load_config("no-such-file.json")


def test_config_reports_missing_fields(tmp_path) -> None:
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"packId": "x", "server": {}}), encoding="utf-8")
    with pytest.raises(mconfig.ConfigError) as ei:
        mconfig.load_config(str(p))
    msg = str(ei.value)
    assert "server.host" in msg and "storage.bucket" in msg


def test_config_expands_tilde(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / "c.json"
    doc = json.loads((REPO_ROOT / "pack.example.json").read_text(encoding="utf-8"))
    doc["signing"]["privateKeyFile"] = "~/mcmodsync/private.key"
    p.write_text(json.dumps(doc), encoding="utf-8")
    cfg = mconfig.load_config(str(p))
    assert not cfg["signing"]["privateKeyFile"].startswith("~")


# --------------------------------------------------------------------------
# logutil
# --------------------------------------------------------------------------

def test_mask_and_mask_text() -> None:
    assert logutil.mask("ABCDEFGH") == "AB****GH"
    assert logutil.mask("ab") == "****"

    text = '{"accessKey": "AKIDEXAMPLE123", "password": "hunter2xyz", "note": "ok"}'
    masked = logutil.mask_text(text)
    assert "AKIDEXAMPLE123" not in masked
    assert "hunter2xyz" not in masked
    assert "ok" in masked


def test_logger_masks_credentials_in_file(tmp_path) -> None:
    log_file = tmp_path / "logs" / "run.log"
    logger = logutil.setup_logger(str(log_file), name="t20", console=False)
    logger.info('secretKey=SUPERSECRETVALUE accessKey=AKIAIOSFODNN7EXAMPLE')
    logger.info("普通消息 keep-me")
    logger.handlers[0].close()

    txt = log_file.read_text(encoding="utf-8")
    assert "SUPERSECRETVALUE" not in txt
    assert "AKIAIOSFODNN7EXAMPLE" not in txt
    assert "keep-me" in txt


# --------------------------------------------------------------------------
# http_download（本地 HTTP 服务器）
# --------------------------------------------------------------------------

@pytest.fixture()
def http_root(tmp_path):
    serve = tmp_path / "www"
    serve.mkdir()
    handler = partial(SimpleHTTPRequestHandler, directory=str(serve))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield "http://127.0.0.1:%d" % srv.server_address[1], serve
    finally:
        srv.shutdown()
        srv.server_close()


def test_download_success(http_root, tmp_path) -> None:
    import hashlib

    base, serve = http_root
    data = b"hello-blob" * 1000
    (serve / "blob.bin").write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()

    dst = tmp_path / "out" / "blob.bin"
    http_download.download(base + "/blob.bin", str(dst), sha, len(data), retries=2)
    assert dst.read_bytes() == data


def test_download_sha_mismatch_exits_5(http_root, tmp_path) -> None:
    base, serve = http_root
    (serve / "bad.bin").write_bytes(b"actual-content")
    dst = tmp_path / "out" / "bad.bin"
    with pytest.raises(http_download.DownloadError) as ei:
        http_download.download(base + "/bad.bin", str(dst), "0" * 64, 14, retries=2)
    assert ei.value.exit_code == 5
    assert not dst.exists()


def test_download_404_exits_5(http_root, tmp_path) -> None:
    base, _serve = http_root
    dst = tmp_path / "out" / "missing.bin"
    with pytest.raises(http_download.DownloadError) as ei:
        http_download.download(base + "/missing.bin", str(dst), "0" * 64, 1, retries=2)
    assert ei.value.exit_code == 5


def test_download_blobs_layout(http_root, tmp_path) -> None:
    import hashlib
    base, serve = http_root
    tasks = []
    for i, data in enumerate([b"one", b"two", b"three"]):
        sha = hashlib.sha256(data).hexdigest()
        d = serve / "blobs" / sha[:2] / sha[2:4]
        d.mkdir(parents=True, exist_ok=True)
        (d / sha).write_bytes(data)
        tasks.append({"path": "mods/%d.jar" % i, "sha256": sha, "size": len(data)})
    staging = tmp_path / "staging"
    total = http_download.download_blobs(tasks, base, str(staging), concurrency=2, retries=2)
    assert total == sum(t["size"] for t in tasks)
    for t in tasks:
        assert (staging / "blobs" / t["sha256"][:2] / t["sha256"][2:4] / t["sha256"]).is_file()


def test_proxy_env_read(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    assert http_download._build_opener() is not None


# --------------------------------------------------------------------------
# ssh（离线: 连接失败路径）
# --------------------------------------------------------------------------

def test_ssh_connect_failure_raises() -> None:
    cli = SSHClient()
    with pytest.raises(SSHError):
        cli.connect("127.0.0.1", 1, "nobody", password="x", timeout=3)


# --------------------------------------------------------------------------
# 联网用例（MCMS_LIVE=1）
# --------------------------------------------------------------------------

@pytest.mark.skipif(not LIVE, reason="需要 MCMS_LIVE=1")
def test_live_s3_roundtrip(tmp_path) -> None:
    cfg = mconfig.load_config(str(REPO_ROOT / "pack.local.json"))
    st = cfg.storage
    store = S3Store(st["endpointUrl"], st["region"], st["bucket"], "t20-live/",
                    st["accessKey"], st["secretKey"], st["pathStyle"])

    assert store.head("probe.txt") is False
    store.put_bytes("probe.txt", b"hello", "no-cache, no-store, must-revalidate")
    assert store.head("probe.txt") is True
    assert store.get_text("probe.txt") == "hello"
    assert store.get_cache_control("probe.txt") == "no-cache, no-store, must-revalidate"

    local = tmp_path / "big.bin"
    payload = os.urandom(2048)
    local.write_bytes(payload)
    store.put_file("big.bin", str(local), "public, max-age=300")
    assert store.get_object("big.bin") == payload
    assert store.get_cache_control("big.bin") == "public, max-age=300"

    store.delete("probe.txt")
    store.delete("big.bin")
    assert store.head("probe.txt") is False


@pytest.mark.skipif(not LIVE, reason="需要 MCMS_LIVE=1")
def test_live_ssh_unknown_host_rejected_by_default(tmp_path) -> None:
    """未知主机默认拒绝；accept_new_host=True 时放行一次。"""
    ssh_cfg_path = REPO_ROOT / "ssh.json"
    if not ssh_cfg_path.is_file():
        pytest.skip("无 ssh.json")
    c = json.loads(ssh_cfg_path.read_text(encoding="utf-8"))
    empty_known_hosts = str(tmp_path / "known_hosts_empty")

    strict = SSHClient()
    with pytest.raises(SSHError):
        strict.connect(c["ip"], int(c["port"]), c["username"],
                       password=c.get("password", ""),
                       known_hosts=empty_known_hosts, accept_new_host=False)

    relaxed = SSHClient()
    try:
        relaxed.connect(c["ip"], int(c["port"]), c["username"],
                        password=c.get("password", ""),
                        known_hosts=empty_known_hosts, accept_new_host=True)
        rc, out, _err = relaxed.run("echo accepted")
        assert rc == 0 and "accepted" in out
    finally:
        relaxed.close()


@pytest.mark.skipif(not LIVE, reason="需要 MCMS_LIVE=1")
def test_live_ssh_roundtrip(tmp_path) -> None:
    ssh_cfg_path = REPO_ROOT / "ssh.json"
    if not ssh_cfg_path.is_file():
        pytest.skip("无 ssh.json")
    c = json.loads(ssh_cfg_path.read_text(encoding="utf-8"))
    cli = SSHClient()
    cli.connect(c["ip"], int(c["port"]), c["username"],
                password=c.get("password", ""), accept_new_host=True)
    try:
        rc, out, err = cli.run("echo mcmodsync-t20")
        assert rc == 0 and "mcmodsync-t20" in out

        local = tmp_path / "up.txt"
        local.write_text("payload", encoding="utf-8")
        remote = "/tmp/mcmodsync-t20/up.txt"
        cli.mkdirs("/tmp/mcmodsync-t20")
        cli.put(str(local), remote)
        assert cli.stat(remote) is not None

        back = tmp_path / "back.txt"
        cli.get(remote, str(back))
        assert back.read_text(encoding="utf-8") == "payload"

        cli.remove(remote)
        assert cli.stat(remote) is None
        cli.run("rm -rf /tmp/mcmodsync-t20")
    finally:
        cli.close()
