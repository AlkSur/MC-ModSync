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

from mcmodsync import canonicaljson, client, hashing, http_download, locking, source_index

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


# --------------------------------------------------------------------------
# 下载埋点（只上报，不改控制流）：字节数 / 开始 / 重新计数
# --------------------------------------------------------------------------

def test_download_instrumentation_reports_all_bytes(env) -> None:
    """on_file_* 埋点：上报的总字节 == 文件大小；开始事件携带 path/source/size。"""
    big = os.urandom(3 * 1024 * 1024)              # 3 MiB -> 多分片
    env.cloud.publish("1.0.0", {"mods/big.jar": big})
    got: list = []
    starts: list = []
    resets: list = []
    dones: list = []
    assert env.inst.sync(on_file_start=starts.append,
                         on_file_bytes=lambda sha, n: got.append(n),
                         on_file_reset=resets.append,
                         on_file_done=dones.append) == 0
    assert sum(got) == len(big)
    assert starts and starts[0]["path"] == "mods/big.jar"
    assert starts[0]["size"] == len(big) and starts[0]["source"] == ""
    assert resets == [starts[0]["sha256"]]          # 单次尝试 -> 只上报一次重新计数
    assert [e["path"] for e in dones] == ["mods/big.jar"]


def test_download_instrumentation_reset_on_segment_fallback(env) -> None:
    """分片回退单连接时再次上报重新计数（避免进度被上一轮字节撑满）。"""
    env.cloud._srv.no_range = True
    big = os.urandom(3 * 1024 * 1024)
    env.cloud.publish("1.0.0", {"mods/big.jar": big})
    starts: list = []
    resets: list = []
    assert env.inst.sync(on_file_start=starts.append,
                         on_file_reset=resets.append) == 0
    assert len(resets) == 2                        # 1 次尝试开始 + 1 次回退单连接
    assert set(resets) == {starts[0]["sha256"]}


def test_download_instrumentation_hooks_are_optional(env) -> None:
    """不传任何埋点回调时行为与原来一致（业务逻辑未被渲染代码影响）。"""
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A", "mods/b.jar": b"B"})
    assert env.inst.sync() == 0
    assert env.inst.mod_names() == ["a.jar", "b.jar"]


# --------------------------------------------------------------------------
# T-52: 多源下载（下载源索引 -> 平台直链优先，失败静默回落对象存储）
# --------------------------------------------------------------------------

def _write_index(env, mapping: dict, pack_id: str = PACK_ID, sign: bool = True) -> None:
    """把（可选的签名）下载源索引放到对象存储替身的 pack 根。"""
    obj = {"schemaVersion": 1, "packId": pack_id, "generatedForVersion": "1.0.0",
           "updatedAt": "2026-09-22T15:00:00+08:00", "sources": dict(mapping)}
    if sign:
        obj = canonicaljson.sign(obj, env.cloud.pem)
    with open(os.path.join(env.cloud.root, "sources.json"), "wb") as f:
        f.write(canonicaljson.canonical(obj))


def _platform_file(plat, name: str, data: bytes) -> str:
    d = os.path.join(plat.root, "platform")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "wb") as f:
        f.write(data)
    return plat.base_url + "/platform/" + name


@pytest.fixture
def plat(env, tmp_path):
    """平台 CDN 替身（本地 http；白名单在用例内用 monkeypatch 放行）。"""
    c = Cloud(str(tmp_path / "plat"), env.pem)
    c.start()
    yield c
    c.stop()


def _allow_plat(monkeypatch, plat) -> str:
    """真实白名单要求 https 域名，测试里改判据放行本地替身（白名单本身另有单测）。"""
    prefix = plat.base_url + "/platform/"
    monkeypatch.setattr(source_index, "is_allowed_url",
                        lambda u: isinstance(u, str) and u.startswith(prefix))
    return prefix


def test_platform_url_used_when_index_hits(env, plat, monkeypatch) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    prefix = _allow_plat(monkeypatch, plat)
    url = _platform_file(plat, "a.jar", b"A")
    sha_a = hashlib.sha256(b"A").hexdigest()
    _write_index(env, {sha_a: {"source": "modrinth", "fileName": "a.jar",
                               "size": 1, "downloadUrl": url}})
    assert env.inst.sync() == 0
    assert env.inst.mod_bytes("a.jar") == b"A"
    assert any("/platform/a.jar" in h for h in plat.hits)     # 走了平台直链
    assert not any(sha_a in h for h in env.blob_hits())       # 完全没碰对象存储 blob
    assert prefix                                  # 前缀确实被用到（防误放行）


