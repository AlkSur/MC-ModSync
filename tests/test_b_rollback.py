"""TL-2 (rollback): 子进程驱动单文件 B 的回滚场景。"""
from __future__ import annotations

import json
import os

from bharness import Pack, sha256_bytes


def _seed_v1(p: Pack, version: str = "srv-01"):
    """mods(a=A1,b=B1,d=D1) -> desired(a=A1,b=B2,c=C1)：含 replaced/added/deleted。"""
    p.add_mod("a.jar", b"A1")
    p.add_mod("b.jar", b"B1")
    p.add_mod("d.jar", b"D1")
    blobs = {
        "a": p.put_blob(b"A1"),
        "b": p.put_blob(b"B2"),
        "c": p.put_blob(b"C1"),
    }
    dpath = p.write_desired(version, [
        {"path": "mods/a.jar", **blobs["a"]},
        {"path": "mods/b.jar", **blobs["b"]},
        {"path": "mods/c.jar", **blobs["c"]},
    ])
    r = p.apply(version, dpath)
    assert r.returncode == 0, r.stderr
    return blobs


def test_normal_rollback_restores_v1(tmp_path) -> None:
    p = Pack(tmp_path)
    _seed_v1(p)
    assert p.mods_dict() == {"a.jar": sha256_bytes(b"A1"), "b.jar": sha256_bytes(b"B2"),
                             "c.jar": sha256_bytes(b"C1")}

    r = p.rollback()
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["result"] == "rolled-back"
    # added 删除、replaced/deleted 还原
    assert p.mods_dict() == {"a.jar": sha256_bytes(b"A1"), "b.jar": sha256_bytes(b"B1"),
                             "d.jar": sha256_bytes(b"D1")}
    # state 重置
    st = p.state()
    assert st["lastAppliedVersion"] is None
    assert {f["path"] for f in st["files"]} == {"mods/a.jar", "mods/b.jar", "mods/d.jar"}
    # changes.json -> .rolled-back
    assert not (p.ver_dir("srv-01") / "changes.json").exists()
    assert (p.ver_dir("srv-01") / "changes.json.rolled-back").is_file()
    # 回滚不清理 history
    assert (p.ver_dir("srv-01")).is_dir()

    again = p.rollback()
    assert again.returncode == 15, (again.returncode, again.stderr)


def test_rollback_after_mid_apply_crash_of_v2(tmp_path) -> None:
    p = Pack(tmp_path)
    _seed_v1(p, "srv-01")
    v1_state = p.mods_dict()

    blobs = {"a": p.put_blob(b"A3"), "b": p.put_blob(b"B3")}
    dpath = p.write_desired("srv-02", [
        {"path": "mods/a.jar", **blobs["a"]},
        {"path": "mods/b.jar", **blobs["b"]},
        {"path": "mods/c.jar", **{"sha256": sha256_bytes(b"C1"), "size": 2}},
    ])
    r = p.apply("srv-02", dpath, inject="mid-apply")
    assert r.returncode == 99, r.stderr

    rb = p.rollback()
    assert rb.returncode == 0, rb.stderr
    assert json.loads(rb.stdout)["version"] == "srv-02"
    assert p.mods_dict() == v1_state


def test_rollback_version_not_latest_is_14(tmp_path) -> None:
    p = Pack(tmp_path)
    _seed_v1(p, "srv-01")
    blobs = {"a": p.put_blob(b"A3")}
    dpath = p.write_desired("srv-02", [
        {"path": "mods/a.jar", **blobs["a"]},
        {"path": "mods/b.jar", **{"sha256": sha256_bytes(b"B2"), "size": 2}},
        {"path": "mods/c.jar", **{"sha256": sha256_bytes(b"C1"), "size": 2}},
    ])
    assert p.apply("srv-02", dpath).returncode == 0

    r = p.rollback(version="srv-01")
    assert r.returncode == 14, (r.returncode, r.stderr)


def test_rollback_drift_moves_original_aside(tmp_path) -> None:
    p = Pack(tmp_path)
    _seed_v1(p)
    p.add_mod("c.jar", b"C1-DRIFT")     # added 漂移
    p.add_mod("b.jar", b"B2-DRIFT")     # replaced 漂移

    r = p.rollback()
    assert r.returncode == 0, r.stderr
    assert p.mods_dict() == {"a.jar": sha256_bytes(b"A1"), "b.jar": sha256_bytes(b"B1"),
                             "d.jar": sha256_bytes(b"D1")}
    drift = p.ver_dir("srv-01") / "rollback-drift" / "mods"
    assert (drift / "c.jar").read_bytes() == b"C1-DRIFT"
    assert (drift / "b.jar").read_bytes() == b"B2-DRIFT"


def test_rollback_does_not_clean_history(tmp_path) -> None:
    p = Pack(tmp_path)
    for i in range(5):
        blob = p.put_blob(b"V%d" % i)
        dpath = p.write_desired("srv-%02d" % i, [{"path": "mods/a.jar", **blob}])
        assert p.apply("srv-%02d" % i, dpath).returncode == 0
    before = sorted(os.listdir(p.history / p.pack_id))
    assert before == ["srv-02", "srv-03", "srv-04"]

    assert p.rollback().returncode == 0
    assert sorted(os.listdir(p.history / p.pack_id)) == before


def test_rollback_missing_pack_id_falls_back_to_state(tmp_path) -> None:
    p = Pack(tmp_path)
    _seed_v1(p)
    r = p.rollback(pack_id="")        # 参数缺省 -> 回退读 state.packId
    assert r.returncode == 0, r.stderr


def test_rollback_missing_target_dir_is_19(tmp_path) -> None:
    p = Pack(tmp_path)
    r = p.rollback()
    assert r.returncode == 19, (r.returncode, r.stderr)
