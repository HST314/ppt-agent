import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from agent_core.models import SampleOutput, SamplePage, TaskCard, digest
from agent_core.jobs import public_job_error
from agent_core.workflow import SampleGenerationError, Workflow, capabilities, stable_hash
from configs.runtime import ManagedRuntime
from storage.project_store import ConflictError, ProjectStore


@pytest.fixture
def workflow(tmp_path: Path, mock_runtime: ManagedRuntime) -> Workflow:
    runtime = mock_runtime
    store = ProjectStore(tmp_path / "projects", "demo")
    task = TaskCard(title="季度复盘", objective="形成下一季度投入共识")
    store.create(task.model_dump(), runtime.snapshot())
    return Workflow(store, runtime)


def finish_automatic_clarification(workflow: Workflow, manifest: dict) -> dict:
    while (
        manifest["state"] == "intake_clarify"
        and manifest["phase"] == "ready_for_clarification"
    ):
        manifest = workflow.start_clarification(manifest["checkpoint_id"])
    return manifest


def ready_for_sample(workflow: Workflow) -> dict:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = finish_automatic_clarification(
        workflow,
        workflow.answer_clarification(
            manifest["checkpoint_id"],
            card["question_card_id"],
            {question["question_id"]: "management" for question in card["questions"]},
        ),
    )
    for document_type in ("narrative_structure", "slide_outline"):
        manifest = workflow.generate_document(document_type, manifest["checkpoint_id"])
        document = manifest["documents"][document_type][-1]
        manifest = workflow.approve_document(
            document_type, manifest["checkpoint_id"], document["revision_hash"]
        )
    return manifest


def realistic_package_output(
    *,
    invalid_path: bool = False,
    source_slide_numbers: tuple[int, ...] = (1, 2),
    html_slide_ids: tuple[str, ...] | None = None,
) -> str:
    slides = []
    sections = []
    for index, source_slide_number in enumerate(source_slide_numbers, start=1):
        slides.append({
            "slide_id": f"sample_{index}",
            "title": "结论先行" if index == 1 else "行动路径",
            "source_slide_number": source_slide_number,
        })
    for index, slide_id in enumerate(
        html_slide_ids or tuple(item["slide_id"] for item in slides),
        start=1,
    ):
        sections.append(
            f'<section class="slide" data-slide-id="{slide_id}">'
            f'<h1>样品页 {index}</h1><p>结论、证据与行动建议。</p></section>'
        )
    html = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="stylesheet" href="assets/deck.css"></head><body>'
        + "".join(sections)
        + '<script src="assets/deck.js"></script></body></html>'
    )
    return json.dumps({
        "entrypoint": "index.html",
        "title": "真实形态 HTML-PPT",
        "slide_count": len(slides),
        "slides": slides,
        "files": [
            {"path": "index.html", "content": html, "encoding": "utf-8"},
            {
                "path": "../escape.css" if invalid_path else "assets/deck.css",
                "content": ".slide{width:100vw;height:100vh;padding:64px}",
                "encoding": "utf-8",
            },
            {
                "path": "assets/deck.js",
                "content": "addEventListener('keydown',()=>{});",
                "encoding": "utf-8",
            },
        ],
    }, ensure_ascii=False)


