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


def test_tv_now_next_banner_has_no_popup():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "tv.html").read_text()
    js = (root / "tv.js").read_text()
    assert 'id="song-banner"' in html
    assert 'id="next-up-row"' in html
    assert 'id="next-up"' in html
    assert 'id="next-tip"' not in html
    assert "nextTip" not in js
    assert "updateNextSong(queue[0])" in js


def test_tv_banner_transparency_control_exists():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "tv.html").read_text()
    js = (root / "tv.js").read_text()
    assert 'id="banner-transparency"' in html
    assert 'id="banner-transparency-value"' in html
    assert "background:#151b2780" in html
    assert "background:#111b29eb" in html  # Expanded panel unchanged.
    assert "var(--banner-opacity,.85)" in html
    assert "vcktv-banner-transparency" in js


def test_provider_timing_help_explains_same_recording_and_no_word_sweep():
    js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
    assert "沿用来源逐句时间轴" in js
    for warning in (
        "网易云音乐",
        "同一音源",
        "演唱会",
        "改编版",
        "逐字渐变",
        "等分估算",
        "普通歌词对齐",
    ):
        assert warning in js


def test_netease_line_review_dialog_is_wired_with_signed_row_selection():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text()
    js = (root / "app.js").read_text()
    css = (root / "style.css").read_text()
    for key in (
        "netease-review-dialog",
        "netease-review-lines",
        "netease-review-count",
        "netease-review-align",
        "netease-review-timed",
        "netease-review-back",
    ):
        assert f'id="{key}"' in html
    assert "openNeteaseReview(candidate,useLyrics)" in js
    assert "selected_lines:" in js and "selection_ticket:" in js
    assert ".netease-review-line.kept" in css
    assert "background: #fff0a8" in css
