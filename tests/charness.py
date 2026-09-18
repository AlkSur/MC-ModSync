# -*- coding: utf-8 -*-
"""TL-5 测试夹具：本地 HTTP 对象存储替身 + 实例目录构造（T-40）。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

from mcmodsync import canonicaljson, client, manifest, signing

PACK_ID = "demo-pack"


# --------------------------------------------------------------------------
# 本地对象存储替身
# --------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "TL5Mock/1.0"

    def log_message(self, *a):  # 静默
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        self.server.hits.append(path)                      # type: ignore[attr-defined]
        rel = path.lstrip("/")
        full = os.path.join(self.server.root, rel.replace("/", os.sep))  # type: ignore[attr-defined]
        if not os.path.isfile(full):
            body = b""
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Cloud:
    """模拟对象存储 + 签名发布端。"""

    def __init__(self, root: str, private_key_pem: bytes, pack_id: str = PACK_ID) -> None:
        self.root = root
        self.pem = private_key_pem
        self.pack_id = pack_id
        os.makedirs(root, exist_ok=True)
        self._srv: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.hits: List[str] = []

    # --- 生命周期 ---
    def start(self) -> str:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        srv.root = self.root          # type: ignore[attr-defined]
        srv.hits = self.hits          # type: ignore[attr-defined]
        self._srv = srv
        self._thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    @property
    def base_url(self) -> str:
        assert self._srv is not None
        return "http://127.0.0.1:%d" % self._srv.server_address[1]

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None

    # --- 发布 ---
    def put_blob(self, data: bytes) -> Tuple[str, int]:
        sha = hashlib.sha256(data).hexdigest()
        d = os.path.join(self.root, "blobs", sha[0:2], sha[2:4])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sha), "wb") as f:
            f.write(data)
        return sha, len(data)

    def blob_path(self, sha: str) -> str:
        return os.path.join(self.root, "blobs", sha[0:2], sha[2:4], sha)

    def publish(self, version: str, contents: Dict[str, bytes],
                delete: Optional[List[str]] = None, notes: str = "", schema: int = 1,
                pack_id: Optional[str] = None, sign: bool = True) -> dict:
        files = []
        for path in sorted(contents):
            sha, size = self.put_blob(contents[path])
            files.append({"path": path, "sha256": sha, "size": size})
        man = {
            "schemaVersion": schema,
            "packId": pack_id if pack_id is not None else self.pack_id,
            "version": version,
            "createdAt": "2026-09-18T12:00:00+08:00",
            "releaseNotes": notes,
            "files": files,
            "delete": [{"path": p, "deletedInVersion": version} for p in (delete or [])],
        }
        if sign:
            man = canonicaljson.sign(man, self.pem)
        md = os.path.join(self.root, "manifests")
        os.makedirs(md, exist_ok=True)
        with open(os.path.join(md, "%s.json" % version), "wb") as f:
            f.write(canonicaljson.canonical(man))
        pointer = {"schemaVersion": 1, "packId": man["packId"], "latest": version,
                   "manifestUrl": "manifests/%s.json" % version}
        if sign:
            pointer = canonicaljson.sign(pointer, self.pem)
        with open(os.path.join(self.root, "manifest.json"), "wb") as f:
            f.write(canonicaljson.canonical(pointer))
        return man


# --------------------------------------------------------------------------
# 实例目录
# --------------------------------------------------------------------------

class Instance:
    def __init__(self, root: str, manifest_url: str, public_key_b64: str,
                 pack_id: str = PACK_ID, mods: Optional[Dict[str, bytes]] = None) -> None:
        self.root = root
        self.mods_dir = os.path.join(root, "mods")
        os.makedirs(self.mods_dir, exist_ok=True)
        os.makedirs(client.updater_dir(root), exist_ok=True)
        cfg = {"schemaVersion": 1, "packId": pack_id, "manifestUrl": manifest_url,
               "publicKey": public_key_b64}
        manifest.write_json(client.config_path(root), cfg)
        if mods:
            self.put_mods(mods)

    # --- 磁盘操作 ---
    def put_mods(self, mods: Dict[str, bytes]) -> None:
        for name, data in mods.items():
            with open(os.path.join(self.mods_dir, name), "wb") as f:
                f.write(data)

    def remove_mods(self, names: List[str]) -> None:
        for n in names:
            p = os.path.join(self.mods_dir, n)
            if os.path.isfile(p):
                os.remove(p)

    def mod_names(self) -> List[str]:
        return sorted(os.listdir(self.mods_dir))

    def mod_bytes(self, name: str) -> bytes:
        with open(os.path.join(self.mods_dir, name), "rb") as f:
            return f.read()

    def state(self) -> dict:
        return client.read_state(self.root)

    # --- 调用 ---
    def sync(self, log=None, **kw) -> int:
        kw.setdefault("game_running_check", lambda: False)
        return client.sync(self.root, log=log or (lambda m: None), **kw)

    def verify(self, log=None, **kw) -> int:
        return client.verify(self.root, log=log or (lambda m: None), **kw)

    def rollback(self, log=None) -> int:
        return client.rollback(self.root, log=log or (lambda m: None))

    def doctor(self, log=None) -> int:
        return client.doctor(self.root, log=log or (lambda m: None))


def make_keys(tmp_path) -> Tuple[bytes, str]:
    """生成 Ed25519 密钥对；返回 (pem, pub_b64)。"""
    pub, path = signing.keygen(str(os.path.join(str(tmp_path), "k.pem")))
    with open(path, "rb") as f:
        return f.read(), pub
