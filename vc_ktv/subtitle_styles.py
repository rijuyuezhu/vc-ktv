"""Small per-event KTV subtitle presets using the upstream ASS Style directly."""

from typing import Literal

from ktv_app.timeline import Style

StylePreset = Literal["classic", "large", "compact"]
DEFAULT_PRESET: StylePreset = "classic"

# Deliberately fixed choices: no second ASS renderer or general style editor.
_PRESETS = {
    "classic": dict(mode="double", size=48, position=0.86, outline=3),
    "large": dict(
        mode="double", size=62, position=0.84, outline=5, highlight="#FFE45E"
    ),
    "compact": dict(mode="single", size=38, position=0.91, outline=2),
}


def preset_style(preset: StylePreset) -> Style:
    return Style(
        font="Noto Sans CJK SC",
        auto_complete=True,
        **_PRESETS[preset],
    )
