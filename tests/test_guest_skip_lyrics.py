"""Guests may explicitly play the untouched original, even before download."""

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import create_app
from vc_ktv.autoprep import AutoPrep


def new_app(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"guest-skip-key",
        auto_prepare=False,
    )
    app.state.events.create("场次", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "《测试歌曲》",
            "uploader": "UP",
            "cover": "",
            "duration": 20,
        }

    app.state.lookup = lookup
    return app


def test_skip_lyrics_before_download_is_playable_without_global_override(tmp_path):
    app = new_app(tmp_path)
    store = app.state.store
    with TestClient(app) as guest, TestClient(app) as other:
        join_and_register(guest, "owner")
        join_and_register(other, "nonowner")
        sid = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        endpoint = f"/api/songs/{sid}/lyrics/skip"
        assert other.post(endpoint).status_code in (401, 404)
        assert guest.post(endpoint).status_code == 200
        assert guest.post(endpoint).status_code == 200  # idempotent
        original = store.db.execute(
            "SELECT skip_subtitles,lyric_choice,prep_state,prep_stage FROM songs WHERE id=?",
            (sid,),
        ).fetchone()
        assert (
            original["skip_subtitles"],
            original["lyric_choice"],
            original["prep_state"],
        ) == (1, "", "pending")
        assert guest.get("/api/state").json()["queue"] == []

        # Simulate the in-flight media download finishing AFTER the user has skipped.
        def download(bvid, page, dest):
            path = dest / "source.mp4"
            path.write_bytes(b"untouched-original")
            return path

        worker = AutoPrep(store, lambda path: 20, download=download)
        claimed = worker.claim()
        assert claimed["id"] == sid and claimed["status"] == "waiting"
        worker.prepare_source(claimed)
        prepared = store.db.execute(
            "SELECT skip_subtitles,lyric_choice,prep_state,media_file,source_file FROM songs WHERE id=?",
            (sid,),
        ).fetchone()
        assert prepared["skip_subtitles"] == 1
        assert prepared["lyric_choice"] == ""
        assert prepared["prep_state"] == "original"
        assert prepared["media_file"] == prepared["source_file"]
        assert (
            store.media / prepared["source_file"]
        ).read_bytes() == b"untouched-original"
        assert worker.claim() is None  # No subtitle enhancement claim.
        state = guest.get("/api/state").json()
        assert [s["id"] for s in state["queue"]] == [sid]
        assert state["queue"][0]["skip_subtitles"] == 1
        assert not store.settings()["allow_unsubtitled"]
        assert guest.post(f"/api/songs/{sid}/lyrics/skip").status_code == 200


def test_skip_lyrics_during_download_and_choose_lyrics_later(tmp_path):
    app = new_app(tmp_path)
    store = app.state.store
    with TestClient(app) as guest:
        join_and_register(guest, "owner")
        sid = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        worker = AutoPrep(
            store, lambda path: 20, download=lambda b, p, d: d / "original.mp4"
        )
        claimed = worker.claim()
        assert claimed["prep_state"] == "pending"
        assert guest.post(f"/api/songs/{sid}/lyrics/skip").status_code == 200
        assert (
            store.db.execute(
                "SELECT prep_state FROM songs WHERE id=?", (sid,)
            ).fetchone()[0]
            == "downloading"
        )

        def complete(bvid, page, folder):
            path = folder / "source.mp4"
            path.write_bytes(b"original")
            return path

        worker.download = complete
        worker.prepare_source(claimed)
        assert guest.get("/api/state").json()["queue"][0]["skip_subtitles"] == 1

        # Explicit guest re-selection must be able to restore subtitle creation.
        async def fake(q):
            return [
                {
                    "title": q,
                    "source": "VCPedia",
                    "text": "第一句有一些歌词\n第二句也有一些歌词",
                }
            ]

        app.state.candidate_search = fake
        result = guest.get(f"/api/songs/{sid}/lyrics/candidates").json()["candidates"][
            0
        ]
        response = guest.post(
            f"/api/songs/{sid}/lyrics/select",
            json={
                "source": result["source"],
                "text": result["text"],
                "ticket": result["ticket"],
            },
        )
        assert response.status_code == 200, response.text
        row = store.db.execute(
            "SELECT skip_subtitles,lyric_choice,prep_state FROM songs WHERE id=?",
            (sid,),
        ).fetchone()
        assert (row["skip_subtitles"], row["lyric_choice"], row["prep_state"]) == (
            0,
            "第一句有一些歌词\n第二句也有一些歌词",
            "downloaded",
        )
        assert worker.claim()["id"] == sid


def test_skip_lyrics_when_original_ready_ignores_in_flight_alignment(tmp_path):
    app = new_app(tmp_path)
    store = app.state.store
    with TestClient(app) as guest, TestClient(app) as other:
        join_and_register(guest, "owner")
        join_and_register(other, "nonowner")
        sid = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        source = store.media / "original.mp4"
        source.write_bytes(b"original-media")
        with store.lock:
            store.db.execute(
                "UPDATE songs SET status='ready',prep_state='aligning',source_file=?,media_file=?,lyric_choice='歌词第一句\\n歌词第二句' WHERE id=?",
                (source.name, source.name, sid),
            )
            store.db.commit()
        assert other.post(f"/api/songs/{sid}/lyrics/skip").status_code in (401, 404)
        assert guest.post(f"/api/songs/{sid}/lyrics/skip").status_code == 200
        row = store.db.execute(
            "SELECT skip_subtitles,lyric_choice,prep_state,media_file FROM songs WHERE id=?",
            (sid,),
        ).fetchone()
        assert (
            row["skip_subtitles"],
            row["lyric_choice"],
            row["prep_state"],
            row["media_file"],
        ) == (1, "", "original", source.name)
        assert [s["id"] for s in guest.get("/api/state").json()["queue"]] == [sid]
        assert guest.post(f"/api/songs/{sid}/lyrics/skip").status_code == 200
        with store.lock:
            store.db.execute("UPDATE songs SET status='playing' WHERE id=?", (sid,))
            store.db.commit()
        assert guest.post(f"/api/songs/{sid}/lyrics/skip").status_code == 409


def test_skip_existing_subtitled_video_switches_back_to_original(tmp_path):
    app = new_app(tmp_path)
    store = app.state.store
    with TestClient(app) as guest:
        join_and_register(guest, "owner")
        sid = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        source = store.media / "source.mp4"
        rendered = store.media / "rendered.mp4"
        source.write_bytes(b"clean-original")
        rendered.write_bytes(b"karaoke-subtitled")
        with store.lock:
            store.db.execute(
                "UPDATE songs SET status='ready',prep_state='done',source_file=?,media_file=?,"
                "lyric_choice='第一句歌词\n第二句歌词' WHERE id=?",
                (source.name, rendered.name, sid),
            )
            store.db.commit()
        assert guest.post(f"/api/songs/{sid}/lyrics/skip").status_code == 200
        row = store.db.execute(
            "SELECT media_file,source_file,prep_state,skip_subtitles,lyric_choice FROM songs WHERE id=?",
            (sid,),
        ).fetchone()
        assert row["media_file"] == row["source_file"] == source.name
        assert row["prep_state"] == "original" and row["skip_subtitles"] == 1
        assert row["lyric_choice"] == ""
        assert source.read_bytes() == b"clean-original"
        assert [song["id"] for song in guest.get("/api/state").json()["queue"]] == [sid]
