from __future__ import annotations

import json
import os

import pytest

from charness import PACK_ID, Cloud, Instance, make_keys
from mcmodsync import canonicaljson, client, client_main, hashing, self_update


@pytest.fixture
def env(tmp_path):
    pem, pub = make_keys(tmp_path)
    cloud = Cloud(str(tmp_path / "cloud"), pem, PACK_ID)
    cloud.start()
    instance = Instance(str(tmp_path / "实例 空格"), cloud.base_url + "/manifest.json", pub)
    yield cloud, instance, pem, pub
    cloud.stop()


def release(env, version="2.3.0", contents=None, sign=True):
    cloud, inst, pem, pub = env
    contents = contents or {"python": b"NEW PYTHON"}
    artifacts = {}
    for kind, data in contents.items():
        sha = hashing.hash_bytes(data)
        path = "updater/files/%s/%s" % (sha, self_update.FILES[kind])
        full = os.path.join(cloud.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as stream:
            stream.write(data)
        artifacts[kind] = {"sha256": sha, "size": len(data), "path": path}
    doc = {"schemaVersion": 1, "packId": PACK_ID, "version": version, "artifacts": artifacts}
    if sign:
        doc = canonicaljson.sign(doc, pem)
    write_release(env, doc)
    return doc


def write_release(env, doc):
    with open(os.path.join(env[0].root, "updater.json"), "wb") as stream:
        stream.write(canonicaljson.canonical(doc))


def test_not_published_and_current_version_continue(env):
    cloud, inst, _, _ = env
    assert self_update.check(inst.root) == 0
    release(env, version=self_update.__version__)
    assert self_update.check(inst.root) == 0
    assert not any("/updater/files/" in hit for hit in cloud.hits)


def test_signed_update_staged_before_mods_and_installed_without_touching_state(env, monkeypatch):
    cloud, inst, _, _ = env
    original = os.path.join(client.updater_dir(inst.root), "mcmodsync.py")
    with open(original, "wb") as stream:
        stream.write(b"OLD PYTHON")
    client.write_state(inst.root, PACK_ID, "1.0.0", [])
    config_before = open(client.config_path(inst.root), "rb").read()
    state_before = open(client.state_path(inst.root), "rb").read()
    release(env)
    assert self_update.check(inst.root) == 42
    assert open(original, "rb").read() == b"OLD PYTHON"
    assert not any("/manifest.json" in hit for hit in cloud.hits)
    monkeypatch.setattr(self_update, "__version__", "2.3.0")
    assert self_update.apply(inst.root) == 0
    assert open(original, "rb").read() == b"NEW PYTHON"
    assert open(client.config_path(inst.root), "rb").read() == config_before
    assert open(client.state_path(inst.root), "rb").read() == state_before
    assert open(os.path.join(self_update.stage_dir(inst.root), "backup", "mcmodsync.py"), "rb").read() == b"OLD PYTHON"
    assert self_update.check(inst.root) == 0


def test_unsigned_release_rejected_before_artifact_download(env):
    cloud, inst, _, _ = env
    release(env, sign=False)
    assert self_update.check(inst.root) == client.EXIT_SIGNATURE
    assert not any("/updater/files/" in hit for hit in cloud.hits)


@pytest.mark.parametrize("bad", ["url", "pack", "size"])
def test_signed_but_invalid_release_rejected(env, bad):
    cloud, inst, pem, _ = env
    doc = release(env)
    if bad == "url":
        doc["artifacts"]["python"]["path"] = "https://attacker.invalid/run.py"
    elif bad == "pack":
        doc["packId"] = "other-pack"
    else:
        doc["artifacts"]["python"]["size"] = 1024 ** 4
    write_release(env, canonicaljson.sign(doc, pem))
    assert self_update.check(inst.root) != 0
    assert not any("/updater/files/" in hit for hit in cloud.hits)


def test_corrupt_download_does_not_replace_current_program(env, monkeypatch):
    cloud, inst, _, _ = env
    doc = release(env)
    row = doc["artifacts"]["python"]
    with open(os.path.join(cloud.root, row["path"]), "wb") as stream:
        stream.write(b"CORRUPT")
    monkeypatch.setattr(self_update.http_download.time, "sleep", lambda _: None)
    assert self_update.check(inst.root) == client.EXIT_NETWORK
    assert not os.path.exists(os.path.join(client.updater_dir(inst.root), "mcmodsync.py"))
    assert json.load(open(self_update._release_path(inst.root), encoding="utf-8")) == {}


def test_install_rechecks_candidate_hash(env, monkeypatch):
    _, inst, _, _ = env
    release(env)
    assert self_update.check(inst.root) == 42
    candidate = os.path.join(self_update.stage_dir(inst.root), "mcmodsync.py")
    with open(candidate, "wb") as stream:
        stream.write(b"CHANGED AFTER DOWNLOAD")
    monkeypatch.setattr(self_update, "__version__", "2.3.0")
    assert self_update.apply(inst.root) == client.EXIT_SIGNATURE


def test_candidate_with_wrong_embedded_version_is_rejected(env):
    _, inst, _, _ = env
    release(env)
    assert self_update.check(inst.root) == 42
    assert self_update.apply(inst.root) == client.EXIT_GENERIC


def test_install_failure_restores_all_replaced_programs(env, monkeypatch):
    _, inst, _, _ = env
    monkeypatch.setattr(self_update, "_kinds", lambda doc: ["windows", "python"])
    release(env, contents={"windows": b"NEW EXE", "python": b"NEW PYTHON"})
    for name, data in (("mcmodsync.exe", b"OLD EXE"), ("mcmodsync.py", b"OLD PYTHON")):
        with open(os.path.join(client.updater_dir(inst.root), name), "wb") as stream:
            stream.write(data)
    assert self_update.check(inst.root) == 42
    monkeypatch.setattr(self_update, "__version__", "2.3.0")
    original_copy = self_update._copy_with_retry

    def fail_on_second(src, dst):
        if src == os.path.join(self_update.stage_dir(inst.root), "mcmodsync.py"):
            raise PermissionError("file locked")
        original_copy(src, dst)

    monkeypatch.setattr(self_update, "_copy_with_retry", fail_on_second)
    assert self_update.apply(inst.root) == client.EXIT_GENERIC
    assert open(os.path.join(client.updater_dir(inst.root), "mcmodsync.exe"), "rb").read() == b"OLD EXE"
    assert open(os.path.join(client.updater_dir(inst.root), "mcmodsync.py"), "rb").read() == b"OLD PYTHON"


def test_main_does_not_start_mod_sync_when_program_update_pending(env, monkeypatch):
    _, inst, _, _ = env
    monkeypatch.setattr(self_update, "check", lambda *args: 42)
    monkeypatch.setattr(client, "sync", lambda *args, **kwargs: pytest.fail("must install updater first"))
    assert client_main.main(["sync", "--target", inst.root, "--no-pause"]) == 42