def test_sample_stage_happy_path_and_feedback_revision(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    assert capabilities(manifest) == ["inspect", "branch", "start_clarification"]

    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    assert card["checkpoint_id"] == manifest["checkpoint_id"]
    assert card["provenance"]["skills_hash"] == stable_hash(card["provenance"]["skill_index"])

    manifest = finish_automatic_clarification(
        workflow,
        workflow.answer_clarification(
            manifest["checkpoint_id"],
            card["question_card_id"],
            {question["question_id"]: "management" for question in card["questions"]},
        ),
    )
    assert manifest["phase"] == "ready_to_generate"
    manifest = workflow.generate_document("narrative_structure", manifest["checkpoint_id"])
    narrative = manifest["documents"]["narrative_structure"][-1]
    assert narrative["provenance"]["template_id"] == "narrative_structure"
    assert narrative["provenance"]["traces"][0]["type"] == "model_call"
    assert narrative["provenance"]["output_hash"] == digest(narrative["markdown_body"])
    manifest = workflow.approve_document("narrative_structure", manifest["checkpoint_id"], narrative["revision_hash"])
    assert manifest["state"] == "slide_outline"

    manifest = workflow.generate_document("slide_outline", manifest["checkpoint_id"])
    outline = manifest["documents"]["slide_outline"][-1]
    manifest = workflow.approve_document("slide_outline", manifest["checkpoint_id"], outline["revision_hash"])
    assert manifest["state"] == "ppt_sample"
    assert manifest["phase"] == "ready_to_generate"

    manifest = workflow.generate_sample(manifest["checkpoint_id"])
    sample = manifest["samples"][-1]
    assert sample["package"]["entrypoint"] == "index.html"
    assert sample["package"]["slide_count"] == 2
    assert [item["source_slide_number"] for item in sample["package"]["slides"]] == [1, 2]
    assert [item["path"] for item in sample["package"]["files"]] == ["index.html"]
    assert sample["provenance"]["sample_page_count"] == 2
    assert {"revise_sample", "approve_sample", "regenerate_sample"} <= set(capabilities(manifest))

    manifest = workflow.generate_sample(manifest["checkpoint_id"], feedback="放大主标题并减少文字")
    revised = manifest["samples"][-1]
    assert revised["revision"] == 2
    assert revised["parent_revision_hash"] == sample["revision_hash"]
    assert revised["feedback"] == "放大主标题并减少文字"

    manifest = workflow.restore_sample(manifest["checkpoint_id"], sample["revision_hash"])
    assert len(manifest["samples"]) == 2
    assert manifest["current_sample_revision_hash"] == sample["revision_hash"]

    manifest = workflow.generate_sample(manifest["checkpoint_id"], feedback="从首版继续修改")
    selected_child = manifest["samples"][-1]
    assert selected_child["revision"] == 3
    assert selected_child["parent_revision_hash"] == sample["revision_hash"]

    workflow.runtime.policy = workflow.runtime.policy.model_copy(update={"sample_page_count": 3})
    manifest = workflow.generate_sample(manifest["checkpoint_id"], regenerate=True)
    regenerated = manifest["samples"][-1]
    assert regenerated["revision"] == 4
    assert regenerated["package"]["slide_count"] == 3

    manifest = workflow.approve_sample(manifest["checkpoint_id"], regenerated["revision_hash"])
    assert manifest["phase"] == "completed"
    assert manifest["samples"][-1]["status"] == "approved"
    assert {"revise_sample", "regenerate_sample"} <= set(capabilities(manifest))
    sample_snapshot = next(
        item for item in workflow.store.progress_snapshots() if item["stage"] == "ppt_sample"
    )
    branched = workflow.store.fork(
        sample_snapshot["checkpoint_id"],
        "sample-rerun",
        mode="rerun_stage",
        stage="ppt_sample",
    )
    assert branched["state"] == "ppt_sample"
    assert branched["phase"] == "ready_to_generate"
    assert branched["samples"] == []


def test_clarification_automatically_runs_multiple_rounds_with_prior_answers(
    workflow: Workflow,
) -> None:
    outputs = iter([
        {
            "needs_clarification": True,
            "questions": [{
                "question_id": "q_audience",
                "field": "audience",
                "prompt": "主要听众是谁？",
                "impact": "决定论证深度",
                "options": [{
                    "value": "management",
                    "label": "管理层",
                    "recommended": True,
                }],
                "allow_free_text": True,
            }],
        },
        {
            "needs_clarification": True,
            "questions": [{
                "question_id": "q_occasion",
                "field": "occasion",
                "prompt": "演示发生在什么场合？",
                "impact": "决定开场方式",
                "options": [{
                    "value": "review",
                    "label": "评审会",
                    "recommended": True,
                }],
                "allow_free_text": True,
            }],
        },
        {"needs_clarification": False, "questions": []},
    ])
    prompts: list[str] = []

    def generate(state: str, prompt: str, **_kwargs):
        assert state == "intake_clarify"
        prompts.append(prompt)
        return json.dumps(next(outputs), ensure_ascii=False), [{
            "type": "model_call",
            "provider": "test",
            "model": "multi-round",
            "usage": {},
        }]

    workflow.gateway.generate = generate
    manifest = workflow.store.read()
    first = workflow.start_clarification(manifest["checkpoint_id"])
    assert first["question_card"]["round"] == 1

    answered_first = workflow.answer_clarification(
        first["checkpoint_id"],
        first["question_card"]["question_card_id"],
        {"q_audience": "管理层"},
    )
    assert answered_first["phase"] == "ready_for_clarification"
    assert answered_first["clarification_answers"]["audience"]["answer"] == "管理层"
    second = workflow.start_clarification(answered_first["checkpoint_id"])
    assert second["state"] == "intake_clarify"
    assert second["phase"] == "waiting_clarification"
    assert second["question_card"]["round"] == 2
    assert "主要听众是谁？" in prompts[1]
    assert "管理层" in prompts[1]

    answered_second = workflow.answer_clarification(
        second["checkpoint_id"],
        second["question_card"]["question_card_id"],
        {"q_occasion": "评审会"},
    )
    assert answered_second["phase"] == "ready_for_clarification"
    completed = workflow.start_clarification(answered_second["checkpoint_id"])
    assert completed["state"] == "narrative_structure"
    assert completed["phase"] == "ready_to_generate"
    assert completed["clarification_completed"] is True
    assert completed["question_card"] is None
    assert [entry["round"] for entry in completed["clarification_history"]] == [1, 2]
    assert completed["clarification_answers"]["audience"]["answer"] == "管理层"
    assert completed["clarification_answers"]["occasion"]["answer"] == "评审会"
    assert all(value in prompts[2] for value in (
        "主要听众是谁？", "管理层", "演示发生在什么场合？", "评审会",
    ))
    calls = [
        item for item in workflow.store.prompt_calls()
        if item["state"] == "intake_clarify"
    ]
    assert len(calls) == 3
    assert calls[1]["parent_prompt_call_id"] == calls[0]["prompt_call_id"]
    assert calls[2]["parent_prompt_call_id"] == calls[1]["prompt_call_id"]


def test_clarification_keeps_prior_answers_when_the_next_round_fails(
    workflow: Workflow,
) -> None:
    first_output = {
        "needs_clarification": True,
        "questions": [{
            "question_id": "q_audience",
            "field": "audience",
            "prompt": "主要听众是谁？",
            "impact": "决定论证深度",
            "options": [{
                "value": "management",
                "label": "管理层",
                "recommended": True,
            }],
            "allow_free_text": True,
        }],
    }
    workflow.gateway.generate = lambda *_args, **_kwargs: (
        json.dumps(first_output, ensure_ascii=False),
        [{"type": "model_call", "provider": "test", "model": "first", "usage": {}}],
    )
    first = workflow.start_clarification(workflow.store.read()["checkpoint_id"])
    answered = workflow.answer_clarification(
        first["checkpoint_id"],
        first["question_card"]["question_card_id"],
        {"q_audience": "管理层"},
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("temporary provider failure")

    workflow.gateway.generate = fail
    with pytest.raises(RuntimeError, match="temporary provider failure"):
        workflow.start_clarification(answered["checkpoint_id"])

    retained = workflow.store.read()
    assert retained["phase"] == "ready_for_clarification"
    assert retained["clarification_history"][0]["answers"] == {
        "q_audience": "管理层",
    }
    assert retained["clarification_answers"]["audience"]["answer"] == "管理层"


def test_clarification_stops_at_the_configured_round_limit(
    workflow: Workflow,
) -> None:
    workflow.runtime.policy = workflow.runtime.policy.model_copy(
        update={"max_clarification_rounds": 2}
    )
    call_count = 0

    def generate(_state: str, _prompt: str, **_kwargs):
        nonlocal call_count
        call_count += 1
        return json.dumps({
            "needs_clarification": True,
            "questions": [{
                "question_id": f"q_{call_count}",
                "field": f"field_{call_count}",
                "prompt": f"第 {call_count} 轮问题？",
                "impact": "补足输入",
                "options": [{
                    "value": "answer",
                    "label": "回答",
                    "recommended": True,
                }],
                "allow_free_text": True,
            }],
        }, ensure_ascii=False), [{
            "type": "model_call",
            "provider": "test",
            "model": "round-limit",
            "usage": {},
        }]

    workflow.gateway.generate = generate
    manifest = workflow.start_clarification(workflow.store.read()["checkpoint_id"])
    for round_number in (1, 2):
        card = manifest["question_card"]
        manifest = workflow.answer_clarification(
            manifest["checkpoint_id"],
            card["question_card_id"],
            {card["questions"][0]["question_id"]: "回答"},
        )
        if round_number == 1:
            manifest = workflow.start_clarification(manifest["checkpoint_id"])

    assert call_count == 2
    assert manifest["state"] == "narrative_structure"
    assert manifest["phase"] == "ready_to_generate"
    assert len(manifest["clarification_history"]) == 2


def test_selecting_approved_sample_preserves_completed_phase(workflow: Workflow) -> None:
    manifest = ready_for_sample(workflow)
    manifest = workflow.generate_sample(manifest["checkpoint_id"])
    first = manifest["samples"][-1]
    manifest = workflow.approve_sample(
        manifest["checkpoint_id"], first["revision_hash"]
    )
    manifest = workflow.generate_sample(
        manifest["checkpoint_id"], feedback="创建第二版"
    )

    selected = workflow.restore_sample(
        manifest["checkpoint_id"], first["revision_hash"]
    )

    assert selected["current_sample_revision_hash"] == first["revision_hash"]
    assert selected["phase"] == "completed"


def test_realistic_package_output_repairs_truncated_json(workflow: Workflow, monkeypatch) -> None:
    manifest = ready_for_sample(workflow)
    truncated = realistic_package_output()[:-80]
    responses = iter([
        truncated,
        realistic_package_output(),
    ])
    prompts: list[str] = []

    def generate(state: str, prompt: str, *, json_mode: bool = False):
        prompts.append(prompt)
        return next(responses), [{"type": "model_call", "provider": "test", "model": "real-shape", "usage": {}}]

    monkeypatch.setattr(workflow.gateway, "generate", generate)

    generated = workflow.generate_sample(manifest["checkpoint_id"])

    sample = generated["samples"][-1]
    assert sample["package"]["slide_count"] == 2
    assert len(sample["package"]["files"]) == 3
    assert sample["provenance"]["sample_repair_attempts"] == 1
    assert sample["provenance"]["sample_html_char_budget_per_page"] == 7_000
    assert len(sample["provenance"]["traces"]) == 2
    assert "SAMPLE_HTML_CHAR_BUDGET_PER_PAGE: 7000" in prompts[0]
    assert "SAMPLE_STAGE_ONLY: true" in prompts[0]
    assert "OUTLINE_SLIDES_JSON:" in prompts[0]
    assert "AUTOMATED_REPAIR_ATTEMPT: 1/2" in prompts[1]
    assert "JSON 未完整闭合" in prompts[1]
    assert truncated not in prompts[1]
    sample_calls = [
        item for item in workflow.store.prompt_calls() if item["state"] == "ppt_sample"
    ]
    assert [item["status"] for item in sample_calls] == ["failed", "completed"]
    assert sample_calls[1]["parent_prompt_call_id"] == sample_calls[0]["prompt_call_id"]
    assert sample_calls[1]["output_ref"] == sample["revision_hash"]


def test_realistic_package_output_repairs_with_exact_path_reason(
    workflow: Workflow, monkeypatch
) -> None:
    manifest = ready_for_sample(workflow)
    responses = iter([realistic_package_output(invalid_path=True), realistic_package_output()])
    prompts: list[str] = []

    def generate(state: str, prompt: str, *, json_mode: bool = False):
        prompts.append(prompt)
        return next(responses), [{"type": "model_call", "provider": "test", "model": "real-shape", "usage": {}}]

    monkeypatch.setattr(workflow.gateway, "generate", generate)

    generated = workflow.generate_sample(manifest["checkpoint_id"])

    sample = generated["samples"][-1]
    assert sample["provenance"]["sample_repair_attempts"] == 1
    assert sample["package"]["entrypoint"] == "index.html"
    assert "包文件校验失败" in prompts[1]
    assert "stay inside the draft" in prompts[1]


def test_sample_can_select_later_contiguous_outline_pages_and_preserves_them_on_revision(
    workflow: Workflow, monkeypatch
) -> None:
    manifest = ready_for_sample(workflow)
    responses = iter([
        realistic_package_output(source_slide_numbers=(2, 3)),
        realistic_package_output(source_slide_numbers=(1, 2)),
        realistic_package_output(source_slide_numbers=(2, 3)),
    ])
    prompts: list[str] = []

    def generate(state: str, prompt: str, *, json_mode: bool = False):
        prompts.append(prompt)
        return next(responses), [
            {"type": "model_call", "provider": "test", "model": "range", "usage": {}}
        ]

    monkeypatch.setattr(workflow.gateway, "generate", generate)

    manifest = workflow.generate_sample(manifest["checkpoint_id"])
    first = manifest["samples"][-1]
    assert [item["source_slide_number"] for item in first["package"]["slides"]] == [2, 3]

    workflow.runtime.policy = workflow.runtime.policy.model_copy(update={"sample_page_count": 3})
    manifest = workflow.generate_sample(manifest["checkpoint_id"], feedback="放大数据")
    revised = manifest["samples"][-1]

    assert revised["package"]["slide_count"] == 2
    assert [item["source_slide_number"] for item in revised["package"]["slides"]] == [2, 3]
    assert "PRESERVE_SOURCE_SLIDE_NUMBERS: [2, 3]" in prompts[1]
    assert "修改样品必须保持原大纲页 [2, 3]" in prompts[2]


def test_sample_rejects_non_contiguous_range_and_html_manifest_mismatch(
    workflow: Workflow, monkeypatch
) -> None:
    manifest = ready_for_sample(workflow)
    responses = iter([
        realistic_package_output(source_slide_numbers=(1, 3)),
        realistic_package_output(html_slide_ids=("sample_1", "sample_2", "extra")),
        realistic_package_output(source_slide_numbers=(2, 3)),
    ])
    prompts: list[str] = []

    def generate(state: str, prompt: str, *, json_mode: bool = False):
        prompts.append(prompt)
        return next(responses), [
            {"type": "model_call", "provider": "test", "model": "range", "usage": {}}
        ]

    monkeypatch.setattr(workflow.gateway, "generate", generate)

    generated = workflow.generate_sample(manifest["checkpoint_id"])

    assert [
        item["source_slide_number"] for item in generated["samples"][-1]["package"]["slides"]
    ] == [2, 3]
    assert "必须按大纲顺序连续" in prompts[1]
    assert "data-slide-id 必须与清单 slides 一一对应" in prompts[2]


def test_new_generation_repairs_legacy_pages_contract(
    workflow: Workflow, monkeypatch
) -> None:
    manifest = ready_for_sample(workflow)
    responses = iter([
        json.dumps({
            "pages": [{"page_id": "sample_1", "title": "旧格式", "html": "<main>旧格式</main>"}],
        }, ensure_ascii=False),
        realistic_package_output(),
    ])
    prompts: list[str] = []

    def generate(state: str, prompt: str, *, json_mode: bool = False):
        prompts.append(prompt)
        return next(responses), [
            {"type": "model_call", "provider": "test", "model": "real-shape", "usage": {}}
        ]

    monkeypatch.setattr(workflow.gateway, "generate", generate)

    generated = workflow.generate_sample(manifest["checkpoint_id"])

    assert generated["samples"][-1]["package"]["entrypoint"] == "index.html"
    assert "不能返回旧版 pages 数组" in prompts[1]


def test_package_output_repair_stops_after_bounded_attempts(workflow: Workflow, monkeypatch) -> None:
    manifest = ready_for_sample(workflow)
    prompts: list[str] = []

    def generate(state: str, prompt: str, *, json_mode: bool = False):
        prompts.append(prompt)
        return realistic_package_output(invalid_path=True), [
            {"type": "model_call", "provider": "test", "model": "real-shape", "usage": {}}
        ]

    monkeypatch.setattr(workflow.gateway, "generate", generate)

    with pytest.raises(SampleGenerationError) as failure:
        workflow.generate_sample(manifest["checkpoint_id"])

    assert failure.value.public_code == "sample_package_invalid"
    assert len(prompts) == 3
    assert "AUTOMATED_REPAIR_ATTEMPT: 2/2" in prompts[-1]
    persisted = workflow.store.read()
    assert persisted["phase"] == "ready_to_generate"
    assert persisted["samples"] == []
    generated_files = sorted(workflow.store.generated_html_root.glob("prompt_*/package/index.html"))
    assert len(generated_files) == 3
    assert all("data-slide-id" in path.read_text(encoding="utf-8") for path in generated_files)
    assert not list(workflow.store.artifacts_root.glob("*.html"))


def test_generated_package_persistence_failure_closes_prompt_audit(
    workflow: Workflow, monkeypatch
) -> None:
    manifest = ready_for_sample(workflow)

    def fail_persistence(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(workflow.store, "save_generated_package_attempt", fail_persistence)

    with pytest.raises(OSError, match="disk unavailable"):
        workflow.generate_sample(manifest["checkpoint_id"])

    sample_calls = [
        item for item in workflow.store.prompt_calls() if item["state"] == "ppt_sample"
    ]
    assert [item["status"] for item in sample_calls] == ["failed"]


def test_job_error_contract_hides_internal_sample_validation_details() -> None:
    failure = SampleGenerationError(
        "sample_html_rejected",
        "样品含有不支持的内容，自动修复后仍未通过，请重试。",
        "pages.0.html: unsupported attribute: internal-validation-detail",
    )

    error = public_job_error(failure)

    assert error == {
        "code": "sample_html_rejected",
        "message": "样品含有不支持的内容，自动修复后仍未通过，请重试。",
    }
    assert "internal-validation-detail" not in str(error)


@pytest.mark.parametrize(
    "html",
    [
        "<script>alert(1)</script>",
        '<iframe srcdoc="<p>unsafe</p>"></iframe>',
        '<object data="data:text/html;base64,AA=="></object>',
        '<embed src="data:text/html;base64,AA==">',
        '<link rel="stylesheet" href="data:text/css;base64,AA==">',
        '<meta http-equiv="refresh" content="0;url=https://example.com">',
        '<img src="https://example.com/tracker.png">',
        '<img src="&#x68;ttps://example.com/tracker.png">',
        '<img src="/relative-tracker.png">',
        '<div onclick="alert(1)">unsafe</div>',
        '<style>@import "https://example.com/theme.css";</style>',
        '<style>@\\69mport "https://example.com/theme.css";</style>',
        '<style>body{background-image:image-set("https://example.com/tracker.png")}</style>',
        '<style>body{background-image:\\69mage-set("https://example.com/tracker.png")}</style>',
        '<style>body{background-image:u\\72l("https://example.com/tracker.png")}</style>',
        '<style>body{background-image:url("\\68 ttps://example.com/tracker.png")}</style>',
        '<style>body{b\\61ckground-image:url("https://example.com/tracker.png")}</style>',
        '<style>body{behavior:url("#legacy")}</style>',
        '<style>body{width:expression(alert(1))}</style>',
        '<style>@media screen{@import "https://example.com/theme.css";}</style>',
        '<style>@font-face{font-family:x;src:url("https://example.com/font.woff2")}</style>',
        '<svg><rect fill="u\\72l(https://example.com/paint.svg#gradient)"></rect></svg>',
        '<svg><feImage href="https://example.com/image.png"></feImage></svg>',
        '<svg xmlns="https://example.com/evil"><rect></rect></svg>',
        '<svg xmlns=" http://www.w3.org/2000/svg"><rect></rect></svg>',
        '<svg xmlns:xlink="http://www.w3.org/1999/xlink"><use xlink:href="https://example.com/icon.svg#dot"></use></svg>',
        '<img srcset="data:image/png;base64,AA== 1x, https://example.com/tracker.png 2x">',
    ],
)
def test_sample_page_rejects_active_or_external_html(html: str) -> None:
    with pytest.raises(ValueError, match="active or external"):
        SamplePage(page_id="sample_1", title="Unsafe", html=html)


def test_sample_page_allows_passive_inline_content() -> None:
    page = SamplePage(
        page_id="sample_1",
        title="Inline",
        html=(
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<style>.mark{background-image:linear-gradient(135deg,#fff,#ddd)}'
            '.dot{background-image:url("data:image/png;base64,AA==")}</style></head>'
            '<body><!-- harmless --><a href="#detail">内容</a>'
            '<svg viewBox="0 0 10 10"><defs><linearGradient id="paint">'
            '<stop offset="0" stop-color="#fff"></stop></linearGradient></defs>'
            '<rect width="10" height="10" fill="url(#paint)"></rect></svg></body></html>'
        ),
    )

    assert page.page_id == "sample_1"
    assert page.html.startswith('<!doctype html><html lang="zh-CN"><head>')
    assert page.html.endswith("</body></html>")
    assert "<!--" not in page.html
    assert "linear-gradient" in page.html
    assert 'fill="url(#paint)"' in page.html


def test_sample_page_supports_common_html_css_and_svg_display_features() -> None:
    page = SamplePage(
        page_id="sample_1",
        title="Compatible",
        html=(
            '<!doctype html><html lang="zh-CN" data-theme="dark"><head><title>样品</title>'
            '<meta name="theme-color" content="#101828"><style>'
            ':root{--columns:3}.slide::before{content:"";pointer-events:none;text-indent:1em;'
            '-webkit-background-clip:text;background-clip:text;backdrop-filter:blur(8px);'
            'grid-template-columns:repeat(var(--columns),minmax(0,1fr));'
            '& .label{text-wrap:balance}}'
            '@media (max-width:800px){.slide{container-type:inline-size}}'
            '@supports (display:grid){.slide{display:grid}}'
            '@keyframes enter{from{opacity:0}to{opacity:1}}'
            '@font-face{font-family:"Deck";src:local("Arial"),'
            'url("data:font/woff2;base64,AA==") format("woff2")}'
            '</style></head><body class="deck" style="margin:0" data-revision="2">'
            '<main class="slide"><dialog open><progress value="60" max="100"></progress></dialog>'
            '<svg viewBox="0 0 20 20" xmlns:xlink="http://www.w3.org/1999/xlink"><defs>'
            '<pattern id="grid" width="4" height="4" '
            'patternUnits="userSpaceOnUse"><rect width="1" height="1"></rect></pattern>'
            '<filter id="soft"><feGaussianBlur stdDeviation="1"></feGaussianBlur></filter>'
            '<symbol id="dot"><circle cx="2" cy="2" r="2"></circle></symbol></defs>'
            '<rect width="20" height="20" fill="url(#grid)" filter="url(#soft)"></rect>'
            '<use xlink:href="#dot"></use></svg></main></body></html>'
        ),
    )

    assert 'data-theme="dark"' in page.html
    assert '<body class="deck" style="margin:0" data-revision="2">' in page.html
    assert "grid-template-columns:repeat(var(--columns),minmax(0,1fr))" in page.html
    assert "& .label{text-wrap:balance" in page.html
    assert "@media (max-width:800px)" in page.html
    assert "@keyframes enter" in page.html
    assert 'patternUnits="userSpaceOnUse"' in page.html
    assert '<feGaussianBlur stdDeviation="1"></feGaussianBlur>' in page.html
    assert '<use href="#dot"></use>' in page.html


def test_sample_page_allows_safe_data_media_and_custom_attributes() -> None:
    page = SamplePage(
        page_id="sample_1",
        title="Media",
        html=(
            '<section data-source="local"><video controls poster="data:image/png;base64,AA==">'
            '<source src="data:video/mp4;base64,AA==" type="video/mp4">'
            '</video></section>'
        ),
    )

    assert 'data-source="local"' in page.html
    assert 'src="data:video/mp4;base64,AA=="' in page.html


def test_sample_page_allows_svg_namespace_declaration_and_drops_it() -> None:
    page = SamplePage(
        page_id="sample_1",
        title="Namespaced SVG",
        html=(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            '<rect width="10" height="10"></rect></svg>'
        ),
    )

    assert '<svg viewBox="0 0 10 10">' in page.html
    assert "xmlns" not in page.html


def test_sample_page_allows_svg_aria_attributes() -> None:
    page = SamplePage(
        page_id="sample_1",
        title="Accessible SVG",
        html=(
            '<svg viewBox="0 0 10 10" aria-label="chart" aria-hidden="false">'
            '<rect width="10" height="10"></rect></svg>'
        ),
    )

    assert 'aria-label="chart"' in page.html
    assert 'aria-hidden="false"' in page.html


def test_sample_page_allows_svg_title_and_desc_elements() -> None:
    page = SamplePage(
        page_id="sample_1",
        title="Described SVG",
        html=(
            '<svg viewBox="0 0 10 10"><title>chart</title><desc>summary</desc>'
            '<rect width="10" height="10"></rect></svg>'
        ),
    )

    assert "<title>chart</title>" in page.html
    assert "<desc>summary</desc>" in page.html


def test_sample_page_rejects_comment_capture_payload() -> None:
    html = (
        '<!-- <head> --><html><head></head><body><style>'
        'body{background-image:image-set("https://audit.invalid/pixel")}'
        '</style></body></html>'
    )

    with pytest.raises(ValueError, match="active or external"):
        SamplePage(page_id="sample_1", title="Unsafe", html=html)


def test_sample_output_caps_total_html_size() -> None:
    pages = [
        SamplePage(page_id=f"sample_{index}", title="Large", html="x" * 130_000)
        for index in range(4)
    ]

    with pytest.raises(ValueError, match="total size limit"):
        SampleOutput(pages=pages)


def test_stale_checkpoint_cannot_mutate(workflow: Workflow) -> None:
    stale = workflow.store.read()["checkpoint_id"]
    workflow.start_clarification(stale)

    with pytest.raises(ConflictError, match="stale_revision"):
        workflow.start_clarification(stale)


def test_editing_approved_narrative_invalidates_outline(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = finish_automatic_clarification(
        workflow,
        workflow.answer_clarification(
            manifest["checkpoint_id"],
            card["question_card_id"],
            {q["question_id"]: "answer" for q in card["questions"]},
        ),
    )
    manifest = workflow.generate_document("narrative_structure", manifest["checkpoint_id"])
    narrative = manifest["documents"]["narrative_structure"][-1]
    manifest = workflow.approve_document("narrative_structure", manifest["checkpoint_id"], narrative["revision_hash"])
    manifest = workflow.generate_document("slide_outline", manifest["checkpoint_id"])

    manifest = workflow.edit_document("narrative_structure", manifest["checkpoint_id"], "# 新叙事")

    assert manifest["state"] == "narrative_structure"
    assert manifest["documents"]["slide_outline"][-1]["status"] == "stale"
    assert manifest["documents"]["narrative_structure"][-1]["revision"] == 2
    assert manifest["documents"]["narrative_structure"][-1]["provenance"]["output_hash"] == digest("# 新叙事")


def test_old_question_card_is_rejected(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])

    with pytest.raises(ConflictError, match="stale_question_card"):
        workflow.answer_clarification(manifest["checkpoint_id"], "questions_old", {})


def test_branch_pointer_tracks_latest_checkpoint(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    assert manifest["branches"]["main"] == manifest["checkpoint_id"]

    branched = workflow.store.fork(manifest["checkpoint_id"], "alternate")
    assert branched["branch"] == "alternate"
    assert branched["branches"]["alternate"] == branched["checkpoint_id"]


def test_branch_switch_restores_branch_head_without_rewriting_history(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    main = workflow.start_clarification(manifest["checkpoint_id"])
    main_head = main["checkpoint_id"]
    alternate = workflow.store.fork(main_head, "alternate")
    alternate_head = alternate["checkpoint_id"]

    switched = workflow.store.switch_branch(main_head)
    assert switched["branch"] == "main"
    assert switched["checkpoint_id"] == main_head
    assert switched["branches"] == {"main": main_head, "alternate": alternate_head}

    view = workflow.store.branches_view()
    assert view["current"] == "main"
    assert {item["name"] for item in view["items"]} == {"main", "alternate"}
    assert next(item for item in view["items"] if item["name"] == "alternate")["head_checkpoint_id"] == alternate_head

    switched_back = workflow.store.switch_branch(alternate_head)
    assert switched_back["branch"] == "alternate"
    assert switched_back["checkpoint_id"] == alternate_head


def test_progress_snapshots_drive_stage_rerun_branch(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = finish_automatic_clarification(
        workflow,
        workflow.answer_clarification(
            manifest["checkpoint_id"],
            card["question_card_id"],
            {question["question_id"]: "answer" for question in card["questions"]},
        ),
    )
    manifest = workflow.generate_document("narrative_structure", manifest["checkpoint_id"])
    narrative = manifest["documents"]["narrative_structure"][-1]
    manifest = workflow.approve_document(
        "narrative_structure", manifest["checkpoint_id"], narrative["revision_hash"]
    )
    manifest = workflow.generate_document("slide_outline", manifest["checkpoint_id"])
    outline = manifest["documents"]["slide_outline"][-1]
    completed = workflow.approve_document(
        "slide_outline", manifest["checkpoint_id"], outline["revision_hash"]
    )

    snapshots = workflow.store.progress_snapshots()
    assert [item["stage"] for item in snapshots] == [
        "intake", "intake_clarify", "narrative_structure", "slide_outline", "ppt_sample"
    ]
    assert [item["completed"] for item in snapshots] == [True, True, True, True, False]
    narrative_snapshot = next(item for item in snapshots if item["stage"] == "narrative_structure")
    assert narrative_snapshot["snapshot"]["documents"]["narrative_structure"][-1]["status"] == "approved"

    branched = workflow.store.fork(
        narrative_snapshot["checkpoint_id"],
        "narrative-rerun",
        mode="rerun_stage",
        stage="narrative_structure",
    )

    assert branched["state"] == "narrative_structure"
    assert branched["phase"] == "ready_to_generate"
    assert branched["documents"] == {"narrative_structure": [], "slide_outline": []}
    assert branched["samples"] == []
    assert branched["clarification_answers"]
    assert branched["branches"]["main"] == completed["checkpoint_id"]
    assert branched["branch_meta"]["narrative-rerun"]["mode"] == "rerun_stage"
    assert branched["branch_meta"]["narrative-rerun"]["source_stage"] == "narrative_structure"

    branched_progress = workflow.store.progress_snapshots()
    assert [item["stage"] for item in branched_progress] == [
        "intake", "intake_clarify", "narrative_structure"
    ]
    assert [item["completed"] for item in branched_progress] == [True, True, False]


def test_completed_outline_from_previous_release_can_enter_sample_stage(workflow: Workflow) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = finish_automatic_clarification(
        workflow,
        workflow.answer_clarification(
            manifest["checkpoint_id"],
            card["question_card_id"],
            {question["question_id"]: "answer" for question in card["questions"]},
        ),
    )
    manifest = workflow.generate_document("narrative_structure", manifest["checkpoint_id"])
    narrative = manifest["documents"]["narrative_structure"][-1]
    manifest = workflow.approve_document(
        "narrative_structure", manifest["checkpoint_id"], narrative["revision_hash"]
    )
    manifest = workflow.generate_document("slide_outline", manifest["checkpoint_id"])
    outline = manifest["documents"]["slide_outline"][-1]
    manifest = workflow.approve_document(
        "slide_outline", manifest["checkpoint_id"], outline["revision_hash"]
    )
    legacy = workflow.store.update(
        lambda value: value | {"state": "slide_outline", "phase": "completed"},
        "legacy_fixture",
        {},
        expected_checkpoint_id=manifest["checkpoint_id"],
    )

    assert "start_sample_stage" in capabilities(legacy)
    migrated = workflow.start_sample_stage(legacy["checkpoint_id"])

    assert migrated["state"] == "ppt_sample"
    assert migrated["phase"] == "ready_to_generate"


def test_concurrent_edits_from_same_checkpoint_use_atomic_cas(workflow: Workflow, monkeypatch) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = finish_automatic_clarification(
        workflow,
        workflow.answer_clarification(
            manifest["checkpoint_id"],
            card["question_card_id"],
            {question["question_id"]: "answer" for question in card["questions"]},
        ),
    )
    manifest = workflow.generate_document("narrative_structure", manifest["checkpoint_id"])
    shared_checkpoint = manifest["checkpoint_id"]

    original_require = workflow._require
    both_validated = Barrier(2)

    def synchronized_require(value, capability, checkpoint_id=None):
        original_require(value, capability, checkpoint_id)
        if capability == "edit_narrative":
            both_validated.wait(timeout=5)

    monkeypatch.setattr(workflow, "_require", synchronized_require)

    def save(markdown: str):
        try:
            return workflow.edit_document("narrative_structure", shared_checkpoint, markdown)
        except ConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["# 并发版本 A", "# 并发版本 B"]))

    assert sum(isinstance(result, ConflictError) for result in results) == 1
    assert str(next(result for result in results if isinstance(result, ConflictError))) == "stale_revision"
    history = workflow.store.read()["documents"]["narrative_structure"]
    assert [revision["revision"] for revision in history] == [1, 2]
    assert history[1]["parent_revision_hash"] == history[0]["revision_hash"]


def test_concurrent_generation_only_completes_audit_for_committed_output(
    workflow: Workflow,
) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = finish_automatic_clarification(
        workflow,
        workflow.answer_clarification(
            manifest["checkpoint_id"],
            card["question_card_id"],
            {question["question_id"]: "answer" for question in card["questions"]},
        ),
    )
    checkpoint_id = manifest["checkpoint_id"]
    competing = Workflow(
        ProjectStore(workflow.store.projects_root, workflow.store.project_id),
        workflow.runtime,
    )
    workflows = [workflow, competing]
    generated_together = Barrier(2)

    for index, candidate in enumerate(workflows, start=1):
        markdown = f"# 并发生成版本 {index}"

        def generate(*_args, owner=candidate, content=markdown, **_kwargs):
            generated_together.wait(timeout=5)
            owner.gateway.last_messages = [{"role": "assistant", "content": content}]
            return content, [{
                "type": "model_call",
                "provider": "test",
                "model": "concurrency-test",
                "usage": {},
            }]

        candidate.gateway.generate = generate

    def run(candidate: Workflow):
        try:
            return candidate.generate_document("narrative_structure", checkpoint_id)
        except ConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, workflows))

    assert sum(isinstance(outcome, ConflictError) for outcome in outcomes) == 1
    persisted = workflow.store.read()
    revisions = persisted["documents"]["narrative_structure"]
    assert len(revisions) == 1
    calls = [
        call for call in workflow.store.prompt_calls()
        if call["state"] == "narrative_structure"
    ]
    assert sorted(call["status"] for call in calls) == ["completed", "conflicted"]
    completed = next(call for call in calls if call["status"] == "completed")
    conflicted = next(call for call in calls if call["status"] == "conflicted")
    assert completed["output_ref"] == revisions[0]["revision_hash"]
    assert conflicted["output_ref"] is None
    assert conflicted["output_hash"] is None
    assert conflicted["error"]["code"] == "stale_revision"


def test_generation_provenance_hashes_skill_index_reads_and_output(workflow: Workflow, monkeypatch) -> None:
    manifest = workflow.store.read()
    manifest = workflow.start_clarification(manifest["checkpoint_id"])
    card = manifest["question_card"]
    manifest = finish_automatic_clarification(
        workflow,
        workflow.answer_clarification(
            manifest["checkpoint_id"],
            card["question_card_id"],
            {question["question_id"]: "answer" for question in card["questions"]},
        ),
    )
    traces = [
        {"type": "model_call", "provider": "test", "model": "test-model", "usage": {}},
        {
            "type": "tool_call",
            "tool": "read",
            "path": "narrative-structure/SKILL.md",
            "content_hash": "sha256:skill-content",
            "offset": 0,
            "end": 128,
        },
    ]
    monkeypatch.setattr(workflow.gateway, "generate", lambda *args, **kwargs: ("# 可复现叙事", traces))

    manifest = workflow.generate_document("narrative_structure", manifest["checkpoint_id"])
    document = manifest["documents"]["narrative_structure"][-1]
    provenance = document["provenance"]

    assert provenance["skills_hash"] == stable_hash(provenance["skill_index"])
    assert provenance["skill_reads"] == [{
        "path": "narrative-structure/SKILL.md",
        "content_hash": "sha256:skill-content",
        "offset": 0,
        "end": 128,
    }]
    assert provenance["skill_reads_hash"] == stable_hash(provenance["skill_reads"])
    assert provenance["output_hash"] == digest("# 可复现叙事")
