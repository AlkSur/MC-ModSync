"""TL-3 (T-21): pusher.py —— B 引导 / push-server / rollback-server。

离线: 以 LocalConn 充当远端（把 POSIX 绝对路径映射到本地临时目录，
      并就地调用本机单文件 B），可完整驱动 push-server / rollback-server。
联网: MCMS_LIVE=1 时在真实主机的隔离测试目录内跑全流程。
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from mcmodsync import pusher
from mcmodsync.config import Config
from mcmodsync.ssh import SSHClient, SSHError

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE = os.environ.get("MCMS_LIVE") == "1"


# --------------------------------------------------------------------------
# 远端替身
# --------------------------------------------------------------------------

class LocalConn:
    """把 "/<abs>" 映射到 root/<abs>，并就地执行单文件 B。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.drop_blobs_once = False
        self.calls: list = []

    def _p(self, remote: str) -> Path:
        r = str(remote)
        assert r.startswith("/"), "LocalConn 仅支持绝对路径: %r" % r
        return self.root / r.lstrip("/")

    def mkdirs(self, remote_dir: str) -> None:
        self._p(remote_dir).mkdir(parents=True, exist_ok=True)

    def put(self, local: str, remote: str) -> None:
        p = self._p(remote)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, p)

    def get(self, remote: str, local: str) -> None:
        shutil.copyfile(self._p(remote), local)

    def stat(self, remote: str):
        p = self._p(remote)
        if not p.is_file():
            return None
        st = p.stat()
        return types.SimpleNamespace(st_size=st.st_size)

    def remove(self, remote: str) -> None:
        p = self._p(remote)
        if p.is_file():
            p.unlink()

    def close(self) -> None:
        pass

    def run(self, cmd, timeout: int = 600):
        self.calls.append(cmd)
        prog = str(cmd).split(" && ")[-1]
        toks = shlex.split(prog)
        if toks[0] == "cp":
            shutil.copyfile(self._p(toks[2]), self._p(toks[3]))
            return (0, "", "")
        if toks[0] == "python3" and toks[1] == "-c":
            path = self._p(toks[-1])
            if not path.is_file():
                return (1, "", "no such file")
            return (0, hashlib.sha256(path.read_bytes()).hexdigest() + "\n", "")
        if toks[0] == "python3":
            b_local = self._p(toks[1])
            if not b_local.is_file():
                return (1, "", "b not found: %s" % toks[1])
            args = [str(self._p(t)) if t.startswith("/") else t for t in toks[2:]]
            if "apply" in args and self.drop_blobs_once:
                self.drop_blobs_once = False
                staging = args[args.index("--staging") + 1]
                blobs = Path(staging) / "blobs"
                if blobs.is_dir():
                    shutil.rmtree(blobs)
            proc = subprocess.run([sys.executable, str(b_local)] + args,
                                  capture_output=True, text=True)
            return (proc.returncode, proc.stdout, proc.stderr)
        return (1, "", "unsupported: %s" % prog)


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

def _make_cfg(root: Path, source_mods: Path, client_mods: Path) -> Config:
    return Config({
        "packId": "t21-pack",
        "server": {
            "host": "localhost", "port": 22, "user": "u",
            "serverDir": "/www/srv", "modsDir": "mods",
            "remoteBPath": "/www/mcmodsync-b.py",
            "historyDir": "/www/mcmodsync-history", "historyKeep": 3,
            "sourceModsDir": str(source_mods),
        },
        "client": {"sourceModsDir": str(client_mods), "manifestUrl": "", "publicKey": ""},
    })


@pytest.fixture()
def env(tmp_path):
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    src = tmp_path / "server-mods"
    src.mkdir()
    cli = tmp_path / "client-mods"
    cli.mkdir()
    cfg = _make_cfg(remote_root, src, cli)
    conn = LocalConn(remote_root)
    return types.SimpleNamespace(root=remote_root, src=src, cli=cli, cfg=cfg, conn=conn,
                                 tmp=tmp_path, logs=[])


def _logf(env):
    def _log(msg):
        env.logs.append(str(msg))
    return _log


def _write_mods(env, mapping):
    for name, data in mapping.items():
        (env.src / name).write_bytes(data)
        (env.cli / name).write_bytes(data)


