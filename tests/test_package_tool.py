from pathlib import Path

import pytest

from agent_core.models import HtmlPptPackage, SampleRevision
from runtime.package_tool import DraftPackage, PackageToolError
from runtime.read_tool import SkillReader


def test_skill_reader_indexes_frontmatter_and_reads_web_assets(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill = root / "deck"
    (skill / "assets").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: deck-skill\ndescription: Build a browser deck.\n---\n# Deck\n",
        encoding="utf-8",
    )
    (skill / "assets" / "template.html").write_text("<main>deck</main>", encoding="utf-8")

    reader = SkillReader(root, per_call=1000, per_job=2000)

    assert reader.index() == [{
        "name": "deck-skill",
        "description": "Build a browser deck.",
        "path": "deck/SKILL.md",
    }]
    assert reader.read("deck/assets/template.html").content == "<main>deck</main>"


def test_draft_package_confines_writes_reads_and_skill_asset_copies(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    asset = root / "deck" / "assets" / "motion.min.js"
    asset.parent.mkdir(parents=True)
    asset.write_text("export const motion = true;", encoding="utf-8")
    draft = DraftPackage(root)

    written = draft.write("index.html", "<script src='assets/motion.min.js'></script>")
    copied = draft.copy_skill_asset("deck/assets/motion.min.js", "assets/motion.min.js")
    replaced = draft.replace_text("index.html", "</script>", "</script><main>deck</main>")

    assert written["path"] == "index.html"
    assert copied["source_path"] == "deck/assets/motion.min.js"
    assert replaced["replacements"] == 1
    assert "<main>deck</main>" in draft.read("index.html")["content"]
    assert draft.read("assets/motion.min.js")["content"] == "export const motion = true;"
    assert [item["path"] for item in draft.payload()] == ["assets/motion.min.js", "index.html"]

    with pytest.raises(PackageToolError, match="stay inside"):
        draft.write("../outside.html", "unsafe")
    with pytest.raises(PackageToolError, match="inside skills_root"):
        draft.copy_skill_asset("../outside.js", "outside.js")


def test_draft_package_rejects_executable_and_oversized_files(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    draft = DraftPackage(root, max_file_bytes=8)

    with pytest.raises(PackageToolError, match="file type"):
        draft.write("run.sh", "echo x")
    with pytest.raises(PackageToolError, match="per-file"):
        draft.write("index.html", "123456789")


def test_sample_revision_identity_includes_parent_revision() -> None:
    package = HtmlPptPackage.model_validate({
        "title": "身份测试",
        "slide_count": 1,
        "slides": [{"slide_id": "cover", "title": "封面"}],
        "files": [{"path": "index.html", "content": "<main>封面</main>"}],
    })

    left = SampleRevision.create_package(
        package, revision=2, parent="sha256:" + "a" * 64, feedback=None,
    )
    right = SampleRevision.create_package(
        package, revision=2, parent="sha256:" + "b" * 64, feedback=None,
    )

    assert left.revision_hash != right.revision_hash
    assert left.package.package_hash == right.package.package_hash


def test_sample_revision_identity_includes_outline_page_mapping() -> None:
    base = {
        "title": "映射测试",
        "slide_count": 1,
        "files": [{"path": "index.html", "content": "<main>样品</main>"}],
    }
    left = HtmlPptPackage.model_validate({
        **base,
        "slides": [{"slide_id": "sample", "title": "样品", "source_slide_number": 1}],
    })
    right = HtmlPptPackage.model_validate({
        **base,
        "slides": [{"slide_id": "sample", "title": "样品", "source_slide_number": 2}],
    })

    left_revision = SampleRevision.create_package(left, revision=1, parent=None, feedback=None)
    right_revision = SampleRevision.create_package(right, revision=1, parent=None, feedback=None)

    assert left.package_hash == right.package_hash
    assert left_revision.revision_hash != right_revision.revision_hash
