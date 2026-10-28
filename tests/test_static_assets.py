from pathlib import Path


def test_no_escaped_javascript_template_delimiters():
    script = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    assert "\\`" not in script
    assert "\\${" not in script


def test_admin_has_stable_subpage_navigation():
    static = Path(__file__).resolve().parents[1] / "static"
    html = (static / "index.html").read_text()
    script = (static / "app.js").read_text()

    assert 'href="/admin/"' in html
    assert 'href="/"' in html
    assert 'role="tablist"' not in html
    assert "window.location.pathname" in script
    assert "showPage();" in script
    assert "showTab(" not in script


def test_ui_uses_light_indigo_palette():
    static = Path(__file__).resolve().parents[1] / "static"
    css = (static / "style.css").read_text()
    assert "--bg: #f6f7f9" in css
    assert "--surface: #ffffff" in css
    assert "--accent: #4f5bd5" in css
    assert "color-scheme: light" in css
    assert "focus-visible" in css
