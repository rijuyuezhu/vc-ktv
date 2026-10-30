"""Source-preserving vocalized syllable readings from VC KTV PR #62."""

import re

SCAT_READINGS = {
    "du": "嘟",
    "bi": "比",
    "ba": "巴",
    "pa": "啪",
    "mi": "咪",
    "da": "哒",
    "la": "啦",
    "ha": "哈",
    "aa": "啊",
    "ah": "啊",
    "oh": "哦",
    "na": "呐",
    "ya": "呀",
    "wu": "呜",
    "woo": "呜",
    "ooh": "呜",
}
SCAT_PART = re.compile(
    "|".join(sorted(SCAT_READINGS, key=len, reverse=True)), re.IGNORECASE
)
SCAT_WORD = re.compile(
    r"(?<![A-Za-z])(?:" + SCAT_PART.pattern + r"){2,}(?![A-Za-z])",
    re.IGNORECASE,
)
UNKNOWN_LATIN = re.compile(r"[A-Za-z]+")
SCAT_PREFIX = re.compile(
    r"^(?:(?:ha|ah|aa|la|oh|woo|ooh|wu|na|ya)\b[\s~]*){1,8}"
    r"(?=[\u3400-\u9fff])",
    re.IGNORECASE,
)


def pronunciation_lyrics(lyrics: str, *, unknown_vocables: bool = False):
    """Explicit Mandarin pronunciation hints with a source-character mapping.

    Translate only known scat syllables, never ordinary English words. All
    displayed text remains in its original form, including repeated refrains.
    """
    lines = [line.strip() for line in lyrics.splitlines() if line.strip()]
    spoken, maps, changes = [], [], []
    for line_no, display in enumerate(lines):
        replacements = []
        # Repeated scat words anywhere in a line: DUDUDU / DaLaDa.
        for match in SCAT_WORD.finditer(display):
            parts = SCAT_PART.findall(match.group())
            if "".join(parts).casefold() == match.group().casefold():
                replacements.append(
                    (
                        match.start(),
                        match.end(),
                        "".join(SCAT_READINGS[p.lower()] for p in parts),
                    )
                )
        # Older supported case: short independent vocables before Hanzi.
        prefix = SCAT_PREFIX.match(display)
        if prefix:
            for match in re.finditer(r"[A-Za-z]+", prefix.group()):
                if SCAT_PART.fullmatch(match.group()) and not any(
                    start <= match.start() < end for start, end, _ in replacements
                ):
                    replacements.append(
                        (
                            match.start(),
                            match.end(),
                            SCAT_READINGS[match.group().lower()],
                        )
                    )
        if unknown_vocables:
            # Prepare unsupported Latin vocalizations before TIFA starts;
            # unrecognized English requires an optional ONNX G2P runtime.
            # Never change the displayed lyrics, only the phonetic input.
            for match in UNKNOWN_LATIN.finditer(display):
                if not any(
                    a < match.end() and match.start() < b for a, b, _ in replacements
                ):
                    repetitions = max(1, min(12, (len(match.group()) + 1) // 2))
                    replacements.append(
                        (match.start(), match.end(), "啦" * repetitions)
                    )
        replacements.sort()
        cursor, text, spans = 0, [], []
        for begin, end, reading in replacements:
            if begin < cursor:
                continue
            for i in range(cursor, begin):
                text.append(display[i])
                spans.append((i, i + 1))
            for syllable in reading:
                text.append(syllable)
                spans.append((begin, end))
            changes.append(
                {
                    "line": line_no,
                    "source_start": begin,
                    "source_end": end,
                    "display": display[begin:end],
                    "pronunciation": reading,
                }
            )
            cursor = end
        for i in range(cursor, len(display)):
            text.append(display[i])
            spans.append((i, i + 1))
        spoken.append("".join(text))
        maps.append(spans)
    return "\n".join(spoken), maps, changes