# --------------------------------------------------------------------------
# 纯逻辑
# --------------------------------------------------------------------------

def test_make_version_format() -> None:
    v = pusher.make_version(datetime(2026, 9, 18, 15, 30, 2, 314000))
    assert v == "srv-20260918-153002-314"
    assert len(pusher.make_version()) == len("srv-20260918-153002-314")


def test_to_desired_entries_prefixes_mods_dir(tmp_path) -> None:
    (tmp_path / "b.jar").write_bytes(b"b")
    (tmp_path / "a.jar").write_bytes(b"a")
    entries = pusher.scan_source_mods(str(tmp_path))
    files = pusher.to_desired_entries(entries, "mods")
    assert [f["path"] for f in files] == ["mods/a.jar", "mods/b.jar"]
    assert all(f["size"] == 1 and len(f["sha256"]) == 64 for f in files)


def test_intersection_check(tmp_path) -> None:
    (tmp_path / "only-client.jar").write_bytes(b"x")
    server = [{"path": "mods/only-client.jar"}, {"path": "mods/only-server.jar"}]
    assert pusher.intersection_check(server, str(tmp_path)) == ["only-server.jar"]


def test_staging_paths_and_blob_rel() -> None:
    p = pusher.staging_paths("/www/srv", "srv-1")
    assert p["staging"] == "/www/srv/.mcmodsync-staging"
    assert p["desired"] == "/www/srv/.mcmodsync-staging/desired-srv-1.json"
    sha = "ab" + "cd" + "0" * 60
    assert pusher.blob_rel(sha) == "blobs/ab/cd/" + sha


def test_apply_with_retry_retries_once_on_11() -> None:
    calls = {"apply": 0, "reup": 0}

    def run_apply():
        calls["apply"] += 1
        return (11, "", "staging") if calls["apply"] == 1 else (0, "applied", "")

    def reupload():
        calls["reup"] += 1

    rc, out, _ = pusher.apply_with_retry(run_apply, reupload, lambda m: None)
    assert rc == 0 and calls == {"apply": 2, "reup": 1}


def test_apply_with_retry_passes_through_other_codes() -> None:
    calls = {"reup": 0}
    rc, _out, err = pusher.apply_with_retry(lambda: (4, "", "disk"),
                                            lambda: calls.__setitem__("reup", 1), lambda m: None)
    assert rc == 4 and calls["reup"] == 0 and err == "disk"


def test_write_push_log(tmp_path) -> None:
    p = pusher.write_push_log(str(tmp_path / "logs"),
                              {"version": "srv-1", "result": "ok"})
    lines = Path(p).read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["version"] == "srv-1"


# --------------------------------------------------------------------------
# B 引导
# --------------------------------------------------------------------------

def test_bootstrap_first_upload(env) -> None:
    log = _logf(env)
    assert pusher.bootstrap_b(env.conn, "/www/srv", "/www/mcmodsync-b.py", log) is True
    assert (env.root / "www/mcmodsync-b.py").is_file()
    assert (env.root / "www/mcmodsync-b.py").read_bytes() == Path(pusher.b_local_path()).read_bytes()
    assert any("协议版本校验通过" in m for m in env.logs)


def test_bootstrap_no_change(env) -> None:
    log = _logf(env)
    pusher.bootstrap_b(env.conn, "/www/srv", "/www/mcmodsync-b.py", log)
    env.logs.clear()
    assert pusher.bootstrap_b(env.conn, "/www/srv", "/www/mcmodsync-b.py", log) is False
    assert any("跳过上传" in m for m in env.logs)


def test_bootstrap_upgrade_backs_up_old(env) -> None:
    log = _logf(env)
    old = env.root / "www/mcmodsync-b.py"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"OLD-B")
    assert pusher.bootstrap_b(env.conn, "/www/srv", "/www/mcmodsync-b.py", log) is True
    bak = env.root / "www/mcmodsync-b.py.bak"
    assert bak.read_bytes() == b"OLD-B"
    assert old.read_bytes() == Path(pusher.b_local_path()).read_bytes()


