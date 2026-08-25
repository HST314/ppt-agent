from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main_front
from agent_core.jobs import JobRegistry
from agent_core.models import (
    DocumentRevision,
    FullDeckContentRef,
    FullDeckPackage,
    FullDeckPageSlot,
    FullDeckPlan,
    FullDeckRevision,
    HtmlPptPackage,
    PackageFile,
    PackageSlide,
    SampleRevision,
    TaskCard,
)
from agent_core.workflow import Workflow
from configs.runtime import ManagedRuntime
from storage.project_store import ProjectStore


OUTLINE = """# 逐页大纲

## 第 1 页｜开场
## 第 2 页｜关键判断
## 第 3 页｜数据证据
## 第 4 页｜行动计划
"""


def _sample_package() -> HtmlPptPackage:
    return HtmlPptPackage(
        title="已确认样品",
        slide_count=2,
        slides=[
            PackageSlide(slide_id="sample-2", title="关键判断", source_slide_number=2),
            PackageSlide(slide_id="sample-3", title="数据证据", source_slide_number=3),
        ],
        files=[
            PackageFile(
                path="index.html",
                content=(
                    '<!doctype html><html><body><section class="slide" '
                    'data-slide-id="sample-2"><h1>关键判断</h1></section>'
                    '<section class="slide" data-slide-id="sample-3">'
                    "<h1>数据证据</h1></section></body></html>"
                ),
            ),
        ],
    )


def _ready_full_deck(
    tmp_path: Path,
    runtime: ManagedRuntime,
) -> tuple[Workflow, dict, dict]:
    store = ProjectStore(tmp_path / "projects", "full-deck-history")
    manifest = store.create(
        TaskCard(title="全稿历史", objective="验证全稿工作区").model_dump(),
        runtime.snapshot(),
    )
    outline = DocumentRevision.create(
        "slide_outline",
        OUTLINE,
        revision=1,
        parent=None,
        created_by="agent",
        provenance={"output_hash": "sha256:" + "1" * 64},
    ).model_copy(update={"status": "approved"})
    sample = SampleRevision.create_package(
        _sample_package(),
        revision=1,
        parent=None,
        feedback=None,
        provenance={
            "model_config_hash": "sha256:" + "2" * 64,
            "runtime_config_hash": "sha256:" + "3" * 64,
            "skills_hash": "sha256:" + "4" * 64,
        },
    ).model_copy(update={"status": "pending_approval"})
    manifest = store.update(
        lambda value: value
        | {
            "documents": {
                "narrative_structure": [],
                "slide_outline": [outline.model_dump(mode="json")],
            },
            "samples": [sample.model_dump(mode="json")],
            "current_sample_revision_hash": sample.revision_hash,
            "state": "ppt_sample",
            "phase": "waiting_human_approval",
        },
        "sample_fixture_ready",
        expected_checkpoint_id=manifest["checkpoint_id"],
    )
    workflow = Workflow(store, runtime)
    entered = workflow.enter_full_deck(
        manifest["checkpoint_id"],
        sample.revision_hash,
    )
    r1 = FullDeckRevision.model_validate(entered["full_deck_revisions"][0])
    inherited_ref = FullDeckContentRef.model_validate(
        next(page.content_ref for page in r1.plan.pages if page.content_ref is not None)
    )
    pages = []
    for page in r1.plan.pages:
        if page.status == "ready":
            pages.append(page)
            continue
        pages.append(FullDeckPageSlot(
            slot_id=page.slot_id,
            position=page.position,
            outline_ref=page.outline_ref,
            title=page.title,
            status="ready",
            source_type="generated_segment",
            content_ref=inherited_ref,
        ))
    package = FullDeckPackage(
        title="完整全稿",
        slide_count=4,
        slides=[
            PackageSlide(
                slide_id=f"slide-{number}",
                title=page.title,
                source_slide_number=number,
            )
            for number, page in enumerate(pages, start=1)
        ],
        files=[PackageFile(
            path="index.html",
            content=(
                '<!doctype html><html><body><section class="slide" '
                'data-slide-id="slide-1"><h1>完整全稿</h1></section></body></html>'
            ),
        )],
        composition_manifest={"version": "full-deck-composer-v1"},
    )
    r2 = FullDeckRevision.create(
        full_deck_id=r1.full_deck_id,
        revision=2,
        parent=r1.revision_hash,
        feedback="补齐其余页面",
        plan=FullDeckPlan(pages=pages),
        package=package,
        status="pending_approval",
        provenance=r1.provenance | {"changed_slot_ids": [page.slot_id for page in pages]},
    )

    def add_r2(value: dict) -> dict:
        value["full_deck_revisions"].append(r2.model_dump(mode="json"))
        value["full_deck"]["revision_refs"].append({
            "revision_hash": r2.revision_hash,
            "status": r2.status,
        })
        value["full_deck"]["current_revision_hash"] = r2.revision_hash
        value.update(state="ppt_full", phase="waiting_human_approval")
        return value

    completed = store.update(
        add_r2,
        "full_deck_fixture_ready",
        expected_checkpoint_id=entered["checkpoint_id"],
    )
    return workflow, entered, completed


