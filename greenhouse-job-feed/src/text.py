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
    text = re.sub(r"(?m)^(- [^\n]*)\n\n(?=- )", r"\1\n", text)  # no blank lines between list items
    return text.strip()


def unescape_content(value: str | None) -> str:
    """Greenhouse sends ``content`` HTML-escaped (``&lt;p&gt;``); return real HTML."""
    if not value:
        return ""
    if "<" not in value and "&lt;" in value:
        return html.unescape(value)
    return value


class _MarkdownExtractor(HTMLParser):
    _HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    _BLOCKS = {"p", "div", "section", "article", "blockquote", "table", "tr", "header", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._lists: list[list] = []   # stack of ["ul"|"ol", counter]
        self._open: list[tuple[str, int, str | None]] = []  # (marker, index in parts, href)

    def _close(self, marker: str, href: str | None = None) -> None:
        """Close an emphasis/link span: whitespace moves outside the markers, an empty span vanishes."""
        for pos in range(len(self._open) - 1, -1, -1):
            if self._open[pos][0] == marker:
                _, idx, _ = self._open.pop(pos)
                break
        else:
            return
        inner = "".join(self.parts[idx:])
        del self.parts[idx:]
        core = inner.strip()
        if not core:
            self.parts.append(inner)
            return
        lead, trail = inner[: len(inner) - len(inner.lstrip())], inner[len(inner.rstrip()):]
        if marker == "a":
            self.parts.append(f"{lead}[{core}]({href}){trail}" if href else inner)
        else:
            self.parts.append(f"{lead}{marker}{core}{marker}{trail}")

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif self._skip:
            return
        elif tag in self._HEADINGS:
            self.parts.append("\n\n" + "#" * self._HEADINGS[tag] + " ")
        elif tag in ("strong", "b"):
            self._open.append(("**", len(self.parts), None))
        elif tag in ("em", "i"):
            self._open.append(("*", len(self.parts), None))
        elif tag == "a":
            self._open.append(("a", len(self.parts), a.get("href")))
        elif tag in ("ul", "ol"):
            self._lists.append([tag, 0])
            if len(self._lists) == 1:
                self.parts.append("\n")
        elif tag == "li":
            indent = "  " * max(0, len(self._lists) - 1)
            if self._lists and self._lists[-1][0] == "ol":
                self._lists[-1][1] += 1
                bullet = f"{self._lists[-1][1]}. "
            else:
                bullet = "- "
            self.parts.append(f"\n{indent}{bullet}")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "hr":
            self.parts.append("\n\n---\n\n")
        elif tag in self._BLOCKS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif self._skip:
            return
        elif tag in self._HEADINGS:
            self.parts.append("\n\n")
        elif tag in ("strong", "b"):
            self._close("**")
        elif tag in ("em", "i"):
            self._close("*")
        elif tag == "a":
            href = next((h for m, _, h in reversed(self._open) if m == "a"), None)
            self._close("a", href)
        elif tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            if not self._lists:
                self.parts.append("\n\n")
        elif tag in self._BLOCKS:
            self.parts.append("\n\n")

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr"):
            self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(re.sub(r"\s+", " ", data))  # HTML whitespace semantics


def html_to_markdown(value: str | None) -> str:
    """Convert (possibly entity-escaped) HTML to light Markdown: headings, bold/italic,
    links, lists and paragraphs. Anything else is reduced to its text."""
    if not value:
        return ""
    parser = _MarkdownExtractor()
    parser.feed(unescape_content(value))
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"(?<=\S) {2,}", " ", text)
    text = re.sub(r" +\n", "\n", text)
    text = re.sub(r"\n (?=\S)", "\n", text)   # stray single space (list indents are 2+)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
