"""During playback, predictions must match the first claim after settlement."""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from tests.test_fair_capped_two import party, ready


def snapshot(store):
    return (
        store.rows("SELECT id,status,priority_next,fair_exempt FROM songs ORDER BY id"),
        store.rows("SELECT id,plays,seconds,fair_offset FROM guests ORDER BY id"),
        store.rows("SELECT name,value FROM room_state ORDER BY name"),
    )


@pytest.mark.parametrize("count", (2, 3, 5))
@pytest.mark.parametrize("reason", ("completed", "skip"))
@pytest.mark.parametrize("policy", ("fair", "fcfs"))
def test_active_preview_is_readonly_and_matches_next_claim(
    tmp_path, count, reason, policy
):
    names = [chr(65 + i) for i in range(count)]
    app, guests, admin, stack = party(tmp_path, names)
    with stack:
        store = app.state.store
        for name in names:
            ready(store, name, 2)
        store.save_settings(policy, False)
        first = admin.post("/api/admin/next").json()["active"]
        assert first["nickname"] == "A"
        token = admin.get("/api/admin/tv-link").json()["url"].partition("?token=")[2]
        before = snapshot(store)
        preview = store.fair_queue()
        assert preview
        expected = preview[0]["id"]
        assert expected == app.state.store.state()["queue"][0]["id"]
        assert expected == admin.get("/api/admin/state").json()["queue"][0]["id"]
        assert expected == guests["A"].get("/api/state").json()["queue"][0]["id"]
        assert (
            expected
            == admin.get("/api/tv/preload", params={"token": token}).json()[
                "candidate"
            ]["id"]
        )
        for _ in range(3):
            store.fair_queue()
            app.state.store.state()
            admin.get("/api/tv/preload", params={"token": token})
        assert snapshot(store) == before
        assert (
            admin.post(
                "/api/admin/finish", json={"song_id": first["id"], "reason": reason}
            ).status_code
            == 200
        )
        second = admin.post("/api/admin/next").json()["active"]
        assert second["id"] == expected
        if policy == "fair":
            assert second["nickname"] == "B"


def test_admin_priority_turn_does_not_change_preview_fair_counts(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B", "C"])
    with stack:
        store = app.state.store
        for name in guests:
            ready(store, name, 3)
        chosen = next(x for x in store.songs() if x["nickname"] == "C")
        assert (
            admin.post(f"/api/admin/songs/{chosen['id']}/pin-next").status_code == 200
        )
        first = admin.post("/api/admin/next").json()["active"]
        assert first["id"] == chosen["id"]
        assert store.current()["fair_exempt"] == 1
        before = snapshot(store)
        expected = store.state()["queue"][0]["id"]
        assert snapshot(store) == before
        assert (
            admin.post("/api/admin/finish", json={"song_id": first["id"]}).status_code
            == 200
        )
        assert store.rows("SELECT plays,fair_offset FROM guests WHERE name='C'") == [
            {"plays": 0, "fair_offset": 0}
        ]
        assert admin.post("/api/admin/next").json()["active"]["id"] == expected


def test_pin_cancel_and_late_joiner_prediction(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B"])
    with stack:
        store = app.state.store
        for name in guests:
            ready(store, name, 3)
        store.db.execute("UPDATE guests SET plays=6 WHERE name='A'")
        store.db.commit()
        first = admin.post("/api/admin/next").json()["active"]
        new_guest = stack.enter_context(TestClient(app))
        join_and_register(new_guest, "C")
        ready(store, "C", 3)
        predicted = store.fair_queue()[0]["id"]
        assert predicted in {x["id"] for x in store.songs() if x["nickname"] == "C"}
        assert admin.post(f"/api/admin/songs/{predicted}/pin-next").status_code == 200
        assert store.fair_queue()[0]["id"] == predicted
        assert new_guest.post(f"/api/songs/{predicted}/cancel").status_code == 200
        expected = store.fair_queue()[0]["id"]
        assert (
            admin.post("/api/admin/finish", json={"song_id": first["id"]}).status_code
            == 200
        )
        assert admin.post("/api/admin/next").json()["active"]["id"] == expected


def test_replay_is_a_virtual_admin_turn_while_another_song_is_active(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B"])
    with stack:
        store = app.state.store
        for name in guests:
            ready(store, name, 2)
        initial = admin.post("/api/admin/next").json()["active"]
        assert (
            admin.post("/api/admin/finish", json={"song_id": initial["id"]}).status_code
            == 200
        )
        active = admin.post("/api/admin/next").json()["active"]
        assert active["nickname"] == "B"
        old_video = store.media / "A-0.mp4"
        old_video.write_bytes(b"video")
        replay = admin.post(f"/api/admin/songs/{initial['id']}/replay")
        assert replay.status_code == 200, replay.text
        replay_id = replay.json()["id"]
        before = snapshot(store)
        assert store.fair_queue()[0]["id"] == replay_id
        assert snapshot(store) == before
        assert (
            admin.post("/api/admin/finish", json={"song_id": active["id"]}).status_code
            == 200
        )
        assert admin.post("/api/admin/next").json()["active"]["id"] == replay_id
        before_stats = store.rows("SELECT plays,fair_offset FROM guests ORDER BY name")
        assert (
            admin.post("/api/admin/finish", json={"song_id": replay_id}).status_code
            == 200
        )
        assert (
            store.rows("SELECT plays,fair_offset FROM guests ORDER BY name")
            == before_stats
        )
