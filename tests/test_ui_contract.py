"""Catch broken JavaScript selectors without pinning page copy or styling."""

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


def test_guest_and_admin_js_element_references_exist():
    root = Path(__file__).resolve().parents[1] / "static"
    document = IdCollector()
    document.feed((root / "index.html").read_text())
    js = (root / "app.js").read_text()
    referenced = set(re.findall(r'\$\("([a-z0-9-]+)"\)', js))
    # These pages are built once by setupSubpages rather than present in HTML.
    runtime_pages = {"page-settings", "page-played", "page-song"}
    assert referenced <= document.ids | runtime_pages, sorted(
        referenced - document.ids - runtime_pages
    )


def test_app_ui_omits_middle_dot_separator():
    root = Path(__file__).resolve().parents[1] / "static"
    for file in ("index.html", "app.js", "tv.html", "tv.js"):
        assert "·" not in (root / file).read_text()


def test_tv_control_panel_selectors_exist():
    root = Path(__file__).resolve().parents[1] / "static"
    document = IdCollector()
    document.feed((root / "tv.html").read_text())
    js = (root / "tv.js").read_text()
    referenced = set(re.findall(r'getElementById\("([a-z0-9-]+)"\)', js))
    assert referenced <= document.ids, sorted(referenced - document.ids)
