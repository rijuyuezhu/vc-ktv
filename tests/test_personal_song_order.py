"""A singer may reorder playable songs without moving singer turns."""

import csv
import io
import sqlite3

import pytest

from tests.test_fair_capped_two import party, ready


def queue(store):
    return [(s["nickname"], s["title"]) for s in store.fair_queue()]


def my_songs(store, name):
    return [
        s["id"]
        for s in store.songs()
        if s["nickname"] == name and s["status"] == "ready"
    ]


def export(admin, event_id):
    res = admin.get(f"/api/admin/events/{event_id}/songs.csv")
    assert res.status_code == 200
    return list(csv.DictReader(io.StringIO(res.text.lstrip("\ufeff"))))


@pytest.mark.parametrize("mode", ["fair", "fcfs"])
def test_repeated_moves_preserve_other_singers_slots_and_actual_counts(tmp_path, mode):
    app, guests, admin, stack = party(tmp_path, ["A", "B"])
    with stack:
        store = app.state.store
        ready(store, "A", 3, start=1)
        ready(store, "B", 3, start=1)
        store.save_settings(mode, False)
        a1, a2, a3 = my_songs(store, "A")
        b1, b2, b3 = my_songs(store, "B")
        orig = [s["id"] for s in store.fair_queue()]
        assert orig == [a1, b1, a2, b2, a3, b3]
        counters = store.rows(
            "SELECT id,plays,seconds,fair_offset FROM guests ORDER BY name"
        )

        assert guests["A"].post(f"/api/songs/{a3}/move-first").status_code == 200
        assert [s["id"] for s in store.fair_queue()] == [a3, b1, a1, b2, a2, b3]
        assert (
            guests["A"].get(f"/api/songs/{a3}/detail").json()["can_move_first"] is False
        )
        assert (
            guests["A"].get(f"/api/songs/{a1}/detail").json()["can_move_first"] is True
        )

        assert guests["A"].post(f"/api/songs/{a2}/move-first").status_code == 200
        expected = [a2, b1, a3, b2, a1, b3]
        assert [s["id"] for s in store.fair_queue()] == expected
        assert guests["A"].post(f"/api/songs/{a2}/move-first").status_code == 200
        assert [s["id"] for s in store.fair_queue()] == expected
        assert [
            s["id"] for s in guests["A"].get("/api/state").json()["queue"]
        ] == expected
        assert [
            s["id"] for s in admin.get("/api/admin/state").json()["queue"]
        ] == expected
        assert (
            store.rows("SELECT id,plays,seconds,fair_offset FROM guests ORDER BY name")
            == counters
        )

        # Active CSV retains original request timestamps while showing the
        # predicted order; closing the room freezes that order.
        event = app.state.events.active_id
        before = export(admin, event)
        assert [r["title"] for r in before] == [
            "A#2",
            "B#1",
            "A#3",
            "B#2",
            "A#1",
            "B#3",
        ]
        assert [r["requested_at"] for r in before] == ["2", "1", "3", "2", "1", "3"]
        assert all(r["played_at"] == "" for r in before)
        assert admin.post("/api/admin/events/close").status_code == 200
        after = export(admin, event)
        assert [r["title"] for r in after] == [r["title"] for r in before]
        assert list(after[0]) == [
            "order",
            "title",
            "requester",
            "status",
            "source_type",
            "source_id",
            "bvid",
            "page",
            "requested_at",
            "played_at",
        ]
        assert all(r["status"] == "closed_unplayed" for r in after)
        with sqlite3.connect(
            tmp_path / "room" / "sessions" / event / "room.sqlite3"
        ) as db:
            frozen = db.execute(
                "SELECT id FROM songs ORDER BY archive_order"
            ).fetchall()
            assert [r[0] for r in frozen] == expected


