from pathlib import Path

import pytest

from runtime.read_tool import ReadToolError, SkillReader


def test_read_is_limited_to_skill_root(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    (root / "guide.md").write_text("abcdef", encoding="utf-8")
    reader = SkillReader(root, per_call=3, per_job=5)

    result = reader.read("guide.md")

    assert result.content == "abc"
    assert result.path == "guide.md"


@pytest.mark.parametrize("path", ["../secret.md", "/etc/passwd", "guide.json", "missing.md", "a\x00.md"])
def test_read_rejects_unsafe_paths(tmp_path: Path, path: str) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    with pytest.raises(ReadToolError):
        SkillReader(root, per_call=100, per_job=100).read(path)


def test_read_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (root / "linked.md").symlink_to(outside)

    with pytest.raises(ReadToolError):
        SkillReader(root, per_call=100, per_job=100).read("linked.md")
