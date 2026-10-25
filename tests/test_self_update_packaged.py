"""Real runtime handoff using a signed local release; no production endpoints."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from charness import PACK_ID, Cloud, Instance, make_keys
from mcmodsync import __version__, canonicaljson, client, hashing, self_update
from tools import build_client

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "dist" / "client-package-v2.2.0"
pytestmark = pytest.mark.skipif(not (PACKAGE / "_updater" / "mcmodsync.py").exists(), reason="需要新版构建")


def _run(command):
    result = subprocess.run(command, capture_output=True, timeout=45)
    return result.returncode, result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace")


def _release(cloud, kinds):
    artifacts = {}
    for kind in kinds:
        src = PACKAGE / "_updater" / self_update.FILES[kind]
        row = hashing.hash_file(str(src))
        row["path"] = "updater/files/%s/%s" % (row["sha256"], src.name)
        dst = Path(cloud.root) / row["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        artifacts[kind] = row
    doc = canonicaljson.sign({"schemaVersion": 1, "packId": PACK_ID, "version": __version__,
                              "artifacts": artifacts}, cloud.pem)
    (Path(cloud.root) / "updater.json").write_bytes(canonicaljson.canonical(doc))


@pytest.fixture
def env(tmp_path):
    pem, pub = make_keys(tmp_path)
    cloud = Cloud(str(tmp_path / "cloud"), pem, PACK_ID)
    cloud.start()
    inst = Instance(str(tmp_path / "实例 中文 空格"), cloud.base_url + "/manifest.json", pub)
    cloud.publish("1.0.0", {"mods/a.jar": b"MOD CONTENT"})
    yield cloud, inst
    cloud.stop()


def test_python_old_runtime_download_install_restart_then_mod_sync(env, tmp_path):
    cloud, inst = env
    # Same bootstrap protocol, older embedded program version; build in a temporary path.
    old_init = tmp_path / "old-init.py"
    old_init.write_text('__version__ = "2.1.9"\n', encoding="utf-8")
    modules = [(name, str(old_init) if Path(path).resolve() == REPO / "mcmodsync" / "__init__.py" else path)
               for name, path in build_client.collect(build_client.ENTRY)]
    old_script = Path(inst.root) / "_updater" / "mcmodsync.py"
    old_script.write_text(build_client.render(build_client.build_zip(modules)), encoding="utf-8")
    _release(cloud, ["python"])
    rc, out, err = _run([sys.executable, str(old_script), "sync", "--target", inst.root, "--no-pause"])
    assert rc == 42, out + err
    assert not (Path(inst.root) / "mods" / "a.jar").exists()
    assert "当前 2.1.9" in out
    candidate = Path(self_update.stage_dir(inst.root)) / "mcmodsync.py"
    rc, out, err = _run([sys.executable, str(candidate), "apply-update", "--target", inst.root, "--no-pause"])
    assert rc == 0, out + err
    assert old_script.read_bytes() == (PACKAGE / "_updater" / "mcmodsync.py").read_bytes()
    rc, out, err = _run([sys.executable, str(old_script), "sync", "--target", inst.root, "--no-pause"])
    assert rc == 0, out + err
    assert "更新器已是最新版本" in out
    assert (Path(inst.root) / "mods" / "a.jar").read_bytes() == b"MOD CONTENT"
    assert inst.state()["version"] == "1.0.0"


@pytest.mark.skipif(os.name != "nt", reason="Windows native handoff")
def test_real_windows_candidate_installs_and_new_exe_syncs_mods(env, monkeypatch):
    cloud, inst = env
    _release(cloud, ["windows", "python"])
    up = Path(inst.root) / "_updater"
    (up / "mcmodsync.exe").write_bytes(b"PREVIOUS PROGRAM")
    (up / "mcmodsync.py").write_bytes(b"PREVIOUS FALLBACK")
    original_config = (up / "config.json").read_bytes()
    monkeypatch.setattr(self_update, "__version__", "2.1.9")
    monkeypatch.setattr(self_update, "_kinds", lambda doc: ["windows", "python"])
    assert self_update.check(inst.root) == 42
    candidate = Path(self_update.stage_dir(inst.root)) / "mcmodsync.exe"
    rc, out, err = _run([str(candidate), "apply-update", "--target", inst.root, "--no-pause"])
    assert rc == 0, out + err
    assert (up / "mcmodsync.exe").read_bytes() == (PACKAGE / "_updater" / "mcmodsync.exe").read_bytes()
    assert (up / "config.json").read_bytes() == original_config
    assert (up / "self-update" / "backup" / "mcmodsync.exe").read_bytes() == b"PREVIOUS PROGRAM"
    rc, out, err = _run([str(up / "mcmodsync.exe"), "sync", "--target", inst.root, "--no-pause"])
    assert rc == 0, out + err
    assert (Path(inst.root) / "mods" / "a.jar").read_bytes() == b"MOD CONTENT"
