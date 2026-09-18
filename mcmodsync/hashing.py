"""SHA-256 hashing and flat mods scanning.

Spec sections: 3.3, 10.3
"""
from __future__ import annotations

import hashlib
import os
from typing import Dict, List, Optional

CHUNK = 1024 * 1024  # 1 MiB

EXIT_IO_ERROR = 19


class FileEntry(dict):
    """Manifest file entry: path/sha256/size plus optional extras."""

    def __init__(self, path: str, sha256: str, size: int, **extra: object) -> None:
        super().__init__(path=path, sha256=sha256, size=size)
        self.update(extra)


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: str) -> Dict[str, object]:
    """Stream-hash a file; returns {"sha256": hex, "size": bytes}."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
            size += len(block)
    return {"sha256": h.hexdigest(), "size": size}


def is_jar(name: str) -> bool:
    return name.lower().endswith(".jar")


def scan_tree(root: str, subdir: str = "mods", exts: tuple = (".jar",),
              recursive: bool = False) -> List[FileEntry]:
    """Scan root/subdir for flat files matching exts (case-insensitive).

    - one level only when recursive is False (spec: no subdirectory recursion)
    - does not follow symlinks
    - returns entries with "path" prefixed as "<subdir>/<name>" using "/"
    """
    target = os.path.join(root, subdir)
    if not os.path.isdir(target):
        return []
    exts_l = tuple(e.lower() for e in exts)
    entries: List[FileEntry] = []
    if recursive:
        walker = os.walk(target, followlinks=False)
        for dirpath, _dirnames, filenames in walker:
            for name in filenames:
                if not name.lower().endswith(exts_l):
                    continue
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                rel = os.path.relpath(full, root).replace("\\", "/")
                info = hash_file(full)
                entries.append(FileEntry(rel, str(info["sha256"]), int(info["size"])))
    else:
        with os.scandir(target) as it:
            for d in it:
                if d.is_symlink() or not d.is_file():
                    continue
                if not d.name.lower().endswith(exts_l):
                    continue
                rel = subdir + "/" + d.name
                info = hash_file(d.path)
                entries.append(FileEntry(rel, str(info["sha256"]), int(info["size"])))
    entries.sort(key=lambda e: e["path"])
    return entries


def entry_map(entries: List[dict]) -> Dict[str, dict]:
    """List of entries -> {path: entry} map (planner input)."""
    return {e["path"]: e for e in entries}


def mtime_of(path: str) -> Optional[int]:
    try:
        return int(os.stat(path).st_mtime)
    except OSError:
        return None
