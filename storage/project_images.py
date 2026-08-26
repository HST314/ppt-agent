"""Project-local image assets synced from the global images directory.

The global directory (default ``frontend/data/images``) holds flat
image/description pairs: ``foo.png`` plus ``foo.md``. Before every generation
job the current snapshot is copied overwrite-style into
``<project>/images/`` so one generation always sees one stable image set.
Dirty entries (unpaired, nested, oversized, unreadable) are skipped with
backend-console diagnostics only; nothing here reaches API responses.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico"}
)
DESCRIPTION_SUFFIX = ".md"
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MiB per image file
IMAGES_DIR_NAME = "images"

_LOG_PREFIX = "[project-images]"


@dataclass(frozen=True)
class SyncReport:
    """Result of one snapshot refresh; diagnostics are print-only."""

    image_count: int
    file_count: int
    warnings: tuple[str, ...]


def _remove_directory(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=False)


def _validate_image(path: Path, warnings: list[str]) -> bool:
    """Return True when the image is within limits and readable."""

    try:
        size = path.stat().st_size
    except OSError as exc:
        warnings.append(f"跳过不可读取的图片: {path.name}（{exc}）")
        return False
    if size > MAX_IMAGE_BYTES:
        warnings.append(
            f"跳过超过 10MiB 上限的图片: {path.name}（{size} 字节）"
        )
        return False
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError as exc:
        warnings.append(f"跳过不可读取的图片: {path.name}（{exc}）")
        return False
    return True


def sync_project_images(source_root: Path, project_root: Path) -> SyncReport:
    """Refresh ``<project>/images/`` to mirror the current source snapshot.

    The target directory is rebuilt from scratch first so stale files never
    survive. Only image + same-stem description pairs are copied; a dirty
    source (missing pairs, subdirectories, oversized or unreadable files)
    produces console diagnostics and is otherwise ignored.
    """

    source = Path(source_root)
    target = Path(project_root) / IMAGES_DIR_NAME
    warnings: list[str] = []

    if not source.is_dir():
        # Feature not configured: mirror the empty snapshot, stay silent.
        if target.exists():
            try:
                _remove_directory(target)
            except OSError as exc:
                print(f"{_LOG_PREFIX} 无法清空项目图片目录: {target}（{exc}）")
        return SyncReport(image_count=0, file_count=0, warnings=())

    images_by_stem: dict[str, list[Path]] = {}
    descriptions_by_stem: dict[str, Path] = {}
    for entry in sorted(source.iterdir()):
        if entry.is_dir():
            warnings.append(f"跳过子目录: {entry.name}/（素材目录仅支持扁平结构）")
            continue
        if not entry.is_file() or entry.name.startswith("."):
            continue
        suffix = entry.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            images_by_stem.setdefault(entry.stem, []).append(entry)
        elif suffix == DESCRIPTION_SUFFIX:
            existing = descriptions_by_stem.get(entry.stem)
            if existing is None:
                descriptions_by_stem[entry.stem] = entry
            else:
                warnings.append(
                    f"同名解读文件冲突，仅保留 {existing.name}，跳过 {entry.name}"
                )
        else:
            warnings.append(
                f"跳过未识别的文件: {entry.name}（仅支持图片与同名 .md 解读文件）"
            )

    surviving_images: dict[str, list[Path]] = {}
    for stem, candidates in images_by_stem.items():
        kept = [path for path in candidates if _validate_image(path, warnings)]
        if kept:
            surviving_images[stem] = kept

    pairs: list[tuple[Path, Path]] = []
    for stem, image_paths in surviving_images.items():
        description = descriptions_by_stem.get(stem)
        if description is None:
            for path in image_paths:
                warnings.append(f"跳过缺少解读文件的图片: {path.name}")
            continue
        try:
            description.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            warnings.append(f"跳过非 UTF-8 编码的解读文件: {description.name}")
            continue
        except OSError as exc:
            warnings.append(f"跳过不可读取的解读文件: {description.name}（{exc}）")
            continue
        if len(image_paths) > 1:
            names = "、".join(sorted(path.name for path in image_paths))
            warnings.append(
                f"同名图片存在多种图像后缀，全部拷贝: {names}"
                f"（解读文件: {description.name}）"
            )
        pairs.extend((path, description) for path in image_paths)

    paired_stems = {image.stem for image, _ in pairs}
    for stem in sorted(descriptions_by_stem):
        if stem not in paired_stems:
            warnings.append(
                f"跳过缺少对应图片的解读文件: {descriptions_by_stem[stem].name}"
            )

    if target.exists():
        try:
            _remove_directory(target)
        except OSError as exc:
            print(f"{_LOG_PREFIX} 无法清空项目图片目录: {target}（{exc}）")
            for warning in warnings:
                print(f"{_LOG_PREFIX} {warning}")
            return SyncReport(image_count=0, file_count=0, warnings=tuple(warnings))

    files_to_copy: dict[str, Path] = {}
    for image, description in pairs:
        files_to_copy[image.name] = image
        files_to_copy[description.name] = description

    copied = 0
    if files_to_copy:
        target.mkdir(parents=True, exist_ok=True)
        for name, source_file in sorted(files_to_copy.items()):
            try:
                shutil.copy2(source_file, target / name)
                copied += 1
            except OSError as exc:
                partial = target / name
                if partial.exists():
                    try:
                        partial.unlink()
                    except OSError:
                        pass
                warnings.append(f"拷贝失败，已跳过: {name}（{exc}）")

    for warning in warnings:
        print(f"{_LOG_PREFIX} {warning}")
    if pairs:
        print(f"{_LOG_PREFIX} 已同步 {len(pairs)} 张图片素材到 {target}")

    return SyncReport(
        image_count=len(pairs),
        file_count=copied,
        warnings=tuple(warnings),
    )


def project_image_manifest(project_root: Path) -> list[dict[str, Any]]:
    """List usable image/description pairs inside ``<project>/images/``.

    Entries are ``{image_path, description_path, size_bytes}`` with paths
    relative to the project root; ordering is deterministic by image path.
    """

    images_dir = Path(project_root) / IMAGES_DIR_NAME
    if not images_dir.is_dir():
        return []
    images_by_stem: dict[str, list[Path]] = {}
    descriptions_by_stem: dict[str, Path] = {}
    for entry in sorted(images_dir.iterdir()):
        if not entry.is_file():
            continue
        suffix = entry.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            images_by_stem.setdefault(entry.stem, []).append(entry)
        elif suffix == DESCRIPTION_SUFFIX:
            descriptions_by_stem.setdefault(entry.stem, entry)
    entries: list[dict[str, Any]] = []
    for stem, image_paths in images_by_stem.items():
        description = descriptions_by_stem.get(stem)
        if description is None:
            continue
        for image in image_paths:
            try:
                size_bytes = image.stat().st_size
            except OSError:
                continue
            entries.append(
                {
                    "image_path": f"{IMAGES_DIR_NAME}/{image.name}",
                    "description_path": f"{IMAGES_DIR_NAME}/{description.name}",
                    "size_bytes": size_bytes,
                }
            )
    entries.sort(key=lambda entry: entry["image_path"])
    return entries


def read_image_descriptions(
    project_root: Path,
    paths: Iterable[str] | None = None,
) -> dict[str, str]:
    """Read description texts by project-relative path (e.g. ``images/a.md``).

    ``paths=None`` reads every description in the manifest. Unreadable,
    non-UTF-8, missing, or out-of-root paths are skipped with console
    diagnostics; image files are never read as text.
    """

    root = Path(project_root)
    if paths is None:
        paths = [
            entry["description_path"]
            for entry in project_image_manifest(root)
        ]
    images_root = (root / IMAGES_DIR_NAME).resolve()
    contents: dict[str, str] = {}
    for path in paths:
        target = (root / path).resolve()
        if (
            not target.is_relative_to(images_root)
            or target.suffix.lower() != DESCRIPTION_SUFFIX
        ):
            print(f"{_LOG_PREFIX} 拒绝读取解读文件: {path}")
            continue
        try:
            contents[path] = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"{_LOG_PREFIX} 解读文件不存在，跳过: {path}")
        except UnicodeDecodeError:
            print(f"{_LOG_PREFIX} 解读文件不是 UTF-8 文本，跳过: {path}")
        except OSError as exc:
            print(f"{_LOG_PREFIX} 解读文件不可读，跳过: {path}（{exc}）")
    return contents