def test_full_deck_history_selection_moves_only_the_current_pointer(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
) -> None:
    workflow, entered, completed = _ready_full_deck(tmp_path, mock_runtime)
    r1_hash = entered["full_deck"]["current_revision_hash"]
    before_hashes = [
        item["revision_hash"] for item in completed["full_deck_revisions"]
    ]

    selected = workflow.restore_full_deck(completed["checkpoint_id"], r1_hash)

    assert selected["full_deck"]["current_revision_hash"] == r1_hash
    assert selected["state"] == "ppt_full"
    assert selected["phase"] == "ready_to_generate"
    assert [
        item["revision_hash"] for item in selected["full_deck_revisions"]
    ] == before_hashes
    assert workflow.store.events()[-1]["event"] == "full_deck_revision_selected"
    history = workflow.store.full_deck_history()
    assert [item["revision"] for item in history] == [2, 1]
    assert next(item for item in history if item["revision"] == 1)["current"] is True
    assert next(item for item in history if item["revision"] == 2)["package"]["file_count"] == 1


def test_full_deck_read_restore_branch_preview_and_export_apis(
    tmp_path: Path,
    mock_runtime: ManagedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", project_root)
    monkeypatch.setattr(main_front, "jobs", JobRegistry(project_root / ".jobs"))
    monkeypatch.setattr(main_front, "runtime", mock_runtime)
    _, entered, completed = _ready_full_deck(tmp_path, mock_runtime)
    r1_hash = entered["full_deck"]["current_revision_hash"]
    r2_hash = completed["full_deck"]["current_revision_hash"]
    client = TestClient(main_front.app)

    history = client.get("/api/projects/full-deck-history/full-deck/revisions")
    assert history.status_code == 200
    assert [item["revision"] for item in history.json()] == [2, 1]
    detail = client.get(
        f"/api/projects/full-deck-history/full-deck/revisions/{r2_hash}"
    )
    assert detail.status_code == 200
    assert detail.json()["preview_url"].endswith(f"/{r2_hash}/preview/index.html")
    assert detail.json()["export_url"].endswith(f"/{r2_hash}/export")

    preview = client.get(detail.json()["preview_url"])
    assert preview.status_code == 200
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert "sandbox allow-scripts" in preview.headers["content-security-policy"]
    exported = client.get(detail.json()["export_url"])
    assert exported.status_code == 200
    with zipfile.ZipFile(BytesIO(exported.content)) as archive:
        assert archive.namelist() == ["index.html"]

    restored = client.post(
        f"/api/projects/full-deck-history/full-deck/revisions/{r1_hash}/restore",
        json={"checkpoint_id": completed["checkpoint_id"]},
    )
    assert restored.status_code == 200
    assert restored.json()["full_deck"]["current_revision_hash"] == r1_hash
    assert len(restored.json()["full_deck_revisions"]) == 2

    branched = client.post(
        f"/api/projects/full-deck-history/full-deck/revisions/{r1_hash}/branches",
        json={
            "checkpoint_id": restored.json()["checkpoint_id"],
            "name": "deck-r1-branch",
        },
    )
    assert branched.status_code == 200
    assert branched.json()["branch"] == "deck-r1-branch"
    assert branched.json()["full_deck"]["current_revision_hash"] == r1_hash
    assert client.get(
        f"/api/projects/full-deck-history/full-deck/revisions/{r2_hash}"
    ).status_code == 404
    assert client.get(detail.json()["preview_url"]).status_code == 404
