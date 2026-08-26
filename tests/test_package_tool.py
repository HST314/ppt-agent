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


def test_draft_package_default_limits_fit_project_images() -> None:
    draft = DraftPackage(Path("skills"))

    assert draft.max_files == 128
    assert draft.max_file_bytes == 10_485_760
    assert draft.max_total_bytes == 83_886_080


def test_copy_project_image_copies_paired_image_under_img_prefix(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    images = tmp_path / "project" / "images"
    images.mkdir(parents=True)
    (images / "封面图.png").write_bytes(b"\x89PNG fake bytes")
    draft = DraftPackage(root, images_root=images)

    record = draft.copy_project_image("images/封面图.png", "img/封面图.png")

    assert record["path"] == "img/封面图.png"
    assert record["source_path"] == "images/封面图.png"
    payload = draft.payload()
    entry = next(item for item in payload if item["path"] == "img/封面图.png")
    assert entry["origin"] == "project_image:images/封面图.png"
    assert entry["encoding"] == "base64"


@pytest.mark.parametrize(
    "source_path",
    [
        "封面图.png",
        "../封面图.png",
        "images/../封面图.png",
        "images/封面图.md",
        "images/封面图.txt",
        "images/missing.png",
        "images/sub/封面图.png",
        "images/",
        "",
    ],
)
def test_copy_project_image_rejects_out_of_bounds_or_wrong_suffix_sources(
    tmp_path: Path, source_path: str
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    images = tmp_path / "project" / "images"
    images.mkdir(parents=True)
    (images / "封面图.png").write_bytes(b"\x89PNG fake bytes")
    (images / "封面图.md").write_text("描述", encoding="utf-8")
    (images / "sub").mkdir()
    (images / "sub" / "封面图.png").write_bytes(b"\x89PNG nested")
    draft = DraftPackage(root, images_root=images)

    with pytest.raises(PackageToolError):
        draft.copy_project_image(source_path, "img/封面图.png")


@pytest.mark.parametrize(
    "destination_path",
    [
        "assets/封面图.png",
        "封面图.png",
        "img/../封面图.png",
        "imgx/封面图.png",
        "img",
        "img/封面图.exe",
    ],
)
def test_copy_project_image_forces_img_destination_prefix(
    tmp_path: Path, destination_path: str
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    images = tmp_path / "project" / "images"
    images.mkdir(parents=True)
    (images / "封面图.png").write_bytes(b"\x89PNG fake bytes")
    draft = DraftPackage(root, images_root=images)

    with pytest.raises(PackageToolError):
        draft.copy_project_image("images/封面图.png", destination_path)


def test_copy_project_image_rejects_oversized_image_via_shared_limits(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    images = tmp_path / "project" / "images"
    images.mkdir(parents=True)
    (images / "big.png").write_bytes(b"x" * 16)
    draft = DraftPackage(root, images_root=images, max_file_bytes=8)

    with pytest.raises(PackageToolError, match="per-file"):
        draft.copy_project_image("images/big.png", "img/big.png")


def test_copy_project_image_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    images = tmp_path / "project" / "images"
    images.mkdir(parents=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG secret")
    (images / "linked.png").symlink_to(outside)
    draft = DraftPackage(root, images_root=images)

    with pytest.raises(PackageToolError):
        draft.copy_project_image("images/linked.png", "img/linked.png")


def test_copy_project_image_unavailable_without_images_root(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    draft = DraftPackage(root)

    with pytest.raises(PackageToolError, match="not available"):
        draft.copy_project_image("images/封面图.png", "img/封面图.png")


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


def test_package_file_content_hard_cap_covers_ten_mib_base64() -> None:
    from pydantic import ValidationError

    from agent_core.models import PackageFile

    # 12.5M characters sat above the old 12M ceiling and below the new one;
    # a 10MiB binary file base64-encodes to roughly 13.4M characters.
    oversized_but_valid = PackageFile.model_validate({
        "path": "img/big.jpg",
        "content": "x" * 12_500_000,
    })
    assert len(oversized_but_valid.content) == 12_500_000

    with pytest.raises(ValidationError):
        PackageFile.model_validate({
            "path": "img/big.jpg",
            "content": "x" * 16_000_001,
        })


def test_package_total_bytes_hard_cap_allows_full_deck_sized_packages() -> None:
    from agent_core.models import HtmlPptPackage

    # 75MB of decoded content: above the former 64MB ceiling, far below the
    # new 256MiB ceiling that composed full decks need.
    files = [{"path": "index.html", "content": "<main>deck</main>"}]
    files.extend(
        {"path": f"img/big-{index}.txt", "content": "x" * 15_000_000}
        for index in range(5)
    )
    package = HtmlPptPackage.model_validate({
        "title": "大包测试",
        "slide_count": 1,
        "slides": [{"slide_id": "cover", "title": "封面"}],
        "files": files,
    })

    assert len(package.files) == 6
    assert sum(len(item.content_bytes()) for item in package.files) > 64_000_000
