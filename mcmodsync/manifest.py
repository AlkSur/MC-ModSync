"""Manifest JSON models, atomic IO and atomic file copy.

Spec sections: 3.1, 4.x
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

SUPPORTED_SCHEMA_VERSION = 1

EXIT_IO_ERROR = 19


def atomic_write(path: str, data: bytes) -> None:
    """Temp file in same directory + flush + fsync + os.replace."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp-%d" % (os.path.basename(path), os.getpid()))
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_text(path: str, text: str) -> None:
    atomic_write(path, text.encode("utf-8"))


def read_json(path: str) -> Any:
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def write_json(path: str, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def atomic_copy(src: str, dst: str) -> None:
    """Copy via temp file in dst directory + fsync + os.replace."""
    d = os.path.dirname(os.path.abspath(dst))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp-%d" % (os.path.basename(dst), os.getpid()))
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        while True:
            block = fi.read(1024 * 1024)
            if not block:
                break
            fo.write(block)
        fo.flush()
        os.fsync(fo.fileno())
    os.replace(tmp, dst)


def check_schema(obj: Dict[str, Any], max_supported: int = SUPPORTED_SCHEMA_VERSION) -> None:
    v = obj.get("schemaVersion")
    if not isinstance(v, int):
        raise ValueError("缺少 schemaVersion")
    if v > max_supported:
        raise ValueError("schemaVersion %d 高于本程序支持值 %d" % (v, max_supported))


def make_state(pack_id: str, version: Optional[str], applied_at: str,
               files: List[dict]) -> Dict[str, Any]:
    return {
        "schemaVersion": SUPPORTED_SCHEMA_VERSION,
        "packId": pack_id,
        "lastAppliedVersion": version,
        "appliedAt": applied_at,
        "files": files,
    }


def make_desired(pack_id: str, version: str, files: List[dict]) -> Dict[str, Any]:
    return {
        "schemaVersion": SUPPORTED_SCHEMA_VERSION,
        "packId": pack_id,
        "version": version,
        "files": files,
    }
