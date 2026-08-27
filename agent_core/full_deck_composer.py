from __future__ import annotations

import base64
import html
import json
import posixpath
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from hashlib import sha256
from typing import Any, Literal
from urllib.parse import unquote

import html5lib
from pydantic import Field, model_validator

from agent_core.models import (
    FullDeckPackage,
    HtmlPptPackage,
    PackageFile,
    PackageSlide,
    StrictModel,
)


COMPOSER_VERSION = "full-deck-composer-v1"
PAGE_CONTENT_GRAPH_VERSION = "page-content-graph-v1"


class FullDeckComposerError(ValueError):
    """A deterministic composition input or transformation is invalid."""


class ComposerSource(StrictModel):
    source_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    package: HtmlPptPackage


class ComposerPage(StrictModel):
    slot_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^slot_[a-f0-9]{24}$",
    )
    slide_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=160)
    source_slide_number: int = Field(ge=1, le=1000)
    source_id: str = Field(min_length=1, max_length=80)
    source_slide_id: str = Field(min_length=1, max_length=80)


class FullDeckComposerInput(StrictModel):
    composer_version: Literal[COMPOSER_VERSION] = COMPOSER_VERSION
    title: str = Field(min_length=1, max_length=160)
    sources: list[ComposerSource] = Field(min_length=1, max_length=80)
    pages: list[ComposerPage] = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_references(self) -> "FullDeckComposerInput":
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("composer source_id values must be unique")
        slide_ids = [page.slide_id for page in self.pages]
        if len(slide_ids) != len(set(slide_ids)):
            raise ValueError("composed slide_id values must be unique")
        source_numbers = [page.source_slide_number for page in self.pages]
        if len(source_numbers) != len(set(source_numbers)):
            raise ValueError("composed source_slide_number values must be unique")

        source_index = {source.source_id: source.package for source in self.sources}
        for page in self.pages:
            package = source_index.get(page.source_id)
            if package is None:
                raise ValueError(f"composer source does not exist: {page.source_id}")
            declared = next(
                (slide for slide in package.slides if slide.slide_id == page.source_slide_id),
                None,
            )
            if declared is None:
                raise ValueError(
                    f"source slide does not exist: {page.source_id}/{page.source_slide_id}"
                )
            if (
                declared.source_slide_number is not None
                and declared.source_slide_number != page.source_slide_number
            ):
                raise ValueError(
                    "composer page number does not match the source package declaration"
                )
        return self


class ContentGraphResource(StrictModel):
    path: str
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class PageContentGraph(StrictModel):
    graph_version: Literal[PAGE_CONTENT_GRAPH_VERSION] = PAGE_CONTENT_GRAPH_VERSION
    slide_id: str
    document_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    resources: list[ContentGraphResource]
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class CompositionResource(StrictModel):
    source_path: str
    output_path: str
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class CompositionSource(StrictModel):
    source_id: str
    source_package_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    namespace: str
    resources: list[CompositionResource]


class CompositionSlide(StrictModel):
    slot_id: str | None = None
    slide_id: str
    title: str
    source_slide_number: int
    source_id: str
    source_slide_id: str
    source_package_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    document_path: str
    source_slide_content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    composed_slide_content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class CompositionManifest(StrictModel):
    composer_version: Literal[COMPOSER_VERSION] = COMPOSER_VERSION
    page_content_graph_version: Literal[PAGE_CONTENT_GRAPH_VERSION] = PAGE_CONTENT_GRAPH_VERSION
    input_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    title: str
    slide_count: int
    sources: list[CompositionSource]
    slides: list[CompositionSlide]


class FullDeckComposition(StrictModel):
    composer_version: Literal[COMPOSER_VERSION] = COMPOSER_VERSION
    manifest: CompositionManifest
    package: HtmlPptPackage


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _hash_bytes(encoded)


def _file_bytes(package: HtmlPptPackage) -> dict[str, bytes]:
    return {item.path: item.content_bytes() for item in package.files}


