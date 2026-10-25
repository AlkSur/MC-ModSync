from __future__ import annotations

import json

import pytest

from charness import PACK_ID, make_keys
from test_publisher_minio import MemStore
from mcmodsync import __version__, canonicaljson, self_update, updater_publish


@pytest.fixture
def env(tmp_path):
    pem, pub = make_keys(tmp_path)
    key = tmp_path / "private.key"
    key.write_bytes(pem)
    cfg = {"packId": PACK_ID, "client": {"publicKey": pub}, "signing": {"privateKeyFile": str(key)}}
    package = tmp_path / "package"
    up = package / "_updater"
    up.mkdir(parents=True)
    (up / "build-info.json").write_text(json.dumps({"version": __version__}), encoding="utf-8")
    (up / "config.json").write_text(json.dumps({"packId": PACK_ID, "publicKey": pub}), encoding="utf-8")
    (up / "mcmodsync.py").write_text("print('MC-ModSync %s')\n" % __version__, encoding="utf-8")
    return cfg, MemStore(), package, up


def test_publish_signs_release_and_writes_pointer_last(env):
    cfg, store, package, _ = env
    assert updater_publish.publish_updater(cfg, store, str(package)) == 0
    assert store.order[-1] == "updater.json"
    doc = json.loads(store.get_text("updater.json"))
    assert doc["version"] == __version__
    assert canonicaljson.verify(doc, cfg["client"]["publicKey"])
    assert store.order[0].startswith("updater/files/")
    assert store.objs["updater.json"][1] == "no-cache,no-store,must-revalidate"


def test_publish_dry_run_does_not_upload(env):
    cfg, store, package, _ = env
    assert updater_publish.publish_updater(cfg, store, str(package), dry_run=True) == 0
    assert not store.order


def test_cannot_publish_old_script_as_new_version(env):
    cfg, store, package, up = env
    (up / "build-info.json").write_text(json.dumps({"version": "9.0.0"}), encoding="utf-8")
    with pytest.raises(updater_publish.UpdaterPublishError, match="实际版本"):
        updater_publish.publish_updater(cfg, store, str(package))
    assert not store.order


def test_cannot_silently_replace_same_version_with_different_files(env):
    cfg, store, package, up = env
    updater_publish.publish_updater(cfg, store, str(package))
    previous_order = list(store.order)
    with (up / "mcmodsync.py").open("a", encoding="utf-8") as stream:
        stream.write("# different binary content, unchanged version\n")
    with pytest.raises(updater_publish.UpdaterPublishError, match="同一更新器版本"):
        updater_publish.publish_updater(cfg, store, str(package))
    assert store.order == previous_order


def test_package_for_wrong_pack_not_published(env):
    cfg, store, package, up = env
    (up / "config.json").write_text(json.dumps({"packId": "other-pack", "publicKey": "other-key"}), encoding="utf-8")
    with pytest.raises(updater_publish.UpdaterPublishError, match="不一致"):
        updater_publish.publish_updater(cfg, store, str(package))
    assert not store.order
