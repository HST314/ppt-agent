from __future__ import annotations

import hashlib
import json
import posixpath
import re
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any, Callable, Literal, NoReturn, Protocol
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from pydantic import ValidationError

from agent_core.full_deck_composer import (
    COMPOSER_VERSION,
    ComposerPage,
    ComposerSource,
    FullDeckComposerError,
    FullDeckComposerInput,
    compose_full_deck,
    normalized_page_content_graph,
)
from agent_core.jobs import JobCancelled
from agent_core.models import (
    FullDeckContentRef,
    FullDeckPackage,
    FullDeckPlan,
    FullDeckRevision,
    HtmlPptPackage,
)
from agent_core.workflow_support import (
    current_full_deck_revision as _current_full_deck_revision,
    generation_provenance,
    outline_slide_catalog as _outline_slide_catalog,
    package_model as _package_model,
    stable_hash,
)
from configs.runtime import ManagedRuntime
from model_router.client import ModelGateway, ModelOutputError
from runtime.package_tool import DraftPackage, PackageToolError
from runtime.read_tool import SkillReader
from storage.project_store import ConflictError, ProjectStore


FULL_DECK_MAX_REPAIR_ATTEMPTS = 2


class FullDeckGenerationError(RuntimeError):
    """A rejected segment or composition with a stable browser-safe contract."""

    def __init__(self, public_code: str, public_message: str, repair_reason: str):
        super().__init__(repair_reason)
        self.public_code = public_code
        self.public_message = public_message
        self.repair_reason = repair_reason


class FullDeckWorkflowHost(Protocol):
    store: ProjectStore
    runtime: ManagedRuntime
    gateway: ModelGateway

    def _template(self, name: str) -> tuple[str, str]: ...

    def _start_prompt_audit(
        self,
        state: str,
        prompt: str,
        *,
        template_id: str,
        template_hash: str,
        skills_hash: str,
        json_mode: bool = False,
        parent_prompt_call_id: str | None = None,
        audit_context: dict[str, Any] | None = None,
    ) -> str: ...

    def _fail_prompt_audit(
        self,
        prompt_call_id: str,
        exc: Exception,
        traces: list[dict[str, Any]] | None = None,
    ) -> None: ...

    @staticmethod
    def _require(
        manifest: dict[str, Any],
        capability: str,
        checkpoint_id: str | None = None,
    ) -> None: ...


