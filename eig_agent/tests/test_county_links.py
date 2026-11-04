"""County site links: no dead .example hrefs, every local link resolves, visible text unchanged."""

import os
import re
import unittest
from html.parser import HTMLParser
from urllib.parse import urlsplit

from eig_agent.pages import visible_text

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PUBLIC = os.path.join(ROOT, "public")
FIXTURES = os.path.join(ROOT, "eig_agent", "fixtures", "i18n")
STUBS = ("polling-places.html", "drop-boxes.html", "register.html", "registration-status.html")


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ("href", "src") and value:
                self.links.append(value)


def county_pages():
    for dirpath, _dirs, names in os.walk(PUBLIC):
        if os.path.relpath(dirpath, PUBLIC).split(os.sep)[0] == "data":
            continue
        for name in names:
            if name.endswith(".html"):
                yield os.path.join(dirpath, name)


def links(path):
    parser = _Links()
    with open(path, encoding="utf-8") as fh:
        parser.feed(fh.read())
    return parser.links


class CountyLinksTest(unittest.TestCase):
    def test_no_county_page_links_to_a_dot_example_host(self):
        bad = [(os.path.relpath(p, PUBLIC), u) for p in county_pages() for u in links(p)
               if (urlsplit(u).hostname or "").endswith(".example")]
        self.assertEqual(bad, [])

    def test_every_local_link_resolves_to_a_file(self):
        missing = []
        for page in county_pages():
            for url in links(page):
                parts = urlsplit(url)
                if parts.scheme or parts.netloc or url.startswith(("#", "mailto:", "tel:")):
                    continue
                target = os.path.normpath(os.path.join(os.path.dirname(page), parts.path))
                if parts.path.startswith("data/") or "/data/" in parts.path:
                    continue  # results.json is built into the site at deploy time
                if not target.startswith(PUBLIC + os.sep) or not os.path.isfile(target):
                    missing.append((os.path.relpath(page, PUBLIC), url))
        self.assertEqual(missing, [])

    def test_language_pages_show_exactly_the_fixture_text(self):
        # The multilingual check compares visible text (incl. the shown domains); only hrefs changed.
        for lang in ("en", "vi", "es", "tl"):
            with open(os.path.join(PUBLIC, "i18n", f"voter-info.{lang}.html"), encoding="utf-8") as fh:
                served = visible_text(fh.read())
            with open(os.path.join(FIXTURES, f"voter-info.{lang}.html"), encoding="utf-8") as fh:
                fixture = visible_text(fh.read())
            self.assertEqual(served, fixture, lang)

    def test_drift_fixture_still_shows_the_lookalike_domain(self):
        import json
        with open(os.path.join(FIXTURES, "scenarios.json"), encoding="utf-8") as fh:
            drift = json.load(fh)["drift"]["replace"]["vi"][0][1]
        self.assertIn("elections.kanaloa-county.example", drift)

    def test_stub_pages_exist_and_say_they_are_fictional(self):
        for name in STUBS:
            with open(os.path.join(PUBLIC, name), encoding="utf-8") as fh:
                html = fh.read()
            self.assertIn("Fictional county", html, name)
            self.assertIn('href="voter-info.html"', html, name)
            self.assertRegex(html, r"form-action 'none'", name)
            self.assertFalse(re.search(r"<form\b", html), name)

    def test_shown_official_domains_are_unchanged(self):
        with open(os.path.join(PUBLIC, "voter-info.html"), encoding="utf-8") as fh:
            text = visible_text(fh.read())
        self.assertEqual(text.count("elections.kanaloacounty.example"), 2)
        self.assertEqual(text.count("register.kanaloacounty.example"), 2)


if __name__ == "__main__":
    unittest.main()
