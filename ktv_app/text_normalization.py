"""Source lyric cleanup and conservative pronunciation normalization."""

import re
import unicodedata

# One-character Cyrillic/Latin lookalikes, restricted to mixed Latin words.
# Unicode confusables: https://www.unicode.org/Public/security/latest/confusables.txt
CYRILLIC_LOOKALIKES = dict(zip("аеорсхуіјѕАВСЕНКМОРТХІЈЅ", "aeopcxyijsABCEHKMOPTXIJS"))
MIXED_WORD = re.compile(r"[A-Za-z\u0400-\u052f]+")


def strip_lyric_timestamps(text):
    text = re.sub(r"\[\d+:\d+(?:[.,:]\d+)*\]", "", text)
    return re.sub(r"<\d+:\d+(?:[.,:]\d+)*>", "", text)


# Only remove confident production credit keys in the opening header.
# Once the first sung line appears, even a lyric beginning with "作曲：" is kept.
CREDIT_FIELD = (
    r"(?:作词|作詞|作曲|编曲|編曲|词曲|詞曲|填词|填詞|词|詞|曲|歌|歌手|"
    r"演唱|制作人|製作人|制作|製作|策划|策劃|混音|母带|母帶|"
    r"调校|調校|调教|調教|画师|畫師|曲绘|曲繪|"
    r"和声|和聲|封面|插画|插畫|动画|動畫|视频|視頻|"
    r"Lyrics|Composer|Arrangement|Mixing|Mastering|Vocal|Illustration|PV|MV)"
)
LEADING_CREDIT = re.compile(
    rf"^{CREDIT_FIELD}(?:\s*[/／&+＋]\s*{CREDIT_FIELD})*\s*[:：]\s*\S",
    re.I,
)


def clean_lyric_text(text, *, keep_leading_credits=False):
    """Strip source timing, and optionally confident *leading* credits only.

    Preserve source order, repeats, and lyric lines after the first verse.
    """
    lines = []
    started = False
    for raw in text.replace("\r", "").split("\n"):
        display = strip_lyric_timestamps(raw)
        display = re.sub(
            r"\[(?:ar|ti|al|by|offset|length):[^\]]*\]", "", display, flags=re.I
        )
        display = re.sub(
            r"\[(?:verse|chorus|intro|outro|bridge|instrumental|副歌|主歌|前奏|间奏)(?:\s+\d+)?\]",
            "",
            display,
            flags=re.I,
        ).strip()
        if not display:
            continue
        if not started and LEADING_CREDIT.match(display):
            if keep_leading_credits:
                lines.append(display)
            continue
        started = True
        lines.append(display)
    return "\n".join(lines)


def repair_mixed_latin(text):
    changes = []

    def replace(match):
        word = match.group()
        latin_count = sum(c.isascii() and c.isalpha() for c in word)
        if not latin_count or latin_count < len(word) - latin_count:
            return word
        if not all(c.isascii() or c in CYRILLIC_LOOKALIKES for c in word):
            return word
        fixed = "".join(CYRILLIC_LOOKALIKES.get(c, c) for c in word)
        for i, (before, after) in enumerate(zip(word, fixed)):
            if before != after:
                changes.append(
                    {
                        "normalized_index": match.start() + i,
                        "original": before,
                        "replacement": after,
                        "codepoint": f"U+{ord(before):04X}",
                        "name": unicodedata.name(before),
                        "word": word,
                        "pronunciation_word": fixed,
                    }
                )
        return fixed

    return MIXED_WORD.sub(replace, text), changes


def normalize_lyrics(text):
    return repair_mixed_latin(unicodedata.normalize("NFKC", text))


def alignment_display_map(text):
    normalized, source_positions = [], []
    for index, char in enumerate(text):
        expanded = unicodedata.normalize("NFKC", char)
        normalized.extend(expanded)
        source_positions.extend([index] * len(expanded))
    repaired, _ = repair_mixed_latin("".join(normalized))
    chars, positions = [], []
    for char, index in zip(repaired, source_positions):
        for folded in char.casefold():
            if folded.isalnum():
                chars.append(folded)
                positions.append(index)
    return "".join(chars), positions