class _SlideMarkupParser(HTMLParser):
    """Collect static HTML-PPT page markers without executing package code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.slide_ids: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if "slide" in classes:
            self.slide_ids.append(attributes.get("data-slide-id"))


_SAFE_XML_NAMESPACES = (
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/XML/1998/namespace",
    "http://www.w3.org/2000/xmlns/",
)
_EXTERNAL_SCHEME = re.compile(r"(?i)\b(?:https?|wss?)://")
_PROTOCOL_RELATIVE_ATTRIBUTE = re.compile(
    r"(?i)\b(?:src|href|poster|data|action)\s*=\s*([\"'])\s*//"
)
_ROOT_RELATIVE_ATTRIBUTE = re.compile(
    r"(?i)\b(?:src|href|poster|data|action)\s*=\s*([\"'])\s*/(?!/)"
)
_ROOT_RELATIVE_CSS = re.compile(r"(?i)url\(\s*([\"']?)\s*/(?!/)")
_NETWORK_RUNTIME = re.compile(
    r"(?i)\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(|"
    r"navigator\s*\.\s*sendBeacon\s*\("
)
_CSS_REFERENCE = re.compile(
    r"(?i)(?:url\(\s*([\"']?)(.*?)\1\s*\)|@import\s+([\"'])(.*?)\3)"
)


class _PackageReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if not value:
                continue
            normalized = name.lower()
            if normalized in {"src", "href", "poster", "data", "action", "xlink:href"}:
                self.references.append(value)
            elif normalized == "srcset":
                self.references.extend(
                    candidate.strip().split()[0]
                    for candidate in value.split(",")
                    if candidate.strip()
                )


def _dependency_target(source_path: str, reference: str) -> str | None:
    value = reference.strip()
    if not value or value.startswith("#"):
        return None
    parsed = urlsplit(value)
    if parsed.scheme in {"data", "blob"}:
        return None
    if parsed.scheme or parsed.netloc:
        raise FullDeckGenerationError(
            "full_deck_segment_invalid",
            "全稿页段包含外部依赖，自动修复后仍未成功，请重试。",
            f"{source_path} 引用了不允许的 URI：{value[:160]}",
        )
    path = unquote(parsed.path).replace("\\", "/")
    if not path:
        return None
    combined = posixpath.normpath(
        str(PurePosixPath(source_path).parent / PurePosixPath(path))
    )
    if combined.startswith("../") or combined in {"", ".", ".."}:
        raise FullDeckGenerationError(
            "full_deck_segment_invalid",
            "全稿页段资源路径越界，自动修复后仍未成功，请重试。",
            f"{source_path} 的依赖 {value[:160]} 不能解析到包内文件。",
        )
    return combined


def _validate_offline_package(package: HtmlPptPackage) -> None:
    """Reject package dependencies that would escape the exported ZIP."""

    package_paths = {item.path for item in package.files}
    for item in package.files:
        try:
            text = item.content_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        inspected = text
        for namespace in _SAFE_XML_NAMESPACES:
            inspected = inspected.replace(namespace, "")
        if (
            _EXTERNAL_SCHEME.search(inspected)
            or _PROTOCOL_RELATIVE_ATTRIBUTE.search(inspected)
            or _ROOT_RELATIVE_ATTRIBUTE.search(inspected)
            or _ROOT_RELATIVE_CSS.search(inspected)
            or _NETWORK_RUNTIME.search(inspected)
        ):
            raise FullDeckGenerationError(
                "full_deck_segment_invalid",
                "全稿页段包含外部依赖，自动修复后仍未成功，请重试。",
                f"{item.path} 包含网络或站点根路径依赖；请改为包内相对路径或内联资源。",
            )
        references: list[str] = []
        if PurePosixPath(item.path).suffix.lower() in {".html", ".htm", ".svg"}:
            parser = _PackageReferenceParser()
            parser.feed(text)
            references.extend(parser.references)
        if PurePosixPath(item.path).suffix.lower() in {".html", ".htm", ".css", ".svg"}:
            references.extend(
                match.group(2) or match.group(4) or ""
                for match in _CSS_REFERENCE.finditer(text)
            )
        for reference in references:
            target = _dependency_target(item.path, reference)
            if target is not None and target not in package_paths:
                raise FullDeckGenerationError(
                    "full_deck_segment_invalid",
                    "全稿页段缺少本地资源，自动修复后仍未成功，请重试。",
                    f"{item.path} 引用的包内文件不存在：{target}",
                )


def _full_deck_slide_ids(draft: DraftPackage) -> list[str]:
    try:
        index_html = draft.read("index.html")["content"]
    except PackageToolError as exc:
        raise FullDeckGenerationError(
            "full_deck_segment_invalid",
            "全稿页段缺少有效入口，自动修复后仍未成功，请重试。",
            f"无法读取 index.html：{exc}",
        ) from exc
    parser = _SlideMarkupParser()
    parser.feed(index_html)
    if any(not slide_id for slide_id in parser.slide_ids):
        raise FullDeckGenerationError(
            "full_deck_segment_invalid",
            "全稿页段页面标识不完整，自动修复后仍未成功，请重试。",
            "每个 class=\"slide\" 的静态页面元素都必须包含非空 data-slide-id。",
        )
    return [str(slide_id) for slide_id in parser.slide_ids]


def _parse_full_deck_segment_output(text: str) -> dict[str, Any]:
    try:
        return ModelGateway.parse_json(text)
    except (json.JSONDecodeError, ModelOutputError, TypeError, ValueError) as exc:
        raise FullDeckGenerationError(
            "full_deck_segment_invalid",
            "全稿页段输出格式不正确，自动修复后仍未成功，请重试。",
            f"页段 JSON 无效：{exc}",
        ) from exc


def _validate_full_deck_segment_output(
    payload: dict[str, Any],
    draft: DraftPackage,
    target_slide_numbers: list[int],
) -> HtmlPptPackage:
    value = dict(payload)
    declared_targets = value.pop("source_slide_numbers", None)
    if declared_targets != target_slide_numbers:
        raise FullDeckGenerationError(
            "full_deck_target_mismatch",
            "全稿页段与目标页号不一致，自动修复后仍未成功，请重试。",
            f"source_slide_numbers 必须精确等于 {target_slide_numbers}，实际为 {declared_targets}。",
        )
    embedded = value.pop("files", [])
    if embedded:
        if not isinstance(embedded, list):
            raise FullDeckGenerationError(
                "full_deck_segment_invalid",
                "全稿页段文件清单无效，自动修复后仍未成功，请重试。",
                "files 必须是文件对象数组。",
            )
        try:
            draft.ingest(embedded)
        except PackageToolError as exc:
            raise FullDeckGenerationError(
                "full_deck_segment_invalid",
                "全稿页段包含无效文件，自动修复后仍未成功，请重试。",
                f"包文件校验失败：{exc}",
            ) from exc
    try:
        package = HtmlPptPackage.model_validate({**value, "files": draft.payload()})
    except ValidationError as exc:
        reasons = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"])
            message = " ".join(error["msg"].removeprefix("Value error, ").split())[:180]
            reasons.append(f"{location}: {message}" if location else message)
        raise FullDeckGenerationError(
            "full_deck_segment_invalid",
            "全稿页段包格式不正确，自动修复后仍未成功，请重试。",
            "页段包契约校验失败：" + ("；".join(reasons[:4]) or "未知错误"),
        ) from exc
    actual_numbers = [slide.source_slide_number for slide in package.slides]
    if package.slide_count != len(target_slide_numbers) or actual_numbers != target_slide_numbers:
        raise FullDeckGenerationError(
            "full_deck_target_mismatch",
            "全稿页段与目标页号不一致，自动修复后仍未成功，请重试。",
            f"slides 必须按顺序精确覆盖 {target_slide_numbers}，实际为 {actual_numbers}。",
        )
    declared_slide_ids = [slide.slide_id for slide in package.slides]
    actual_slide_ids = _full_deck_slide_ids(draft)
    if actual_slide_ids != declared_slide_ids:
        raise FullDeckGenerationError(
            "full_deck_segment_invalid",
            "全稿页段实际页面与清单不一致，自动修复后仍未成功，请重试。",
            "index.html 中 class=\"slide\" 的 data-slide-id 必须与 slides 一一对应；"
            f"清单为 {declared_slide_ids}，实际为 {actual_slide_ids}。",
        )
    _validate_offline_package(package)
    return package


def pending_full_deck_segments(
    pages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Partition pending pages by consecutive outline page number."""

    segments: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_number: int | None = None
    for page in pages:
        if page.get("status") != "pending":
            if current:
                segments.append(current)
                current = []
            previous_number = None
            continue
        outline_ref = page.get("outline_ref") or {}
        number = outline_ref.get("source_slide_number")
        if not isinstance(number, int):
            raise FullDeckGenerationError(
                "full_deck_plan_invalid",
                "全稿页面清单缺少大纲页号，请重新进入全稿后重试。",
                f"待生成槽位 {page.get('slot_id')} 缺少 source_slide_number。",
            )
        if current and number != previous_number + 1:
            segments.append(current)
            current = []
        current.append(page)
        previous_number = number
    if current:
        segments.append(current)
    return segments


