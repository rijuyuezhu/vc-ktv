"""Lyric preprocessing copied from upstream; VC KTV retains its own lyric search.

Source: Volta233/AutoASSrenderer4karaOK@39cedb3 ktv_app/lyrics.py.
The online lyric retrieval in the upstream module is intentionally not vendored.
"""

import re
import uuid

from .text_normalization import clean_lyric_text, normalize_lyrics
from .timeline import Line, canonical


def language_of(text, default="zh"):
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\u3400-\u9fff]", text):
        return default if default == "ja" else "zh"
    return "en"


def contextual_languages(texts):
    """Use predominantly Japanese lyrics to disambiguate kanji-only lines.

    Kana is positive evidence; isolated Japanese phrases in bilingual lyrics
    are insufficient. Explicit user language choices never call this helper.
    """
    languages = [language_of(text) for text in texts]
    japanese = languages.count("ja")
    cjk = japanese + languages.count("zh")
    if japanese >= 2 and japanese / max(1, cjk) >= 0.7:
        return ["ja" if language == "zh" else language for language in languages]
    return languages


def preprocess(text, language="auto", *, confirmed=False):
    lines = []
    # A selected/edited candidate is authoritative. Do not silently strip
    # credits the user deliberately restored during candidate selection.
    cleaned = clean_lyric_text(text, keep_leading_credits=confirmed)
    for index, display in enumerate(cleaned.split("\n")):
        normalized, normalization_changes = normalize_lyrics(display)
        if not canonical(normalized):
            continue
        lines.append(
            Line(
                id=uuid.uuid4().hex[:12],
                text=display,
                normalized=normalized,
                language=language_of(display) if language == "auto" else language,
                source_line=index,
                normalization_changes=normalization_changes,
            )
        )
    if language == "auto":
        for line, inferred in zip(lines, contextual_languages(l.text for l in lines)):
            line.language = inferred
    if not lines:
        raise ValueError("没有可对齐的歌词，请粘贴文本或导入 TXT/LRC")
    return lines
