from __future__ import annotations

import html
import re
from collections.abc import Iterable
from xml.etree.ElementTree import Element

import html5lib
import tinycss2


HTML_NAMESPACE = "http://www.w3.org/1999/xhtml"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"
XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
XMLNS_NAMESPACE = "http://www.w3.org/2000/xmlns/"
SANITIZER_VERSION = "sample-html-v2"

# Passive semantic, layout, media, and form-control elements are safe inside the
# scriptless sandbox used by the preview. Browsing contexts, plugin containers,
# executable markup, and document URL controls intentionally remain absent.
HTML_TAGS = {
    "a", "abbr", "address", "area", "article", "aside", "audio", "b", "bdi", "bdo",
    "blockquote", "br", "button", "canvas", "caption", "cite", "code", "col",
    "colgroup", "data", "datalist", "dd", "del", "details", "dfn", "dialog", "div",
    "dl", "dt", "em", "fieldset", "figcaption", "figure", "footer", "form", "h1",
    "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "i", "img", "input",
    "ins", "kbd", "label", "legend", "li", "main", "map", "mark", "menu", "meter",
    "nav", "ol", "optgroup", "option", "output", "p", "picture", "pre", "progress",
    "q", "rp", "rt", "ruby", "s", "samp", "search", "section", "select", "small",
    "source", "span", "strong", "sub", "summary", "sup", "table", "tbody", "td",
    "textarea", "tfoot", "th", "thead", "time", "tr", "track", "u", "ul", "var",
    "video", "wbr",
}
SVG_TAGS = {
    "a", "circle", "clipPath", "defs", "desc", "ellipse", "feBlend", "feColorMatrix",
    "feComponentTransfer", "feComposite", "feConvolveMatrix", "feDiffuseLighting",
    "feDisplacementMap", "feDistantLight", "feDropShadow", "feFlood", "feFuncA",
    "feFuncB", "feFuncG", "feFuncR", "feGaussianBlur", "feImage", "feMerge",
    "feMergeNode", "feMorphology", "feOffset", "fePointLight", "feSpecularLighting",
    "feSpotLight", "feTile", "feTurbulence", "filter", "g", "image", "line",
    "linearGradient", "marker", "mask", "path", "pattern", "polygon", "polyline",
    "radialGradient", "rect", "stop", "style", "svg", "switch", "symbol", "text",
    "textPath", "title", "tspan", "use", "view",
}
VOID_HTML_TAGS = {"area", "br", "col", "hr", "img", "input", "source", "track", "wbr"}

BLOCKED_ATTRIBUTES = {
    "action", "formaction", "ping", "srcdoc", "srcset",
}
URL_ATTRIBUTES = {"background", "href", "manifest", "poster", "src", "usemap"}
SVG_URL_VALUE_ATTRIBUTES = {
    "clip-path", "cursor", "fill", "filter", "marker-end", "marker-mid", "marker-start",
    "mask", "stroke",
}

# CSS property names are not an execution boundary. Accepting standard and vendor
# display properties avoids a permanently incomplete allowlist; values and rule
# types below enforce the actual script/network boundary. These legacy properties
# are the exceptions because historical engines treated them as executable hooks.
BLOCKED_CSS_PROPERTIES = {"behavior", "-ms-behavior", "-moz-binding"}
BLOCKED_CSS_FUNCTIONS = {"expression"}
RESOURCE_STRING_FUNCTIONS = {
    "cross-fade", "image", "image-set", "src", "-webkit-cross-fade", "-webkit-image-set",
}
NESTED_RULE_AT_RULES = {"container", "layer", "media", "scope", "starting-style", "supports"}
DECLARATION_AT_RULES = {"counter-style", "font-face", "page", "property"}
KEYFRAMES_AT_RULES = {"keyframes", "-webkit-keyframes"}

