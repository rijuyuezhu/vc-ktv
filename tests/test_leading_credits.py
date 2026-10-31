"""Only conservative production credits in the opening header are removed."""

import pytest
from fastapi.testclient import TestClient

from ktv_app.lyrics import preprocess
from ktv_app.text_normalization import clean_lyric_text
from tests.helpers import join_and_register
from vc_ktv.app import create_app
from vc_ktv.lyric_selection import _candidate


@pytest.mark.parametrize(
    "credit",
    [
        "画师：A",
        "调校：小王",
        "策划：某某",
        "混音 : Alice",
        "混音/母带：One",
        "作曲/编曲：M",
        "编曲：B",
        "作詞：A",
        "插畫：X",
        "PV：Team",
        "Lyrics: Writer",
        "Mastering: Mike",
    ],
)
def test_confident_leading_credits_only(credit):
    raw = f"[ar:Singer]\n[00:01.00]{credit}\n\n[00:02.00]第一句\n[00:08.00]第二句"
    assert clean_lyric_text(raw) == "第一句\n第二句"
    assert (
        clean_lyric_text(raw, keep_leading_credits=True) == f"{credit}\n第一句\n第二句"
    )


def test_preserve_body_credits_sung_line_refrains_and_multilingual_lyrics():
    raw = "[00:00.00]混音：A\n[00:01.00]今夜唱一首歌\n[00:07.00]作曲：你来唱\n[00:08.00]今夜唱一首歌\n[00:09.00]君の声"
    assert clean_lyric_text(raw) == "今夜唱一首歌\n作曲：你来唱\n今夜唱一首歌\n君の声"
    assert (
        clean_lyric_text("作曲/别的事：你来唱\n第二句") == "作曲/别的事：你来唱\n第二句"
    )
    assert (
        clean_lyric_text("词：\n这是正常歌词\n第二句") == "词：\n这是正常歌词\n第二句"
    )


def test_candidate_preserves_restorable_source_version_and_ticket(tmp_path):
    source = "[00:00.00]画师：Alice\n[00:01.00]混音/母带：Bob\n[00:10.00]第一句歌词\n[00:15.00]第二句歌词"
    result = _candidate(
        "测试歌", "网易云音乐", source, "https://music.163.com/song?id=1"
    )
    assert result["text"] == "第一句歌词\n第二句歌词"
    assert result["unfiltered_text"].startswith("画师：Alice\n混音/母带：Bob\n")
    assert result["url"].startswith("https://music.163.com/")

    app = create_app(
        root=tmp_path / "room", password="pw", secret=b"credit-test", auto_prepare=False
    )
    app.state.events.create("测试场次", "123456")

    async def lookup(bvid, page):
        return dict(
            bvid=bvid, page=page, title="测试歌", uploader="UP", cover="", duration=120
        )

    async def candidates(_query, _phase="all"):
        return [result.copy()]

    app.state.lookup = lookup
    # The guest candidate API's phase=all dispatches through this injection.
    app.state.candidate_search = candidates
    with TestClient(app) as guest:
        join_and_register(guest, "测试用户")
        song_id = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        response = guest.get(
            f"/api/songs/{song_id}/lyrics/candidates",
            params={"query": "测试歌", "phase": "all"},
        )
        assert response.status_code == 200, response.text
        candidate = response.json()["candidates"][0]
        assert candidate["ticket"] != candidate["unfiltered_ticket"]
        assert (
            guest.post(
                f"/api/songs/{song_id}/lyrics/select",
                json={
                    "source": candidate["source"],
                    "text": candidate["unfiltered_text"],
                    "ticket": candidate["ticket"],
                },
            ).status_code
            == 403
        )
        assert (
            guest.post(
                f"/api/songs/{song_id}/lyrics/select",
                json={
                    "source": candidate["source"],
                    "text": candidate["unfiltered_text"],
                    "ticket": candidate["unfiltered_ticket"],
                },
            ).status_code
            == 200
        )
        saved = app.state.store.rows(
            "SELECT lyric_choice FROM songs WHERE id=?", (song_id,)
        )[0]
        assert saved["lyric_choice"] == result["unfiltered_text"]


def test_confirmed_lyrics_restore_does_not_get_cleaned_again():
    lyrics = "作曲：有人真的唱出这句\n下一句歌词"
    assert [
        line.text for line in preprocess(lyrics, confirmed=True)
    ] == lyrics.splitlines()
    assert [line.text for line in preprocess(lyrics)] == ["下一句歌词"]
