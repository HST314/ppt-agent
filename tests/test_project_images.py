from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main_front
from agent_core.jobs import JobRegistry
from configs.runtime import ManagedRuntime
from storage.project_images import (
    MAX_IMAGE_BYTES,
    project_image_manifest,
    read_image_descriptions,
    sync_project_images,
)
from tests.job_support import wait_for_terminal_job
from tests.test_full_deck_session import _ready_full_deck

ALL_IMAGE_SUFFIXES = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico"]


def _make_pair(source: Path, stem: str, suffix: str = ".png", text: str = "") -> None:
    (source / f"{stem}{suffix}").write_bytes(b"image-bytes")
    (source / f"{stem}.md").write_text(text or f"{stem} 的描述", encoding="utf-8")


def _target_files(project_root: Path) -> set[str]:
    images_dir = project_root / "images"
    if not images_dir.is_dir():
        return set()
    return {entry.name for entry in images_dir.iterdir()}


def test_sync_copies_pairs_across_supported_suffixes(tmp_path: Path) -> None:
    source = tmp_path / "images-source"
    source.mkdir()
    project = tmp_path / "project"

    for index, suffix in enumerate(ALL_IMAGE_SUFFIXES):
        _make_pair(source, f"asset-{index}", suffix)

    report = sync_project_images(source, project)

    assert report.image_count == len(ALL_IMAGE_SUFFIXES)
    assert report.file_count == len(ALL_IMAGE_SUFFIXES) * 2
    assert report.warnings == ()
    assert len(_target_files(project)) == len(ALL_IMAGE_SUFFIXES) * 2

    manifest = project_image_manifest(project)
    assert [entry["image_path"] for entry in manifest] == sorted(
        f"images/asset-{index}{suffix}"
        for index, suffix in enumerate(ALL_IMAGE_SUFFIXES)
    )
    assert manifest[0] == {
        "image_path": "images/asset-0.png",
        "description_path": "images/asset-0.md",
        "size_bytes": len(b"image-bytes"),
    }


def test_sync_skips_dirty_assets_and_logs_diagnostics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "images-source"
    source.mkdir()
    (source / "sub").mkdir()
    (source / "sub" / "nested.png").write_bytes(b"nested")
    (source / "orphan-image.png").write_bytes(b"no-md")
    (source / "orphan-description.md").write_text("no image", encoding="utf-8")
    (source / "stray.txt").write_text("unexpected", encoding="utf-8")
    (source / ".DS_Store").write_bytes(b"dotfile")
    (source / "oversize.png").write_bytes(b"x" * (MAX_IMAGE_BYTES + 1))
    (source / "oversize.md").write_text("too big", encoding="utf-8")
    (source / "broken.md").write_bytes(b"\xff\xfe not utf-8")
    (source / "broken.png").write_bytes(b"image")
    _make_pair(source, "clean")

    report = sync_project_images(source, tmp_path / "project")

    assert report.image_count == 1
    assert _target_files(tmp_path / "project") == {"clean.png", "clean.md"}

    console = capsys.readouterr().out
    for expected in (
        "sub/",
        "orphan-image.png",
        "orphan-description.md",
        "stray.txt",
        "oversize.png",
        "broken.md",
        "已同步 1 张图片素材",
    ):
        assert expected in console
    assert ".DS_Store" not in console


@pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="file permission denial is only observable on POSIX as non-root",
)
def test_sync_skips_unreadable_image(tmp_path: Path) -> None:
    source = tmp_path / "images-source"
    source.mkdir()
    _make_pair(source, "locked")
    (source / "locked.png").chmod(0o000)

    report = sync_project_images(source, tmp_path / "project")

    assert report.image_count == 0
    assert "locked.png" in report.warnings[0]
    assert _target_files(tmp_path / "project") == set()


def test_sync_rebuilds_snapshot_each_run(tmp_path: Path) -> None:
    source = tmp_path / "images-source"
    source.mkdir()
    project = tmp_path / "project"

    _make_pair(source, "first")
    first = sync_project_images(source, project)
    again = sync_project_images(source, project)
    assert (first.image_count, first.file_count) == (1, 2)
    assert (again.image_count, again.file_count) == (1, 2)
    assert _target_files(project) == {"first.png", "first.md"}

    (source / "first.png").unlink()
    (source / "first.md").unlink()
    _make_pair(source, "second")

    refreshed = sync_project_images(source, project)

    assert refreshed.image_count == 1
    assert _target_files(project) == {"second.png", "second.md"}


def test_sync_empty_or_missing_source_keeps_project_images_empty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "images-source"
    source.mkdir()
    project = tmp_path / "project"

    assert sync_project_images(source, project).image_count == 0
    assert not (project / "images").exists()
    assert capsys.readouterr().out == ""

    assert sync_project_images(tmp_path / "missing", project).image_count == 0
    assert not (project / "images").exists()
    assert capsys.readouterr().out == ""

    (project / "images").mkdir(parents=True)
    (project / "images" / "stale.png").write_bytes(b"stale")
    for empty_again in (source, tmp_path / "missing"):
        sync_project_images(empty_again, project)
    assert not (project / "images").exists()


