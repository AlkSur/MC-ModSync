"""T-33 / T-34: fetcher.py —— 路由、落盘、幂等、锁回写、manual 闭环（provider 以 fake 注入）。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from mcmodsync import fetcher
from mcmodsync.config import Config

PAYLOAD = {"mr-a.jar": b"MRA", "mr-b.jar": b"MRB",
           "cf-a.jar": b"CFA", "cf-b.jar": b"CFB", "manual-x.jar": b"MAN"}
CALLS = {"mr_resolve": 0, "mr_dl": 0, "cf_resolve": 0, "cf_dl": 0}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture()
def fake_providers(monkeypatch):
    CALLS.update({k: 0 for k in CALLS})

    def mr_resolve(slug, mc, loader, pin, base_url, log):
        CALLS["mr_resolve"] += 1
        if slug == "mr-bad":
            return None
        return {"resolvedVersion": "1.0.0", "fileName": "mr-%s.jar" % slug,
                "downloadUrl": "mem://mr/%s" % slug, "size": len(PAYLOAD.get("mr-%s.jar" % slug, b"")),
                "source": "modrinth", "manualNeeded": False,
                "projectUrl": "https://modrinth.com/mod/%s" % slug}

    def cf_resolve(slug, mc, api_key, pin, base_url, log):
        CALLS["cf_resolve"] += 1
        if slug == "cf-nodl":
            return {"resolvedVersion": "2.0.0", "fileName": "manual-x.jar", "downloadUrl": "",
                    "size": 0, "source": "curseforge", "manualNeeded": True,
                    "projectUrl": "https://www.curseforge.com/minecraft/mc-mods/%s" % slug}
        return {"resolvedVersion": "2.0.0", "fileName": "cf-%s.jar" % slug,
                "downloadUrl": "mem://cf/%s" % slug, "size": len(PAYLOAD.get("cf-%s.jar" % slug, b"")),
                "source": "curseforge", "manualNeeded": False,
                "projectUrl": "https://www.curseforge.com/minecraft/mc-mods/%s" % slug}

    def _dl(info, dest_dir, log):
        name = info["fileName"]
        data = PAYLOAD[name]
        os.makedirs(dest_dir, exist_ok=True)
        p = os.path.join(dest_dir, name)
        with open(p, "wb") as f:
            f.write(data)
        return {"fileName": name, "sha256": _sha(data), "size": len(data),
                "downloadUrl": info["downloadUrl"]}

    def mr_dl(info, dest_dir, log):
        CALLS["mr_dl"] += 1
        if info["fileName"] == "mr-b.jar" and os.environ.get("_FETCH_FAIL"):
            from mcmodsync.provider_modrinth import ProviderError
            raise ProviderError(5, "模拟下载失败")
        return _dl(info, dest_dir, log)

    def cf_dl(info, dest_dir, log):
        CALLS["cf_dl"] += 1
        return _dl(info, dest_dir, log)

    monkeypatch.setattr(fetcher.mr, "resolve", mr_resolve)
    monkeypatch.setattr(fetcher.mr, "download_file", mr_dl)
    monkeypatch.setattr(fetcher.cf, "resolve", cf_resolve)
    monkeypatch.setattr(fetcher.cf, "download_file", cf_dl)
    return CALLS


@pytest.fixture()
def env(tmp_path):
    server_dir = tmp_path / "server-mods"
    client_dir = tmp_path / "client-mods"
    server_dir.mkdir()
    client_dir.mkdir()
    cfg = Config({
        "packId": "fetch-pack",
        "server": {"sourceModsDir": str(server_dir)},
        "client": {"sourceModsDir": str(client_dir)},
        "curseforge": {"apiKey": "FAKE-KEY-1234"},
    })
    lock = tmp_path / "mods.lock.json"
    lock.write_text(json.dumps({
        "schemaVersion": 1,
        "packMeta": {"minecraftVersion": "1.21.1", "loader": "neoforge"},
        "mods": [
            {"name": "MR-A", "side": "server", "source": "modrinth", "projectSlug": "a",
             "versionPin": "latest", "fileName": "mr-a.jar"},
            {"name": "MR-B", "side": "both", "source": "modrinth", "projectSlug": "b",
             "versionPin": "latest", "fileName": "mr-b.jar"},
            {"name": "CF-A", "side": "client", "source": "curseforge", "projectSlug": "a",
             "versionPin": "latest", "fileName": "cf-a.jar"},
            {"name": "CF-NODL", "side": "server", "source": "curseforge", "projectSlug": "cf-nodl",
             "versionPin": "latest", "fileName": "manual-x.jar"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    return dict(cfg=cfg, lock=lock, server=server_dir, client=client_dir, tmp=tmp_path)


def _run(env, **kw):
    logs = []
    rc = fetcher.fetch_mods(env["cfg"], str(env["lock"]), log=logs.append,
                            tmp_dir=str(env["tmp"] / "tmp"), **kw)
    return rc, logs


# --------------------------------------------------------------------------
# T-33
# --------------------------------------------------------------------------

def test_mixed_routing_and_placement(env, fake_providers) -> None:
    rc, logs = _run(env)
    assert rc == 0, logs
    assert (env["server"] / "mr-a.jar").read_bytes() == PAYLOAD["mr-a.jar"]
    assert (env["server"] / "mr-b.jar").read_bytes() == PAYLOAD["mr-b.jar"]
    assert (env["client"] / "mr-b.jar").read_bytes() == PAYLOAD["mr-b.jar"]   # both -> 两份
    assert (env["client"] / "cf-a.jar").read_bytes() == PAYLOAD["cf-a.jar"]
    assert not (env["server"] / "cf-a.jar").exists()
    assert not (env["client"] / "mr-a.jar").exists()

    lock = json.loads(env["lock"].read_text(encoding="utf-8"))
    by_name = {m["name"]: m for m in lock["mods"]}
    assert by_name["MR-A"]["sha256"] == _sha(PAYLOAD["mr-a.jar"])
    assert by_name["MR-A"]["size"] == 3 and by_name["MR-A"]["downloadUrl"] == "mem://mr/a"
    assert by_name["CF-A"]["resolvedVersion"] == "2.0.0"

    assert any("【已更新 3】" in m for m in logs)
    assert any("【需人工下载 1】" in m for m in logs)
    assert any("CF-NODL" in m and "curseforge.com" in m for m in logs)
    # 禁下载条目被改写为 manual
    assert by_name["CF-NODL"]["source"] == "manual"


def test_rerun_is_idempotent(env, fake_providers) -> None:
    assert _run(env)[0] == 0
    dl_before = CALLS["mr_dl"] + CALLS["cf_dl"]
    rc, logs = _run(env)
    assert rc == 0, logs
    assert CALLS["mr_dl"] + CALLS["cf_dl"] == dl_before          # 未再下载
    assert any("【无需更新 3】" in m for m in logs)


def test_dry_run_no_writes(env, fake_providers) -> None:
    before = env["lock"].read_text(encoding="utf-8")
    rc, logs = _run(env, dry_run=True)
    assert rc == 0
    assert env["lock"].read_text(encoding="utf-8") == before
    assert not (env["server"] / "mr-a.jar").exists()
    assert CALLS["mr_dl"] == 0 and CALLS["cf_dl"] == 0
    assert any("[dry-run]" in m for m in logs)


def test_download_failure_exit_5(env, fake_providers, monkeypatch) -> None:
    monkeypatch.setenv("_FETCH_FAIL", "1")
    rc, logs = _run(env)
    assert rc == 5, logs
    assert any("【失败 1】" in m for m in logs)
    # 其余 mod 继续成功
    assert (env["server"] / "mr-a.jar").exists()


def test_upgrade_re_resolves(env, fake_providers) -> None:
    assert _run(env)[0] == 0
    n1 = CALLS["mr_resolve"]
    assert _run(env)[0] == 0
    assert CALLS["mr_resolve"] == n1            # 不带 --upgrade 不再解析
    assert _run(env, upgrade=True)[0] == 0
    assert CALLS["mr_resolve"] > n1             # --upgrade 重新解析


def test_missing_lock_file(env) -> None:
    with pytest.raises(fetcher.FetchError):
        fetcher.fetch_mods(env["cfg"], str(env["tmp"] / "nope.json"), log=lambda m: None)


def test_side_dirs_validation(env) -> None:
    assert fetcher.side_dirs(env["cfg"], "server") == [str(env["server"])]
    assert fetcher.side_dirs(env["cfg"], "both") == [str(env["server"]), str(env["client"])]
    with pytest.raises(fetcher.FetchError):
        fetcher.side_dirs(env["cfg"], "weird")


# --------------------------------------------------------------------------
# T-34: manual 闭环
# --------------------------------------------------------------------------

def _manual_lock(path: Path) -> None:
    path.write_text(json.dumps({
        "schemaVersion": 1,
        "packMeta": {"minecraftVersion": "1.21.1", "loader": "neoforge"},
        "mods": [{"name": "某禁分发 mod", "side": "both", "source": "manual",
                  "projectSlug": "some-mod", "versionPin": "manual",
                  "fileName": "some-mod-1.2.3.jar", "sha256": None, "size": None,
                  "downloadUrl": "https://www.curseforge.com/minecraft/mc-mods/some-mod",
                  "note": "禁第三方下载"}],
    }, ensure_ascii=False), encoding="utf-8")


def test_lock_manual_writes_hash_and_version(env, fake_providers) -> None:
    _manual_lock(env["lock"])
    data = b"MANUAL-JAR"
    for d in (env["server"], env["client"]):
        (d / "some-mod-1.2.3.jar").write_bytes(data)

    logs = []
    rc = fetcher.fetch_mods(env["cfg"], str(env["lock"]), lock_manual=True, log=logs.append,
                            tmp_dir=str(env["tmp"] / "tmp"))
    assert rc == 0, logs
    entry = json.loads(env["lock"].read_text(encoding="utf-8"))["mods"][0]
    assert entry["sha256"] == _sha(data) and entry["size"] == len(data)
    assert entry["resolvedVersion"] == "1.2.3"
    assert any("【已更新 1】" in m for m in logs)

    # 重复执行幂等
    logs2 = []
    assert fetcher.fetch_mods(env["cfg"], str(env["lock"]), lock_manual=True,
                              log=logs2.append, tmp_dir=str(env["tmp"] / "tmp")) == 0
    assert any("【无需更新 1】" in m for m in logs2)


def test_lock_manual_missing_file_reports(env, fake_providers) -> None:
    _manual_lock(env["lock"])
    logs = []
    assert fetcher.fetch_mods(env["cfg"], str(env["lock"]), lock_manual=True,
                              log=logs.append, tmp_dir=str(env["tmp"] / "tmp")) == 0
    assert any("源目录未找到人工放入的文件" in m for m in logs)


def test_lock_stale_hint(env, fake_providers) -> None:
    m = os.path.getmtime(env["lock"])
    jar = env["server"] / "newer.jar"
    jar.write_bytes(b"X")
    os.utime(jar, (m + 10, m + 10))
    hint = fetcher.lock_stale_hint(env["cfg"], str(env["lock"]))
    assert hint and "fetch-mods" in hint
    os.utime(env["lock"], (m + 100, m + 100))
    assert fetcher.lock_stale_hint(env["cfg"], str(env["lock"])) is None
    assert fetcher.lock_stale_hint(env["cfg"], str(env["tmp"] / "none.json")) is None


# --------------------------------------------------------------------------
# T-35: fetch-mods -> push-server 端到端（增量只推变更 jar）
# --------------------------------------------------------------------------

def test_t35_e2e_fetch_then_push_incremental(tmp_path, fake_providers) -> None:
    from test_pusher import LocalConn

    from mcmodsync import pusher

    remote = tmp_path / "remote"
    remote.mkdir()
    server_src = tmp_path / "server-mods"
    server_src.mkdir()
    client_src = tmp_path / "client-mods"
    client_src.mkdir()

    cfg = Config({
        "packId": "t35-pack",
        "server": {"serverDir": "/www/srv", "modsDir": "mods",
                   "remoteBPath": "/www/mcmodsync-b.py",
                   "historyDir": "/www/mcmodsync-history", "historyKeep": 3,
                   "sourceModsDir": str(server_src)},
        "client": {"sourceModsDir": str(client_src)},
        "curseforge": {"apiKey": "FAKE-KEY-1234"},
    })
    lock = tmp_path / "mods.lock.json"
    lock.write_text(json.dumps({
        "schemaVersion": 1,
        "packMeta": {"minecraftVersion": "1.21.1", "loader": "neoforge"},
        "mods": [
            {"name": "MR-A", "side": "server", "source": "modrinth", "projectSlug": "a",
             "versionPin": "latest", "fileName": "mr-a.jar"},
            {"name": "MR-B", "side": "server", "source": "modrinth", "projectSlug": "b",
             "versionPin": "latest", "fileName": "mr-b.jar"},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    logs: list = []
    tmp_run = tmp_path / "run"
    assert fetcher.fetch_mods(cfg, str(lock), log=logs.append, tmp_dir=str(tmp_run)) == 0
    assert (server_src / "mr-a.jar").exists() and (server_src / "mr-b.jar").exists()

    conn = LocalConn(remote)
    assert pusher.push_server(cfg, conn, log=logs.append, tmp_dir=str(tmp_run),
                              log_dir=str(tmp_path / "logs"),
                              lock_path=str(lock)) == 0
    hist = remote / "www/mcmodsync-history/t35-pack"
    v1 = sorted(hist.iterdir())[0]
    ch1 = json.loads((v1 / "changes.json").read_text(encoding="utf-8"))
    assert len(ch1["changes"]["added"]) == 2

    # 第二次: 改动一个 mod -> fetch -> push 只推变更 jar
    PAYLOAD["mr-a.jar"] = b"MRA-V2"
    doc = json.loads(lock.read_text(encoding="utf-8"))
    doc["mods"][0]["sha256"] = None          # 模拟「列表已变更，需重新获取」
    lock.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    assert fetcher.fetch_mods(cfg, str(lock), log=logs.append, tmp_dir=str(tmp_run)) == 0
    assert pusher.push_server(cfg, conn, log=logs.append, tmp_dir=str(tmp_run),
                              log_dir=str(tmp_path / "logs"),
                              lock_path=str(lock)) == 0
    v2 = sorted(hist.iterdir())[1]
    ch2 = json.loads((v2 / "changes.json").read_text(encoding="utf-8"))
    assert [e["path"] for e in ch2["changes"]["added"]] == []
    assert [e["path"] for e in ch2["changes"]["replaced"]] == ["mods/mr-a.jar"]
    assert ch2["changes"]["deleted"] == []

    # 联动提示: 手工把源目录 jar 变新 -> push 输出 warning（不阻断）
    os.utime(server_src / "mr-b.jar", (os.path.getmtime(lock) + 50,) * 2)
    logs2: list = []
    assert pusher.push_server(cfg, conn, dry_run=True, log=logs2.append,
                              tmp_dir=str(tmp_run), log_dir=str(tmp_path / "logs"),
                              lock_path=str(lock)) == 0
    assert any("mod 列表未刷新" in m for m in logs2)


def test_lock_manual_ambiguous_lists_candidates(env, fake_providers) -> None:
    _manual_lock(env["lock"])
    (env["server"] / "some-mod-1.2.3.jar").write_bytes(b"A")
    (env["client"] / "some-mod-1.2.3.jar").write_bytes(b"B")     # 两份 -> 无法唯一确定
    logs = []
    assert fetcher.fetch_mods(env["cfg"], str(env["lock"]), lock_manual=True,
                              log=logs.append, tmp_dir=str(env["tmp"] / "tmp")) == 0
    assert any("无法唯一匹配" in m for m in logs)
    entry = json.loads(env["lock"].read_text(encoding="utf-8"))["mods"][0]
    assert entry["sha256"] is None                               # 禁止自动猜测