def _validate_full_deck_package_limits(
    package: HtmlPptPackage,
    runtime: ManagedRuntime,
) -> None:
    files = package.files
    sizes = [len(item.content_bytes()) for item in files]
    if (
        len(files) > runtime.policy.full_deck_max_files
        or any(size > runtime.policy.full_deck_max_file_bytes for size in sizes)
        or sum(sizes) > runtime.policy.full_deck_max_total_bytes
    ):
        raise FullDeckGenerationError(
            "full_deck_package_invalid",
            "完整全稿超过资源上限，请减少资源体积后重试。",
            "Composer 输出超过 full_deck 文件数、单文件或整包大小限制。",
        )


def generate_full_deck(
    workflow: FullDeckWorkflowHost,
    checkpoint_id: str,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Generate every pending segment and atomically publish one composed revision."""

    should_cancel = cancel_requested or (lambda: False)
    if should_cancel():
        raise JobCancelled("full-deck generation cancelled before model execution")
    manifest = workflow.store.read()
    workflow._require(manifest, "generate_full_deck", checkpoint_id)
    root = manifest.get("full_deck") or {}
    current = _current_full_deck_revision(manifest)
    if not current or current.get("revision_hash") != root.get("current_revision_hash"):
        raise ConflictError("stale_revision:当前全稿版本已变化，请刷新后重试。")
    pages = current.get("plan", {}).get("pages", [])
    segments = pending_full_deck_segments(pages)
    if not segments:
        raise ConflictError("full_deck_incomplete:当前全稿没有待生成页，请刷新后重试。")
    outline = next(
        (
            item
            for item in manifest.get("documents", {}).get("slide_outline", [])
            if item.get("revision_hash") == root.get("outline_revision_hash")
        ),
        None,
    )
    sample = next(
        (
            item
            for item in manifest.get("samples", [])
            if item.get("revision_hash") == root.get("approved_sample_revision_hash")
        ),
        None,
    )
    if not outline or not sample or not sample.get("package"):
        raise ConflictError(
            "full_deck_plan_invalid:全稿的已确认大纲或样品来源缺失，请从样品阶段重建全稿。"
        )
    sample_package = _package_model(sample["package"])
    outline_catalog = _outline_slide_catalog(outline["markdown_body"])
    skill_index = SkillReader(
        workflow.runtime.skills_root,
        per_call=1000,
        per_job=1000,
    ).index()
    skills_hash = stable_hash(skill_index)
    template, template_hash = workflow._template("ppt_full.md")
    generation_id = "fullgen_" + uuid4().hex
    sample_reference = {
        "title": sample_package.title,
        "revision_hash": sample["revision_hash"],
        "package_hash": sample_package.package_hash,
        "slides": [item.model_dump(mode="json") for item in sample_package.slides],
        "files": [
            {
                "path": item.path,
                "media_type": item.media_type,
                "size_bytes": len(item.content_bytes()),
            }
            for item in sample_package.files
        ],
    }
    successful_audits: list[dict[str, Any]] = []
    all_prompt_call_ids: list[str] = []
    all_traces: list[dict[str, Any]] = []
    segment_results: list[dict[str, Any]] = []

    def close_successful_audits(
        *,
        status: Literal["failed", "conflicted"],
        code: str,
        message: str,
    ) -> None:
        workflow.store.finish_prompt_calls([
            {
                "prompt_call_id": audit["prompt_call_id"],
                "status": status,
                "traces": audit["traces"],
                "messages": audit["messages"],
                "error": {
                    "type": "FullDeckGenerationError",
                    "code": code,
                    "message": message[:1000],
                },
            }
            for audit in successful_audits
        ])
        successful_audits.clear()

    def raise_finalization_failure(
        exc: Exception,
        *,
        composition: bool = False,
    ) -> NoReturn:
        if isinstance(exc, JobCancelled):
            close_successful_audits(
                status="failed",
                code="job_cancelled",
                message="任务在最终发布前取消，未移动全稿指针。",
            )
            raise exc
        if isinstance(exc, ConflictError) and str(exc).startswith("stale_revision"):
            close_successful_audits(
                status="conflicted",
                code="stale_revision",
                message="组装完成时工程版本已变化，未发布生成结果。",
            )
            raise exc
        if isinstance(exc, FullDeckGenerationError):
            failure = exc
        elif composition and isinstance(
            exc,
            (FullDeckComposerError, ValidationError, ValueError),
        ):
            failure = FullDeckGenerationError(
                "full_deck_composition_failed",
                "完整全稿组装失败，请重试。",
                f"Composer 拒绝了页段输入：{exc}",
            )
        else:
            failure = FullDeckGenerationError(
                "full_deck_finalization_failed",
                "完整全稿收尾失败，请重试。",
                f"完整全稿组装或发布收尾失败：{exc}",
            )
        close_successful_audits(
            status="failed",
            code=failure.public_code,
            message=failure.repair_reason,
        )
        if failure is exc:
            raise failure
        raise failure from exc

    for segment_index, segment in enumerate(segments, start=1):
        if should_cancel():
            close_successful_audits(
                status="failed",
                code="job_cancelled",
                message="任务在最终发布前取消，未移动全稿指针。",
            )
            raise JobCancelled("full-deck generation cancelled between segments")
        target_numbers = [
            int(page["outline_ref"]["source_slide_number"])
            for page in segment
        ]
        first_position = int(segment[0]["position"])
        last_position = int(segment[-1]["position"])
        neighbors = {
            "before": pages[first_position - 1] if first_position > 0 else None,
            "after": pages[last_position + 1] if last_position + 1 < len(pages) else None,
        }
        base_prompt = (
            template
            + f"\n\nFULL_DECK_GENERATION_ID: {generation_id}\n"
            + f"FULL_DECK_TARGET_SLIDE_NUMBERS: {json.dumps(target_numbers)}\n"
            + "FULL_DECK_SEGMENT_PAGES_JSON: "
            + json.dumps(segment, ensure_ascii=False)
            + "\nALL_OUTLINE_SLIDES_JSON: "
            + json.dumps(outline_catalog, ensure_ascii=False)
            + "\nADJACENT_PAGES_JSON: "
            + json.dumps(neighbors, ensure_ascii=False)
            + "\nSAMPLE_VISUAL_REFERENCE_JSON: "
            + json.dumps(sample_reference, ensure_ascii=False)
            + f"\nTask card:\n{json.dumps(manifest['task_card'], ensure_ascii=False)}\n"
            + f"Approved slide outline:\n{outline['markdown_body']}\n"
            + f"Skill index:\n{json.dumps(skill_index, ensure_ascii=False)}"
        )
        current_prompt = base_prompt
        parent_prompt_call_id: str | None = None
        package_output: HtmlPptPackage | None = None
        for attempt in range(FULL_DECK_MAX_REPAIR_ATTEMPTS + 1):
            if should_cancel():
                close_successful_audits(
                    status="failed",
                    code="job_cancelled",
                    message="任务在页段生成前取消，未移动全稿指针。",
                )
                raise JobCancelled("full-deck generation cancelled before segment attempt")
            draft = DraftPackage(
                workflow.runtime.skills_root,
                max_files=workflow.runtime.policy.full_deck_max_files,
                max_file_bytes=workflow.runtime.policy.full_deck_max_file_bytes,
                max_total_bytes=workflow.runtime.policy.full_deck_max_total_bytes,
            )
            prompt_call_id = workflow._start_prompt_audit(
                "ppt_full",
                current_prompt,
                template_id="ppt_full",
                template_hash=template_hash,
                skills_hash=skills_hash,
                json_mode=True,
                parent_prompt_call_id=parent_prompt_call_id,
                audit_context={
                    "operation": "generate_full_deck",
                    "generation_id": generation_id,
                    "segment_index": segment_index,
                    "target_slide_numbers": target_numbers,
                    "attempt": attempt + 1,
                    "composer_version": COMPOSER_VERSION,
                },
            )
            all_prompt_call_ids.append(prompt_call_id)
            attempt_traces: list[dict[str, Any]] = []
            try:
                try:
                    text, attempt_traces = workflow.gateway.generate(
                        "ppt_full",
                        current_prompt,
                        json_mode=True,
                        package_draft=draft,
                    )
                except TypeError as exc:
                    if "package_draft" not in str(exc):
                        raise
                    text, attempt_traces = workflow.gateway.generate(
                        "ppt_full",
                        current_prompt,
                        json_mode=True,
                    )
            except Exception as exc:
                workflow._fail_prompt_audit(prompt_call_id, exc, attempt_traces)
                close_successful_audits(
                    status="failed",
                    code=getattr(exc, "public_code", "full_deck_incomplete"),
                    message=str(exc),
                )
                raise
            all_traces.extend(attempt_traces)
            if should_cancel():
                workflow.store.finish_prompt_call(
                    prompt_call_id,
                    status="failed",
                    traces=attempt_traces,
                    messages=workflow.gateway.last_messages,
                    error={
                        "type": "JobCancelled",
                        "code": "job_cancelled",
                        "message": "任务在模型返回后取消，页段未发布。",
                    },
                )
                close_successful_audits(
                    status="failed",
                    code="job_cancelled",
                    message="任务在最终发布前取消，未移动全稿指针。",
                )
                raise JobCancelled("full-deck generation cancelled after model response")
            try:
                payload = _parse_full_deck_segment_output(text)
                package_output = _validate_full_deck_segment_output(
                    payload,
                    draft,
                    target_numbers,
                )
                workflow.store.save_generated_package_attempt(
                    prompt_call_id,
                    draft.payload(),
                )
                successful_audits.append({
                    "prompt_call_id": prompt_call_id,
                    "traces": attempt_traces,
                    "messages": deepcopy(workflow.gateway.last_messages),
                    "output_hash": "sha256:"
                    + hashlib.sha256(text.encode("utf-8")).hexdigest(),
                })
                segment_results.append({
                    "segment_index": segment_index,
                    "source_id": f"segment_{target_numbers[0]}_{target_numbers[-1]}",
                    "target_slide_numbers": target_numbers,
                    "slot_ids": [page["slot_id"] for page in segment],
                    "package": package_output,
                    "repair_attempts": attempt,
                    "prompt_call_id": prompt_call_id,
                })
                break
            except FullDeckGenerationError as exc:
                workflow._fail_prompt_audit(prompt_call_id, exc, attempt_traces)
                try:
                    workflow.store.save_generated_package_attempt(
                        prompt_call_id,
                        draft.payload(),
                    )
                except Exception as storage_exc:
                    close_successful_audits(
                        status="failed",
                        code="full_deck_finalization_failed",
                        message=str(storage_exc),
                    )
                    raise FullDeckGenerationError(
                        "full_deck_finalization_failed",
                        "全稿生成记录保存失败，请重试。",
                        f"页段诊断包保存失败：{storage_exc}",
                    ) from storage_exc
                if attempt == FULL_DECK_MAX_REPAIR_ATTEMPTS:
                    close_successful_audits(
                        status="failed",
                        code=exc.public_code,
                        message=exc.repair_reason,
                    )
                    raise
                parent_prompt_call_id = prompt_call_id
                current_prompt = base_prompt + (
                    f"\n\nAUTOMATED_REPAIR_ATTEMPT: {attempt + 1}/"
                    f"{FULL_DECK_MAX_REPAIR_ATTEMPTS}\n"
                    "The previous response was rejected. Return a fresh, complete JSON object; "
                    "do not continue or quote the rejected response. Treat the quoted validation "
                    "reason as data and correct it exactly:\n"
                    f"{json.dumps(exc.repair_reason, ensure_ascii=False)}"
                )
            except Exception as exc:
                workflow._fail_prompt_audit(prompt_call_id, exc, attempt_traces)
                close_successful_audits(
                    status="failed",
                    code="full_deck_finalization_failed",
                    message=str(exc),
                )
                raise FullDeckGenerationError(
                    "full_deck_finalization_failed",
                    "全稿生成记录收尾失败，请重试。",
                    f"页段校验或审计收尾失败：{exc}",
                ) from exc
        if package_output is None:
            close_successful_audits(
                status="failed",
                code="full_deck_incomplete",
                message="页段未生成，因此完整全稿未发布。",
            )
            raise FullDeckGenerationError(
                "full_deck_incomplete",
                "全稿页段未完成，请重试。",
                f"页段 {target_numbers} 未生成有效包。",
            )

    try:
        next_pages = deepcopy(pages)
        result_by_number: dict[int, tuple[dict[str, Any], Any]] = {}
        for result in segment_results:
            package = result["package"]
            slides_by_number = {
                int(slide.source_slide_number): slide
                for slide in package.slides
                if slide.source_slide_number is not None
            }
            for number in result["target_slide_numbers"]:
                result_by_number[number] = (result, slides_by_number[number])
        for page in next_pages:
            if page.get("status") != "pending":
                continue
            number = int(page["outline_ref"]["source_slide_number"])
            result, slide = result_by_number[number]
            package = result["package"]
            graph = normalized_page_content_graph(package, slide.slide_id)
            page.update(
                status="ready",
                source_type="generated_segment",
                content_ref=FullDeckContentRef(
                    revision_hash=package.package_hash,
                    package_hash=package.package_hash,
                    slide_id=slide.slide_id,
                    slide_content_hash=graph.content_hash,
                ).model_dump(mode="json"),
            )
        next_plan = FullDeckPlan.model_validate({"pages": next_pages})
        sources = [
            ComposerSource(source_id="approved_sample", package=sample_package)
        ] + [
            ComposerSource(source_id=result["source_id"], package=result["package"])
            for result in segment_results
        ]
        result_for_number = {
            number: result
            for result in segment_results
            for number in result["target_slide_numbers"]
        }
        composer_pages: list[ComposerPage] = []
        for page in next_plan.pages:
            number = page.outline_ref.source_slide_number if page.outline_ref else page.position + 1
            if page.source_type == "approved_sample":
                source_id = "approved_sample"
            elif page.source_type == "generated_segment":
                source_id = result_for_number[number]["source_id"]
            else:
                raise FullDeckGenerationError(
                    "full_deck_incomplete",
                    "全稿仍有未完成页面，请重新生成。",
                    f"槽位 {page.slot_id} 的来源类型 {page.source_type} 不能参与首次组装。",
                )
            composer_pages.append(ComposerPage(
                slot_id=page.slot_id,
                slide_id=page.slot_id,
                title=page.title,
                source_slide_number=number,
                source_id=source_id,
                source_slide_id=page.content_ref.slide_id,
            ))
        composer_input = FullDeckComposerInput(
            title=manifest["title"],
            sources=sources,
            pages=composer_pages,
        )
        composition = compose_full_deck(composer_input)
        if (
            [slide.slot_id for slide in composition.manifest.slides]
            != [page.slot_id for page in next_plan.pages]
            or [slide.source_slide_number for slide in composition.manifest.slides]
            != [page.outline_ref.source_slide_number for page in next_plan.pages]
        ):
            raise FullDeckGenerationError(
                "full_deck_composition_failed",
                "全稿页序校验失败，请重试。",
                "Composer manifest 的槽位或大纲页号顺序与全稿清单不一致。",
            )
        for page, slide in zip(next_plan.pages, composition.manifest.slides, strict=True):
            if (
                page.content_ref is None
                or slide.source_slide_content_hash != page.content_ref.slide_content_hash
                or slide.composed_slide_content_hash != page.content_ref.slide_content_hash
            ):
                raise FullDeckGenerationError(
                    "full_deck_composition_failed",
                    "全稿页面来源保真校验失败，请重试。",
                    f"槽位 {page.slot_id} 的规范化内容图在组装前后不一致。",
                )
        _validate_offline_package(composition.package)
        _validate_full_deck_package_limits(composition.package, workflow.runtime)
        full_package = FullDeckPackage.model_validate({
            **composition.package.model_dump(mode="json"),
            "composition_manifest": composition.manifest.model_dump(mode="json"),
        })
    except Exception as exc:
        raise_finalization_failure(exc, composition=True)

    try:
        if should_cancel():
            raise JobCancelled("full-deck generation cancelled before commit")

        next_revision_number = max(
            (item["revision"] for item in manifest.get("full_deck_revisions", [])),
            default=0,
        ) + 1
        provenance_output = json.dumps(
            composition.manifest.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        )
        revision = FullDeckRevision.create(
            full_deck_id=root["full_deck_id"],
            revision=next_revision_number,
            parent=current["revision_hash"],
            feedback="首次生成完整 HTML-PPT",
            plan=next_plan,
            package=full_package,
            status="pending_approval",
            provenance={
                **generation_provenance(skill_index, all_traces, provenance_output),
                "outline_revision_hash": root["outline_revision_hash"],
                "approved_sample_revision_hash": root["approved_sample_revision_hash"],
                "model_config_hash": workflow.runtime.model_hash,
                "runtime_config_hash": workflow.runtime.runtime_hash,
                "template_id": "ppt_full",
                "template_version": 1,
                "template_hash": template_hash,
                "composer_version": COMPOSER_VERSION,
                "composition_input_hash": composition.manifest.input_hash,
                "package_hash": full_package.package_hash,
                "changed_slot_ids": [
                    page["slot_id"] for segment in segments for page in segment
                ],
                "generation_id": generation_id,
                "segments": [
                    {
                        "target_slide_numbers": result["target_slide_numbers"],
                        "slot_ids": result["slot_ids"],
                        "package_hash": result["package"].package_hash,
                        "repair_attempts": result["repair_attempts"],
                        "prompt_call_id": result["prompt_call_id"],
                    }
                    for result in segment_results
                ],
                "prompt_call_ids": all_prompt_call_ids,
                "resource_limits": {
                    "max_files": workflow.runtime.policy.full_deck_max_files,
                    "max_file_bytes": workflow.runtime.policy.full_deck_max_file_bytes,
                    "max_total_bytes": workflow.runtime.policy.full_deck_max_total_bytes,
                },
            },
        )

        def apply(value: dict[str, Any]) -> dict[str, Any]:
            value["full_deck_revisions"].append(revision.model_dump(mode="json"))
            value["full_deck"]["revision_refs"].append({
                "revision_hash": revision.revision_hash,
                "status": revision.status,
            })
            value["full_deck"]["current_revision_hash"] = revision.revision_hash
            value.update(state="ppt_full", phase="waiting_human_approval")
            return value

        committed = workflow.store.update(
            apply,
            "full_deck_generated",
            {
                "revision_hash": revision.revision_hash,
                "parent_revision_hash": current["revision_hash"],
                "page_count": len(next_plan.pages),
                "segment_ranges": [
                    result["target_slide_numbers"] for result in segment_results
                ],
                "package_hash": full_package.package_hash,
                "composer_version": COMPOSER_VERSION,
            },
            expected_checkpoint_id=checkpoint_id,
        )
    except Exception as exc:
        raise_finalization_failure(exc)
    workflow.store.finish_prompt_calls([
        {
            "prompt_call_id": audit["prompt_call_id"],
            "status": "completed",
            "traces": audit["traces"],
            "messages": audit["messages"],
            "output_ref": revision.revision_hash,
            "output_hash": audit["output_hash"],
        }
        for audit in successful_audits
    ])
    successful_audits.clear()
    return committed
