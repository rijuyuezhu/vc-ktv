"""Bounded catch-up with finite users cannot let a dormant guest monopolize turns."""

from contextlib import ExitStack

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import Store, create_app


def party(tmp_path, names):
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"fair-capped",
        auto_prepare=False,
    )
    app.state.events.create("Party", "123456")
    stack = ExitStack()
    guests = {}
    for name in names:
        client = stack.enter_context(TestClient(app))
        join_and_register(client, name)
        guests[name] = client
    admin = stack.enter_context(TestClient(app))
    assert (
        admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
        == 200
    )
    return app, guests, admin, stack


def ready(store, name, count, start=1):
    user = store.db.execute("SELECT id FROM guests WHERE name=?", (name,)).fetchone()[0]
    for i in range(count):
        store.db.execute(
            """INSERT INTO songs (bvid,page,title,uploader,cover,duration,requester,
               requested_at,status,prep_state,media_file)
               VALUES ('BV0000000001',1,?,'UP','',90,?,?,'ready','manual',?)""",
            (f"{name}#{i + 1}", user, start + i, f"{name}-{i}.mp4"),
        )
    store.db.commit()


def play_next(admin, store, *, skip=False):
    preview = store.fair_queue()
    assert preview
    expected = preview[0]["nickname"]
    response = admin.post("/api/admin/next")
    assert response.status_code == 200, response.text
    song = response.json()["active"]
    assert song["nickname"] == expected
    finished = admin.post(
        "/api/admin/finish",
        json={"song_id": song["id"], "reason": "skip" if skip else "completed"},
    )
    assert finished.status_code == 200, finished.text
    return expected


def test_two_or_three_guests_with_two_songs_each_rotate(tmp_path):
    for count in (2, 3, 5):
        app, guests, admin, stack = party(
            tmp_path / str(count), [chr(65 + i) for i in range(count)]
        )
        with stack:
            store = app.state.store
            for name in guests:
                ready(store, name, 2)
            assert [play_next(admin, store) for _ in range(count * 2)] == list(
                guests
            ) * 2
            assert not store.fair_queue()


