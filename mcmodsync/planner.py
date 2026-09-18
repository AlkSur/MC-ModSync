"""Change planner: pure diff between base and desired file maps.

Spec section: 10.2
"""
from __future__ import annotations

from typing import Dict, List


def plan(base: Dict[str, dict], desired: Dict[str, dict]) -> Dict[str, List[dict]]:
    """Compare {path: entry} maps; returns added/replaced/deleted/unchanged lists.

    - added:    in desired, not in base
    - replaced: in both, sha256 differs
    - deleted:  in base, not in desired
    - unchanged: in both, sha256 identical
    Rename with same content shows up as added + deleted (blob dedup handles it).
    """
    added: List[dict] = []
    replaced: List[dict] = []
    deleted: List[dict] = []
    unchanged: List[dict] = []

    for path, want in sorted(desired.items()):
        cur = base.get(path)
        if cur is None:
            added.append({
                "path": path,
                "newSha256": want["sha256"],
                "size": want["size"],
            })
        elif cur.get("sha256") != want["sha256"]:
            replaced.append({
                "path": path,
                "oldSha256": cur.get("sha256"),
                "newSha256": want["sha256"],
                "size": want["size"],
            })
        else:
            unchanged.append({
                "path": path,
                "sha256": want["sha256"],
                "size": want["size"],
            })

    for path, cur in sorted(base.items()):
        if path not in desired:
            deleted.append({
                "path": path,
                "oldSha256": cur.get("sha256"),
                "size": cur.get("size"),
            })

    return {
        "added": added,
        "replaced": replaced,
        "deleted": deleted,
        "unchanged": unchanged,
    }
