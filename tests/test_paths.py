"""TL-1: paths.py —— 路径规范化与越界防护。"""
from __future__ import annotations

import pytest

from mcmodsync import paths


class TestNormalizeRel:
    def test_allows_normal(self) -> None:
        assert paths.normalize_rel("mods/a.jar") == "mods/a.jar"

    def test_backslash_to_slash(self) -> None:
        assert paths.normalize_rel("mods\\sub\\a.jar") == "mods/sub/a.jar"

    def test_strips_leading_dot_slash(self) -> None:
        assert paths.normalize_rel("./mods/a.jar") == "mods/a.jar"
        assert paths.normalize_rel("././mods/a.jar") == "mods/a.jar"

    @pytest.mark.parametrize("bad", [".//mods/a.jar", ".///x"])
    def test_dot_slash_then_absolute_rejected(self, bad: str) -> None:
        # strip "./" 后以 "/" 开头 -> 按规范判为绝对路径拒绝
        with pytest.raises(paths.UnsafePath):
            paths.normalize_rel(bad)

    @pytest.mark.parametrize("bad", [
        "",
        "/abs/path.jar",
        "C:/windows/a.jar",
        "c:\\windows\\a.jar",
        "../etc/passwd",
        "mods/../../etc/passwd",
        "mods/../a.jar",
    ])
    def test_rejects_illegal(self, bad: str) -> None:
        with pytest.raises(paths.UnsafePath):
            paths.normalize_rel(bad)

    def test_non_string_rejected(self) -> None:
        with pytest.raises(paths.UnsafePath):
            paths.normalize_rel(None)  # type: ignore[arg-type]


class TestSafeJoin:
    def test_join_inside_base(self, tmp_path) -> None:
        got = paths.safe_join(str(tmp_path), "mods/a.jar")
        assert got == str(tmp_path / "mods" / "a.jar")

    @pytest.mark.parametrize("rel", ["../x", "/etc/passwd", "C:/x"])
    def test_rejects_escape(self, tmp_path, rel: str) -> None:
        with pytest.raises(paths.UnsafePath):
            paths.safe_join(str(tmp_path), rel)


class TestIsWithin:
    def test_within(self, tmp_path) -> None:
        child = tmp_path / "mods" / "a.jar"
        assert paths.is_within(str(child), str(tmp_path))

    def test_not_within(self, tmp_path) -> None:
        assert not paths.is_within(str(tmp_path.parent), str(tmp_path / "mods"))


def test_unsafe_path_exit_code_is_13() -> None:
    assert paths.EXIT_UNSAFE_PATH == 13
    assert paths.UnsafePath.exit_code == 13
