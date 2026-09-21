# -*- coding: utf-8 -*-
"""TL-5: C 端 client.py 全场景测试（本地 HTTP 模拟对象存储）。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from charness import PACK_ID, Cloud, Instance, make_keys

from mcmodsync import client, hashing, http_download, locking

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

class Env:
    def __init__(self, tmp_path, root_name="inst"):
        self.tmp = tmp_path
        self.pem, self.pub = make_keys(tmp_path)
        self.cloud = Cloud(str(tmp_path / "cloud"), self.pem, PACK_ID)
        self.base = self.cloud.start()
        self.inst = Instance(str(tmp_path / root_name), self.base + "/manifest.json", self.pub)

    def close(self):
        self.cloud.stop()

    def blob_hits(self) -> list:
        return [h for h in self.cloud.hits if h.startswith("/blobs/")]

    def reset_hits(self) -> None:
        self.cloud.hits.clear()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def _changes(inst) -> dict:
    root = client.backup_root(inst.root)
    dirs = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    assert dirs, "无备份目录"
    with open(os.path.join(root, dirs[-1], "changes.json"), "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# TL-5: 首次全量 + 连续 3 个版本增量
# --------------------------------------------------------------------------

def test_first_full_sync(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A", "mods/b.jar": b"B"})
    logs = []
    assert env.inst.sync(log=logs.append) == 0
    assert env.inst.mod_names() == ["a.jar", "b.jar"]
    st = env.inst.state()
    assert st["version"] == "1.0.0" and st["packId"] == PACK_ID
    assert [f["path"] for f in st["files"]] == ["mods/a.jar", "mods/b.jar"]
    assert any("新增 2" in m for m in logs)


def test_three_incremental_versions(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A", "mods/b.jar": b"B"})
    assert env.inst.sync() == 0

    # v2: 改 a、加 c（b 不应被下载）
    env.cloud.publish("1.0.1", {"mods/a.jar": b"A2", "mods/b.jar": b"B", "mods/c.jar": b"C"})
    env.reset_hits()
    assert env.inst.sync() == 0
    assert env.inst.mod_bytes("a.jar") == b"A2"
    assert env.inst.mod_bytes("c.jar") == b"C"
    assert env.inst.state()["version"] == "1.0.1"
    sha_b = hashlib.sha256(b"B").hexdigest()
    assert not any(sha_b in h for h in env.blob_hits()), "未变更的 b.jar 不应被下载"

    # v3: 删除 b
    env.cloud.publish("1.0.2", {"mods/a.jar": b"A2", "mods/c.jar": b"C"},
                      delete=["mods/b.jar"])
    assert env.inst.sync() == 0
    assert env.inst.mod_names() == ["a.jar", "c.jar"]
    assert env.inst.state()["version"] == "1.0.2"

    # v4: 全量替换内容
    env.cloud.publish("1.0.3", {"mods/a.jar": b"A3", "mods/c.jar": b"C3", "mods/d.jar": b"D"})
    assert env.inst.sync() == 0
    assert env.inst.mod_names() == ["a.jar", "c.jar", "d.jar"]
    assert env.inst.mod_bytes("c.jar") == b"C3"


# --------------------------------------------------------------------------
# TL-5: 同一次同步内改名零下载
# --------------------------------------------------------------------------

def test_rename_within_same_sync_needs_no_download(env) -> None:
    env.cloud.publish("1.0.0", {"mods/old.jar": b"SAME"})
    assert env.inst.sync() == 0
    env.cloud.publish("1.0.1", {"mods/new.jar": b"SAME"}, delete=["mods/old.jar"])
    env.reset_hits()
    logs = []
    assert env.inst.sync(log=logs.append) == 0
    assert env.inst.mod_names() == ["new.jar"]
    assert env.inst.mod_bytes("new.jar") == b"SAME"
    assert env.blob_hits() == [], "改名应复用磁盘现存文件、不产生下载"
    assert any("改名复用 1" in m for m in logs)


# --------------------------------------------------------------------------
# TL-5: 宽松保留自装 / --strict 备份删除
# --------------------------------------------------------------------------

def test_loose_keeps_self_installed_and_strict_deletes(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    assert env.inst.sync() == 0
    env.inst.put_mods({"self.jar": b"SELF"})

    env.cloud.publish("1.0.1", {"mods/a.jar": b"A"})
    assert env.inst.sync() == 0
    assert "self.jar" in env.inst.mod_names(), "宽松模式不应删除玩家自装 jar"

    env.cloud.publish("1.0.2", {"mods/a.jar": b"A"})
    assert env.inst.sync(strict=True) == 0
    assert "self.jar" not in env.inst.mod_names(), "--strict 应删除玩家自装 jar"
    ch = _changes(env.inst)["changes"]
    strict = [e for e in ch["deleted"] if e.get("reason") == "strict"]
    assert [e["path"] for e in strict] == ["mods/self.jar"]
    bk = sorted(os.listdir(client.backup_root(env.inst.root)))[-1]
    assert os.path.isfile(os.path.join(client.backup_root(env.inst.root), bk,
                                       "backup", "mods", "self.jar"))


# --------------------------------------------------------------------------
# TL-5: .jar.disabled 不删不同步 / files 优先于 delete
# --------------------------------------------------------------------------

def test_jar_disabled_untouched(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    env.inst.put_mods({"x.jar.disabled": b"DIS"})
    assert env.inst.sync() == 0
    assert "x.jar.disabled" in env.inst.mod_names()
    assert all("disabled" not in f["path"] for f in env.inst.state()["files"])

    # 连 strict 也不应动它
    env.cloud.publish("1.0.1", {"mods/a.jar": b"A"})
    assert env.inst.sync(strict=True) == 0
    assert "x.jar.disabled" in env.inst.mod_names()


def test_files_wins_over_delete(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"}, delete=["mods/a.jar"])
    assert env.inst.sync() == 0
    assert env.inst.mod_names() == ["a.jar"], "files 应优先于 delete，不得删除"
    ch = _changes(env.inst)["changes"]
    assert ch["deleted"] == []


# --------------------------------------------------------------------------
# TL-5: 篡改 blob / 伪造指针或清单
# --------------------------------------------------------------------------

def test_tampered_blob_exit5(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    sha = hashlib.sha256(b"A").hexdigest()
    with open(env.cloud.blob_path(sha), "wb") as f:
        f.write(b"CORRUPT")
    assert env.inst.sync() == 5
    assert env.inst.mod_names() == [], "校验失败不应落位"


def test_forged_pointer_exit2(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"}, sign=False)
    assert env.inst.sync() == 2


def test_tampered_manifest_exit2(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    p = os.path.join(env.cloud.root, "manifests", "1.0.0.json")
    with open(p, "r", encoding="utf-8") as f:
        obj = json.load(f)
    obj["files"][0]["size"] = 999999          # 篡改后签名不再匹配
    with open(p, "wb") as f:
        f.write(json.dumps(obj).encode("utf-8"))
    assert env.inst.sync() == 2


# --------------------------------------------------------------------------
# TL-5: packId 不符 / schemaVersion 过新
# --------------------------------------------------------------------------

def test_packid_mismatch_exit7(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"}, pack_id="someone-else")
    assert env.inst.sync() == 7


def test_schema_too_new_exit6(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"}, schema=2)
    assert env.inst.sync() == 6


# --------------------------------------------------------------------------
# TL-5: 版本回退
# --------------------------------------------------------------------------

def test_downgrade_warns_and_continues(env) -> None:
    env.cloud.publish("1.0.1", {"mods/a.jar": b"A1"})
    assert env.inst.sync() == 0
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A0"})
    logs = []
    assert env.inst.sync(log=logs.append) == 0
    assert any("版本回退" in m and m.startswith("WARN") for m in logs)
    assert env.inst.mod_bytes("a.jar") == b"A0"


def test_no_downgrade_exit1(env) -> None:
    env.cloud.publish("1.0.1", {"mods/a.jar": b"A1"})
    assert env.inst.sync() == 0
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A0"})
    assert env.inst.sync(no_downgrade=True) == 1
    assert env.inst.mod_bytes("a.jar") == b"A1", "拒绝回退时不得修改磁盘"


# --------------------------------------------------------------------------
# TL-5: 游戏运行 / 并发双击
# --------------------------------------------------------------------------

def test_game_running_exit3(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    logs = []
    assert env.inst.sync(log=logs.append, game_running_check=lambda: True) == 3
    assert env.inst.mod_names() == []
    assert any("正在运行" in m for m in logs)


def test_concurrent_lock_exit10(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    code = ("import sys,time\n"
            "sys.path.insert(0, %r)\n"
            "from mcmodsync import client, locking\n"
            "lk = locking.FileLock(client.lock_path(%r))\n"
            "lk.acquire()\n"
            "print('HELD', flush=True)\n"
            "time.sleep(20)\n") % (REPO, env.inst.root)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, cwd=REPO)
    try:
        line = p.stdout.readline().strip()
        assert line == "HELD", (line, p.stderr.read())
        assert env.inst.sync() == 10
    finally:
        p.kill()
        p.wait(timeout=20)


# --------------------------------------------------------------------------
# TL-5: 下载重试耗尽 / 代理环境变量
# --------------------------------------------------------------------------

def test_download_retry_exhausted_exit5(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    sha = hashlib.sha256(b"A").hexdigest()
    os.remove(env.cloud.blob_path(sha))
    logs = []
    assert env.inst.sync(log=logs.append) == 5
    assert any("重试" in m for m in logs)


class _Proxy(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        self.server.count += 1                     # type: ignore[attr-defined]
        rel = urllib.parse.urlsplit(self.path).path.lstrip("/")
        full = os.path.join(self.server.root, rel.replace("/", os.sep))  # type: ignore[attr-defined]
        body = b""
        if os.path.isfile(full):
            with open(full, "rb") as f:
                body = f.read()
        self.send_response(200 if body else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_proxy_env_used(env, monkeypatch) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Proxy)
    srv.root = env.cloud.root        # type: ignore[attr-defined]
    srv.count = 0                    # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:%d" % srv.server_address[1])
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        assert env.inst.sync() == 0
        assert srv.count > 0, "应经由 HTTP_PROXY 访问对象存储"
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------------------------
# TL-5: 崩溃注入后重跑收敛
# --------------------------------------------------------------------------

@pytest.mark.parametrize("point", ["after-backup", "mid-apply", "after-state"])
def test_crash_injection_then_converge(env, monkeypatch, point) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A", "mods/b.jar": b"B"})
    monkeypatch.setenv("MCMODSYNC_FAULT", point)
    assert env.inst.sync() == 99, "注入点应中断"
    monkeypatch.delenv("MCMODSYNC_FAULT")
    logs = []
    assert env.inst.sync(log=logs.append) == 0
    assert env.inst.mod_names() == ["a.jar", "b.jar"]
    assert env.inst.mod_bytes("a.jar") == b"A"
    assert env.inst.mod_bytes("b.jar") == b"B"
    assert env.inst.state()["version"] == "1.0.0"
    st = env.inst.state()
    for f in st["files"]:
        disk = hashing.hash_file(os.path.join(env.inst.root, f["path"]))
        assert str(disk["sha256"]) == f["sha256"]


# --------------------------------------------------------------------------
# TL-5: rollback 三类反向操作 + 重复 rollback 拒绝
# --------------------------------------------------------------------------

def test_rollback_three_kinds_and_repeat_rejected(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A", "mods/old.jar": b"O", "mods/rep.jar": b"R"})
    assert env.inst.sync() == 0
    env.cloud.publish("1.0.1", {"mods/a.jar": b"A", "mods/rep.jar": b"R2", "mods/new.jar": b"N"},
                      delete=["mods/old.jar"])
    assert env.inst.sync() == 0
    assert env.inst.mod_names() == ["a.jar", "new.jar", "rep.jar"]

    logs = []
    assert env.inst.rollback(log=logs.append) == 0
    assert env.inst.mod_names() == ["a.jar", "old.jar", "rep.jar"]
    assert env.inst.mod_bytes("rep.jar") == b"R"      # replaced 反向还原
    assert env.inst.mod_bytes("old.jar") == b"O"      # deleted 反向恢复
    assert env.inst.state()["version"] is None

    root = client.backup_root(env.inst.root)
    d = sorted(os.listdir(root))[-1]
    assert os.path.isfile(os.path.join(root, d, "changes.json.rolled-back"))

    assert env.inst.rollback() == 1, "重复 rollback 应被拒绝"


def test_rollback_drift_moves_aside(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    assert env.inst.sync() == 0
    env.cloud.publish("1.0.1", {"mods/a.jar": b"A2"})
    assert env.inst.sync() == 0
    env.inst.put_mods({"a.jar": b"PLAYER-EDIT"})      # 手工漂移
    assert env.inst.rollback() == 0
    assert env.inst.mod_bytes("a.jar") == b"A"
    root = client.backup_root(env.inst.root)
    d = sorted(os.listdir(root))[-1]
    drift = os.path.join(root, d, "rollback-drift", "mods", "a.jar")
    assert os.path.isfile(drift)
    with open(drift, "rb") as f:
        assert f.read() == b"PLAYER-EDIT"


# --------------------------------------------------------------------------
# TL-5: --keep-backups / 中文与空格路径
# --------------------------------------------------------------------------

def test_keep_backups_three(env) -> None:
    for i in range(4):
        env.cloud.publish("1.0.%d" % i, {"mods/a.jar": b"A%d" % i})
        assert env.inst.sync(keep_backups=3) == 0
    assert len(client.list_backups(env.inst.root)) == 3


def test_paths_with_spaces_and_chinese(tmp_path) -> None:
    pem, pub = make_keys(tmp_path)
    cloud = Cloud(str(tmp_path / "cloud"), pem, PACK_ID)
    base = cloud.start()
    try:
        cloud.publish("1.0.0", {"mods/中文 模组.jar": b"ZH", "mods/a b.jar": b"SP"})
        inst = Instance(str(tmp_path / "实例 目录"), base + "/manifest.json", pub)
        logs = []
        assert inst.sync(log=logs.append) == 0
        assert inst.mod_names() == ["a b.jar", "中文 模组.jar"]
        assert inst.mod_bytes("中文 模组.jar") == b"ZH"
        assert inst.verify() == 0
    finally:
        cloud.stop()


# --------------------------------------------------------------------------
# TL-5: verify 退出码与输出
# --------------------------------------------------------------------------

def test_verify_clean_then_dirty(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A", "mods/b.jar": b"B"})
    assert env.inst.sync() == 0
    logs = []
    assert env.inst.verify(log=logs.append) == 0
    assert any("与云端一致" in m for m in logs)

    env.inst.put_mods({"extra.jar": b"X"})                 # 多余
    env.inst.put_mods({"a.jar": b"A-CHANGED"})             # 损坏
    env.inst.remove_mods(["b.jar"])                        # 缺失
    logs = []
    assert env.inst.verify(log=logs.append) == 1
    text = "\n".join(logs)
    assert "缺失: 1" in text and "损坏: 1" in text and "多余（玩家自装）: 1" in text
    assert "存在差异" in text


def test_verify_detects_downgrade(env) -> None:
    env.cloud.publish("1.0.1", {"mods/a.jar": b"A1"})
    assert env.inst.sync() == 0
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A0"})
    logs = []
    assert env.inst.verify(log=logs.append) == 1
    assert any("版本回退: 是" in m for m in logs)


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def test_doctor_ok_then_bad_config(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    assert env.inst.doctor() == 0
    with open(client.config_path(env.inst.root), "w", encoding="utf-8") as f:
        f.write(json.dumps({"schemaVersion": 1, "packId": PACK_ID}))
    assert env.inst.doctor() == 1


# --------------------------------------------------------------------------
# HTTPS 证书失败诊断（玩家机器时间/根证书问题）
# --------------------------------------------------------------------------

def test_report_ssl_failure_diagnoses_expired_cert() -> None:
    import ssl
    import urllib.error

    err = urllib.error.URLError(
        ssl.SSLCertVerificationError(1, "certificate has expired"))
    err.filename = "https://cdn.example.invalid/packs/x/manifest.json"
    lines = []
    hit = client.report_ssl_failure(err, lines.append)
    assert hit is True
    text = "\n".join(lines)
    assert "HTTPS 证书校验失败" in text
    assert "本机时间" in text
    assert "排查顺序" in text and "根证书" in text
    assert "浏览器" in text


def test_report_ssl_failure_ignores_plain_network_error() -> None:
    import urllib.error

    err = urllib.error.URLError(OSError("connection refused"))
    lines = []
    assert client.report_ssl_failure(err, lines.append) is False
    assert lines == []


def test_san_covers_matches_wildcard_and_detects_interception() -> None:
    good = {"subjectAltName": (("DNS", "*.cdn.7caiyun.com"),)}
    assert client._san_covers(good, "server-mods-u0demo00.cdn.7caiyun.com") is True
    # 被安全软件/代理拦截时，出示的证书是发给别的主机的
    fake = {"subjectAltName": (("DNS", "*.antivirus-vendor.example"),)}
    assert client._san_covers(fake, "server-mods-u0demo00.cdn.7caiyun.com") is False
    assert client._san_covers({}, "cdn.example.com") is False


def test_flatten_name_reads_subject_fields() -> None:
    seq = ((("commonName", "*.cdn.7caiyun.com"),),
           (("organizationName", "Example"),))
    text = client._flatten_name(seq)
    assert "commonName=*.cdn.7caiyun.com" in text
    assert "organizationName=Example" in text


def test_ssl_cert_info_skips_unknown_host() -> None:
    assert client._ssl_cert_info("") == {}
    assert client._ssl_cert_info("?") == {}


# --------------------------------------------------------------------------
# 大文件多分片并发下载
# --------------------------------------------------------------------------

def test_sync_segmented_download_large_file(env) -> None:
    """≥2MB 的文件走多分片并发下载，合并后内容必须与清单一致。"""
    big = os.urandom(3 * 1024 * 1024)          # 3 MiB -> 3 片
    env.cloud.publish("1.0.0", {"mods/big.jar": big})
    assert env.inst.sync() == 0
    assert env.inst.mod_bytes("big.jar") == big


def test_sync_falls_back_when_range_unsupported(env) -> None:
    """源站不支持 Range（返回 200）时应回退单连接，下载仍然成功。"""
    env.cloud._srv.no_range = True             # 模拟不支持分片的源站
    big = os.urandom(3 * 1024 * 1024)
    env.cloud.publish("1.0.0", {"mods/big.jar": big})
    assert env.inst.sync() == 0
    assert env.inst.mod_bytes("big.jar") == big


def test_auto_segments_thresholds() -> None:
    """分片策略：小于 2MB 不分片；大文件按约 1MB 切片且不超过上限。"""
    assert http_download._auto_segments(1024) == 1
    assert http_download._auto_segments(2 * 1024 * 1024 - 1) == 1
    assert http_download._auto_segments(3 * 1024 * 1024) == 3
    assert http_download._auto_segments(100 * 1024 * 1024) == http_download.SEGMENT_MAX
    assert http_download._auto_segments(100 * 1024 * 1024, limit=2) == 2