def test_owner_restrictions_and_only_playable_prepared_songs(tmp_path, monkeypatch):
    monkeypatch.setenv("VC_KTV_BACKING_TRACKS", "1")
    app, guests, admin, stack = party(tmp_path, ["A", "B"])
    with stack:
        store = app.state.store
        ready(store, "A", 2)
        ready(store, "B", 1)
        a1, a2 = my_songs(store, "A")
        b1 = my_songs(store, "B")[0]
        user = store.db.execute("SELECT id FROM guests WHERE name='A'").fetchone()[0]
        not_ready = store.db.execute(
            """INSERT INTO songs (bvid,page,title,uploader,cover,duration,
               requester,requested_at,status,prep_state,media_file)
               VALUES ('BV0000000001',1,'下载中','UP','',90,?,4,'waiting','pending',NULL)""",
            (user,),
        ).lastrowid
        store.db.commit()
        assert admin.post(f"/api/songs/{a2}/move-first").status_code == 401
        assert guests["B"].post(f"/api/songs/{a2}/move-first").status_code == 403
        assert guests["A"].post(f"/api/songs/{b1}/move-first").status_code == 403
        assert guests["A"].post("/api/songs/999999/move-first").status_code == 404
        assert guests["A"].post(f"/api/songs/{not_ready}/move-first").status_code == 409
        assert (
            guests["A"].get(f"/api/songs/{not_ready}/detail").json()["can_move_first"]
            is False
        )

        # Backing-ready requirement uses the normal can_play() rule, and no
        # pre-requirement personal order is modified on rejection.
        store.save_settings(None, None, require_backing_ready=True)
        assert guests["A"].post(f"/api/songs/{a2}/move-first").status_code == 409
        assert (
            store.db.execute(
                "SELECT personal_order FROM songs WHERE id=?", (a2,)
            ).fetchone()[0]
            == 0
        )
        store.save_settings(None, None, require_backing_ready=False)

        assert guests["A"].post(f"/api/songs/{a2}/move-first").status_code == 200
        assert [s["id"] for s in store.fair_queue() if s["requester_id"] == user] == [
            a2,
            a1,
        ]
        assert guests["A"].post(f"/api/songs/{a2}/cancel").status_code == 200
        assert [s["id"] for s in store.fair_queue() if s["requester_id"] == user] == [
            a1
        ]
        assert guests["A"].post(f"/api/songs/{a2}/move-first").status_code == 409
        assert (
            guests["A"].get(f"/api/songs/{a1}/detail").json()["can_move_first"] is False
        )