def test_newcomer_after_everyone_has_two_gets_two_and_then_rotates(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B"])
    with stack:
        store = app.state.store
        for name in ("A", "B"):
            store.db.execute(
                "UPDATE guests SET plays=2,seconds=180 WHERE name=?", (name,)
            )
            ready(store, name, 5)
        # Newcomer's real plays are zero, effective starting point is 2 - 2.
        guest = stack.enter_context(TestClient(app))
        join_and_register(guest, "C")
        assert (
            store.db.execute(
                "SELECT fair_offset FROM guests WHERE name='C'"
            ).fetchone()[0]
            == 0
        )
        ready(store, "C", 5)
        assert [play_next(admin, store) for _ in range(8)] == [
            "C",
            "C",
            "A",
            "B",
            "C",
            "A",
            "B",
            "C",
        ]
        assert (
            store.db.execute("SELECT plays FROM guests WHERE name='C'").fetchone()[0]
            == 4
        )


def test_idle_return_only_gets_two_credit_and_never_regains_it_on_refresh(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B"])
    with stack:
        store = app.state.store
        store.db.execute("UPDATE guests SET plays=15,seconds=1350 WHERE name='A'")
        store.db.commit()
        ready(store, "A", 8)
        ready(store, "B", 8)
        for _ in range(4):
            assert store.fair_queue()[0]["nickname"] == "B"
        assert (
            store.db.execute(
                "SELECT fair_offset FROM guests WHERE name='B'"
            ).fetchone()[0]
            == 0
        )
        assert play_next(admin, store) == "B"
        offset = store.db.execute(
            "SELECT fair_offset FROM guests WHERE name='B'"
        ).fetchone()[0]
        assert offset == 13
        assert play_next(admin, store) == "B"
        # B has now used both of its catch-up turns; A gets one next.
        assert store.fair_queue()[0]["nickname"] == "A"
        assert (
            store.db.execute(
                "SELECT fair_offset FROM guests WHERE name='B'"
            ).fetchone()[0]
            == offset
        )
        assert play_next(admin, store) == "A"
        reopened = Store(store.root)
        assert (
            reopened.db.execute(
                "SELECT fair_offset FROM guests WHERE name='B'"
            ).fetchone()[0]
            == 13
        )
        assert [play_next(admin, store) for _ in range(5)] == ["B", "A", "B", "A", "B"]


def test_multiple_idle_guests_two_turn_catchup_then_round_robin(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B", "C"])
    with stack:
        store = app.state.store
        store.db.execute("UPDATE guests SET plays=10,seconds=900 WHERE name='A'")
        store.db.commit()
        for name in guests:
            ready(store, name, 7)
        assert [play_next(admin, store) for _ in range(11)] == [
            "B",
            "C",
            "B",
            "C",
            "A",
            "B",
            "C",
            "A",
            "B",
            "C",
            "A",
        ]
        assert (
            store.db.execute(
                "SELECT fair_offset FROM guests WHERE name='B'"
            ).fetchone()[0]
            == 8
        )
        assert (
            store.db.execute(
                "SELECT fair_offset FROM guests WHERE name='C'"
            ).fetchone()[0]
            == 8
        )


def test_five_guests_can_never_starve_with_finite_ready_population(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B", "C", "D", "E"])
    with stack:
        store = app.state.store
        for name, played in zip(guests, (14, 0, 2, 9, 4)):
            store.db.execute("UPDATE guests SET plays=? WHERE name=?", (played, name))
            ready(store, name, 16)
        turns = [play_next(admin, store) for _ in range(50)]
        # All remain eligible throughout the simulation. Each gets a turn
        # within two rounds of the rest, even with wildly different histories.
        for name in guests:
            positions = [i for i, winner in enumerate(turns) if winner == name]
            assert len(positions) >= 8, (name, turns)
            assert positions[0] <= 2 * (len(guests) - 1)
            assert all(
                b - a <= 2 * len(guests) for a, b in zip(positions, positions[1:])
            )


def test_skipping_a_selected_song_advances_only_virtual_count(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B"])
    with stack:
        store = app.state.store
        store.db.execute("UPDATE guests SET plays=10,seconds=900 WHERE name='A'")
        store.db.commit()
        ready(store, "A", 3)
        ready(store, "B", 5)
        assert play_next(admin, store, skip=True) == "B"
        assert tuple(
            store.db.execute(
                "SELECT plays,fair_offset FROM guests WHERE name='B'"
            ).fetchone()
        ) == (0, 9)
        assert play_next(admin, store, skip=True) == "B"
        assert tuple(
            store.db.execute(
                "SELECT plays,fair_offset FROM guests WHERE name='B'"
            ).fetchone()
        ) == (0, 10)
        assert play_next(admin, store) == "A"


def test_fcfs_and_pinned_song_unchanged(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B"])
    with stack:
        store = app.state.store
        store.db.execute("UPDATE guests SET plays=10 WHERE name='A'")
        ready(store, "A", 3, 1)
        ready(store, "B", 3, 20)
        store.save_settings("fcfs", False)
        assert play_next(admin, store) == "A"
        assert (
            store.db.execute(
                "SELECT fair_offset FROM guests WHERE name='B'"
            ).fetchone()[0]
            == 0
        )
        store.save_settings("fair", False)
        b = next(song for song in store.fair_queue() if song["nickname"] == "B")
        assert admin.post(f"/api/admin/songs/{b['id']}/pin-next").status_code == 200
        assert play_next(admin, store) == "B"


def test_cancel_pending_and_pinned_never_consumes_a_turn(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B"])
    with stack:
        store = app.state.store
        store.db.execute("UPDATE guests SET plays=10 WHERE name='A'")
        store.db.commit()
        ready(store, "A", 3)
        ready(store, "B", 5)
        b_songs = [x for x in store.songs() if x["nickname"] == "B"]
        assert (
            admin.post(f"/api/admin/songs/{b_songs[0]['id']}/pin-next").status_code
            == 200
        )
        # Cancel a song before it starts: no actual or virtual singer counts.
        assert (
            guests["B"].post(f"/api/songs/{b_songs[0]['id']}/cancel").status_code == 200
        )
        assert (
            guests["B"].post(f"/api/songs/{b_songs[0]['id']}/cancel").status_code == 409
        )
        assert not any(x["id"] == b_songs[0]["id"] for x in store.fair_queue())
        assert tuple(
            store.db.execute(
                "SELECT plays,fair_offset FROM guests WHERE name='B'"
            ).fetchone()
        ) == (0, 0)
        assert play_next(admin, store) == "B"
        assert tuple(
            store.db.execute(
                "SELECT plays,fair_offset FROM guests WHERE name='B'"
            ).fetchone()
        ) == (1, 8)
        assert (
            guests["B"].post(f"/api/songs/{b_songs[2]['id']}/cancel").status_code == 200
        )
        assert tuple(
            store.db.execute(
                "SELECT plays,fair_offset FROM guests WHERE name='B'"
            ).fetchone()
        ) == (1, 8)
        assert (
            admin.post(f"/api/admin/songs/{b_songs[3]['id']}/cancel").status_code == 200
        )
        assert tuple(
            store.db.execute(
                "SELECT plays,fair_offset FROM guests WHERE name='B'"
            ).fetchone()
        ) == (1, 8)
        # Cancellation has no special "refill" effect on virtual count.
        assert store.fair_queue()[0]["nickname"] == "B"


def test_admin_pin_and_replay_are_fictitious_admin_turns_not_user_plays(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B"])
    with stack:
        store = app.state.store
        ready(store, "A", 4)
        ready(store, "B", 4)
        # First normal turn establishes A as the last actual singer.
        assert play_next(admin, store) == "A"
        last = store.db.execute(
            "SELECT value FROM room_state WHERE name='last_requester'"
        ).fetchone()[0]
        original_stats = {
            x["name"]: (x["plays"], x["seconds"], x["fair_offset"])
            for x in store.rows("SELECT name,plays,seconds,fair_offset FROM guests")
        }
        b = next(x for x in store.fair_queue() if x["nickname"] == "B")
        assert admin.post(f"/api/admin/songs/{b['id']}/pin-next").status_code == 200
        for _ in range(4):
            assert store.fair_queue()[0]["id"] == b["id"]
        assert admin.post("/api/admin/next").json()["active"]["id"] == b["id"]
        playing = store.db.execute(
            "SELECT fair_exempt,priority_next FROM songs WHERE id=?", (b["id"],)
        ).fetchone()
        assert tuple(playing) == (1, 0)  # Survives priority flag reset.
        assert (
            admin.post("/api/admin/finish", json={"song_id": b["id"]}).status_code
            == 200
        )
        assert (
            store.db.execute(
                "SELECT fair_exempt,status FROM songs WHERE id=?", (b["id"],)
            ).fetchone()[0]
            == 1
        )
        assert {
            x["name"]: (x["plays"], x["seconds"], x["fair_offset"])
            for x in store.rows("SELECT name,plays,seconds,fair_offset FROM guests")
        } == original_stats
        assert (
            store.db.execute(
                "SELECT value FROM room_state WHERE name='last_requester'"
            ).fetchone()[0]
            == last
        )
        assert store.fair_queue()[0]["id"] != b["id"]
        assert store.fair_queue()[0]["nickname"] == "B"
        # A replay of A's already played song is an admin turn too.
        a_played = store.db.execute(
            "SELECT id FROM songs WHERE status='played' AND requester=?", (last,)
        ).fetchone()[0]
        (store.media / "A-0.mp4").write_bytes(b"replay original video")
        replay = admin.post(f"/api/admin/songs/{a_played}/replay")
        assert replay.status_code == 200, replay.text
        replay_id = replay.json()["id"]
        assert admin.post("/api/admin/next").json()["active"]["id"] == replay_id
        assert (
            store.db.execute(
                "SELECT fair_exempt FROM songs WHERE id=?", (replay_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            admin.post(
                "/api/admin/finish", json={"song_id": replay_id, "reason": "skip"}
            ).status_code
            == 200
        )
        assert {
            x["name"]: (x["plays"], x["seconds"], x["fair_offset"])
            for x in store.rows("SELECT name,plays,seconds,fair_offset FROM guests")
        } == original_stats
        assert (
            store.db.execute(
                "SELECT value FROM room_state WHERE name='last_requester'"
            ).fetchone()[0]
            == last
        )
        # Real normal queue proceeds as if these two admin tracks never
        # consumed a fairness turn.
        assert store.fair_queue()[0]["nickname"] == "B"
        reopened = Store(store.root)
        assert (
            reopened.db.execute(
                "SELECT fair_exempt FROM songs WHERE id=?", (replay_id,)
            ).fetchone()[0]
            == 1
        )


def test_admin_pin_does_not_persist_idle_catchup_when_claimed(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B"])
    with stack:
        store = app.state.store
        store.db.execute("UPDATE guests SET plays=10 WHERE name='A'")
        store.db.commit()
        ready(store, "A", 2)
        ready(store, "B", 3)
        a = next(x for x in store.songs() if x["nickname"] == "A")
        assert admin.post(f"/api/admin/songs/{a['id']}/pin-next").status_code == 200
        assert admin.post("/api/admin/next").json()["active"]["id"] == a["id"]
        assert (
            store.db.execute(
                "SELECT fair_offset FROM guests WHERE name='B'"
            ).fetchone()[0]
            == 0
        )
        assert (
            admin.post("/api/admin/finish", json={"song_id": a["id"]}).status_code
            == 200
        )
        assert (
            store.db.execute("SELECT plays FROM guests WHERE name='A'").fetchone()[0]
            == 10
        )
        assert play_next(admin, store) == "B"
        assert (
            store.db.execute(
                "SELECT fair_offset FROM guests WHERE name='B'"
            ).fetchone()[0]
            == 8
        )


def test_elapsed_seconds_and_song_length_do_not_influence_turn_order(tmp_path):
    app, guests, admin, stack = party(tmp_path, ["A", "B", "C"])
    with stack:
        store = app.state.store
        for i, name in enumerate(guests):
            store.db.execute(
                "UPDATE guests SET plays=3,seconds=?,fair_seconds=? WHERE name=?",
                (i * 5000, (3 - i) * 100_000, name),
            )
            ready(store, name, 2)
            store.db.execute(
                "UPDATE songs SET duration=? WHERE requester="
                "(SELECT id FROM guests WHERE name=?)",
                (3600 if name == "A" else 10 if name == "B" else 600, name),
            )
        store.db.commit()
        assert [play_next(admin, store) for _ in range(6)] == [
            "A",
            "B",
            "C",
            "A",
            "B",
            "C",
        ]