def _slide_elements(document: Any) -> list[Any]:
    return [
        element
        for element in document.iter()
        if "slide" in set((element.attrib.get("class") or "").split())
    ]


_URL_WITH_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_CSS_URL = re.compile(r"url\(\s*(['\"]?)([^)'\"]+?)\1\s*\)")
_DECK_CHROME_MARKERS = ("pagenow", "navbtn")


def _is_deck_chrome(element: Any) -> bool:
    """The source deck's own pager, not the per-slide header that shares its class."""

    for node in element.iter():
        if node.attrib.get("id") in _DECK_CHROME_MARKERS:
            return True
        classes = set((node.attrib.get("class") or "").split())
        if classes.intersection(_DECK_CHROME_MARKERS):
            return True
    return False


def _strip_deck_chrome(document: Any) -> None:
    """Remove the source deck's pager bar and its navigation script.

    A composed page lives inside the full-deck shell, which provides its own
    navigation. The source deck's pager (`1 / N · 原稿第 x – y 页`) and its
    key/touch handlers are dead weight there, so they are stripped from every
    single-slide document. Page headers that reuse the `chrome` class but
    contain no pager markers are kept.
    """

    parent_by_child = {child: parent for parent in document.iter() for child in parent}
    removals = []
    for element in document.iter():
        if element.tag == "script":
            text = "".join(element.itertext())
            if any(marker in text for marker in _DECK_CHROME_MARKERS):
                removals.append(element)
            continue
        classes = set((element.attrib.get("class") or "").split())
        if "chrome" in classes and _is_deck_chrome(element):
            removals.append(element)
    for element in removals:
        parent = parent_by_child.get(element)
        if parent is not None:
            parent.remove(element)


