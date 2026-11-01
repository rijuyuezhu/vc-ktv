"""NetEase confirmation keeps only signed, ordered user-selected source lines."""

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app
from vc_ktv.lyric_selection import _candidate
from vc_ktv.timed_lyrics import project_provider_timeline

RAW = (
    "[ar:Artist]\n[00:01]画师：Alice\n[00:02]策划：Bob\n"
    "[00:05]第一句\n[00:07]重复句\n[00:09]杂项\n"
    "[00:12]重复句\n[00:16]收尾"
)
UNTIMED = (
    "[ar:Artist]\n作曲：Unstamped\n"
    "[00:05]第一句\n[00:07]重复句\n[00:09]杂项\n"
    "[00:12]重复句\n[00:16]收尾"
)


def make_guest(tmp_path, raw=RAW):
    app = create_app(
        root=tmp_path / "room", password="pw", secret=b"lines", auto_prepare=False
    )
    app.state.events.create("测试场次", "123456")

    async def lookup(bvid, page):
        return dict(
            bvid=bvid, page=page, title="测试", uploader="UP", cover="", duration=35
        )

    async def find(query, phase="all"):
        return [
            _candidate("测试", "网易云音乐", raw, "https://music.163.com/song?id=1")
        ]

    app.state.lookup = lookup
    app.state.candidate_search = find
    return app


def candidate_and_song(client):
    join_and_register(client, "客人")
    song = client.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
    candidate = client.get(f"/api/songs/{song}/lyrics/candidates").json()["candidates"][
        0
    ]
    return song, candidate


def payload(candidate, selected, *, timed=False):
    data = dict(
        source=candidate["source"],
        text=candidate["text"],
        ticket=candidate["ticket"],
        selection_text=candidate.get("unfiltered_text", candidate["text"]),
        selection_ticket=candidate.get("unfiltered_ticket", candidate["ticket"]),
        selected_lines=selected,
    )
    if timed:
        data.update(
            timing_mode="provider",
            source_lrc_raw=candidate["source_lrc_raw"],
            timed_lrc=candidate["timed_lrc"],
            timing_ticket=candidate["timing_ticket"],
            offset_ms=500,
        )
    return data


def test_signed_selected_lines_are_saved_in_order_with_duplicates(tmp_path):
    app = make_guest(tmp_path)
    with TestClient(app) as guest:
        sid, c = candidate_and_song(guest)
        assert c["unfiltered_text"].splitlines() == [
            "画师：Alice",
            "策划：Bob",
            "第一句",
            "重复句",
            "杂项",
            "重复句",
            "收尾",
        ]
        choice = payload(c, [2, 3, 5, 6])
        res = guest.post(f"/api/songs/{sid}/lyrics/select", json=choice)
        assert res.status_code == 200, res.text
        row = app.state.store.rows(
            "SELECT lyric_choice,lyric_timing_mode FROM songs WHERE id=?", (sid,)
        )[0]
        assert row == {
            "lyric_choice": "第一句\n重复句\n重复句\n收尾",
            "lyric_timing_mode": "align",
        }


def test_timed_selection_retains_exact_source_positions_including_duplicate_lines(
    tmp_path,
):
    app = make_guest(tmp_path)
    with TestClient(app) as guest:
        sid, c = candidate_and_song(guest)
        res = guest.post(
            f"/api/songs/{sid}/lyrics/select", json=payload(c, [2, 3, 5, 6], timed=True)
        )
        assert res.status_code == 200, res.text
        row = app.state.store.rows(
            "SELECT lyric_choice,provider_lrc,lyric_offset_ms FROM songs WHERE id=?",
            (sid,),
        )[0]
        assert row["lyric_choice"] == "第一句\n重复句\n重复句\n收尾"
        assert (
            row["provider_lrc"]
            == "[00:05.00]第一句\n[00:07.00]重复句\n[00:12.00]重复句\n[00:16.00]收尾"
        )
        assert row["lyric_offset_ms"] == 500
        # Explicitly re-adding a timed production credit is allowed.
        again = guest.post(
            f"/api/songs/{sid}/lyrics/select", json=payload(c, [0, 2, 5], timed=True)
        )
        assert again.status_code == 200, again.text
        assert app.state.store.rows(
            "SELECT provider_lrc FROM songs WHERE id=?", (sid,)
        )[0]["provider_lrc"].startswith("[00:01.00]画师：Alice")


def test_untimed_selected_credit_requires_alignment_not_fabricated_timing(tmp_path):
    app = make_guest(tmp_path, UNTIMED)
    with TestClient(app) as guest:
        sid, c = candidate_and_song(guest)
        assert c.get("timed_lrc")
        res = guest.post(
            f"/api/songs/{sid}/lyrics/select", json=payload(c, [0, 1, 3], timed=True)
        )
        assert res.status_code == 400 and "无时间戳" in res.text
        assert (
            guest.post(
                f"/api/songs/{sid}/lyrics/select", json=payload(c, [0, 1, 3])
            ).status_code
            == 200
        )
        assert app.state.store.rows(
            "SELECT lyric_choice FROM songs WHERE id=?", (sid,)
        )[0]["lyric_choice"].startswith("作曲：Unstamped")


def test_forgery_and_invalid_selection_are_rejected(tmp_path):
    app = make_guest(tmp_path)
    with TestClient(app) as guest:
        sid, c = candidate_and_song(guest)
        url = f"/api/songs/{sid}/lyrics/select"
        base = payload(c, [2, 3])
        attempts = [
            ({**base, "selection_text": "伪造\n歌词"}, 403),
            ({**base, "selection_ticket": "bad"}, 403),
            ({**base, "selected_lines": [3, 2]}, 400),
            ({**base, "selected_lines": [3, 3]}, 400),
            ({**base, "selected_lines": [100, 2]}, 400),
            ({**base, "selected_lines": [2]}, 400),
            ({**base, "text": "恶意替换原文内容\n另有恶意歌词"}, 403),
        ]
        for body, status in attempts:
            res = guest.post(url, json=body)
            assert res.status_code == status, (body, res.text)
        assert (
            app.state.store.rows("SELECT lyric_choice FROM songs WHERE id=?", (sid,))[
                0
            ]["lyric_choice"]
            == ""
        )


def test_project_stamps_directly_and_rejects_tampering():
    result = project_provider_timeline(
        RAW, "画师：Alice\n策划：Bob\n第一句\n重复句\n杂项\n重复句\n收尾", [3, 5]
    )
    assert result == "[00:07.00]重复句\n[00:12.00]重复句"


def test_provider_timing_without_credits_can_filter_a_middle_lyric(tmp_path):
    raw = "[00:01]第一句歌词\n[00:05]不需要的歌词\n[00:09]最后一句歌词"
    app = make_guest(tmp_path, raw)
    with TestClient(app) as guest:
        sid, candidate = candidate_and_song(guest)
        assert "unfiltered_text" not in candidate
        result = guest.post(
            f"/api/songs/{sid}/lyrics/select",
            json=payload(candidate, [0, 2], timed=True),
        )
        assert result.status_code == 200, result.text
        record = app.state.store.rows(
            "SELECT lyric_choice,provider_lrc FROM songs WHERE id=?", (sid,)
        )[0]
        assert record["lyric_choice"] == "第一句歌词\n最后一句歌词"
        assert record["provider_lrc"] == (
            "[00:01.00]第一句歌词\n[00:09.00]最后一句歌词"
        )