_SAFE_FRAGMENT = re.compile(r"#[A-Za-z_][A-Za-z0-9_.:-]*")
_SAFE_DATA_RESOURCE = re.compile(
    r"data:(?:"
    r"image/(?:avif|gif|jpeg|png|webp)"
    r"|audio/(?:aac|mpeg|ogg|wav|webm)"
    r"|video/(?:mp4|ogg|webm)"
    r"|font/(?:otf|ttf|woff|woff2)"
    r"|application/(?:font-woff|font-sfnt)"
    r"|text/vtt"
    r");base64,[A-Za-z0-9+/]*={0,2}",
    re.IGNORECASE,
)
_CSS_PROPERTY = re.compile(r"(?:--|-{0,2}[a-z])[a-z0-9-]*")
_STYLE_END = re.compile(r"</\s*style", re.IGNORECASE)


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
    return bool(_SAFE_FRAGMENT.fullmatch(candidate) or _SAFE_DATA_RESOURCE.fullmatch(candidate))


def _css_url(function: object) -> str:
    arguments = [token for token in function.arguments if token.type != "whitespace"]
    if len(arguments) != 1 or arguments[0].type != "string":
        raise SampleHtmlError("CSS url() must contain one quoted local value")
    return arguments[0].value


def _ensure_safe_css_serialization(value: str) -> str:
    if _STYLE_END.search(value):
        raise SampleHtmlError("unsafe CSS serialization")
    return value


def _validate_resource_function_strings(tokens: Iterable[object], *, depth: int = 0) -> None:
    if depth > 32:
        raise SampleHtmlError("CSS nesting limit exceeded")
    for token in tokens:
        if token.type == "string" and not _is_safe_url(token.value):
            raise SampleHtmlError("external CSS resource")
        nested = getattr(token, "content", None)
        if nested is not None:
            _validate_resource_function_strings(nested, depth=depth + 1)
        if token.type == "function":
            _validate_resource_function_strings(token.arguments, depth=depth + 1)


def _validate_css_values(tokens: Iterable[object], *, depth: int = 0) -> None:
    if depth > 32:
        raise SampleHtmlError("CSS nesting limit exceeded")
    for token in tokens:
        if token.type in {"at-keyword", "bad-url", "error"}:
            raise SampleHtmlError(f"unsupported CSS token: {token.type}")
        if token.type == "url":
            if not _is_safe_url(token.value):
                raise SampleHtmlError("external CSS URL")
            continue
        if token.type == "function":
            name = token.lower_name
            if name in BLOCKED_CSS_FUNCTIONS:
                raise SampleHtmlError(f"unsafe CSS function: {name}")
            if name == "url":
                if not _is_safe_url(_css_url(token)):
                    raise SampleHtmlError("external CSS URL")
            else:
                if name in RESOURCE_STRING_FUNCTIONS:
                    _validate_resource_function_strings(token.arguments)
                _validate_css_values(token.arguments, depth=depth + 1)
            continue
        content = getattr(token, "content", None)
        if content is not None:
            _validate_css_values(content, depth=depth + 1)


def _sanitize_declarations(tokens: Iterable[object]) -> str:
    declarations: list[str] = []
    for declaration in tokens:
        if declaration.type != "declaration":
            raise SampleHtmlError(f"unsupported CSS item: {declaration.type}")
        name = declaration.lower_name
        if not _CSS_PROPERTY.fullmatch(name) or name in BLOCKED_CSS_PROPERTIES:
            raise SampleHtmlError(f"unsafe CSS declaration: {name}")
        _validate_css_values(declaration.value)
        value = _ensure_safe_css_serialization(tinycss2.serialize(declaration.value).strip())
        important = " !important" if declaration.important else ""
        declarations.append(f"{name}:{value}{important}")
    return ";".join(declarations)


def _sanitize_style_attribute(value: str) -> str:
    tokens = tinycss2.parse_declaration_list(value, skip_comments=True, skip_whitespace=True)
    return _sanitize_declarations(tokens)


