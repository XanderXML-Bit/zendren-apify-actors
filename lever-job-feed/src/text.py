"""HTML -> plain text / Markdown / list items (stdlib only)."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "br", "tr", "table", "ul", "ol", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "header", "footer",
}
_SKIP_TAGS = {"script", "style", "head"}


def _tidy(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class _Converter(HTMLParser):
    """One walker for text and Markdown output (``markdown=True`` adds the markup)."""

    def __init__(self, markdown: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.md = markdown
        self.parts: list[str] = []
        self._skip = 0
        self._links: list[str | None] = []
        self._ol: list[int] = []  # open ordered lists: next number

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag == "ol":
            self._ol.append(1)
        elif tag == "ul":
            self._ol.append(0)
        if tag == "li":
            if self._ol and self._ol[-1] > 0:
                self.parts.append(f"\n{self._ol[-1]}. ")
                self._ol[-1] += 1
            else:
                self.parts.append("\n- ")
        elif self.md and tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif self.md and tag in ("strong", "b"):
            self.parts.append("**")
        elif self.md and tag in ("em", "i"):
            self.parts.append("*")
        elif self.md and tag == "a":
            href = dict(attrs).get("href")
            self._links.append(href)
            self.parts.append("[")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag in ("ul", "ol") and self._ol:
            self._ol.pop()
        if self.md and tag in ("strong", "b"):
            self.parts.append("**")
        elif self.md and tag in ("em", "i"):
            self.parts.append("*")
        elif self.md and tag == "a" and self._links:
            href = self._links.pop()
            self.parts.append(f"]({href})" if href else "]")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _convert(value: str | None, markdown: bool) -> str:
    if not value:
        return ""
    if "<" not in value and "&lt;" in value:
        value = html.unescape(value)
    parser = _Converter(markdown)
    parser.feed(value)
    parser.close()
    text = _tidy("".join(parser.parts))
    if markdown:
        text = re.sub(r"\*\*\s*\*\*", "", text)  # empty bold left by <strong> </strong>
    return text


def html_to_text(value: str | None) -> str:
    """Strip tags/entities, keeping paragraph and list line breaks."""
    return _convert(value, markdown=False)


def html_to_markdown(value: str | None) -> str:
    """Small HTML -> Markdown: headings, bold/italic, links, bullet and numbered lists."""
    return _convert(value, markdown=True)


class _ItemExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[str] = []
        self.saw_li = False
        self._depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.saw_li = True
            if self._depth == 0:
                self._buf = []
            else:
                self._buf.append(" ")
            self._depth += 1
        elif self._depth and tag in ("br", "p", "div", "ul", "ol"):
            self._buf.append(" ")

    def handle_endtag(self, tag):
        if tag == "li" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                item = re.sub(r"\s+", " ", "".join(self._buf).replace("\xa0", " ")).strip()
                if item:
                    self.items.append(item)

    def handle_data(self, data):
        if self._depth:
            self._buf.append(data)


def list_items(content: str | None) -> list[str]:
    """Plain-text items of one Lever list section.

    Lever sends the items as ``<li>`` elements (usually without a wrapping ``ul``).
    Content without any ``<li>`` falls back to one item per text line."""
    if not content:
        return []
    parser = _ItemExtractor()
    parser.feed(content)
    parser.close()
    if parser.items or parser.saw_li:
        return parser.items
    return [line.strip() for line in html_to_text(content).split("\n") if line.strip()]
