"""TL-2 测试夹具: 以子进程驱动单文件 B（server/mcmodsync-b.py）。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
B_PATH = REPO_ROOT / "server" / "mcmodsync-b.py"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_b(*args, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(B_PATH)] + [str(a) for a in args],
        capture_output=True, text=True, timeout=timeout, cwd=str(B_PATH.parent),
    )


def read_json(path) -> dict:
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


class Pack:
    """一个受控的 server-dir + staging + history 布局（全部位于同一临时目录）。"""

    def __init__(self, root, pack_id: str = "test-pack", mods_dir: str = "mods"):
        self.root = Path(root)
        self.server = self.root / "server"
        self.staging = self.server / ".mcmodsync-staging"
        self.history = self.root / "history"
        self.pack_id = pack_id
        self.mods_dir = mods_dir
        (self.server / mods_dir).mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self.history.mkdir(parents=True, exist_ok=True)

    # ---- 数据构造 ----
    def put_blob(self, data: bytes) -> dict:
        sha = sha256_bytes(data)
        d = self.staging / "blobs" / sha[:2] / sha[2:4]
        d.mkdir(parents=True, exist_ok=True)
        (d / sha).write_bytes(data)
        return {"sha256": sha, "size": len(data)}

    def blob_path(self, sha: str) -> Path:
        return self.staging / "blobs" / sha[:2] / sha[2:4] / sha

    def add_mod(self, name: str, data: bytes) -> str:
        p = self.server / self.mods_dir / name
        p.write_bytes(data)
        return sha256_bytes(data)

    def mod_path(self, name: str) -> Path:
        return self.server / self.mods_dir / name

    def desired_doc(self, version: str, files: list) -> dict:
        return {"schemaVersion": 1, "packId": self.pack_id, "version": version, "files": files}

    def write_desired(self, version: str, files: list):
        p = self.staging / ("desired-%s.json" % version)
        p.write_text(json.dumps(self.desired_doc(version, files), ensure_ascii=False), encoding="utf-8")
        return str(p)

    # ---- 调用 B ----
    def apply(self, version: str, desired_path=None, inject: str = "", extra=()):
        args = ["apply", "--server-dir", self.server, "--staging", self.staging,
                "--desired", desired_path or (self.staging / ("desired-%s.json" % version)),
                "--version", version, "--pack-id", self.pack_id,
                "--history-dir", self.history, *extra]
        if inject:
            args += ["--inject-fault", inject]
        return run_b(*args)

    def rollback(self, version=None, pack_id=None, extra=()):
        args = ["rollback", "--server-dir", self.server, "--history-dir", self.history,
                "--pack-id", self.pack_id if pack_id is None else pack_id, *extra]
        if version:
            args += ["--version", version]
        return run_b(*args)

    def manifest(self):
        return run_b("manifest", "--server-dir", self.server)

    # ---- 查询 ----
    def mods_dict(self) -> dict:
        out = {}
        for p in sorted((self.server / self.mods_dir).glob("*.jar")):
            out[p.name] = sha256_bytes(p.read_bytes())
        return out

    def state(self) -> dict:
        return read_json(self.server / ".mcmodsync" / "state.json")

    def ver_dir(self, version: str) -> Path:
        return self.history / self.pack_id / version

    def changes(self, version: str) -> dict:
        return read_json(self.ver_dir(version) / "changes.json")

    def clear_staging(self) -> None:
        import shutil
        for name in os.listdir(self.staging):
            p = self.staging / name
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