def test_bootstrap_protocol_mismatch_mentions_restore(env, monkeypatch) -> None:
    log = _logf(env)
    monkeypatch.setattr(pusher, "PROTOCOL_VERSION", "MC-ModSync-B 9.9")
    with pytest.raises(pusher.PushError) as ei:
        pusher.bootstrap_b(env.conn, "/www/srv", "/www/mcmodsync-b.py", log)
    assert ".bak" in str(ei.value)


# --------------------------------------------------------------------------
# push-server
# --------------------------------------------------------------------------

def test_push_server_full_flow(env) -> None:
    _write_mods(env, {"a.jar": b"A1", "b.jar": b"B1"})
    log = _logf(env)
    rc = pusher.push_server(env.cfg, env.conn, log=log,
                            tmp_dir=str(env.tmp / "t"), log_dir=str(env.tmp / "logs"))
    assert rc == 0, env.logs
    srv = env.root / "www/srv/mods"
    assert sorted(p.name for p in srv.glob("*.jar")) == ["a.jar", "b.jar"]
    # desired 完整
    hist = env.root / "www/mcmodsync-history/t21-pack"
    ver_dirs = list(hist.iterdir())
    assert len(ver_dirs) == 1
    ch = json.loads((ver_dirs[0] / "changes.json").read_text(encoding="utf-8"))
    assert len(ch["changes"]["added"]) == 2
    # staging 已清空
    assert list((env.root / "www/srv/.mcmodsync-staging").iterdir()) == []
    assert (env.tmp / "logs/push-log.jsonl").is_file()


def test_push_server_dry_run_no_writes(env) -> None:
    _write_mods(env, {"a.jar": b"A1"})
    log = _logf(env)
    rc = pusher.push_server(env.cfg, env.conn, dry_run=True, log=log,
                            tmp_dir=str(env.tmp / "t"), log_dir=str(env.tmp / "logs"))
    assert rc == 0
    assert not (env.root / "www/srv/mods").exists() or list((env.root / "www/srv/mods").glob("*.jar")) == []
    assert not (env.root / "www/srv/.mcmodsync-staging").exists()
    assert any("[dry-run]" in m for m in env.logs)
    assert not (env.tmp / "logs/push-log.jsonl").exists()


def test_push_server_retries_on_code_11(env) -> None:
    _write_mods(env, {"a.jar": b"A1", "b.jar": b"B2"})
    log = _logf(env)
    env.conn.drop_blobs_once = True          # 首次 apply 前清掉远端 blob -> 码 11
    rc = pusher.push_server(env.cfg, env.conn, log=log,
                            tmp_dir=str(env.tmp / "t"), log_dir=str(env.tmp / "logs"))
    assert rc == 0, env.logs
    assert any("重传 desired 与全部 blob" in m for m in env.logs)
    srv = env.root / "www/srv/mods"
    assert sorted(p.name for p in srv.glob("*.jar")) == ["a.jar", "b.jar"]


def test_push_server_intersection_warn_and_block(env) -> None:
    _write_mods(env, {"a.jar": b"A1"})
    assert pusher.push_server(env.cfg, env.conn, log=_logf(env),
                              tmp_dir=str(env.tmp / "t"), log_dir=str(env.tmp / "logs")) == 0
    # 服务端被手工放入一个客户端源目录没有的 mod
    (env.root / "www/srv/mods/server-only.jar").write_bytes(b"SO")

    log = _logf(env)
    rc = pusher.push_server(env.cfg, env.conn, dry_run=True, log=log,
                            tmp_dir=str(env.tmp / "t"), log_dir=str(env.tmp / "logs"))
    assert rc == 0
    assert any("服务端存在而客户端源目录缺少" in m for m in env.logs)

    with pytest.raises(pusher.PushError):
        pusher.push_server(env.cfg, env.conn, dry_run=True, check_server_client=True,
                           log=_logf(env), tmp_dir=str(env.tmp / "t"),
                           log_dir=str(env.tmp / "logs"))


def test_push_server_no_source_is_error(env) -> None:
    with pytest.raises(pusher.PushError):
        pusher.push_server(env.cfg, env.conn, log=_logf(env),
                           tmp_dir=str(env.tmp / "t"), log_dir=str(env.tmp / "logs"))


