import json
from pathlib import Path

import pytest

import mcmodsync
from mcmodsync import packaging


def inputs(tmp_path):
    config=tmp_path/'pack.json'
    config.write_text(json.dumps({'packId':'test','client':{'manifestUrl':'https://example.invalid/manifest.json','publicKey':'PUB'}}),encoding='utf-8')
    fallback=tmp_path/'fallback.py'
    fallback.write_text("print('MC-ModSync 2.2.1')\n",encoding='utf-8')
    return config,fallback


def test_menu_cached_version_cannot_label_new_package_as_old(tmp_path, monkeypatch):
    config,fallback=inputs(tmp_path)
    monkeypatch.setattr(mcmodsync,'__version__','2.2.0')
    out=tmp_path/'package'
    packaging.assemble(str(config),str(out),no_exe=True,fallback=str(fallback))
    info=json.loads((out/'_updater/build-info.json').read_text(encoding='utf-8'))
    assert info['version']=='2.2.1'


def test_mixed_runtime_versions_fail_during_packaging(tmp_path, monkeypatch):
    config,fallback=inputs(tmp_path)
    exe=tmp_path/'old.exe'
    exe.write_bytes(b'stub')
    monkeypatch.setattr(packaging,'runtime_version',lambda path,python=False:'2.2.1' if python else '2.2.0')
    out=tmp_path/'package'
    with pytest.raises(packaging.PackagingError,match='不一致'):
        packaging.assemble(str(config),str(out),exe=str(exe),fallback=str(fallback))
    assert not (out/'_updater/build-info.json').exists()


def test_invalid_runtime_version_does_not_pass(tmp_path):
    script=tmp_path/'invalid.py'
    script.write_text("print('not a version')\n",encoding='utf-8')
    with pytest.raises(packaging.PackagingError,match='有效版本'):
        packaging.runtime_version(str(script),python=True)
