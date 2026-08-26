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


def test_index_rejects_symlinked_skill_directory_escape(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# Leaked\n\nsecret instructions", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ReadToolError):
        SkillReader(root, per_call=100, per_job=100).index()


def test_images_prefix_reads_descriptions_from_project_images_root(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    (root / "guide.md").write_text("skill guide", encoding="utf-8")
    images = tmp_path / "project" / "images"
    images.mkdir(parents=True)
    (images / "封面图.png").write_bytes(b"\x89PNG fake bytes")
    (images / "封面图.md").write_text("描述：封面主视觉", encoding="utf-8")

    reader = SkillReader(root, per_call=100, per_job=100, images_root=images)

    result = reader.read("images/封面图.md")
    assert result.path == "images/封面图.md"
    assert result.content == "描述：封面主视觉"
    # Skills reads keep working on the same reader.
    assert reader.read("guide.md").content == "skill guide"


@pytest.mark.parametrize(
    "path",
    [
        "images/封面图.png",
        "images/封面图.jpg",
        "images/../outside.md",
        "images/missing.md",
        "images/",
        "images/sub/封面图.md",
    ],
)
def test_images_prefix_rejects_non_description_or_unsafe_paths(
    tmp_path: Path, path: str
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    images = tmp_path / "project" / "images"
    images.mkdir(parents=True)
    (images / "封面图.png").write_bytes(b"\x89PNG fake bytes")
    (images / "封面图.md").write_text("描述", encoding="utf-8")
    (images / "sub").mkdir()
    (images / "sub" / "封面图.md").write_text("嵌套", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (images / "linked.md").symlink_to(outside)

    reader = SkillReader(root, per_call=100, per_job=100, images_root=images)
    with pytest.raises(ReadToolError):
        reader.read(path)


def test_images_prefix_symlink_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    images = tmp_path / "project" / "images"
    images.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (images / "linked.md").symlink_to(outside)

    reader = SkillReader(root, per_call=100, per_job=100, images_root=images)
    with pytest.raises(ReadToolError):
        reader.read("images/linked.md")


def test_reader_without_images_root_keeps_images_prefix_on_skills_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()

    reader = SkillReader(root, per_call=100, per_job=100)

    with pytest.raises(ReadToolError):
        reader.read("images/anything.md")


def test_images_root_reader_shares_one_budget_across_roots(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    (root / "guide.md").write_text("abcdef", encoding="utf-8")
    images = tmp_path / "project" / "images"
    images.mkdir(parents=True)
    (images / "a.md").write_text("123456", encoding="utf-8")

    reader = SkillReader(root, per_call=3, per_job=3, images_root=images)

    assert reader.read("guide.md").content == "abc"
    with pytest.raises(ReadToolError, match="budget"):
        reader.read("images/a.md")
