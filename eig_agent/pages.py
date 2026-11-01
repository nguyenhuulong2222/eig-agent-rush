"""Visible text of an HTML page (stdlib html.parser), for model input and quote checks."""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser

_SKIP = {"script", "style", "noscript", "template", "head"}
_BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "br", "section", "main",
          "header", "footer", "nav", "table", "ul", "ol", "article"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in _SKIP and self.skip:
            self.skip -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def visible_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def squash(text: str) -> str:
    """Lowercase letters and digits only (any script), for tolerant substring checks."""
    text = unicodedata.normalize("NFC", text).casefold()
    return "".join(ch for ch in text if ch.isalnum())
