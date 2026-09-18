"""T-23: cli.py —— keygen / doctor / 分发与必填校验。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from mcmodsync import cli, signing

FAKE_AK = "AKIAIOSFODNN7EXAMPLE"
FAKE_SK = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def _git(repo: Path, *args: str) -> None:
    p = subprocess.run(["git"] + list(args), cwd=str(repo), capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


def _make_repo(tmp_path: Path, gitignore: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    if gitignore:
        (repo / ".gitignore").write_text(
            "pack.local.json\nssh.json\n.mcmodsync/\n*.pem\n", encoding="utf-8")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "init")
    return repo


def _make_cfg(repo: Path, key_file: str, pub_b64: str) -> dict:
    return {
        "packId": "cli-test-pack",
        "server": {"host": "example.com", "port": 22, "user": "u",
                   "authMethod": "password", "keyFile": "", "password": "P@ssw0rd-123",
                   "knownHosts": "~/.ssh/known_hosts", "serverDir": "/www/srv",
                   "modsDir": "mods", "sourceModsDir": "./server-mods",
                   "remoteBPath": "/www/mcmodsync-b.py", "historyDir": "/www/mcmodsync-history",
                   "historyKeep": 3},
        "client": {"sourceModsDir": "./client-mods", "manifestUrl": "https://example.com/p/manifest.json",
                   "publicKey": pub_b64, "clientStateFile": "./client-publish-state.json"},
        "storage": {"endpointUrl": "https://s3.example.com", "region": "us-west",
                    "pathStyle": True, "bucket": "bkt", "prefix": "packs/p/",
                    "accessKey": FAKE_AK, "secretKey": FAKE_SK,
                    "publicBaseUrl": "https://cdn.example.com"},
        "curseforge": {"apiKey": ""},
        "signing": {"privateKeyFile": key_file},
        "concurrency": {"upload": 4, "download": 4},
    }


@pytest.fixture()
def env(tmp_path):
    repo = _make_repo(tmp_path)
    key_file = str(tmp_path / "private.key")
    pub_b64, key_file = signing.keygen(key_file)
    cfg = _make_cfg(repo, key_file, pub_b64)
    (repo / "pack.local.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return dict(repo=repo, key=key_file, pub=pub_b64, cfg=cfg, tmp=tmp_path)


# --------------------------------------------------------------------------
# keygen
# --------------------------------------------------------------------------

def test_keygen_writes_and_prints(tmp_path) -> None:
    logs = []
    kf = str(tmp_path / "k" / "private.key")
    assert cli.cmd_keygen(kf, log=logs.append) == 0
    assert os.path.isfile(kf)
    assert os.path.getsize(kf) > 0
    assert any(len(m) == 44 for m in logs)          # Base64 公钥
    # 已存在 -> 拒绝覆盖
    assert cli.cmd_keygen(kf, log=lambda m: None) == 1


def test_main_keygen_dispatch(tmp_path) -> None:
    assert cli.main(["keygen", "--key-file", str(tmp_path / "x.key")]) == 0


# --------------------------------------------------------------------------
# publish-client 必填
# --------------------------------------------------------------------------

def test_publish_client_requires_version() -> None:
    class A:
        version = ""
        config = "nope.json"
        notes = None
        dry_run = False
    assert cli.cmd_publish_client(A()) == 1


def test_main_publish_client_without_version_returns_1(tmp_path) -> None:
    assert cli.main(["publish-client", "-c", str(tmp_path / "nope.json")]) == 1


def test_main_package_client_missing_config() -> None:
    # 配置缺失 -> 码 1（不再有「占位」实现，T-50 已实装）
    assert cli.main(["package-client", "-c", "nope.json"]) == 1


def test_main_package_client_no_exe_assembles(tmp_path, monkeypatch) -> None:
    """package-client --no-exe: 组装 .py 兜底 + config.json + SHA256SUMS。"""
    from mcmodsync import packaging

    fb = tmp_path / "fallback.py"
    fb.write_text("# fallback stub\n", encoding="utf-8")
    pack = tmp_path / "pack.json"
    pack.write_text(json.dumps({"packId": "demo-pack",
                                "client": {"manifestUrl": "https://h/p/manifest.json",
                                           "publicKey": "PUBKEY"}}), encoding="utf-8")
    out = tmp_path / "pkg"
    rc = cli.main(["package-client", "-c", str(pack), "--out", str(out),
                   "--no-exe", "--fallback", str(fb)])
    assert rc == 0
    assert (out / "更新mod.bat").is_file()
    assert (out / "更新mod.sh").is_file()
    assert (out / "_updater" / "mcmodsync.py").read_text(encoding="utf-8") == "# fallback stub\n"
    cfg = json.loads((out / "_updater" / "config.json").read_text(encoding="utf-8"))
    assert cfg["packId"] == "demo-pack" and cfg["schemaVersion"] == 1
    assert (out / "SHA256SUMS.txt").is_file()
    # 校验和逐条复核
    for line in (out / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        sha, rel = line.split("  ", 1)
        data = (out / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha


def test_main_fetch_mods_bad_config_returns_1(tmp_path) -> None:
    assert cli.main(["fetch-mods", "-c", str(tmp_path / "nope.json")]) == 1


def test_main_fetch_mods_green_with_empty_lock(tmp_path) -> None:
    """合法配置 + 空 mods 清单 -> fetch-mods 返回 0（无网络访问）。"""
    repo = _make_repo(tmp_path)
    key_file = str(tmp_path / "private.key")
    pub_b64, key_file = signing.keygen(key_file)
    cfg = _make_cfg(repo, key_file, pub_b64)
    (repo / "server-mods").mkdir()
    (repo / "client-mods").mkdir()
    (repo / "pack.local.json").write_text(json.dumps(cfg), encoding="utf-8")
    lock = repo / "mods.lock.json"
    lock.write_text(json.dumps({"schemaVersion": 1,
                                "packMeta": {"minecraftVersion": "1.21.1", "loader": "neoforge"},
                                "mods": []}), encoding="utf-8")
    assert cli.main(["fetch-mods", "-c", str(repo / "pack.local.json"),
                     "--lock", str(lock)]) == 0


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def test_doctor_offline_green(env) -> None:
    logs = []
    rc = cli.doctor(str(env["repo"] / "pack.local.json"), repo_dir=str(env["repo"]),
                    offline=True, log=logs.append)
    assert rc == 0, logs
    assert any("结论: 全绿" in m for m in logs)
    assert any("[PASS] 配置完整性" in m for m in logs)
    assert any("未被 git 追踪" in m for m in logs)


def test_doctor_detects_unignored_config(tmp_path) -> None:
    repo = _make_repo(tmp_path, gitignore=False)
    key_file = str(tmp_path / "private.key")
    pub_b64, key_file = signing.keygen(key_file)
    cfg = _make_cfg(repo, key_file, pub_b64)
    (repo / "pack.local.json").write_text(json.dumps(cfg), encoding="utf-8")

    logs = []
    rc = cli.doctor(str(repo / "pack.local.json"), repo_dir=str(repo), offline=True, log=logs.append)
    assert rc == 1
    assert any("未忽略" in m for m in logs)


def test_doctor_detects_tracked_config(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    key_file = str(tmp_path / "private.key")
    pub_b64, key_file = signing.keygen(key_file)
    cfg = _make_cfg(repo, key_file, pub_b64)
    (repo / "pack.local.json").write_text(json.dumps(cfg), encoding="utf-8")
    _git(repo, "add", "-f", "pack.local.json")     # 强制入库（模拟误提交）
    _git(repo, "commit", "-q", "-m", "oops")

    logs = []
    rc = cli.doctor(str(repo / "pack.local.json"), repo_dir=str(repo), offline=True, log=logs.append)
    assert rc == 1
    assert any("已被追踪" in m for m in logs)


def test_doctor_detects_credentials_in_history(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    key_file = str(tmp_path / "private.key")
    pub_b64, key_file = signing.keygen(key_file)
    cfg = _make_cfg(repo, key_file, pub_b64)
    (repo / "pack.local.json").write_text(json.dumps(cfg), encoding="utf-8")
    # 伪造：把 AK 明文提交进历史
    (repo / "leak.txt").write_text("key=%s" % FAKE_AK, encoding="utf-8")
    _git(repo, "add", "leak.txt")
    _git(repo, "commit", "-q", "-m", "leak")
    (repo / "leak.txt").unlink()
    _git(repo, "rm", "--cached", "-q", "leak.txt")
    _git(repo, "commit", "-q", "-m", "remove leak file")

    logs = []
    rc = cli.doctor(str(repo / "pack.local.json"), repo_dir=str(repo), offline=True, log=logs.append)
    assert rc == 1
    assert any("git 历史凭证扫描" in m and "[FAIL]" in m for m in logs)
    assert any("删文件无效" in m for m in logs)


def test_doctor_detects_public_key_mismatch(env) -> None:
    other_pub, _ = signing.keygen(str(env["tmp"] / "other.key"))
    cfg = dict(env["cfg"])
    cfg["client"] = dict(cfg["client"], publicKey=other_pub)
    (env["repo"] / "pack.local.json").write_text(json.dumps(cfg), encoding="utf-8")

    logs = []
    rc = cli.doctor(str(env["repo"] / "pack.local.json"), repo_dir=str(env["repo"]),
                    offline=True, log=logs.append)
    assert rc == 1
    assert any("publicKey 与私钥不匹配" in m for m in logs)


def test_doctor_missing_config_fails(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    logs = []
    rc = cli.doctor(str(repo / "absent.json"), repo_dir=str(repo), offline=True, log=logs.append)
    assert rc == 1
    assert any("[FAIL] 配置完整性" in m for m in logs)