def _sanitize_block_contents(tokens: Iterable[object], *, depth: int) -> str:
    if depth > 16:
        raise SampleHtmlError("CSS rule nesting limit exceeded")
    items = tinycss2.parse_blocks_contents(tokens, skip_comments=True, skip_whitespace=True)
    sanitized: list[str] = []
    for item in items:
        if item.type == "declaration":
            sanitized.append(_sanitize_declarations([item]) + ";")
        elif item.type == "qualified-rule":
            sanitized.append(_sanitize_qualified_rule(item, depth=depth + 1))
        elif item.type == "at-rule":
            sanitized.append(_sanitize_at_rule(item, depth=depth + 1))
        else:
            raise SampleHtmlError(f"unsupported CSS block item: {item.type}")
    return "".join(sanitized)


def _sanitize_qualified_rule(rule: object, *, depth: int) -> str:
    if depth > 16:
        raise SampleHtmlError("CSS rule nesting limit exceeded")
    _validate_css_values(rule.prelude)
    selector = _ensure_safe_css_serialization(tinycss2.serialize(rule.prelude).strip())
    if not selector:
        raise SampleHtmlError("invalid CSS selector")
    return f"{selector}{{{_sanitize_block_contents(rule.content, depth=depth)}}}"


def _sanitize_at_rule(rule: object, *, depth: int) -> str:
    if depth > 16:
        raise SampleHtmlError("CSS rule nesting limit exceeded")
    name = rule.lower_at_keyword
    _validate_css_values(rule.prelude)
    prelude = _ensure_safe_css_serialization(tinycss2.serialize(rule.prelude).strip())
    header = f"@{name}" + (f" {prelude}" if prelude else "")
    if rule.content is None:
        if name == "layer" and prelude:
            return header + ";"
        raise SampleHtmlError(f"CSS at-rule requires a block: {name}")
    if name in NESTED_RULE_AT_RULES or name in KEYFRAMES_AT_RULES:
        nested = tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True)
        return f"{header}{{{_sanitize_rules(nested, depth=depth + 1)}}}"
    if name in DECLARATION_AT_RULES:
        declarations = tinycss2.parse_declaration_list(
            rule.content, skip_comments=True, skip_whitespace=True
        )
        return f"{header}{{{_sanitize_declarations(declarations)}}}"
    raise SampleHtmlError(f"CSS at-rule is not allowed: {name}")


def _sanitize_rules(rules: Iterable[object], *, depth: int = 0) -> str:
    if depth > 16:
        raise SampleHtmlError("CSS rule nesting limit exceeded")
    sanitized: list[str] = []
    for rule in rules:
        if rule.type == "qualified-rule":
            sanitized.append(_sanitize_qualified_rule(rule, depth=depth))
        elif rule.type == "at-rule":
            sanitized.append(_sanitize_at_rule(rule, depth=depth))
        else:
            raise SampleHtmlError(f"unsupported CSS rule: {rule.type}")
    return "".join(sanitized)


def _sanitize_stylesheet(value: str) -> str:
    rules = tinycss2.parse_stylesheet(value, skip_comments=True, skip_whitespace=True)
    return _sanitize_rules(rules)


def _sanitize_attribute(
    namespace: str, tag: str, raw_name: str, value: str
) -> tuple[str, str] | None:
    attribute_namespace, name = _qualified_name(raw_name)
    if attribute_namespace == XMLNS_NAMESPACE:
        standard_namespace = (
            tag == "svg"
            and (
                (name == "xmlns" and value == SVG_NAMESPACE)
                or (name == "xlink" and value == XLINK_NAMESPACE)
            )
        )
        if standard_namespace:
            return None
        raise SampleHtmlError("unsupported namespace declaration")
    if attribute_namespace == XLINK_NAMESPACE:
        if namespace != SVG_NAMESPACE or name != "href":
            raise SampleHtmlError("unsupported XLink attribute")
        name = "href"
    elif attribute_namespace == XML_NAMESPACE:
        if name not in {"lang", "space"}:
            raise SampleHtmlError("unsupported XML attribute")
        return f"xml:{name}", value
    elif attribute_namespace:
        raise SampleHtmlError("namespaced attributes are not allowed")

    normalized = name.lower()
    if normalized.startswith("on"):
        raise SampleHtmlError(f"event attribute is not allowed: {name}")
    if normalized in BLOCKED_ATTRIBUTES:
        raise SampleHtmlError(f"unsafe attribute: {name}")
    if normalized == "style":
        value = _sanitize_style_attribute(value)
    elif normalized in URL_ATTRIBUTES:
        if not _is_safe_url(value):
            raise SampleHtmlError("external resource attribute")
    elif namespace == SVG_NAMESPACE and normalized in SVG_URL_VALUE_ATTRIBUTES:
        tokens = tinycss2.parse_component_value_list(value, skip_comments=True)
        _validate_css_values(tokens)
        value = _ensure_safe_css_serialization(tinycss2.serialize(tokens).strip())
    return name, value


