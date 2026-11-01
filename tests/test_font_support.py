"""Font checks must be explicit but never depend on the CI host's fonts."""

import json

from ktv_app import font_support
from ktv_app.subtitles import write_bundle
from ktv_app.timeline import Style, Timeline


def test_installed_ranges_include_all_available_fallback_fonts(monkeypatch):
    def query(*args):
        if args[0] == "fc-match":
            return "Noto Sans CJK SC\n"
        if args[0] == "fc-list":
            return "Noto Sans CJK SC|20-7e 4e00-4e9f 1f3b5\nNoto Sans CJK JP|3041-309f\nNoto Color Emoji|1f600\n"
        raise AssertionError(args)

    monkeypatch.setattr(font_support, "_fontconfig", query)
    font_support._installed_ranges.cache_clear()
    result = font_support.font_diagnostics("Noto Sans CJK SC", ["中あ🎵😀\u200b\ufe0f"])
    assert result["warnings"] == [
        "ASS 可用字体缺少 1 种歌词字符，可能显示为方框：😀(U+1F600)"
    ]
    assert result["missing_glyph_count"] == 1
    assert result["missing_glyphs"] == ["U+1F600"]
    assert result["fontconfig_checked"] is True
    font_support._installed_ranges.cache_clear()


def test_missing_primary_font_and_missing_fontconfig_are_reported(monkeypatch):
    monkeypatch.setattr(
        font_support,
        "_fontconfig",
        lambda *args: (
            "DejaVu Sans\n" if args[0] == "fc-match" else "DejaVu Sans|20-7e\n"
        ),
    )
    font_support._installed_ranges.cache_clear()
    result = font_support.font_diagnostics("Noto Sans CJK SC", ["Hello"])
    assert "未安装" in result["warnings"][0]
    assert "DejaVu Sans" in result["warnings"][0]
    assert result["missing_glyph_count"] == 0
    monkeypatch.setattr(font_support, "_fontconfig", lambda *args: None)
    font_support._installed_ranges.cache_clear()
    result = font_support.font_diagnostics("Noto Sans CJK SC", ["甲"])
    assert result["fontconfig_checked"] is False
    assert len(result["warnings"]) == 2
    assert result["missing_glyph_count"] == 0  # Unknown is not 'covered'.
    font_support._installed_ranges.cache_clear()


def test_export_report_records_font_warnings_without_changing_subtitles(
    monkeypatch, tmp_path
):
    def fake_font_check(font, texts):
        assert texts == ["甲😀"]
        return {
            "requested_font": font,
            "fontconfig_checked": True,
            "missing_glyph_count": 1,
            "missing_glyphs": ["U+1F600"],
            "warnings": ["系统字体缺少 1 种歌词字符"],
        }

    monkeypatch.setattr("ktv_app.subtitles.font_diagnostics", fake_font_check)
    write_bundle(
        Timeline(duration=10, lines=[{"id": "one", "text": "甲😀"}]), Style(), tmp_path
    )
    report = json.loads((tmp_path / "export_report.json").read_text())
    assert report["font_diagnostics"]["missing_glyphs"] == ["U+1F600"]
    assert {"reason": "系统字体缺少 1 种歌词字符", "source": "fontconfig"} in report[
        "warnings"
    ]
    assert "甲😀" in (tmp_path / "timeline.json").read_text()


def test_startup_checks_primary_font_without_stopping_service(
    monkeypatch, tmp_path, caplog
):
    from fastapi.testclient import TestClient

    from vc_ktv.app import create_app

    monkeypatch.setattr(
        font_support, "primary_font_warning", lambda font: "测试主字体缺失"
    )
    app = create_app(
        root=tmp_path / "data",
        password="test-password",
        secret=b"font-test-key",
        auto_prepare=True,
    )
    with caplog.at_level("WARNING"), TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
    assert "KTV subtitle font: 测试主字体缺失" in caplog.text