def _inline_local_resources(
    document: Any,
    files: dict[str, PackageFile],
    base_dir: str,
) -> None:
    """Inline package-local references so the page is a self-contained file.

    Composed pages render inside sandboxed iframes (`sandbox="allow-scripts"`).
    Opened from `file://`, a sandboxed document has an opaque origin and the
    browser refuses to load its `file://` subresources, so `<img src="img/...">`
    breaks in the exported deck even though the same page works when opened
    directly. Rewriting every package-local reference (binary resources as
    `data:` URIs, scripts and stylesheets as inline blocks) removes the
    dependency on subresource loads under any protocol or sandbox policy.
    """

    def resolve(ref: str) -> PackageFile | None:
        target = ref.strip()
        if (
            not target
            or target.startswith(("/", "#", "//"))
            or _URL_WITH_SCHEME.match(target)
            or "?" in target
            or "#" in target
        ):
            return None
        # References may be raw UTF-8 or percent-encoded; try both forms.
        for candidate in (target, unquote(target)):
            path = posixpath.normpath(posixpath.join(base_dir, candidate))
            if path.startswith("../"):
                continue
            item = files.get(path)
            if item is not None:
                return item
        return None

    def data_uri(ref: str) -> str | None:
        item = resolve(ref)
        if item is None:
            return None
        encoded = base64.b64encode(item.content_bytes()).decode("ascii")
        return f"data:{item.media_type};base64,{encoded}"

    def inline_text(ref: str) -> str | None:
        item = resolve(ref)
        if item is None:
            return None
        try:
            return item.content_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return None

    def rewrite_srcset(value: str) -> str:
        candidates = []
        changed = False
        for candidate in value.split(","):
            parts = candidate.strip().split(None, 1)
            if not parts:
                continue
            url = parts[0]
            descriptor = f" {parts[1]}" if len(parts) == 2 else ""
            inlined = data_uri(url)
            if inlined is not None:
                changed = True
            candidates.append(f"{inlined or url}{descriptor}")
        return ", ".join(candidates) if changed else value

    def rewrite_css(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            url = match.group(2)
            inlined = data_uri(url)
            if inlined is None:
                return match.group(0)
            return f'url("{inlined}")'

        return _CSS_URL.sub(replace, text)

    parent_by_child = {child: parent for parent in document.iter() for child in parent}
    stylesheet_links: list[tuple[Any, str]] = []
    for element in document.iter():
        if element.tag in ("img", "source", "video", "audio", "track"):
            src = element.attrib.get("src")
            if src is not None:
                inlined = data_uri(src)
                if inlined is not None:
                    element.attrib["src"] = inlined
            srcset = element.attrib.get("srcset")
            if srcset is not None:
                element.attrib["srcset"] = rewrite_srcset(srcset)
            poster = element.attrib.get("poster")
            if poster is not None:
                inlined = data_uri(poster)
                if inlined is not None:
                    element.attrib["poster"] = inlined
        elif element.tag == "script":
            src = element.attrib.get("src")
            if src is not None:
                text = inline_text(src)
                if text is not None:
                    element.text = text
                    del element.attrib["src"]
        elif element.tag == "link":
            rel = set((element.attrib.get("rel") or "").split())
            href = element.attrib.get("href")
            if "stylesheet" in rel and href is not None:
                text = inline_text(href)
                if text is not None:
                    stylesheet_links.append((element, text))
        if element.tag == "style" and element.text:
            element.text = rewrite_css(element.text)
        style_attr = element.attrib.get("style")
        if style_attr and "url(" in style_attr:
            element.attrib["style"] = rewrite_css(style_attr)
    for element, text in stylesheet_links:
        parent = parent_by_child.get(element)
        if parent is None:
            continue
        style = ET.Element("style")
        if element.attrib.get("media"):
            style.attrib["media"] = element.attrib["media"]
        style.text = text
        index = list(parent).index(element)
        parent.remove(element)
        parent.insert(index, style)


def _single_slide_document(package: HtmlPptPackage, slide_id: str) -> str:
    files = _file_bytes(package)
    try:
        source = files[package.entrypoint].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FullDeckComposerError("HTML-PPT entrypoint must be UTF-8") from exc

    document = html5lib.parse(source, namespaceHTMLElements=False)
    slides = _slide_elements(document)
    matches = [element for element in slides if element.attrib.get("data-slide-id") == slide_id]
    if len(matches) != 1:
        raise FullDeckComposerError(
            f"source slide must occur exactly once in index.html: {slide_id}"
        )
    selected = matches[0]
    parent_by_child = {child: parent for parent in document.iter() for child in parent}
    for element in slides:
        if element is selected:
            continue
        parent = parent_by_child.get(element)
        if parent is None:
            raise FullDeckComposerError(f"source slide has no removable parent: {slide_id}")
        parent.remove(element)
    if _slide_elements(document) != [selected]:
        raise FullDeckComposerError("source slide elements must not be nested")

    _strip_deck_chrome(document)
    resource_files = {
        item.path: item for item in package.files if item.path != package.entrypoint
    }
    _inline_local_resources(
        document,
        resource_files,
        posixpath.dirname(package.entrypoint),
    )

    serialized = html5lib.serialize(
        document,
        tree="etree",
        alphabetical_attributes=True,
        quote_attr_values="always",
        omit_optional_tags=False,
    )
    return "<!doctype html>" + serialized


def _content_graph(
    *,
    slide_id: str,
    document: bytes,
    resources: list[tuple[str, bytes]],
) -> PageContentGraph:
    resource_models = [
        ContentGraphResource(path=path, content_hash=_hash_bytes(content))
        for path, content in sorted(resources)
    ]
    return _content_graph_from_hashes(
        slide_id=slide_id,
        document=document,
        resources=resource_models,
    )


def _content_graph_from_hashes(
    *,
    slide_id: str,
    document: bytes,
    resources: list[ContentGraphResource],
) -> PageContentGraph:
    document_hash = _hash_bytes(document)
    identity = {
        "graph_version": PAGE_CONTENT_GRAPH_VERSION,
        "slide_id": slide_id,
        "document_hash": document_hash,
        "resources": [item.model_dump() for item in resources],
    }
    return PageContentGraph(
        slide_id=slide_id,
        document_hash=document_hash,
        resources=resources,
        content_hash=_stable_hash(identity),
    )


def normalized_page_content_graph(
    package: HtmlPptPackage,
    slide_id: str,
) -> PageContentGraph:
    """Hash one static slide document plus its conservatively complete resource graph.

    Every non-entrypoint file is included. This intentionally favors false dependencies
    over missing a runtime-loaded local asset; the logical source paths are retained so a
    namespaced copy can be normalized and checked against the same graph.
    """

    document = _single_slide_document(package, slide_id).encode("utf-8")
    resources = [
        (item.path, item.content_bytes())
        for item in package.files
        if item.path != package.entrypoint
    ]
    return _content_graph(slide_id=slide_id, document=document, resources=resources)


def _namespace(package: HtmlPptPackage) -> str:
    package_hash = package.package_hash or package.content_hash()
    return f"sources/{package_hash.removeprefix('sha256:')}"


def _page_document_path(namespace: str, source_slide_id: str) -> str:
    suffix = sha256(source_slide_id.encode("utf-8")).hexdigest()[:12]
    return f"{namespace}/page-{source_slide_id}-{suffix}.html"


def _input_hash(spec: FullDeckComposerInput) -> str:
    return _stable_hash({
        "composer_version": spec.composer_version,
        "title": spec.title,
        "sources": sorted(
            (
                {
                    "source_id": source.source_id,
                    "package_hash": source.package.package_hash,
                }
                for source in spec.sources
            ),
            key=lambda item: item["source_id"],
        ),
        "pages": [page.model_dump() for page in spec.pages],
    })


def _outer_index(spec: FullDeckComposerInput, slides: list[CompositionSlide]) -> str:
    sections = []
    for index, slide in enumerate(slides):
        hidden = "" if index == 0 else " hidden"
        sections.append(
            f'<section class="slide" data-slide-id="{html.escape(slide.slide_id)}"{hidden}>'
            f'<iframe src="{html.escape(slide.document_path)}" '
            f'title="{html.escape(slide.title)}" sandbox="allow-scripts" '
            'referrerpolicy="no-referrer"></iframe></section>'
        )
    return "".join((
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>{html.escape(spec.title)}</title>",
        "<style>",
        "*{box-sizing:border-box}html,body{width:100%;height:100%;margin:0;overflow:hidden;background:#111}",
        ".slide{position:absolute;inset:0}.slide[hidden]{display:block;visibility:hidden;pointer-events:none}.slide iframe{width:100%;height:100%;border:0;background:#fff}",
        ".deck-nav{position:fixed;z-index:10;right:16px;bottom:14px;display:flex;align-items:center;gap:8px;padding:7px 9px;border-radius:999px;background:rgba(17,17,17,.78);color:#fff;font:13px/1.2 system-ui,sans-serif}",
        ".deck-nav button{min-width:44px;min-height:44px;border:1px solid rgba(255,255,255,.35);border-radius:999px;background:#fff;color:#111;font:700 18px/1 system-ui;cursor:pointer}",
        ".deck-nav button:focus-visible{outline:3px solid #6ee7ff;outline-offset:2px}",
        "</style></head><body><main id=\"deck\">",
        "".join(sections),
        "</main><nav class=\"deck-nav\" aria-label=\"幻灯片导航\"><button id=\"prev\" type=\"button\" aria-label=\"上一页\">←</button><output id=\"counter\" aria-live=\"polite\"></output><button id=\"next\" type=\"button\" aria-label=\"下一页\">→</button></nav>",
        "<script>(()=>{",
        "const slides=[...document.querySelectorAll('.slide')],counter=document.querySelector('#counter');",
        "let index=0,touchStart=null;",
        "const announce=()=>{if(parent!==window)parent.postMessage({type:'ppt-agent:slidechange',slide_id:slides[index].dataset.slideId,index},'*')};",
        "const show=next=>{index=(next+slides.length)%slides.length;slides.forEach((slide,itemIndex)=>slide.hidden=itemIndex!==index);counter.textContent=`${index+1} / ${slides.length}`;announce()};",
        "document.querySelector('#prev').addEventListener('click',()=>show(index-1));",
        "document.querySelector('#next').addEventListener('click',()=>show(index+1));",
        "addEventListener('keydown',event=>{if(event.key==='ArrowLeft'||event.key==='PageUp')show(index-1);if(event.key==='ArrowRight'||event.key==='PageDown'||event.key===' ')show(index+1)});",
        "addEventListener('touchstart',event=>{touchStart=event.changedTouches[0].clientX},{passive:true});",
        "addEventListener('touchend',event=>{if(touchStart===null)return;const distance=event.changedTouches[0].clientX-touchStart;if(Math.abs(distance)>40)show(index+(distance<0?1:-1));touchStart=null},{passive:true});",
        "addEventListener('message',event=>{if(event.source!==parent||!event.data||event.data.type!=='ppt-agent:navigate'||typeof event.data.slide_id!=='string')return;const next=slides.findIndex(slide=>slide.dataset.slideId===event.data.slide_id);if(next>=0)show(next)});",
        "show(0)})();</script>",
        "</body></html>",
    ))


def compose_full_deck(spec: FullDeckComposerInput) -> FullDeckComposition:
    """Compose immutable source packages without entering the application workflow."""

    source_by_id = {source.source_id: source.package for source in spec.sources}
    output_files: dict[str, PackageFile] = {}
    source_manifests: list[CompositionSource] = []
    source_graph_resources: dict[str, list[ContentGraphResource]] = {}

    for source in sorted(spec.sources, key=lambda item: item.source_id):
        package = source.package
        namespace = _namespace(package)
        resources: list[CompositionResource] = []
        for item in package.files:
            if item.path == package.entrypoint:
                continue
            output_path = f"{namespace}/{item.path}"
            copied = PackageFile(
                path=output_path,
                content=item.content,
                encoding=item.encoding,
                media_type=item.media_type,
                origin=f"composer:{source.source_id}:{item.path}",
            )
            existing = output_files.get(output_path)
            if existing is not None and existing.content_bytes() != copied.content_bytes():
                raise FullDeckComposerError(f"namespaced resource collision: {output_path}")
            output_files[output_path] = copied
            resources.append(CompositionResource(
                source_path=item.path,
                output_path=output_path,
                content_hash=_hash_bytes(item.content_bytes()),
            ))
        # Content graphs hash resources in path order regardless of how the
        # source package happened to order its file list.
        resources.sort(key=lambda item: item.source_path)
        source_graph_resources[source.source_id] = [
            ContentGraphResource(path=item.source_path, content_hash=item.content_hash)
            for item in resources
        ]
        source_manifests.append(CompositionSource(
            source_id=source.source_id,
            source_package_hash=package.package_hash or package.content_hash(),
            namespace=namespace,
            resources=resources,
        ))

    slide_manifests: list[CompositionSlide] = []
    for page in spec.pages:
        package = source_by_id[page.source_id]
        namespace = _namespace(package)
        document = _single_slide_document(package, page.source_slide_id)
        source_graph = _content_graph_from_hashes(
            slide_id=page.source_slide_id,
            document=document.encode("utf-8"),
            resources=source_graph_resources[page.source_id],
        )
        document_path = _page_document_path(namespace, page.source_slide_id)
        page_file = PackageFile(
            path=document_path,
            content=document,
            encoding="utf-8",
            media_type="text/html; charset=utf-8",
            origin=f"composer:{page.source_id}:{page.source_slide_id}",
        )
        existing = output_files.get(document_path)
        if existing is not None and existing.content_bytes() != page_file.content_bytes():
            raise FullDeckComposerError(f"namespaced page collision: {document_path}")
        output_files[document_path] = page_file

        composed_graph = _content_graph_from_hashes(
            slide_id=page.source_slide_id,
            document=output_files[document_path].content_bytes(),
            resources=source_graph_resources[page.source_id],
        )
        if source_graph.content_hash != composed_graph.content_hash:
            raise FullDeckComposerError(
                f"page content graph changed during composition: {page.source_slide_id}"
            )
        slide_manifests.append(CompositionSlide(
            slot_id=page.slot_id,
            slide_id=page.slide_id,
            title=page.title,
            source_slide_number=page.source_slide_number,
            source_id=page.source_id,
            source_slide_id=page.source_slide_id,
            source_package_hash=package.package_hash or package.content_hash(),
            document_path=document_path,
            source_slide_content_hash=source_graph.content_hash,
            composed_slide_content_hash=composed_graph.content_hash,
        ))

    manifest = CompositionManifest(
        input_hash=_input_hash(spec),
        title=spec.title,
        slide_count=len(spec.pages),
        sources=source_manifests,
        slides=slide_manifests,
    )
    output_files["composition_manifest.json"] = PackageFile(
        path="composition_manifest.json",
        content=json.dumps(
            manifest.model_dump(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
        media_type="application/json; charset=utf-8",
        origin="composer:manifest",
    )
    output_files["index.html"] = PackageFile(
        path="index.html",
        content=_outer_index(spec, slide_manifests),
        encoding="utf-8",
        media_type="text/html; charset=utf-8",
        origin="composer:shell",
    )
    package = HtmlPptPackage(
        title=spec.title,
        slide_count=len(spec.pages),
        slides=[
            PackageSlide(
                slide_id=page.slide_id,
                title=page.title,
                source_slide_number=page.source_slide_number,
            )
            for page in spec.pages
        ],
        files=[deepcopy(item) for _, item in sorted(output_files.items())],
    )
    return FullDeckComposition(manifest=manifest, package=package)


def compose_full_deck_revision(
    *,
    title: str,
    parent_package: FullDeckPackage,
    replacement_sources: list[ComposerSource],
    replacement_pages: list[ComposerPage],
    ordered_pages: list[ComposerPage],
) -> FullDeckComposition:
    """Replace declared pages while carrying unchanged parent files byte-for-byte."""

    parent_manifest = CompositionManifest.model_validate(
        parent_package.composition_manifest
    )
    replacement = compose_full_deck(FullDeckComposerInput(
        title=title,
        sources=replacement_sources,
        pages=replacement_pages,
    ))
    parent_slide_by_slot = {
        slide.slot_id: slide for slide in parent_manifest.slides
    }
    parent_slots = set(parent_slide_by_slot)
    if (
        None in parent_slots
        or len(parent_slide_by_slot) != len(parent_manifest.slides)
        or parent_manifest.slide_count != len(parent_manifest.slides)
    ):
        raise FullDeckComposerError("parent composition has invalid page identities")
    replacement_slide_by_slot = {
        slide.slot_id: slide for slide in replacement.manifest.slides
    }
    replacement_slots = set(replacement_slide_by_slot)
    if not replacement_slots or replacement_slots != {
        page.slot_id for page in replacement_pages
    }:
        raise FullDeckComposerError("replacement pages do not match their manifest")
    ordered_slots = [page.slot_id for page in ordered_pages]
    if (
        None in ordered_slots
        or len(ordered_slots) != len(set(ordered_slots))
        or set(ordered_slots) != parent_slots
        or not replacement_slots.issubset(parent_slots)
    ):
        raise FullDeckComposerError(
            "revision must preserve the parent's complete ordered page set"
        )

    slides: list[CompositionSlide] = []
    for page in ordered_pages:
        source = (
            replacement_slide_by_slot.get(page.slot_id)
            if page.slot_id in replacement_slots
            else parent_slide_by_slot.get(page.slot_id)
        )
        if source is None:
            raise FullDeckComposerError(
                f"ordered page is absent from parent and replacement: {page.slot_id}"
            )
        if (
            source.slide_id != page.slide_id
            or source.title != page.title
            or source.source_slide_number != page.source_slide_number
        ):
            raise FullDeckComposerError(
                f"ordered page metadata changed during revision: {page.slot_id}"
            )
        slides.append(source)

    parent_source_ids = {
        slide.source_id
        for slide in slides
        if slide.slot_id not in replacement_slots
    }
    replacement_source_ids = {
        slide.source_id
        for slide in slides
        if slide.slot_id in replacement_slots
    }
    if parent_source_ids.intersection(replacement_source_ids):
        raise FullDeckComposerError("parent and replacement source IDs must be disjoint")
    sources = [
        source
        for source in parent_manifest.sources
        if source.source_id in parent_source_ids
    ] + [
        source
        for source in replacement.manifest.sources
        if source.source_id in replacement_source_ids
    ]
    if {source.source_id for source in sources} != (
        parent_source_ids | replacement_source_ids
    ):
        raise FullDeckComposerError("revision source manifest is incomplete")

    parent_files = {item.path: item for item in parent_package.files}
    replacement_files = {item.path: item for item in replacement.package.files}
    output_files: dict[str, PackageFile] = {}

    def copy_file(
        path: str,
        candidates: dict[str, PackageFile],
        expected_hash: str | None = None,
    ) -> PackageFile:
        item = candidates.get(path)
        if item is None:
            raise FullDeckComposerError(f"revision source file is missing: {path}")
        if expected_hash is not None and _hash_bytes(item.content_bytes()) != expected_hash:
            raise FullDeckComposerError(f"revision source file hash changed: {path}")
        existing = output_files.get(path)
        if existing is not None and existing.content_bytes() != item.content_bytes():
            raise FullDeckComposerError(f"revision file collision: {path}")
        output_files[path] = deepcopy(item)
        return item

    for source in parent_manifest.sources:
        if source.source_id not in parent_source_ids:
            continue
        for resource in source.resources:
            copy_file(resource.output_path, parent_files, resource.content_hash)
    for source in replacement.manifest.sources:
        if source.source_id not in replacement_source_ids:
            continue
        for resource in source.resources:
            copy_file(resource.output_path, replacement_files, resource.content_hash)
    source_by_id = {source.source_id: source for source in sources}
    for slide in slides:
        document = copy_file(
            slide.document_path,
            replacement_files if slide.slot_id in replacement_slots else parent_files,
        )
        if slide.source_slide_content_hash != slide.composed_slide_content_hash:
            raise FullDeckComposerError(
                f"revision page content graph is not faithful: {slide.slot_id}"
            )
        source = source_by_id[slide.source_id]
        graph = _content_graph_from_hashes(
            slide_id=slide.source_slide_id,
            document=document.content_bytes(),
            resources=[
                ContentGraphResource(
                    path=resource.source_path,
                    content_hash=resource.content_hash,
                )
                for resource in source.resources
            ],
        )
        if graph.content_hash != slide.composed_slide_content_hash:
            raise FullDeckComposerError(
                f"revision page content graph changed: {slide.slot_id}"
            )

    manifest = CompositionManifest(
        input_hash=_stable_hash({
            "composer_version": COMPOSER_VERSION,
            "operation": "compose_full_deck_revision",
            "title": title,
            "parent_package_hash": parent_package.package_hash,
            "replacement_input_hash": replacement.manifest.input_hash,
            "pages": [page.model_dump() for page in ordered_pages],
        }),
        title=title,
        slide_count=len(slides),
        sources=sources,
        slides=slides,
    )
    output_files["composition_manifest.json"] = PackageFile(
        path="composition_manifest.json",
        content=json.dumps(
            manifest.model_dump(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        media_type="application/json; charset=utf-8",
        origin="composer:manifest",
    )
    output_files["index.html"] = PackageFile(
        path="index.html",
        content=_outer_index(FullDeckComposerInput(
            title=title,
            sources=replacement_sources,
            pages=replacement_pages,
        ), slides),
        media_type="text/html; charset=utf-8",
        origin="composer:shell",
    )
    package = HtmlPptPackage(
        title=title,
        slide_count=len(ordered_pages),
        slides=[PackageSlide(
            slide_id=page.slide_id,
            title=page.title,
            source_slide_number=page.source_slide_number,
        ) for page in ordered_pages],
        files=[deepcopy(item) for _, item in sorted(output_files.items())],
    )
    return FullDeckComposition(manifest=manifest, package=package)
