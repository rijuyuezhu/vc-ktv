"""Song-title normalization shared by queue and preparation."""

import re


def title_key(name: str) -> str:
    name = name.strip()
    # Original song titles are frequently embedded in a long Bilibili headline:
    # 「宣传语。对你许下了《歌名》。【UP/活动标签】」.
    # Extract only when there is one unambiguous Chinese title enclosure.
    enclosed = [x.strip() for x in re.findall(r"《([^《》]{1,48})》", name)]
    if len(enclosed) == 1:
        return enclosed[0]
    # Trim tags such as 【洛天依AI】 and [cover] without stripping actual song words.
    for _ in range(4):
        trimmed = re.sub(r"^\s*[【\[][^\]】]{1,55}[】\]]\s*", "", name)
        if trimmed == name:
            break
        name = trimmed
    # Bilibili often appends promotional tags to an otherwise complete title.
    # Search the song name, not the full upload description.
    name = re.split(r"\s*【[^】]{1,55}】", name, maxsplit=1)[0]
    return re.split(
        r"\s*[-—｜|]\s*(?:PV|MV|翻唱|cover|完整版)\b", name, maxsplit=1, flags=re.I
    )[0].strip()