def _serialize_attributes(namespace: str, tag: str, values: dict[str, str]) -> str:
    attributes: dict[str, str] = {}
    for name, value in values.items():
        attribute = _sanitize_attribute(namespace, tag, name, value)
        if attribute is None:
            continue
        rendered_name, rendered_value = attribute
        if rendered_name in attributes:
            raise SampleHtmlError(f"duplicate normalized attribute: {rendered_name}")
        attributes[rendered_name] = rendered_value
    return "".join(
        f' {name}="{html.escape(value, quote=True)}"' for name, value in attributes.items()
    )


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

    rendered_attributes = _serialize_attributes(namespace, tag, element.attrib)
    if namespace == HTML_NAMESPACE and tag in VOID_HTML_TAGS:
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


def _serialize_meta(element: Element) -> str:
    parsed_attributes = [(*_qualified_name(name), value) for name, value in element.attrib.items()]
    if any(attribute_namespace for attribute_namespace, _, _ in parsed_attributes):
        raise SampleHtmlError("namespaced meta attributes are not allowed")
    attributes = {name.lower(): value for _, name, value in parsed_attributes}
    charset = attributes.keys() == {"charset"} and attributes["charset"].lower() == "utf-8"
    named = (
        attributes.keys() == {"content", "name"}
        and attributes["name"].lower() in {"color-scheme", "theme-color", "viewport"}
    )
    if charset:
        return '<meta charset="utf-8">'
    if named:
        return (
            f'<meta name="{html.escape(attributes["name"].lower(), quote=True)}" '
            f'content="{html.escape(attributes["content"], quote=True)}">'
        )
    raise SampleHtmlError("unsupported meta tag")


def sanitize_sample_html(value: str) -> str:
    """Return a passive, self-contained HTML document from untrusted model markup."""

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

    html_attributes = _serialize_attributes(HTML_NAMESPACE, "html", document.attrib)
    if head.attrib:
        raise SampleHtmlError("head attributes are not allowed")
    body_attributes = _serialize_attributes(HTML_NAMESPACE, "body", body.attrib)

    head_output: list[str] = []
    has_renderable_style = False
    for node in head:
        namespace, tag = _qualified_name(node.tag)
        if not namespace:
            continue
        if (namespace, tag) == (HTML_NAMESPACE, "style"):
            head_output.append(_serialize_element(node))
            has_renderable_style = True
        elif (namespace, tag) == (HTML_NAMESPACE, "meta"):
            head_output.append(_serialize_meta(node))
        elif (namespace, tag) == (HTML_NAMESPACE, "title"):
            if node.attrib or list(node):
                raise SampleHtmlError("title attributes or child markup are not allowed")
            head_output.append(f"<title>{html.escape(node.text or '', quote=False)}</title>")
        else:
            raise SampleHtmlError(f"unsupported head tag: {tag}")

    body_output = _serialize_children(body)
    if not body_output.strip() and not has_renderable_style:
        raise SampleHtmlError("sample HTML has no renderable content")
    return (
        f"<!doctype html><html{html_attributes}><head>{''.join(head_output)}</head>"
        f"<body{body_attributes}>{body_output}</body></html>"
    )
