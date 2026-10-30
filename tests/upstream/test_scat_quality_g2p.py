"""Upstream-quality G2P keeps PR #62 scat pronunciation and original source text."""

from types import SimpleNamespace

from ktv_app.aligners.partial_g2p import convert_scat_parts
from ktv_app.aligners.scat_pronunciation import pronunciation_lyrics


class UnknownWord(ValueError):
    def __init__(self, token):
        self.unconverted_tokens = [token]
        super().__init__(token)


def convert(text):
    if any(c.isascii() and c.isalpha() for c in text):
        raise UnknownWord(text)
    return [SimpleNamespace(text=c, readings=[[c]]) for c in text if not c.isspace()]


def compose(text, words):
    return SimpleNamespace(
        text=text, readings=[sum((w.readings[0] for w in words), [])]
    )


def test_known_scat_tifa_phonemes_preserve_source():
    text = "唱起来 DaLaDa DUDUDU 你好吗"
    parts = convert_scat_parts(text, convert, UnknownWord, compose)
    words = [word for word, _, _ in parts if word is not None]
    assert [w.text for w in words if w.text in ("DaLaDa", "DUDUDU")] == [
        "DaLaDa",
        "DUDUDU",
    ]
    assert next(w for w in words if w.text == "DaLaDa").readings == [["哒", "啦", "哒"]]
    assert next(w for w in words if w.text == "DUDUDU").readings == [["嘟", "嘟", "嘟"]]
    spoken, spans, changes = pronunciation_lyrics("DUDUDU")
    assert spoken == "嘟嘟嘟" and spans == [[(0, 6)] * 3]
    assert changes[0]["display"] == "DUDUDU"


def test_unknown_english_not_fabricated_as_mandarin():
    items = convert_scat_parts("hello", convert, UnknownWord, compose)
    assert items and items[0][0] is None
    assert items[0][1] == "hello"


def test_scats_without_convertible_mandarin_fall_back_to_source():
    def fail_chinese(text):
        raise UnknownWord(text)

    items = convert_scat_parts("DUDUDU", fail_chinese, UnknownWord, compose)
    assert items[0][0] is None and items[0][1] == "DUDUDU"
