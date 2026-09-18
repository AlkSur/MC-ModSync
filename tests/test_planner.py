"""TL-1: planner.py —— 纯函数变更分类。"""
from __future__ import annotations

from mcmodsync import planner


def _e(sha: str, size: int) -> dict:
    return {"sha256": sha, "size": size}


def test_classification() -> None:
    base = {
        "mods/keep.jar": _e("aa", 1),
        "mods/upd.jar": _e("bb", 2),
        "mods/gone.jar": _e("cc", 3),
    }
    desired = {
        "mods/keep.jar": _e("aa", 1),
        "mods/upd.jar": _e("dd", 4),
        "mods/new.jar": _e("ee", 5),
    }
    d = planner.plan(base, desired)

    assert [e["path"] for e in d["unchanged"]] == ["mods/keep.jar"]
    assert [e["path"] for e in d["added"]] == ["mods/new.jar"]
    assert [e["path"] for e in d["deleted"]] == ["mods/gone.jar"]
    assert [e["path"] for e in d["replaced"]] == ["mods/upd.jar"]

    rep = d["replaced"][0]
    assert rep["oldSha256"] == "bb"
    assert rep["newSha256"] == "dd"
    assert rep["size"] == 4


def test_same_content_rename_shows_as_added_plus_deleted() -> None:
    base = {"mods/old-name.jar": _e("aa", 10)}
    desired = {"mods/new-name.jar": _e("aa", 10)}
    d = planner.plan(base, desired)
    assert [e["path"] for e in d["added"]] == ["mods/new-name.jar"]
    assert [e["path"] for e in d["deleted"]] == ["mods/old-name.jar"]
    assert d["replaced"] == []
    assert d["unchanged"] == []


def test_empty_both_sides() -> None:
    d = planner.plan({}, {})
    assert d == {"added": [], "replaced": [], "deleted": [], "unchanged": []}


def test_added_and_deleted_carry_size() -> None:
    d = planner.plan({"mods/a.jar": _e("aa", 7)}, {})
    assert d["deleted"][0]["size"] == 7
    d2 = planner.plan({}, {"mods/b.jar": _e("bb", 9)})
    assert d2["added"][0]["size"] == 9


def test_is_pure_no_mutation() -> None:
    base = {"mods/a.jar": _e("aa", 1)}
    desired = {"mods/a.jar": _e("bb", 2)}
    snapshot = (dict(base), dict(desired))
    planner.plan(base, desired)
    assert (base, desired) == snapshot
