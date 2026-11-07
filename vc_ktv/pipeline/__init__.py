"""Stable integration boundary for the vendored subtitle engine.

Do not import implementation modules from ktv_app elsewhere in VC-KTV.
The engine itself remains pinned and independently tested under ktv_app/.
"""

from ktv_app import font_support
from ktv_app.aligners.scat_pronunciation import UNKNOWN_LATIN, pronunciation_lyrics
from ktv_app.lyric_names import title_variants
from ktv_app.media import probe, run
from ktv_app.rendering import RenderOptions, capped_h264_args, render
from ktv_app.subtitles import write_bundle
from ktv_app.text_normalization import LEADING_CREDIT, clean_lyric_text
from ktv_app.timeline import Line, Style, Timeline, Unit
from vc_ktv.infra.config import pipeline_settings
from vc_ktv.processing.upstream_pipeline import make_karaoke_video


def primary_font_warning(font: str) -> str | None:
    """Look up fonts at call time so environment overrides remain effective."""
    return font_support.primary_font_warning(font)


__all__ = [
    "LEADING_CREDIT",
    "Line",
    "RenderOptions",
    "Style",
    "Timeline",
    "UNKNOWN_LATIN",
    "Unit",
    "capped_h264_args",
    "clean_lyric_text",
    "make_karaoke_video",
    "pipeline_settings",
    "primary_font_warning",
    "probe",
    "pronunciation_lyrics",
    "render",
    "run",
    "title_variants",
    "write_bundle",
]
