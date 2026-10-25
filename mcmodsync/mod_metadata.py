"""Read root mod identities without loading code or extracting a JAR.

Only declared mods count; dependency IDs and bundled Jar-in-Jar mods do not.
Unknown or malformed metadata returns no identity and never authorizes deletion.
"""
from __future__ import annotations

import json
import re
import zipfile
from typing import List

MAX_METADATA_SIZE = 1024 * 1024
_ID = re.compile(r"[a-z][a-z0-9_-]{1,127}\Z")


def _toml_ids(text: str) -> List[str]:
    try:
        import tomllib
    except ImportError:  # Python 3.10: conservatively read literal [[mods]] entries.
        ids = []
        in_mod = False
        multiline = None
        for line in text.splitlines():
            line = line.strip()
            if multiline:
                if line.count(multiline) % 2:
                    multiline = None
                continue
            if line.startswith("#"):
                continue
            for quote in ('"""', "'''"):
                if line.split("#", 1)[0].count(quote) % 2:
                    multiline = quote
                    break
            if multiline:
                continue
            if line.startswith("["):
                in_mod = bool(re.fullmatch(r"\[\[\s*mods\s*\]\]\s*(?:#.*)?", line))
            elif in_mod:
                match = re.fullmatch(r'''modId\s*=\s*(["'])([a-z][a-z0-9_-]{1,127})\1\s*(?:#.*)?''', line)
                if match:
                    ids.append(match.group(2))
        return ids
    try:
        doc = tomllib.loads(text)
        return [row.get("modId") for row in doc.get("mods", []) if isinstance(row, dict)]
    except (ValueError, TypeError):
        return []


def read_mod_ids(path: str) -> List[str]:
    """Return sorted root NeoForge/Forge/Fabric/Quilt IDs, or [] if unknown."""
    ids = []
    try:
        with zipfile.ZipFile(path) as jar:
            for name in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml",
                         "fabric.mod.json", "quilt.mod.json"):
                try:
                    info = jar.getinfo(name)
                except KeyError:
                    continue
                if info.file_size > MAX_METADATA_SIZE:
                    return []
                text = jar.read(info).decode("utf-8-sig")
                if name.endswith(".toml"):
                    ids.extend(_toml_ids(text))
                else:
                    doc = json.loads(text)
                    if name == "quilt.mod.json":
                        doc = doc.get("quilt_loader", {})
                    ids.append(doc.get("id"))
    except (OSError, ValueError, TypeError, AttributeError, zipfile.BadZipFile,
            RuntimeError, NotImplementedError):
        return []
    return sorted({value for value in ids if isinstance(value, str) and _ID.fullmatch(value)})
