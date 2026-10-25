from __future__ import annotations

import builtins
import io
import json
import zipfile

import pytest

from mcmodsync import client
from mcmodsync.mod_metadata import MAX_METADATA_SIZE, read_mod_ids
from test_client import env  # noqa: F401 - shared local-only signed cloud fixture


def jar_bytes(ids, version="1", metadata="META-INF/neoforge.mods.toml"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as jar:
        if metadata.endswith(".toml"):
            text = 'modLoader="javafml"\nloaderVersion="[1,)"\nlicense="test"\n'
            for mod_id in ids:
                text += '[[mods]]\nmodId="%s"\nversion="%s"\n' % (mod_id, version)
            text += '[[dependencies.%s]]\nmodId="neoforge"\n' % ids[0]
        elif metadata == "fabric.mod.json":
            text = json.dumps({"id": ids[0], "version": version, "depends": {"fabricloader": "*"}})
        else:
            text = json.dumps({"quilt_loader": {"id": ids[0], "version": version}})
        jar.writestr(metadata, text)
        jar.writestr("payload.txt", version)
    return out.getvalue()


@pytest.mark.parametrize("metadata", ["META-INF/neoforge.mods.toml", "META-INF/mods.toml",
                                      "fabric.mod.json", "quilt.mod.json"])
def test_reads_root_mod_id_not_dependencies(tmp_path, metadata):
    path = tmp_path / "arbitrary-name.jar"
    path.write_bytes(jar_bytes(["thaumcraft"], metadata=metadata))
    assert read_mod_ids(str(path)) == ["thaumcraft"]


def test_invalid_oversized_and_nested_metadata_are_unknown(tmp_path):
    path = tmp_path / "mod.jar"
    path.write_bytes(b"not a jar")
    assert read_mod_ids(str(path)) == []
    with zipfile.ZipFile(path, "w") as jar:
        jar.writestr("META-INF/jarjar/dependency.jar", jar_bytes(["dependency"]))
    assert read_mod_ids(str(path)) == []
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as jar:
        jar.writestr("fabric.mod.json", "x" * (MAX_METADATA_SIZE + 1))
    assert read_mod_ids(str(path)) == []


def test_python310_literal_parser_ignores_description_and_dependencies(tmp_path, monkeypatch):
    path = tmp_path / "mod.jar"
    with zipfile.ZipFile(path, "w") as jar:
        jar.writestr("META-INF/neoforge.mods.toml", '''[[mods]]
modId = 'actual_mod' # actual identity
description = """
[[mods]]
modId = 'fake_mod'
"""
[[dependencies.actual_mod]]
modId = 'neoforge'
''')
    original_import = builtins.__import__

    def without_tomllib(name, *args, **kwargs):
        if name == "tomllib":
            raise ImportError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_tomllib)
    assert read_mod_ids(str(path)) == ["actual_mod"]


def test_renamed_old_version_replaced_and_private_mod_retained(env):
    old = jar_bytes(["thaumcraft"], "1")
    new = jar_bytes(["thaumcraft"], "10")
    private = jar_bytes(["private_helper"], "3")
    env.inst.put_mods({"我改过名字.jar": old, "thaumcraft-helper.jar": private})
    env.cloud.publish("1.1.3", {"mods/thaumcraft-new.jar": new})
    logs = []
    assert env.inst.sync(log=logs.append) == 0
    assert env.inst.mod_names() == ["thaumcraft-helper.jar", "thaumcraft-new.jar"]
    assert env.inst.mod_bytes("thaumcraft-helper.jar") == private
    assert any("mod-id" in line for line in logs)
    # 自动清理必须进入备份与回滚，玩家可恢复原来的旧文件。
    assert env.inst.rollback() == 0
    assert env.inst.mod_bytes("我改过名字.jar") == old
    assert env.inst.mod_bytes("thaumcraft-helper.jar") == private
    assert "thaumcraft-new.jar" not in env.inst.mod_names()


def test_renamed_duplicate_removed_even_when_canonical_mod_is_unchanged(env):
    current = jar_bytes(["thaumcraft"], "10")
    env.inst.put_mods({"current.jar": current, "my-old-copy.jar": jar_bytes(["thaumcraft"], "1")})
    env.cloud.publish("1.1.3", {"mods/current.jar": current})
    assert env.inst.sync() == 0
    assert env.inst.mod_names() == ["current.jar"]


def test_no_metadata_uses_exact_content_identity_only(env):
    env.inst.put_mods({"renamed.jar": b"CURRENT", "unknown-old.jar": b"OLD"})
    env.cloud.publish("1.1.3", {"mods/current.jar": b"CURRENT"})
    assert env.inst.sync() == 0
    assert env.inst.mod_names() == ["current.jar", "unknown-old.jar"]


def test_mixed_bundle_conflict_does_not_delete_private_mod(env):
    mixed = jar_bytes(["thaumcraft", "private_helper"], "1")
    env.inst.put_mods({"mixed.jar": mixed})
    env.cloud.publish("1.1.3", {"mods/current.jar": jar_bytes(["thaumcraft"], "10")})
    logs = []
    assert env.inst.sync(log=logs.append) == client.EXIT_GENERIC
    assert env.inst.mod_names() == ["mixed.jar"]
    assert env.inst.mod_bytes("mixed.jar") == mixed
    assert not env.inst.state()
    assert any("为避免误删已停止" in line for line in logs)


def test_duplicate_ids_in_published_pack_are_rejected(env):
    env.cloud.publish("1.1.3", {"mods/old.jar": jar_bytes(["thaumcraft"], "1"),
                                "mods/new.jar": jar_bytes(["thaumcraft"], "10")})
    assert env.inst.sync() == client.EXIT_GENERIC
    assert env.inst.mod_names() == []
