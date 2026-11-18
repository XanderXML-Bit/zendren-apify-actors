"""HTML -> plain text helper (stdlib only)."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "br", "tr", "table", "ul", "ol", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "header", "footer",
}
_SKIP_TAGS = {"script", "style", "head"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(value: str | None) -> str:
    """Strip HTML tags and entities, keeping paragraph/list line breaks.

    Greenhouse returns its ``content`` HTML entity-escaped (``&lt;div&gt;``);
    that is detected and unescaped first.
    """
    if not value:
        return ""
    if "<" not in value and "&lt;" in value:
        value = html.unescape(value)
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
