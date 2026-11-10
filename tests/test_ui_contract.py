"""Check that JavaScript DOM selectors actually reference elements in the HTML."""

import re
from html.parser import HTMLParser
from pathlib import Path


class IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == "id":
                assert value not in self.ids, f"Duplicate HTML ID: {value}"
                self.ids.add(value)


def test_guest_and_admin_dom_references_exist():
    root = Path(__file__).resolve().parents[1] / "static"
    document = IdCollector()
    document.feed((root / "index.html").read_text())
    scripts = "\n".join(
        p.read_text()
        for p in root.glob("*.js")
        if p.name not in ("tv.js", "tv-overlay.js")
    )
    referenced = set(re.findall(r'\$\(\s*"([a-z0-9-]+)"\s*\)', scripts))
    # The application creates these sections dynamically.
    runtime_pages = {"page-settings", "page-played", "page-song"}
    assert referenced <= document.ids | runtime_pages, sorted(
        referenced - document.ids - runtime_pages
    )


def test_tv_dom_references_exist():
    root = Path(__file__).resolve().parents[1] / "static"
    document = IdCollector()
    document.feed((root / "tv.html").read_text())
    script = (root / "tv.js").read_text()
    referenced = set(re.findall(r'getElementById\(\s*"([a-z0-9-]+)"\s*\)', script))
    assert referenced <= document.ids, sorted(referenced - document.ids)
