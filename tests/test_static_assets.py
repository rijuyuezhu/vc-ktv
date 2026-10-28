from pathlib import Path


def test_no_escaped_javascript_template_delimiters():
    script = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    assert "\\`" not in script
    assert "\\${" not in script
