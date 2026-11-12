"""Virtual replays must resolve source files when their lyrics change."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import vc_ktv.application.admin_preparation as prep_admin
import vc_ktv.processing.worker as processing
from tests.helpers import join_and_register, use_browser_playback
from vc_ktv.application.factory import create_app
from vc_ktv.lyrics.lyric_selection import _candidate

SOURCE = b"raw source video"
BAKED = b"original baked video"
TIMED = "[00:04.00]第一句歌词\n[00:14.00]第二句歌词"


def create_replay_scene(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="fixture-admin",
        secret=b"virtual-replay-lyrics-fixture",
        auto_prepare=False,
        gpu_alignment=True,
    )
    app.state.events.create("Replay lyric test", "123456")
    use_browser_playback(app)

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "Fixture song",
            "uploader": "Fake",
            "duration": 180,
            "cover": "",
        }

    async def search(term):
        return [_candidate(term, "网易云音乐", TIMED, "https://music.163.com/")]

    app.state.lookup = lookup
    app.state.candidate_search = search
    return app


def prepare_original(store, song_id, *, source):
    (store.media / "baked.mp4").write_bytes(BAKED)
    if source:
        (store.media / "source.mp4").write_bytes(SOURCE)
    with store.lock:
        store.db.execute(
            "UPDATE songs SET status='played',prep_state='done',"
            "source_file=?,media_file='baked.mp4',skip_subtitles=0,"
            "lyric_choice='old lyrics',lyric_source='VCPedia' WHERE id=?",
            ("source.mp4" if source else None, song_id),
        )
        store.db.commit()


def signed_provider_selection(guest, song_id):
    candidates = guest.get(f"/api/songs/{song_id}/lyrics/candidates")
    assert candidates.status_code == 200, candidates.text
    ticket = candidates.json()["candidates"][0]
    return {
        "text": ticket["text"],
        "ticket": ticket["ticket"],
        "source": ticket["source"],
        "timing_mode": "provider",
        "timed_lrc": ticket["timed_lrc"],
        "source_lrc_raw": ticket["source_lrc_raw"],
        "timing_ticket": ticket["timing_ticket"],
        "offset_ms": 300,
    }


def row(store, song_id):
    return store.rows("SELECT * FROM songs WHERE id=?", (song_id,))[0]


@pytest.mark.parametrize(
    "source", [True, False], ids=["raw-and-baked", "legacy-baked-only"]
)
@pytest.mark.parametrize(
    "nested", [False, True], ids=["single-replay", "nested-replay"]
)
def test_skip_on_virtual_replay_chooses_own_inherited_source(tmp_path, source, nested):
    app = create_replay_scene(tmp_path)
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "Singer")
        assert (
            admin.post(
                "/api/admin/login", json={"password": "fixture-admin"}
            ).status_code
            == 200
        )
        store = app.state.events.active()
        original = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        prepare_original(store, original, source=source)
        first = guest.post(f"/api/songs/{original}/replay").json()["id"]
        if nested:
            assert admin.post("/api/admin/next").json()["active"]["id"] == first
            assert (
                admin.post("/api/admin/finish", json={"song_id": first}).status_code
                == 200
            )
        target_id = (
            guest.post(f"/api/songs/{first if nested else original}/replay").json()[
                "id"
            ]
            if nested
            else first
        )
        assert row(store, target_id)["source_file"] is None
        assert row(store, target_id)["media_file"] is None
        skipped = guest.post(f"/api/songs/{target_id}/lyrics/skip")
        assert skipped.status_code == 200, skipped.text
        expected = "source.mp4" if source else "baked.mp4"
        changed = row(store, target_id)
        assert changed["source_file"] == expected
        assert changed["media_file"] == expected
        assert changed["prep_state"] == "original"
        assert changed["lyric_choice"] == "" and changed["skip_subtitles"]
        assert guest.get(f"/api/songs/{target_id}/download").content == (
            SOURCE if source else BAKED
        )
        assert target_id in [entry["id"] for entry in store.fair_queue()]
        assert row(store, original)["media_file"] == "baked.mp4"
        assert (store.media / "baked.mp4").read_bytes() == BAKED
        assert {entry.name for entry in store.media.iterdir() if entry.is_file()} == (
            {"baked.mp4", "source.mp4"} if source else {"baked.mp4"}
        )


@pytest.mark.parametrize(
    "source", [True, False], ids=["raw-and-baked", "legacy-baked-only"]
)
def test_editing_replay_lyrics_prepares_from_inherited_source_and_preserves_siblings(
    tmp_path, monkeypatch, source
):
    app = create_replay_scene(tmp_path)
    with TestClient(app) as guest:
        join_and_register(guest, "Singer")
        store = app.state.events.active()
        original = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        prepare_original(store, original, source=source)
        replay = guest.post(f"/api/songs/{original}/replay").json()["id"]
        sibling = guest.post(f"/api/songs/{original}/replay").json()["id"]
        selection = signed_provider_selection(guest, replay)
        selected = guest.post(f"/api/songs/{replay}/lyrics/select", json=selection)
        assert selected.status_code == 200, selected.text
        expected = "source.mp4" if source else "baked.mp4"
        changed = row(store, replay)
        assert changed["prep_state"] == "downloaded"
        assert (changed["source_file"], changed["media_file"]) == (expected, expected)
        assert replay not in [entry["id"] for entry in store.fair_queue()]
        assert sibling in [entry["id"] for entry in store.fair_queue()]
        assert store.replay_assets(row(store, sibling))["media_file"] == "baked.mp4"

        seen = []

        def render(src, timed, offset, destination, archive, duration, **kwargs):
            assert src == store.media / expected
            assert timed == TIMED
            assert offset == 300
            seen.append(src.read_bytes())
            (archive / "aligned.lrc").write_text(timed)
            destination.write_bytes(b"new replay-only video")
            return {"mode": "provider"}

        monkeypatch.setattr(processing, "render_provider_video", render)
        monkeypatch.setattr(
            processing, "prepare_tv_source", lambda *args, **kwargs: None
        )
        worker = processing.AutoPrep(store, lambda path: 180, gpu_alignment=False)
        task = worker.claim("fast")
        assert task and task["id"] == replay
        worker.prepare_subtitles(task)
        output = row(store, replay)
        assert output["prep_state"] == "done"
        assert (
            store.media / output["media_file"]
        ).read_bytes() == b"new replay-only video"
        assert seen == [SOURCE if source else BAKED]
        assert row(store, sibling)["media_file"] is None
        assert store.replay_assets(row(store, sibling))["media_file"] == "baked.mp4"
        assert row(store, original)["media_file"] == "baked.mp4"
        assert (store.media / "baked.mp4").read_bytes() == BAKED

        # Switching a newly rendered replay back to source must not change
        # the original baked video or the other virtual replay.
        assert guest.post(f"/api/songs/{replay}/lyrics/skip").status_code == 200
        assert row(store, replay)["media_file"] == expected
        assert guest.get(f"/api/songs/{replay}/download").content == seen[0]
        assert store.replay_assets(row(store, sibling))["media_file"] == "baked.mp4"


def test_failed_provider_render_keeps_virtual_replay_playable(tmp_path, monkeypatch):
    app = create_replay_scene(tmp_path)
    with TestClient(app) as guest:
        join_and_register(guest, "Singer")
        store = app.state.events.active()
        original = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        prepare_original(store, original, source=True)
        replay = guest.post(f"/api/songs/{original}/replay").json()["id"]
        selected = guest.post(
            f"/api/songs/{replay}/lyrics/select",
            json=signed_provider_selection(guest, replay),
        )
        assert selected.status_code == 200

        def fail(*args, **kwargs):
            raise RuntimeError("fixture render failure")

        monkeypatch.setattr(processing, "render_provider_video", fail)
        worker = processing.AutoPrep(store, lambda path: 180, gpu_alignment=False)
        task = worker.claim("fast")
        assert task and task["id"] == replay
        worker.prepare_subtitles(task)
        updated = row(store, replay)
        assert updated["prep_state"] == "original"
        assert updated["media_file"] == "source.mp4"
        # Normal queue policy requires subtitles or an explicit original-only
        # choice; failed rendering preserves the media for that recovery path.
        assert guest.get(f"/api/songs/{replay}/download").content == SOURCE
        assert guest.post(f"/api/songs/{replay}/lyrics/skip").status_code == 200
        assert replay in [entry["id"] for entry in store.fair_queue()]
        assert row(store, original)["media_file"] == "baked.mp4"


def test_replay_lyrics_missing_source_fails_without_stranding_song(tmp_path):
    app = create_replay_scene(tmp_path)
    with TestClient(app) as guest:
        join_and_register(guest, "Singer")
        store = app.state.events.active()
        original = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        prepare_original(store, original, source=True)
        replay = guest.post(f"/api/songs/{original}/replay").json()["id"]
        (store.media / "source.mp4").unlink()
        before = row(store, replay)
        assert guest.post(f"/api/songs/{replay}/lyrics/skip").status_code == 409
        assert (
            guest.post(
                f"/api/songs/{replay}/lyrics/select",
                json=signed_provider_selection(guest, replay),
            ).status_code
            == 409
        )
        assert row(store, replay) == before


def test_concurrent_skip_and_lyric_select_on_virtual_replay(tmp_path):
    app = create_replay_scene(tmp_path)
    with TestClient(app) as guest:
        join_and_register(guest, "Singer")
        store = app.state.events.active()
        original = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        prepare_original(store, original, source=True)
        replay = guest.post(f"/api/songs/{original}/replay").json()["id"]
        selection = signed_provider_selection(guest, replay)
        with ThreadPoolExecutor(max_workers=2) as pool:
            skip = pool.submit(guest.post, f"/api/songs/{replay}/lyrics/skip")
            choose = pool.submit(
                guest.post, f"/api/songs/{replay}/lyrics/select", json=selection
            )
            results = (skip.result(timeout=8), choose.result(timeout=8))
        assert [r.status_code for r in results] == [200, 200]
        output = row(store, replay)
        assert output["source_file"] == "source.mp4"
        assert output["media_file"] == "source.mp4"
        assert output["prep_state"] in ("original", "downloaded")
        assert output["skip_subtitles"] != bool(output["lyric_choice"])


def test_gpu_alignment_uses_replay_source_not_parent_baked_video(tmp_path):
    app = create_replay_scene(tmp_path)
    with TestClient(app) as guest:
        join_and_register(guest, "Singer")
        store = app.state.events.active()
        original = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        prepare_original(store, original, source=True)
        replay = guest.post(f"/api/songs/{original}/replay").json()["id"]
        selection = signed_provider_selection(guest, replay)
        selection["timing_mode"] = "align"
        for key in ("timed_lrc", "source_lrc_raw", "timing_ticket", "offset_ms"):
            del selection[key]
        response = guest.post(f"/api/songs/{replay}/lyrics/select", json=selection)
        assert response.status_code == 200, response.text
        seen = []

        def enhance(src, lyrics, target, archive, duration, **options):
            seen.append((src.name, src.read_bytes(), lyrics))
            (archive / "aligned.lrc").write_text("[00:01.00]hi")
            target.write_bytes(b"replay aligned video")
            return {"mode": "gpu_fixture"}

        worker = processing.AutoPrep(
            store, lambda path: 180, enhance=enhance, gpu_alignment=True
        )
        task = worker.claim("general")
        assert task and task["id"] == replay
        worker.prepare_subtitles(task)
        output = row(store, replay)
        assert output["prep_state"] == "done"
        assert (
            store.media / output["media_file"]
        ).read_bytes() == b"replay aligned video"
        assert seen == [("source.mp4", SOURCE, selection["text"])]
        assert row(store, original)["media_file"] == "baked.mp4"
        assert (store.media / "baked.mp4").read_bytes() == BAKED


def test_admin_subtitle_retry_on_edited_virtual_replay(tmp_path, monkeypatch):
    app = create_replay_scene(tmp_path)
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "Singer")
        assert (
            admin.post(
                "/api/admin/login", json={"password": "fixture-admin"}
            ).status_code
            == 200
        )
        store = app.state.events.active()
        original = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        prepare_original(store, original, source=True)
        replay = guest.post(f"/api/songs/{original}/replay").json()["id"]
        assert (
            guest.post(
                f"/api/songs/{replay}/lyrics/select",
                json=signed_provider_selection(guest, replay),
            ).status_code
            == 200
        )
        worker = processing.AutoPrep(store, lambda path: 180, gpu_alignment=False)
        task = worker.claim("fast")
        assert task and task["id"] == replay

        def broken(*args, **kwargs):
            raise RuntimeError("failed fixture subtitle render")

        monkeypatch.setattr(processing, "render_provider_video", broken)
        worker.prepare_subtitles(task)
        assert row(store, replay)["prep_state"] == "original"
        assert row(store, replay)["media_file"] == "source.mp4"
        retry = admin.post(f"/api/admin/songs/{replay}/subtitles/retry")
        assert retry.status_code == 200, retry.text
        assert row(store, replay)["prep_state"] == "downloaded"

        def rendered(src, timed, offset, dest, archive, duration, **options):
            assert src.read_bytes() == SOURCE
            (archive / "aligned.lrc").write_text(timed)
            dest.write_bytes(b"retried video")
            return {"mode": "provider"}

        monkeypatch.setattr(processing, "render_provider_video", rendered)
        again = worker.claim("fast")
        assert again and again["id"] == replay
        worker.prepare_subtitles(again)
        updated = row(store, replay)
        assert updated["prep_state"] == "done"
        assert (store.media / updated["media_file"]).read_bytes() == b"retried video"
        assert (store.media / "source.mp4").read_bytes() == SOURCE
        assert (store.media / "baked.mp4").read_bytes() == BAKED


def test_replay_after_modifying_lyrics_uses_own_new_media(tmp_path, monkeypatch):
    app = create_replay_scene(tmp_path)
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "Singer")
        assert (
            admin.post(
                "/api/admin/login", json={"password": "fixture-admin"}
            ).status_code
            == 200
        )
        store = app.state.events.active()
        original = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        prepare_original(store, original, source=True)
        first = guest.post(f"/api/songs/{original}/replay").json()["id"]
        assert (
            guest.post(
                f"/api/songs/{first}/lyrics/select",
                json=signed_provider_selection(guest, first),
            ).status_code
            == 200
        )

        def render(src, timed, offset, dest, archive, duration, **kwargs):
            (archive / "aligned.lrc").write_text(timed)
            dest.write_bytes(b"parent replay video")
            return {"mode": "provider"}

        monkeypatch.setattr(processing, "render_provider_video", render)
        worker = processing.AutoPrep(store, lambda path: 180, gpu_alignment=False)
        task = worker.claim("fast")
        assert task and task["id"] == first
        worker.prepare_subtitles(task)
        first_asset = row(store, first)["media_file"]
        assert first_asset != "baked.mp4"
        assert admin.post("/api/admin/next").json()["active"]["id"] == first
        assert (
            admin.post("/api/admin/finish", json={"song_id": first}).status_code == 200
        )
        nested = guest.post(f"/api/songs/{first}/replay").json()["id"]
        assert (
            guest.get(f"/api/songs/{nested}/download").content == b"parent replay video"
        )
        # Select no subtitles on the nested replay: it must use the original
        # raw source, not the parent replay's burned-in subtitles.
        assert guest.post(f"/api/songs/{nested}/lyrics/skip").status_code == 200
        assert guest.get(f"/api/songs/{nested}/download").content == SOURCE
        assert (store.media / first_asset).read_bytes() == b"parent replay video"
        assert row(store, original)["media_file"] == "baked.mp4"


def test_admin_replay_priority_survives_guest_lyric_changes(tmp_path):
    app = create_replay_scene(tmp_path)
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "Singer")
        assert (
            admin.post(
                "/api/admin/login", json={"password": "fixture-admin"}
            ).status_code
            == 200
        )
        store = app.state.events.active()
        original = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        prepare_original(store, original, source=True)
        priority = admin.post(f"/api/admin/songs/{original}/replay")
        assert priority.status_code == 200, priority.text
        replay = priority.json()["id"]
        assert row(store, replay)["priority_next"] == 1
        assert (
            guest.post(
                f"/api/songs/{replay}/lyrics/select",
                json=signed_provider_selection(guest, replay),
            ).status_code
            == 200
        )
        assert replay not in [x["id"] for x in store.fair_queue()]
        assert guest.post(f"/api/songs/{replay}/lyrics/skip").status_code == 200
        assert store.fair_queue()[0]["id"] == replay
        playing = admin.post("/api/admin/next").json()["active"]
        assert playing["id"] == replay and playing["fair_exempt"]
        assert guest.post(f"/api/songs/{replay}/cancel").status_code == 409
        assert (
            admin.post("/api/admin/finish", json={"song_id": replay}).status_code == 200
        )
        assert row(store, original)["status"] == "played"
        assert row(store, original)["media_file"] == "baked.mp4"


def test_cancel_edited_replay_does_not_touch_original_or_sibling(tmp_path):
    app = create_replay_scene(tmp_path)
    with TestClient(app) as guest:
        join_and_register(guest, "Singer")
        store = app.state.events.active()
        original = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        prepare_original(store, original, source=True)
        replay = guest.post(f"/api/songs/{original}/replay").json()["id"]
        sibling = guest.post(f"/api/songs/{original}/replay").json()["id"]
        assert (
            guest.post(
                f"/api/songs/{replay}/lyrics/select",
                json=signed_provider_selection(guest, replay),
            ).status_code
            == 200
        )
        cancelled = guest.post(f"/api/songs/{replay}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert row(store, replay)["status"] == "cancelled"
        assert replay not in [x["id"] for x in store.fair_queue()]
        assert sibling in [x["id"] for x in store.fair_queue()]
        worker = processing.AutoPrep(store, lambda path: 180, gpu_alignment=False)
        assert worker.claim("fast") is None
        assert (store.media / "source.mp4").read_bytes() == SOURCE
        assert (store.media / "baked.mp4").read_bytes() == BAKED
        assert guest.get(f"/api/songs/{sibling}/download").content == BAKED


def test_replay_upload_replacement_keeps_original_shared_source(tmp_path, monkeypatch):
    app = create_replay_scene(tmp_path)
    app.state.probe = lambda path: 180
    monkeypatch.setattr(
        prep_admin, "normalize_original_codec", lambda path, **kwargs: path
    )
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "Singer")
        assert (
            admin.post(
                "/api/admin/login", json={"password": "fixture-admin"}
            ).status_code
            == 200
        )
        store = app.state.events.active()
        original = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        prepare_original(store, original, source=True)
        replay = guest.post(f"/api/songs/{original}/replay").json()["id"]
        sibling = guest.post(f"/api/songs/{original}/replay").json()["id"]
        assert guest.post(f"/api/songs/{replay}/lyrics/skip").status_code == 200
        assert row(store, replay)["source_file"] == "source.mp4"
        replaced = admin.post(
            f"/api/admin/songs/{replay}/media",
            files={"file": ("replacement.mp4", b"replacement", "video/mp4")},
        )
        assert replaced.status_code == 200, replaced.text
        assert guest.get(f"/api/songs/{replay}/download").content == b"replacement"
        assert (store.media / "source.mp4").read_bytes() == SOURCE
        assert (store.media / "baked.mp4").read_bytes() == BAKED
        assert guest.get(f"/api/songs/{original}/download").content == BAKED
        assert guest.get(f"/api/songs/{sibling}/download/source").content == SOURCE


def test_upload_replacement_still_cleans_unreferenced_media(tmp_path, monkeypatch):
    app = create_replay_scene(tmp_path)
    app.state.probe = lambda path: 180
    monkeypatch.setattr(
        prep_admin, "normalize_original_codec", lambda path, **kwargs: path
    )
    with TestClient(app) as guest, TestClient(app) as admin:
        join_and_register(guest, "Singer")
        assert (
            admin.post(
                "/api/admin/login", json={"password": "fixture-admin"}
            ).status_code
            == 200
        )
        store = app.state.events.active()
        song_id = guest.post("/api/songs", json={"bvid": "BV0000000001"}).json()["id"]
        old = store.media / "unshared.mp4"
        old.write_bytes(b"unshared source")
        with store.lock:
            store.db.execute(
                "UPDATE songs SET status='ready',prep_state='original',"
                "media_file=?,source_file=?,skip_subtitles=1 WHERE id=?",
                (old.name, old.name, song_id),
            )
            store.db.commit()
        replaced = admin.post(
            f"/api/admin/songs/{song_id}/media",
            files={"file": ("new.mp4", b"new source", "video/mp4")},
        )
        assert replaced.status_code == 200, replaced.text
        assert not old.exists()
        assert guest.get(f"/api/songs/{song_id}/download").content == b"new source"