def test_platform_failure_falls_back_silently(env, plat, monkeypatch) -> None:
    """平台 404/超时 -> 终端零输出回落，同步仍然成功。"""
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    prefix = _allow_plat(monkeypatch, plat)        # 平台上没有这个文件 -> 404
    sha_a = hashlib.sha256(b"A").hexdigest()
    _write_index(env, {sha_a: {"source": "modrinth", "fileName": "a.jar",
                               "size": 1, "downloadUrl": prefix + "missing.jar"}})
    logs: list = []
    traces: list = []
    assert env.inst.sync(log=logs.append, trace=traces.append) == 0
    assert env.inst.mod_bytes("a.jar") == b"A"
    assert any(sha_a in h for h in env.blob_hits())           # 回落成功
    assert not [m for m in logs if "平台" in m or "直链" in m or "回落" in m]
    assert any("回落对象存储" in m for m in traces)            # 只在日志文件留一行


def test_platform_content_mismatch_falls_back(env, plat, monkeypatch) -> None:
    """平台给出同名但内容不同的文件 -> sha256 不符 -> 静默回落（不会写坏磁盘）。"""
    env.cloud.publish("1.0.0", {"mods/a.jar": b"REAL"})
    prefix = _allow_plat(monkeypatch, plat)
    url = _platform_file(plat, "a.jar", b"EVIL")             # 内容不符
    sha_real = hashlib.sha256(b"REAL").hexdigest()
    _write_index(env, {sha_real: {"source": "modrinth", "downloadUrl": url}})
    logs: list = []
    assert env.inst.sync(log=logs.append) == 0
    assert env.inst.mod_bytes("a.jar") == b"REAL"
    assert not [m for m in logs if "回落" in m or "校验" in m]


def test_unsigned_index_is_ignored(env, plat, monkeypatch) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    _allow_plat(monkeypatch, plat)
    url = _platform_file(plat, "a.jar", b"A")
    sha_a = hashlib.sha256(b"A").hexdigest()
    _write_index(env, {sha_a: {"source": "modrinth", "downloadUrl": url}}, sign=False)
    logs: list = []
    traces: list = []
    assert env.inst.sync(log=logs.append, trace=traces.append) == 0
    assert any(sha_a in h for h in env.blob_hits())           # 未签名 -> 走对象存储
    assert not [m for m in logs if "索引" in m or "验签" in m]
    assert any("验签失败" in m for m in traces)


def test_cache_overwritten_on_every_sync(env, plat, monkeypatch) -> None:
    """每次（需要下载的）同步都会用线上最新索引覆盖本地老文件。"""
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    _allow_plat(monkeypatch, plat)
    sha_a = hashlib.sha256(b"A").hexdigest()
    _write_index(env, {sha_a: {"source": "modrinth",
                               "downloadUrl": plat.base_url + "/platform/a.jar"}})
    assert env.inst.sync() == 0
    p = client.sources_path(env.inst.root)
    assert os.path.isfile(p)
    assert source_index.count(source_index.load_cached(p)) == 1

    env.cloud.publish("1.0.1", {"mods/a.jar": b"A2"})          # 内容变 -> 要下载
    _write_index(env, {})                                      # 线上索引变空
    assert env.inst.sync() == 0
    assert source_index.count(source_index.load_cached(p)) == 0   # 老缓存被覆盖


