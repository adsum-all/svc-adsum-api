"""Allowlist HTML sanitiser for rich descriptions (activities and collaboration
cards).

The description is authored in a rich-text editor (bold, italic, underline,
strikethrough, headings, lists, quote, code, links and inline images). We store and
later render it as HTML, so we MUST strip anything that could execute (scripts,
event handlers, javascript: URLs). A tag/attribute allowlist built on the standard
library HTML parser is used rather than a fragile regex: unknown tags are dropped,
disallowed attributes removed, links keep only safe schemes and images keep only
data:image or https sources (never a script-capable scheme).
"""
from __future__ import annotations

import html
from html.parser import HTMLParser

# Formatting tags a description may contain. Everything else is removed (its text
# content is kept). ``img`` and ``hr`` are void (self-closing).
_ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "s", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "a", "span", "div",
    "code", "pre", "hr", "img",
}
_VOID = {"br", "hr", "img"}
# "rel" is deliberately not accepted from the input: a canonical rel is always
# emitted for links (see _attrs), so accepting it too would duplicate the attribute.
_ALLOWED_ATTRS = {"a": {"href", "title", "target"}, "img": {"src", "alt"}}
_SAFE_URL_SCHEMES = ("http://", "https://", "mailto:", "tel:")
# Inline images are limited to embedded data-image payloads or https URLs, so no
# javascript:/data:text/ vector can ride in through the src.
_SAFE_IMG_SCHEMES = ("data:image/", "https://")


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        allowed = _ALLOWED_ATTRS.get(tag, set())
        parts: list[str] = []
        for name, value in attrs:
            key = name.lower()
            if key not in allowed or key.startswith("on"):
                continue
            val = value or ""
            if key == "href" and not val.lower().startswith(_SAFE_URL_SCHEMES):
                continue
            if key == "src" and not val.lower().startswith(_SAFE_IMG_SCHEMES):
                continue
            safe = val.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
            parts.append(f'{key}="{safe}"')
        if tag == "a":
            parts.append('rel="noopener noreferrer nofollow"')
        return (" " + " ".join(parts)) if parts else ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t not in _ALLOWED_TAGS:
            return
        self.out.append(f"<{t}{self._attrs(t, attrs)}{' /' if t in _VOID else ''}>")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _ALLOWED_TAGS and t not in _VOID:
            self.out.append(f"</{t}>")

    def handle_data(self, data: str) -> None:
        # convert_charrefs=True already decoded entities, so re-escape & as well as
        # < > (html.escape covers all three; quote=False keeps text nodes readable).
        self.out.append(html.escape(data, quote=False))


def sanitize_html(value: str | None) -> str | None:
    """Return a safe HTML string keeping only the allowlisted tags/attributes, or
    None for empty input."""
    if not value or not value.strip():
        return None
    parser = _Sanitizer()
    parser.feed(value)
    parser.close()
    cleaned = "".join(parser.out).strip()
    return cleaned or None
