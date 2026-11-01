"""Small HTML scanner built on html.parser: tags with attributes and line numbers."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional


@dataclass
class Tag:
    name: str
    attrs: dict
    line: int
    text: str = ""  # inline content, only collected for <script> and <style>


@dataclass
class HtmlDoc:
    tags: list = field(default_factory=list)

    def find(self, name: str) -> list:
        return [t for t in self.tags if t.name == name]

    def meta_http_equiv(self, header: str) -> Optional[Tag]:
        for t in self.find("meta"):
            if (t.attrs.get("http-equiv") or "").lower() == header.lower():
                return t
        return None

    def meta_name(self, name: str) -> Optional[Tag]:
        for t in self.find("meta"):
            if (t.attrs.get("name") or "").lower() == name.lower():
                return t
        return None


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = HtmlDoc()
        self._capture: Optional[Tag] = None

    def handle_starttag(self, tag, attrs):
        line, _ = self.getpos()
        # Keep attribute names lowercase; a bare attribute (no value) becomes "".
        node = Tag(tag.lower(), {k.lower(): (v if v is not None else "") for k, v in attrs}, line)
        self.doc.tags.append(node)
        if node.name in ("script", "style"):
            self._capture = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self._capture = None

    def handle_endtag(self, tag):
        if self._capture is not None and tag.lower() == self._capture.name:
            self._capture = None

    def handle_data(self, data):
        if self._capture is not None:
            self._capture.text += data


def parse_html(source: str) -> HtmlDoc:
    parser = _Parser()
    parser.feed(source)
    parser.close()
    return parser.doc