def test_no_download_means_no_index_request(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    assert env.inst.sync() == 0
    env.reset_hits()
    assert env.inst.sync() == 0                    # 磁盘已一致 -> 无需下载
    assert "/sources.json" not in env.cloud.hits


def test_prefer_platform_off_skips_index(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    env.reset_hits()
    assert env.inst.sync(prefer_platform=False) == 0
    assert env.inst.mod_bytes("a.jar") == b"A"
    assert "/sources.json" not in env.cloud.hits
    assert not os.path.isfile(client.sources_path(env.inst.root))


def test_broken_local_cache_still_syncs(env) -> None:
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A"})
    p = client.sources_path(env.inst.root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{broken json")
    assert env.inst.sync() == 0
    assert env.inst.mod_bytes("a.jar") == b"A"


def test_missing_index_keeps_behaviour_unchanged(env) -> None:
    """没有 sources.json（未改造的发布端）时，行为与改造前完全一致。"""
    env.cloud.publish("1.0.0", {"mods/a.jar": b"A", "mods/b.jar": b"B"})
    sha_a = hashlib.sha256(b"A").hexdigest()
    assert env.inst.sync() == 0
    assert env.inst.mod_names() == ["a.jar", "b.jar"]
    assert any(sha_a in h for h in env.blob_hits())


# ---------------------------------------------------------------------------
# 步骤3 游戏运行检测：只认 Minecraft 客户端，不被任意 java 进程误伤
# ---------------------------------------------------------------------------

def test_mc_image_matching() -> None:
    assert client._is_mc_image("Minecraft.exe")
    assert client._is_mc_image("minecraftlauncher.exe")
    assert not client._is_mc_image("javaw.exe")        # java 宿主进程不算
    assert not client._is_mc_image("java.exe")


def test_looks_like_mc_client_true_for_clients() -> None:
    vanilla = (r'"C:\Program Files\Java\bin\javaw.exe" -XX:+UseG1GC '
               r'-Djava.library.path=C:\Users\x\.minecraft\versions\1.20.1\natives '
               r'-cp forge.jar net.minecraft.client.main.Main --username Steve '
               r'--uuid abc --gameDir C:\Users\x\.minecraft')
    assert client._looks_like_mc_client(vanilla)
    neoforge = ('javaw.exe -cp "C:\\mc\\libraries\\net\\neoforged\\neoforge\\21.1\\'
                'neoforge.jar" cpw.mods.bootstraplauncher.BootstrapLauncher '
                '--launchTarget forgeclient --username Alex --gameDir D:\\instances\\demo-pack')
    assert client._looks_like_mc_client(neoforge)


def test_looks_like_mc_client_false_for_servers_and_other_java() -> None:
    for cl in (
        "javaw.exe -jar fabric-server-launch.jar nogui",
        "java.exe -Xmx4G -jar forge-1.20.1-47.2.0.jar nogui",
        "java.exe -jar minecraft_server.1.20.1.jar nogui",
        "java.exe -Xmx2G -jar server.jar nogui",
        r'"C:\Program Files\JetBrains\IntelliJ IDEA\jbr\bin\java.exe" -Xmx750m '
        r'-Didea.launcher.port=7531 com.intellij.idea.Main',
        "java.exe -jar some-random-tool.jar --mode=convert",
        "",
    ):
        assert not client._looks_like_mc_client(cl), cl


@pytest.mark.skipif(os.name != "nt", reason="tasklist/CIM 检测仅 Windows")
def test_detect_game_running_ignores_plain_java(monkeypatch) -> None:
    """旧实现（裸 java 令牌）会把 IDEA 误判成游戏在跑；修复后必须放过。"""
    def fake_run(argv, **kw):
        r = type("R", (), {"stdout": "", "stderr": "", "returncode": 0})()
        if argv and argv[0] == "tasklist":
            r.stdout = ('"java.exe","30180","Console","1","4,370,928 K"\n'
                        '"explorer.exe","100","Console","1","50,000 K"\n')
        elif argv and argv[0] == "wmic":
            r.stdout = ("Node,CommandLine,ProcessId\n"
                        "PC,\"java.exe -Didea.launcher.port=7531 com.intellij.idea.Main\",30180\n")
        return r
    monkeypatch.setattr(client.subprocess, "run", fake_run)
    assert client.detect_game_running() is False


@pytest.mark.skipif(os.name != "nt", reason="tasklist/CIM 检测仅 Windows")
def test_detect_game_running_true_for_mc_client(monkeypatch) -> None:
    def fake_run(argv, **kw):
        r = type("R", (), {"stdout": "", "stderr": "", "returncode": 0})()
        if argv and argv[0] == "tasklist":
            r.stdout = '"javaw.exe","4001","Console","1","3,000,000 K"\n'
        elif argv and argv[0] == "wmic":
            r.stdout = ("Node,CommandLine,ProcessId\n"
                        "PC,\"javaw.exe net.minecraft.client.main.Main --username Steve\",4001\n")
        return r
    monkeypatch.setattr(client.subprocess, "run", fake_run)
    assert client.detect_game_running() is True


@pytest.mark.skipif(os.name != "nt", reason="tasklist/CIM 检测仅 Windows")
def test_detect_game_running_true_for_minecraft_image(monkeypatch) -> None:
    def fake_run(argv, **kw):
        r = type("R", (), {"stdout": "", "stderr": "", "returncode": 0})()
        if argv and argv[0] == "tasklist":
            r.stdout = '"Minecraft.exe","777","Console","1","1,000 K"\n'
        return r
    monkeypatch.setattr(client.subprocess, "run", fake_run)
    assert client.detect_game_running() is True

