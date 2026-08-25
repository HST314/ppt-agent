from __future__ import annotations

import html
import re
from collections.abc import Iterable
from xml.etree.ElementTree import Element

import html5lib
import tinycss2


HTML_NAMESPACE = "http://www.w3.org/1999/xhtml"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"

HTML_TAGS = {
    "a", "abbr", "article", "aside", "b", "bdi", "bdo", "blockquote", "br",
    "caption", "cite", "code", "dd", "del", "details", "div", "dl", "dt", "em",
    "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "i", "img", "kbd", "li", "main", "mark", "nav", "ol", "p", "pre", "q",
    "s", "section", "small", "span", "strong", "sub", "summary", "sup", "table", "tbody",
    "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
SVG_TAGS = {
    "circle", "clipPath", "defs", "ellipse", "g", "line", "linearGradient", "path",
    "pattern", "polygon", "polyline", "radialGradient", "rect", "stop", "svg", "text", "tspan",
}
VOID_TAGS = {"br", "hr", "img"}

GLOBAL_ATTRIBUTES = {"class", "dir", "id", "lang", "role", "style", "title"}
HTML_ATTRIBUTES = {
    "a": {"href"},
    "img": {"alt", "height", "src", "width"},
    "li": {"value"},
    "ol": {"reversed", "start", "type"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
}
SVG_ATTRIBUTES = {
    "class", "clip-path", "cx", "cy", "d", "dominant-baseline", "dx", "dy", "fill",
    "fill-opacity", "font-family", "font-size", "font-weight", "height", "id", "offset",
    "opacity", "pathLength", "points", "preserveAspectRatio", "r", "role", "rx", "ry",
    "stop-color", "stop-opacity", "stroke", "stroke-dasharray", "stroke-dashoffset",
    "stroke-linecap", "stroke-linejoin", "stroke-opacity", "stroke-width", "style",
    "text-anchor", "transform", "vector-effect", "viewBox", "width", "x", "x1", "x2", "y",
    "y1", "y2",
}

CSS_PROPERTIES = {
    "--accent", "--background", "--border", "--foreground", "--font-body", "--font-display",
    "--muted", "--shadow", "--surface", "align-content", "align-items", "align-self",
    "aspect-ratio", "background", "background-color", "background-image", "background-position",
    "background-repeat", "background-size", "border", "border-bottom", "border-color",
    "border-left", "border-radius", "border-right", "border-style", "border-top", "border-width",
    "bottom", "box-shadow", "box-sizing", "clip-path", "color", "column-gap", "columns",
    "display", "fill", "fill-opacity", "filter", "flex", "flex-basis", "flex-direction",
    "flex-flow", "flex-grow", "flex-shrink", "flex-wrap", "font", "font-family", "font-size",
    "font-stretch", "font-style", "font-variant", "font-weight", "gap", "grid", "grid-area",
    "grid-auto-columns", "grid-auto-flow", "grid-auto-rows", "grid-column", "grid-column-end",
    "grid-column-start", "grid-row", "grid-row-end", "grid-row-start", "grid-template",
    "grid-template-areas", "grid-template-columns", "grid-template-rows", "height", "inset",
    "justify-content", "justify-items", "justify-self", "left", "letter-spacing", "line-height",
    "list-style", "margin", "margin-bottom", "margin-left", "margin-right", "margin-top",
    "max-height", "max-width", "min-height", "min-width", "object-fit", "object-position",
    "opacity", "order", "outline", "overflow", "overflow-wrap", "overflow-x", "overflow-y",
    "padding", "padding-bottom", "padding-left", "padding-right", "padding-top", "place-content",
    "place-items", "position", "right", "row-gap", "stroke", "stroke-dasharray",
    "stroke-dashoffset", "stroke-linecap", "stroke-linejoin", "stroke-opacity", "stroke-width",
    "table-layout", "text-align", "text-decoration", "text-overflow", "text-shadow",
    "text-transform", "top", "transform", "transform-origin", "vertical-align", "visibility",
    "white-space", "width", "word-break", "word-spacing", "z-index",
}
CSS_FUNCTIONS = {
    "blur", "brightness", "calc", "circle", "clamp", "conic-gradient", "contrast",
    "drop-shadow", "ellipse", "grayscale", "hsl", "hsla", "hue-rotate", "inset", "invert",
    "linear-gradient", "matrix", "matrix3d", "max", "min", "opacity", "perspective", "polygon",
    "radial-gradient", "repeating-conic-gradient", "repeating-linear-gradient",
    "repeating-radial-gradient", "rgb", "rgba", "rotate", "rotate3d", "rotatex", "rotatey",
    "rotatez", "saturate", "scale", "scale3d", "scalex", "scaley", "scalez", "sepia",
    "skew", "skewx", "skewy", "translate", "translate3d", "translatex", "translatey",
    "translatez", "var",
}

_SAFE_DATA_IMAGE = re.compile(
    r"data:image/(?:gif|jpeg|png|webp);base64,[A-Za-z0-9+/]*={0,2}",
    re.IGNORECASE,
)
_SAFE_FRAGMENT = re.compile(r"#[A-Za-z_][A-Za-z0-9_.:-]*")


class SampleHtmlError(ValueError):
    pass


def _qualified_name(name: object) -> tuple[str, str]:
    if not isinstance(name, str):
        return "", ""
    if name.startswith("{"):
        namespace, local = name[1:].split("}", 1)
        return namespace, local
    return "", name


def _is_safe_url(value: str) -> bool:
    candidate = value.strip()
    return bool(_SAFE_FRAGMENT.fullmatch(candidate) or _SAFE_DATA_IMAGE.fullmatch(candidate))


def _css_url(function: object) -> str:
    arguments = [token for token in function.arguments if token.type != "whitespace"]
    if len(arguments) != 1 or arguments[0].type != "string":
        raise SampleHtmlError("CSS url() must contain one quoted local value")
    return arguments[0].value


def _validate_css_values(tokens: Iterable[object], *, depth: int = 0) -> None:
    if depth > 32:
        raise SampleHtmlError("CSS nesting limit exceeded")
    for token in tokens:
        if token.type in {"at-keyword", "bad-url", "error"}:
            raise SampleHtmlError("unsupported CSS token")
        if token.type == "url":
            if not _is_safe_url(token.value):
                raise SampleHtmlError("external CSS URL")
            continue
        if token.type == "function":
            name = token.lower_name
            if name == "url":
                if not _is_safe_url(_css_url(token)):
                    raise SampleHtmlError("external CSS URL")
            elif name not in CSS_FUNCTIONS:
                raise SampleHtmlError(f"unsupported CSS function: {name}")
            else:
                _validate_css_values(token.arguments, depth=depth + 1)
            continue
        content = getattr(token, "content", None)
        if content is not None:
            _validate_css_values(content, depth=depth + 1)


def _sanitize_declarations(tokens: Iterable[object]) -> str:
    declarations: list[str] = []
    for declaration in tokens:
        if declaration.type != "declaration" or declaration.lower_name not in CSS_PROPERTIES:
            raise SampleHtmlError("unsupported CSS declaration")
        _validate_css_values(declaration.value)
        value = tinycss2.serialize(declaration.value).strip()
        if "<" in value:
            raise SampleHtmlError("unsafe CSS serialization")
        important = " !important" if declaration.important else ""
        declarations.append(f"{declaration.lower_name}:{value}{important}")
    return ";".join(declarations)


def _sanitize_style_attribute(value: str) -> str:
    tokens = tinycss2.parse_declaration_list(value, skip_comments=True, skip_whitespace=True)
    return _sanitize_declarations(tokens)


def _sanitize_stylesheet(value: str) -> str:
    rules = tinycss2.parse_stylesheet(value, skip_comments=True, skip_whitespace=True)
    sanitized: list[str] = []
    for rule in rules:
        if rule.type != "qualified-rule":
            raise SampleHtmlError("CSS at-rules are not allowed")
        _validate_css_values(rule.prelude)
        selector = tinycss2.serialize(rule.prelude).strip()
        if not selector or "<" in selector:
            raise SampleHtmlError("invalid CSS selector")
        declarations = tinycss2.parse_declaration_list(
            rule.content, skip_comments=True, skip_whitespace=True
        )
        sanitized.append(f"{selector}{{{_sanitize_declarations(declarations)}}}")
    return "".join(sanitized)


def _sanitize_attribute(namespace: str, tag: str, raw_name: str, value: str) -> tuple[str, str]:
    attribute_namespace, name = _qualified_name(raw_name)
    if attribute_namespace:
        raise SampleHtmlError("namespaced attributes are not allowed")
    allowed = (
        SVG_ATTRIBUTES
        if namespace == SVG_NAMESPACE
        else GLOBAL_ATTRIBUTES | HTML_ATTRIBUTES.get(tag, set())
    )
    if name not in allowed and not (namespace == HTML_NAMESPACE and name.startswith("aria-")):
        raise SampleHtmlError(f"unsupported attribute: {name}")
    if name == "style":
        value = _sanitize_style_attribute(value)
    elif name in {"href", "src"} and not _is_safe_url(value):
        raise SampleHtmlError("external resource attribute")
    elif namespace == SVG_NAMESPACE and name in {"clip-path", "fill", "stroke"}:
        tokens = tinycss2.parse_component_value_list(value, skip_comments=True)
        _validate_css_values(tokens)
        value = tinycss2.serialize(tokens).strip()
        if "<" in value:
            raise SampleHtmlError("unsafe SVG paint serialization")
    return name, value


def _serialize_element(element: Element, *, depth: int = 0) -> str:
    if depth > 64:
        raise SampleHtmlError("HTML nesting limit exceeded")
    namespace, tag = _qualified_name(element.tag)
    if namespace == HTML_NAMESPACE:
        if tag not in HTML_TAGS and tag != "style":
            raise SampleHtmlError(f"unsupported HTML tag: {tag}")
    elif namespace == SVG_NAMESPACE:
        if tag not in SVG_TAGS:
            raise SampleHtmlError(f"unsupported SVG tag: {tag}")
    else:
        raise SampleHtmlError("unsupported markup namespace")

    attributes = [
        _sanitize_attribute(namespace, tag, name, value)
        for name, value in element.attrib.items()
    ]
    rendered_attributes = "".join(
        f' {name}="{html.escape(value, quote=True)}"' for name, value in attributes
    )
    if tag in VOID_TAGS:
        if list(element):
            raise SampleHtmlError("void HTML tag has children")
        return f"<{tag}{rendered_attributes}>"
    if tag == "style":
        if list(element):
            raise SampleHtmlError("style tag has child markup")
        content = _sanitize_stylesheet(element.text or "")
    else:
        content = html.escape(element.text or "", quote=False)
        for child in element:
            if isinstance(child.tag, str):
                content += _serialize_element(child, depth=depth + 1)
            content += html.escape(child.tail or "", quote=False)
    return f"<{tag}{rendered_attributes}>{content}</{tag}>"


def _serialize_children(element: Element) -> str:
    output = html.escape(element.text or "", quote=False)
    for child in element:
        if isinstance(child.tag, str):
            output += _serialize_element(child)
        output += html.escape(child.tail or "", quote=False)
    return output


def sanitize_sample_html(value: str) -> str:
    """Parse untrusted model HTML and return an allowlisted body fragment."""

    parser = html5lib.HTMLParser(tree=html5lib.getTreeBuilder("etree"))
    document = parser.parse(value)
    head = next(
        (node for node in document if _qualified_name(node.tag) == (HTML_NAMESPACE, "head")),
        None,
    )
    body = next(
        (node for node in document if _qualified_name(node.tag) == (HTML_NAMESPACE, "body")),
        None,
    )
    if head is None or body is None:
        raise SampleHtmlError("HTML document could not be parsed")
    document_attributes = {_qualified_name(name) for name in document.attrib}
    if not document_attributes <= {("", "dir"), ("", "lang")} or head.attrib or body.attrib:
        raise SampleHtmlError("document wrapper attributes are not allowed")

    head_output: list[str] = []
    for node in head:
        namespace, tag = _qualified_name(node.tag)
        if not namespace:
            continue
        if (namespace, tag) == (HTML_NAMESPACE, "style"):
            head_output.append(_serialize_element(node))
        elif (namespace, tag) == (HTML_NAMESPACE, "meta"):
            parsed_attributes = [
                (*_qualified_name(name), value) for name, value in node.attrib.items()
            ]
            if any(attribute_namespace for attribute_namespace, _, _ in parsed_attributes):
                raise SampleHtmlError("namespaced meta attributes are not allowed")
            attributes = {name: value for _, name, value in parsed_attributes}
            charset = attributes.keys() == {"charset"} and attributes["charset"].lower() == "utf-8"
            viewport = (
                attributes.keys() == {"content", "name"}
                and attributes["name"].lower() == "viewport"
            )
            if not charset and not viewport:
                raise SampleHtmlError("unsupported meta tag")
        elif (namespace, tag) == (HTML_NAMESPACE, "title"):
            if node.attrib:
                raise SampleHtmlError("title attributes are not allowed")
        else:
            raise SampleHtmlError(f"unsupported head tag: {tag}")

    fragment = "".join(head_output) + _serialize_children(body)
    if not fragment.strip():
        raise SampleHtmlError("sample HTML has no renderable content")
    return fragment.strip()