def test_sync_ten_mib_boundary(tmp_path: Path) -> None:
    source = tmp_path / "images-source"
    source.mkdir()
    project = tmp_path / "project"

    (source / "exact.png").write_bytes(b"x" * MAX_IMAGE_BYTES)
    (source / "exact.md").write_text("exact", encoding="utf-8")
    (source / "over.png").write_bytes(b"x" * (MAX_IMAGE_BYTES + 1))
    (source / "over.md").write_text("over", encoding="utf-8")

    report = sync_project_images(source, project)

    assert report.image_count == 1
    assert _target_files(project) == {"exact.png", "exact.md"}
    assert any("over.png" in warning for warning in report.warnings)


def test_sync_multi_suffix_conflict_copies_all_with_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "images-source"
    source.mkdir()
    project = tmp_path / "project"

    _make_pair(source, "shared")
    (source / "shared.jpg").write_bytes(b"jpg-variant")

    report = sync_project_images(source, project)

    assert report.image_count == 2
    assert report.file_count == 3
    assert _target_files(project) == {"shared.png", "shared.jpg", "shared.md"}
    console = capsys.readouterr().out
    assert "shared.png" in console and "shared.jpg" in console

    manifest = project_image_manifest(project)
    assert [entry["image_path"] for entry in manifest] == [
        "images/shared.jpg",
        "images/shared.png",
    ]
    assert all(
        entry["description_path"] == "images/shared.md" for entry in manifest
    )


def test_read_image_descriptions_variants(tmp_path: Path) -> None:
    source = tmp_path / "images-source"
    source.mkdir()
    project = tmp_path / "project"
    _make_pair(source, "alpha", text="alpha 的完整解读")
    _make_pair(source, "beta", text="beta 的完整解读")
    sync_project_images(source, project)

    all_texts = read_image_descriptions(project)
    assert all_texts == {
        "images/alpha.md": "alpha 的完整解读",
        "images/beta.md": "beta 的完整解读",
    }

    subset = read_image_descriptions(project, ["images/beta.md"])
    assert subset == {"images/beta.md": "beta 的完整解读"}

    # Tampered descriptions: invalid UTF-8 must be skipped with a log line.
    (project / "images" / "beta.md").write_bytes(b"\xff\xfe")
    partial = read_image_descriptions(project)
    assert partial == {"images/alpha.md": "alpha 的完整解读"}

    # Image files and paths escaping the images directory are never read.
    rejected = read_image_descriptions(
        project,
        ["images/alpha.png", "images/../project.db", "images/missing.md"],
    )
    assert rejected == {}

    assert read_image_descriptions(tmp_path / "empty-project") == {}
    assert project_image_manifest(tmp_path / "empty-project") == []


def test_start_job_refreshes_project_image_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    source_root = tmp_path / "images-source"
    source_root.mkdir()
    _make_pair(source_root, "cover", text="封面图片的解读")
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "IMAGES_ROOT", source_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    client = TestClient(main_front.app)

    created = client.post("/api/projects", json={
        "project_id": "sync-demo",
        "task_card": {"title": "素材同步", "objective": "验证任务启动前刷新"},
    })
    assert created.status_code == 201

    started = client.post("/api/projects/sync-demo/jobs", json={
        "operation": "start_clarification",
        "checkpoint_id": created.json()["checkpoint_id"],
    })
    assert started.status_code == 202

    project_images = project_root / "sync-demo" / "images"
    assert {entry.name for entry in project_images.iterdir()} == {
        "cover.png",
        "cover.md",
    }
    assert read_image_descriptions(project_images.parent) == {
        "images/cover.md": "封面图片的解读"
    }

    job = wait_for_terminal_job(
        lambda job_id: client.get(f"/api/jobs/{job_id}").json(),
        started.json()["job_id"],
    )
    assert job["status"] == "succeeded", job


def test_full_deck_session_route_refreshes_project_image_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_runtime: ManagedRuntime,
) -> None:
    project_root = tmp_path / "projects"
    source_root = tmp_path / "images-source"
    source_root.mkdir()
    _make_pair(source_root, "chart", text="图表素材的解读")
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "IMAGES_ROOT", source_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", mock_runtime)

    def _stub_session_run(self, session_id, **kwargs):
        return self.store.full_deck_generation_session(session_id)

    monkeypatch.setattr(
        main_front.Workflow,
        "run_full_deck_generation_session",
        _stub_session_run,
    )
    _, entered = _ready_full_deck(
        tmp_path,
        mock_runtime,
        project_id="session-sync",
    )
    client = TestClient(main_front.app)

    response = client.post(
        "/api/projects/session-sync/full-deck/generation-sessions",
        json={
            "checkpoint_id": entered["checkpoint_id"],
            "revision_hash": entered["full_deck"]["current_revision_hash"],
        },
    )
    assert response.status_code == 202

    project_images = project_root / "session-sync" / "images"
    assert {entry.name for entry in project_images.iterdir()} == {
        "chart.png",
        "chart.md",
    }