def test_rollback_server_flow(env) -> None:
    _write_mods(env, {"a.jar": b"A1"})
    log = _logf(env)
    assert pusher.push_server(env.cfg, env.conn, log=log,
                              tmp_dir=str(env.tmp / "t"), log_dir=str(env.tmp / "logs")) == 0
    env.logs.clear()
    assert pusher.rollback_server(env.cfg, env.conn, log=log) == 0
    assert list((env.root / "www/srv/mods").glob("*.jar")) == []
    assert any("手动重启" in m for m in env.logs)


# --------------------------------------------------------------------------
# 联网（真实主机隔离目录）
# --------------------------------------------------------------------------

@pytest.mark.skipif(not LIVE, reason="需要 MCMS_LIVE=1")
def test_live_unknown_host_rejected_by_connect_ssh(tmp_path) -> None:
    ssh_cfg = REPO_ROOT / "ssh.json"
    if not ssh_cfg.is_file():
        pytest.skip("无 ssh.json")
    c = json.loads(ssh_cfg.read_text(encoding="utf-8"))
    cfg = Config({"server": {"host": c["ip"], "port": int(c["port"]), "user": c["username"],
                             "password": c.get("password", ""), "keyFile": "",
                             "knownHosts": str(tmp_path / "empty_known_hosts")}})
    with pytest.raises(SSHError):
        pusher.connect_ssh(cfg, accept_new_host=False)


@pytest.mark.skipif(not LIVE, reason="需要 MCMS_LIVE=1")
def test_live_push_and_rollback_in_isolated_dir(tmp_path) -> None:
    ssh_cfg = REPO_ROOT / "ssh.json"
    if not ssh_cfg.is_file():
        pytest.skip("无 ssh.json")
    c = json.loads(ssh_cfg.read_text(encoding="utf-8"))
    scratch = "/www/mcmodsync-t21test"
    src = tmp_path / "server-mods"
    cli = tmp_path / "client-mods"
    src.mkdir()
    cli.mkdir()
    for name, data in {"live-a.jar": b"A1", "live-b.jar": b"B1"}.items():
        (src / name).write_bytes(data)
        (cli / name).write_bytes(data)

    cfg = Config({
        "packId": "t21-live-pack",
        "server": {"host": c["ip"], "port": int(c["port"]), "user": c["username"],
                   "password": c.get("password", ""), "keyFile": "",
                   "knownHosts": "", "serverDir": scratch + "/server", "modsDir": "mods",
                   "remoteBPath": scratch + "/mcmodsync-b.py",
                   "historyDir": scratch + "/history", "historyKeep": 3,
                   "sourceModsDir": str(src)},
        "client": {"sourceModsDir": str(cli), "manifestUrl": "", "publicKey": ""},
    })
    conn = pusher.connect_ssh(cfg, accept_new_host=True)
    try:
        conn.run("rm -rf %s" % scratch)
        logs = []
        rc = pusher.push_server(cfg, conn, log=logs.append,
                                tmp_dir=str(tmp_path / "t"),
                                log_dir=str(tmp_path / "logs"))
        assert rc == 0, logs
        srv = scratch + "/server/mods"
        _rc, out, _err = conn.run("cd %s && ls -1 *.jar | sort" % srv)
        assert out.split() == ["live-a.jar", "live-b.jar"]

        # 二次 push（增量）: 替换一个
        (src / "live-b.jar").write_bytes(b"B2")
        (cli / "live-b.jar").write_bytes(b"B2")
        conn.run("rm -rf %s/.mcmodsync-staging" % scratch)
        rc = pusher.push_server(cfg, conn, log=logs.append,
                                tmp_dir=str(tmp_path / "t"),
                                log_dir=str(tmp_path / "logs"))
        assert rc == 0, logs

        roll_logs = []
        assert pusher.rollback_server(cfg, conn, log=roll_logs.append) == 0
        _rc, out, _err = conn.run("cd %s && python3 -c \"import hashlib;"
                                  "print(hashlib.sha256(open('live-b.jar','rb').read()).hexdigest())\"" % srv)
        # 回滚 v2 -> 恢复 v1 状态（live-b.jar 回到 B1）
        assert out.strip() == hashlib.sha256(b"B1").hexdigest()
    finally:
        conn.run("rm -rf %s" % scratch)
        conn.close()