def test_personal_reorder_does_not_override_admin_pin_or_charge_exempt_turn(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B"])
    with stack:
        store = app.state.store
        ready(store, "A", 3)
        ready(store, "B", 2)
        a1, a2, a3 = my_songs(store, "A")
        b1, b2 = my_songs(store, "B")
        assert admin.post(f"/api/admin/songs/{b2}/pin-next").status_code == 200
        assert guests["A"].post(f"/api/songs/{a3}/move-first").status_code == 200
        assert [s["id"] for s in store.fair_queue()][0] == b2
        assert admin.post("/api/admin/next").json()["active"]["id"] == b2
        assert store.current()["fair_exempt"] == 1
        assert admin.post("/api/admin/finish", json={"song_id": b2}).status_code == 200
        assert (
            store.db.execute("SELECT plays FROM guests WHERE name='B'").fetchone()[0]
            == 0
        )
        assert [s["id"] for s in store.fair_queue()][0] == a3
        assert admin.post("/api/admin/next").json()["active"]["id"] == a3
        assert guests["A"].post(f"/api/songs/{a3}/move-first").status_code == 409
        assert admin.post("/api/admin/finish", json={"song_id": a3}).status_code == 200
        assert (
            store.db.execute("SELECT plays FROM guests WHERE name='A'").fetchone()[0]
            == 1
        )
        assert [s["id"] for s in store.fair_queue()] == [b1, a1, a2]


def test_replay_joins_personal_queue_and_order_survives_restart(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A"])
    with stack:
        store = app.state.store
        ready(store, "A", 3)
        a1, a2, a3 = my_songs(store, "A")
        assert guests["A"].post(f"/api/songs/{a3}/move-first").status_code == 200
        assert guests["A"].post(f"/api/songs/{a2}/move-first").status_code == 200
        assert admin.post("/api/admin/next").json()["active"]["id"] == a2
        assert admin.post("/api/admin/finish", json={"song_id": a2}).status_code == 200
        (store.media / "A-1.mp4").write_bytes(b"test replay video")
        replay = guests["A"].post(f"/api/songs/{a2}/replay")
        assert replay.status_code == 200, replay.text
        replay_id = replay.json()["id"]
        assert [s["id"] for s in store.fair_queue()] == [a3, a1, replay_id]
        assert guests["A"].post(f"/api/songs/{replay_id}/move-first").status_code == 200
        assert [s["id"] for s in store.fair_queue()] == [replay_id, a3, a1]
        store.db.commit()
        with sqlite3.connect(store.root / "room.sqlite3") as db:
            ordering = db.execute(
                "SELECT id,personal_order FROM songs ORDER BY id"
            ).fetchall()
            assert [r[1] for r in ordering] == [0, -2, -1, -2]


def test_reorder_singer_slots_remain_invariant_for_randomized_queue(
    tmp_path, monkeypatch
):
    import random

    from vc_ktv.core.store import Store

    rng = random.Random(474)
    store = Store(tmp_path / "simulated")
    original_rows = store.rows
    users = []

    def rows(sql, *args):
        return users if sql == "SELECT * FROM guests" else original_rows(sql, *args)

    monkeypatch.setattr(store, "rows", rows)
    try:
        for _ in range(120):
            users = [
                {
                    "id": f"u{i}",
                    "plays": rng.randint(0, 8),
                    "fair_offset": rng.randint(0, 3),
                }
                for i in range(rng.randint(2, 20))
            ]
            song_count = rng.randint(15, 150)
            pool = [
                {
                    "id": j,
                    "requester_id": rng.choice(users)["id"],
                    "requested_at": rng.randint(1, 200),
                    "personal_order": 0,
                    "priority_next": False,
                    "status": "ready",
                    "prep_state": "done",
                    "has_media": True,
                }
                for j in range(1, song_count + 1)
            ]
            pool.sort(key=lambda s: (s["requested_at"], s["id"]))
            # Mimic one administrator-global priority turn.
            if rng.randint(0, 2) == 0:
                rng.choice(pool)["priority_next"] = True
            for policy in ("fair", "fcfs"):
                settings = {"queue_policy": policy, "allow_unsubtitled": False}
                before = store.fair_queue(pool, settings)
                baseline_users = [s["requester_id"] for s in before]
                by_user = {}
                for s in pool:
                    by_user.setdefault(s["requester_id"], []).append(s)
                for singer_songs in by_user.values():
                    for priority, s in enumerate(
                        rng.sample(singer_songs, len(singer_songs))
                    ):
                        s["personal_order"] = priority - len(singer_songs)
                after = store.fair_queue(pool, settings)
                assert [s["requester_id"] for s in after] == baseline_users
                assert {s["id"] for s in after} == {s["id"] for s in before}
                # Reset before testing the next global scheduling policy.
                for s in pool:
                    s["personal_order"] = 0
    finally:
        store.db.close()


def test_existing_sqlite_song_records_migrate_to_default_order(tmp_path):
    from vc_ktv.core.store import Store

    event = tmp_path / "legacy"
    event.mkdir()
    with sqlite3.connect(event / "room.sqlite3") as db:
        db.execute(
            "CREATE TABLE songs(id INTEGER PRIMARY KEY, requester TEXT, requested_at INTEGER, status TEXT, media_file TEXT)"
        )
        db.execute(
            "INSERT INTO songs(id,requester,requested_at,status) VALUES(1,'a',1,'played')"
        )
    store = Store(event)
    try:
        assert (
            store.db.execute("SELECT personal_order FROM songs WHERE id=1").fetchone()[
                0
            ]
            == 0
        )
    finally:
        store.db.close()


def test_active_csv_and_close_keep_personal_order_when_backing_is_required(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VC_KTV_BACKING_TRACKS", "1")
    app, guests, admin, stack = party(tmp_path, ["A", "B"])
    with stack:
        store = app.state.store
        ready(store, "A", 2, start=1)
        ready(store, "B", 1, start=1)
        a1, a2 = my_songs(store, "A")
        assert guests["A"].post(f"/api/songs/{a2}/move-first").status_code == 200
        store.save_settings(None, None, require_backing_ready=True)
        # Forced backing has not been generated, yet the closed-room preview
        # should keep unplayed records and the singer's chosen order.
        expected = ["A#2", "B#1", "A#1"]
        event_id = app.state.events.active_id
        assert [r["title"] for r in export(admin, event_id)] == expected
        assert admin.post("/api/admin/events/close").status_code == 200
        assert [r["title"] for r in export(admin, event_id)] == expected
