"""Keep the opt-in HTML overlay experiment self-contained and safe."""

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "experiments" / "bilibili-overlay"


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "id":
                self.ids.add(value)


def test_overlay_element_ids_and_origin_isolation():
    html = (ROOT / "index.html").read_text()
    js = (ROOT / "overlay.js").read_text()
    tree = Elements()
    tree.feed(html)
    references = set(re.findall(r'\$\("([a-z0-9-]+)"\)', js))
    assert references <= tree.ids
    assert {
        "video",
        "media-file",
        "media-url",
        "timeline-file",
        "plan-file",
        "offset",
        "line-0",
        "line-1",
    } <= tree.ids
    assert "overlaySnapshot" in js and "video.currentTime" in js
    assert "createObjectURL" in js and "revokeObjectURL" in js
    assert 'document.createElement("span")' in js and "textContent=text" in js
    # No site privileges, signed TV token or fetch to remote media from JS.
    for forbidden in (
        "/api/tv/",
        "vc_admin",
        "vc_guest",
        "X-Real-IP",
        "fetch(",
        "https://api.bilibili.com",
    ):
        assert forbidden not in js
    assert "bilivideo.com" not in html
