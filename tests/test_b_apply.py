"""TL-2 (apply): 子进程驱动单文件 B 的全部 apply 场景。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest
from bharness import B_PATH, Pack, read_json, run_b, sha256_bytes

import server.b_main as b_main  # 仅用于磁盘/同分区两例的进程内注入


# --------------------------------------------------------------------------
# 基础路径
# --------------------------------------------------------------------------

def test_first_apply_all_added(tmp_path) -> None:
    p = Pack(tmp_path)
    a = p.put_blob(b"AAA")
    b = p.put_blob(b"BBBB")
    dpath = p.write_desired("srv-1", [
        {"path": "mods/a.jar", **a}, {"path": "mods/b.jar", **b}])

    r = p.apply("srv-1", dpath)
    assert r.returncode == 0, r.stderr
    summary = json.loads(r.stdout)
    assert summary["result"] == "applied"
    assert (summary["added"], summary["replaced"], summary["deleted"]) == (2, 0, 0)
    assert p.mods_dict() == {"a.jar": a["sha256"], "b.jar": b["sha256"]}
    st = p.state()
    assert st["lastAppliedVersion"] == "srv-1" and len(st["files"]) == 2
    # staging 已清空（含 desired）
    assert os.listdir(p.staging) == []
    # history: changes.json + backup 目录
    assert (p.ver_dir("srv-1") / "changes.json").is_file()
    ch = p.changes("srv-1")
    assert {e["path"] for e in ch["changes"]["added"]} == {"mods/a.jar", "mods/b.jar"}


def test_incremental_add_replace_delete(tmp_path) -> None:
    p = Pack(tmp_path)
    p.add_mod("a.jar", b"A1")
    p.add_mod("b.jar", b"B1")
    p.add_mod("old.jar", b"OLD")
    na = p.put_blob(b"A1")
    nb = p.put_blob(b"B2")
    nc = p.put_blob(b"C1")
    dpath = p.write_desired("srv-2", [
        {"path": "mods/a.jar", **na}, {"path": "mods/b.jar", **nb}, {"path": "mods/c.jar", **nc}])

    r = p.apply("srv-2", dpath)
    assert r.returncode == 0, r.stderr
    s = json.loads(r.stdout)
    assert (s["added"], s["replaced"], s["deleted"]) == (1, 1, 1)
    assert p.mods_dict() == {"a.jar": na["sha256"], "b.jar": nb["sha256"], "c.jar": nc["sha256"]}
    ch = p.changes("srv-2")["changes"]
    assert [e["path"] for e in ch["added"]] == ["mods/c.jar"]
    rep = ch["replaced"][0]
    assert rep["path"] == "mods/b.jar"
    assert rep["oldSha256"] == sha256_bytes(b"B1")
    assert rep["oldSize"] == 2
    dele = ch["deleted"][0]
    assert dele["path"] == "mods/old.jar" and dele["oldSha256"] == sha256_bytes(b"OLD")
    # 备份内容为替换/删除前的磁盘原文件
    assert (p.ver_dir("srv-2") / rep["backupPath"]).read_bytes() == b"B1"
    assert (p.ver_dir("srv-2") / dele["backupPath"]).read_bytes() == b"OLD"


def test_same_content_rename_single_blob(tmp_path) -> None:
    p = Pack(tmp_path)
    p.add_mod("old-name.jar", b"X")
    blob = p.put_blob(b"X")
    dpath = p.write_desired("srv-3", [{"path": "mods/new-name.jar", **blob}])

    blob_files = [f for f in (p.staging / "blobs").rglob("*") if f.is_file()]
    assert len(blob_files) == 1  # 同内容改名只上传一份 blob
    r = p.apply("srv-3", dpath)
    assert r.returncode == 0, r.stderr
    ch = p.changes("srv-3")["changes"]
    assert [e["path"] for e in ch["added"]] == ["mods/new-name.jar"]
    assert [e["path"] for e in ch["deleted"]] == ["mods/old-name.jar"]
    assert p.mods_dict() == {"new-name.jar": blob["sha256"]}


def test_disabled_and_non_jar_untouched(tmp_path) -> None:
    p = Pack(tmp_path)
    p.add_mod("keep.jar.disabled", b"D")
    (p.server / "mods" / "notes.txt").write_bytes(b"T")
    p.add_mod("real.jar", b"R")
    blob = p.put_blob(b"R")
    dpath = p.write_desired("srv-4", [{"path": "mods/real.jar", **blob}])

    r = p.apply("srv-4", dpath)
    assert r.returncode == 0, r.stderr
    s = json.loads(r.stdout)
    assert (s["added"], s["replaced"], s["deleted"]) == (0, 0, 0)
    assert p.mod_path("keep.jar.disabled").read_bytes() == b"D"
    assert (p.server / "mods" / "notes.txt").read_bytes() == b"T"


def test_manual_drift_backup_contains_drifted_file(tmp_path) -> None:
    p = Pack(tmp_path)
    p.add_mod("a.jar", b"A1")
    blob = p.put_blob(b"A2")
    dpath = p.write_desired("srv-5", [{"path": "mods/a.jar", **blob}])
    p.add_mod("a.jar", b"DRIFT")  # 手工漂移

    r = p.apply("srv-5", dpath)
    assert r.returncode == 0, r.stderr
    rep = p.changes("srv-5")["changes"]["replaced"][0]
    assert rep["oldSha256"] == sha256_bytes(b"DRIFT")
    assert (p.ver_dir("srv-5") / rep["backupPath"]).read_bytes() == b"DRIFT"
    assert p.mod_path("a.jar").read_bytes() == b"A2"


# --------------------------------------------------------------------------
# 幂等与重跑
# --------------------------------------------------------------------------

def test_noop_when_state_matches_and_staging_empty(tmp_path) -> None:
    p = Pack(tmp_path)
    blob = p.put_blob(b"A")
    dpath = p.write_desired("srv-6", [{"path": "mods/a.jar", **blob}])
    assert p.apply("srv-6", dpath).returncode == 0

    p.clear_staging()
    p.write_desired("srv-6", [{"path": "mods/a.jar", **blob}])
    r = p.apply("srv-6")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["result"] == "noop"
    assert os.listdir(p.staging) == []


def test_same_version_disk_changed_converges(tmp_path) -> None:
    p = Pack(tmp_path)
    blob = p.put_blob(b"A")
    dpath = p.write_desired("srv-7", [{"path": "mods/a.jar", **blob}])
    assert p.apply("srv-7", dpath).returncode == 0

    p.add_mod("a.jar", b"TAMPERED")
    p.put_blob(b"A")          # A 重传（模拟重新推送同一版本）
    p.write_desired("srv-7", [{"path": "mods/a.jar", **blob}])
    r = p.apply("srv-7")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["result"] == "applied"
    assert p.mod_path("a.jar").read_bytes() == b"A"


def test_same_version_clean_rerun_is_noop(tmp_path) -> None:
    p = Pack(tmp_path)
    blob = p.put_blob(b"A")
    dpath = p.write_desired("srv-8", [{"path": "mods/a.jar", **blob}])
    assert p.apply("srv-8", dpath).returncode == 0
    p.write_desired("srv-8", [{"path": "mods/a.jar", **blob}])
    r = p.apply("srv-8")
    assert r.returncode == 0 and json.loads(r.stdout)["result"] == "noop"


# --------------------------------------------------------------------------
# 崩溃注入
# --------------------------------------------------------------------------

def test_after_backup_crash_then_rerun(tmp_path) -> None:
    p = Pack(tmp_path)
    p.add_mod("a.jar", b"A1")
    blob = p.put_blob(b"A2")
    dpath = p.write_desired("srv-9", [{"path": "mods/a.jar", **blob}])

    r1 = p.apply("srv-9", dpath, inject="after-backup")
    assert r1.returncode == 99, r1.stderr
    assert (p.ver_dir("srv-9") / "changes.json").is_file()
    assert p.mod_path("a.jar").read_bytes() == b"A1"        # 尚未应用
    bak = p.ver_dir("srv-9") / p.changes("srv-9")["changes"]["replaced"][0]["backupPath"]
    m1 = bak.stat().st_mtime_ns

    r2 = p.apply("srv-9", dpath)                            # 重跑：复核 staging、跳过备份、重放
    assert r2.returncode == 0, r2.stderr
    assert p.mod_path("a.jar").read_bytes() == b"A2"
    assert bak.stat().st_mtime_ns == m1                     # 备份未被重做


def test_half_backup_without_changes_reruns_backup(tmp_path) -> None:
    p = Pack(tmp_path)
    p.add_mod("a.jar", b"A1")
    blob = p.put_blob(b"A2")
    dpath = p.write_desired("srv-10", [{"path": "mods/a.jar", **blob}])
    # 模拟「备份中途崩溃」：backup 目录有半成品且无 changes.json
    half = p.ver_dir("srv-10") / "backup" / "mods"
    half.mkdir(parents=True, exist_ok=True)
    (half / "a.jar").write_bytes(b"PARTIAL")

    r = p.apply("srv-10", dpath)
    assert r.returncode == 0, r.stderr
    rep = p.changes("srv-10")["changes"]["replaced"][0]
    assert (p.ver_dir("srv-10") / rep["backupPath"]).read_bytes() == b"A1"  # 覆盖为正确内容
    assert p.mod_path("a.jar").read_bytes() == b"A2"


def test_mid_apply_crash_then_rerun_and_rollback(tmp_path) -> None:
    p = Pack(tmp_path)
    p.add_mod("a.jar", b"A1")
    p.add_mod("b.jar", b"B1")
    p.add_mod("gone.jar", b"G1")
    blobs = [p.put_blob(b"A2"), p.put_blob(b"B2"), p.put_blob(b"C1")]
    dpath = p.write_desired("srv-11", [
        {"path": "mods/a.jar", **blobs[0]},
        {"path": "mods/b.jar", **blobs[1]},
        {"path": "mods/c.jar", **blobs[2]},
    ])

    r1 = p.apply("srv-11", dpath, inject="mid-apply")
    assert r1.returncode == 99, r1.stderr
    assert not (p.server / ".mcmodsync" / "state.json").exists()   # 未写 state

    r2 = p.apply("srv-11", dpath)
    assert r2.returncode == 0, r2.stderr
    assert p.mods_dict() == {"a.jar": blobs[0]["sha256"], "b.jar": blobs[1]["sha256"],
                             "c.jar": blobs[2]["sha256"]}

    rb = p.rollback()
    assert rb.returncode == 0, rb.stderr
    assert p.mods_dict() == {"a.jar": sha256_bytes(b"A1"), "b.jar": sha256_bytes(b"B1"),
                             "gone.jar": sha256_bytes(b"G1")}


def test_after_state_crash_rerun_noop_and_clears_staging(tmp_path) -> None:
    p = Pack(tmp_path)
    blob = p.put_blob(b"A")
    dpath = p.write_desired("srv-12", [{"path": "mods/a.jar", **blob}])
    r1 = p.apply("srv-12", dpath, inject="after-state")
    assert r1.returncode == 99, r1.stderr
    assert p.state()["lastAppliedVersion"] == "srv-12"
    assert os.listdir(p.staging) != []           # staging 未清

    r2 = p.apply("srv-12", dpath)
    assert r2.returncode == 0 and json.loads(r2.stdout)["result"] == "noop"
    assert os.listdir(p.staging) == []


def test_rerun_with_staging_emptied_is_11(tmp_path) -> None:
    """两文件（4B + 2B）: mid-apply 在半程触发 -> 第二个文件尚未应用；
    清空 staging 后重跑因缺件报 11（模拟 A 未重传）。"""
    p = Pack(tmp_path)
    p.add_mod("a.jar", b"A1")
    p.add_mod("b.jar", b"B1")
    ba = p.put_blob(b"AAAA")
    bb = p.put_blob(b"BB")
    dpath = p.write_desired("srv-13", [{"path": "mods/a.jar", **ba},
                                       {"path": "mods/b.jar", **bb}])
    assert p.apply("srv-13", dpath, inject="mid-apply").returncode == 99

    p.clear_staging()
    p.write_desired("srv-13", [{"path": "mods/a.jar", **ba},
                               {"path": "mods/b.jar", **bb}])
    r = p.apply("srv-13")
    assert r.returncode == 11, (r.returncode, r.stderr)


def test_rerun_with_corrupted_staging_is_11(tmp_path) -> None:
    """第二个文件尚未应用且其 blob 被损坏 -> 重跑报 11。"""
    p = Pack(tmp_path)
    p.add_mod("a.jar", b"A1")
    p.add_mod("b.jar", b"B1")
    ba = p.put_blob(b"AAAA")
    bb = p.put_blob(b"BB")
    dpath = p.write_desired("srv-14", [{"path": "mods/a.jar", **ba},
                                       {"path": "mods/b.jar", **bb}])
    assert p.apply("srv-14", dpath, inject="mid-apply").returncode == 99

    p.blob_path(bb["sha256"]).write_bytes(b"CORRUPT")
    p.write_desired("srv-14", [{"path": "mods/a.jar", **ba},
                               {"path": "mods/b.jar", **bb}])
    r = p.apply("srv-14")
    assert r.returncode == 11, (r.returncode, r.stderr)


# --------------------------------------------------------------------------
# 错误码
# --------------------------------------------------------------------------

def test_illegal_path_in_desired_is_13(tmp_path) -> None:
    p = Pack(tmp_path)
    blob = p.put_blob(b"A")
    dpath = p.write_desired("srv-15", [{"path": "../evil.jar", **blob}])
    r = p.apply("srv-15", dpath)
    assert r.returncode == 13, (r.returncode, r.stderr)


def test_staging_outside_server_dir_is_13(tmp_path) -> None:
    p = Pack(tmp_path)
    outside = tmp_path / "outside-staging"
    outside.mkdir()
    blob = {"sha256": sha256_bytes(b"A"), "size": 1}
    d = outside / "blobs" / blob["sha256"][:2] / blob["sha256"][2:4]
    d.mkdir(parents=True)
    (d / blob["sha256"]).write_bytes(b"A")
    dpath = outside / "desired-srv-16.json"
    dpath.write_text(json.dumps(p.desired_doc("srv-16", [{"path": "mods/a.jar", **blob}])),
                     encoding="utf-8")

    r = run_b("apply", "--server-dir", p.server, "--staging", outside, "--desired", dpath,
              "--version", "srv-16", "--pack-id", p.pack_id, "--history-dir", p.history)
    assert r.returncode == 13, (r.returncode, r.stderr)


def test_concurrent_apply_second_gets_10(tmp_path) -> None:
    p = Pack(tmp_path)
    blob = p.put_blob(b"A")
    dpath = p.write_desired("srv-17", [{"path": "mods/a.jar", **blob}])
    locker = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from mcmodsync.locking import FileLock\n"
        "l = FileLock(%r)\n"
        "l.acquire()\n"
        "print('LOCKED', flush=True)\n"
        "time.sleep(25)\n"
        % (str(B_PATH.parents[1]), str(p.server / ".mcmodsync.lock"))
    )
    proc = subprocess.Popen([sys.executable, "-c", locker], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "LOCKED"
        r = p.apply("srv-17", dpath)
        assert r.returncode == 10, (r.returncode, r.stderr)
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_history_cleanup_keeps_latest(tmp_path) -> None:
    p = Pack(tmp_path)
    for i in range(5):
        blob = p.put_blob(b"V%d" % i)
        dpath = p.write_desired("srv-%02d" % i, [{"path": "mods/a.jar", **blob}])
        assert p.apply("srv-%02d" % i, dpath).returncode == 0
    names = sorted(os.listdir(p.history / p.pack_id))
    assert names == ["srv-02", "srv-03", "srv-04"]


def test_delete_missing_target_is_tolerated(tmp_path) -> None:
    """重跑时删除目标缺失 -> 记 warning 且不报错。"""
    p = Pack(tmp_path)
    p.add_mod("gone.jar", b"G")
    p.add_mod("a.jar", b"A1")
    blob = p.put_blob(b"A1")
    dpath = p.write_desired("srv-18", [{"path": "mods/a.jar", **blob}])

    assert p.apply("srv-18", dpath, inject="after-backup").returncode == 99
    (p.server / "mods" / "gone.jar").unlink()          # 备份后手工删除目标
    r = p.apply("srv-18", dpath)
    assert r.returncode == 0, r.stderr
    log_files = list((p.server / ".mcmodsync" / "logs").glob("apply-*.log"))
    assert any("删除目标不存在" in f.read_text(encoding="utf-8") for f in log_files)


# --------------------------------------------------------------------------
# 进程内注入（磁盘不足 / 跨分区）
# --------------------------------------------------------------------------

_U = namedtuple("usage", "total used free")


def _layout(tmp_path) -> Pack:
    return Pack(tmp_path)


def _argv(p: Pack, version: str, desired) -> list:
    return ["apply", "--server-dir", str(p.server), "--staging", str(p.staging),
            "--desired", str(desired), "--version", version, "--pack-id", p.pack_id,
            "--history-dir", str(p.history)]


def test_disk_insufficient_is_4(tmp_path, monkeypatch) -> None:
    p = _layout(tmp_path)
    blob = p.put_blob(b"AAAA")
    dpath = p.write_desired("srv-19", [{"path": "mods/a.jar", **blob}])
    monkeypatch.setattr(b_main, "_disk_usage", lambda path: _U(100, 100, 0))
    rc = b_main.main(_argv(p, "srv-19", dpath))
    assert rc == 4


def test_cross_device_is_18(tmp_path, monkeypatch) -> None:
    p = _layout(tmp_path)
    blob = p.put_blob(b"AAAA")
    dpath = p.write_desired("srv-20", [{"path": "mods/a.jar", **blob}])
    monkeypatch.setattr(b_main.platform, "same_device", lambda a, b: False)
    rc = b_main.main(_argv(p, "srv-20", dpath))
    assert rc == 18
