"""Late arrivals accrue no inactive-round debt, and only real performances count."""

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.app import Store, create_app


def test_newcomer_gets_only_two_early_turns_but_plays_remain_real(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="admin",
        secret=b"fair-test",
        auto_prepare=False,
    )
    app.state.events.create("Party", "123456")
    with TestClient(app) as a, TestClient(app) as b, TestClient(app) as c:
        join_and_register(a, "A")
        join_and_register(b, "B")
        store = app.state.store
        with store.lock:
            store.db.execute("UPDATE guests SET plays=3,seconds=450 WHERE name='A'")
            store.db.execute("UPDATE guests SET plays=2,seconds=290 WHERE name='B'")
            store.db.commit()
        join_and_register(c, "C")
        with store.lock:
            users = {u["name"]: u for u in store.rows("SELECT * FROM guests")}
            assert users["C"]["plays"] == 0 and users["C"]["seconds"] == 0
            assert users["C"]["fair_offset"] == 0
            assert users["C"]["fair_seconds"] == 0
            for idx, name in enumerate(("A", "B", "C"), 1):
                store.db.execute(
                    """INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,
                       status,prep_state,media_file)
                       VALUES('BV0000000001',?,?,?,?,?,?,?,?,?,?)""",
                    (
                        idx,
                        name,
                        "UP",
                        "",
                        80,
                        users[name]["id"],
                        idx,
                        "ready",
                        "manual",
                        f"{name}.mp4",
                    ),
                )
            store.db.commit()
            assert [s["nickname"] for s in store.fair_queue()] == ["C", "B", "A"]
            assert [u["plays"] for u in store.state()["guests"]] == [3, 2, 0]
            store.save_settings("fcfs", False)
            assert [s["nickname"] for s in store.fair_queue()] == ["A", "B", "C"]
            store.save_settings("fair", False)
            assert [s["nickname"] for s in store.fair_queue()] == ["C", "B", "A"]
            store.db.execute("UPDATE songs SET priority_next=1 WHERE title=?", ("C",))
            store.db.commit()
            assert store.fair_queue()[0]["nickname"] == "C"
        assert c.post("/api/users/logout").status_code == 200
        assert c.post("/api/join", json={"code": "123456"}).status_code == 200
        assert (
            c.post(
                "/api/users/login",
                json={"username": "C", "password": "test-login-password"},
            ).status_code
            == 200
        )
        assert tuple(
            store.db.execute(
                "SELECT plays,fair_offset FROM guests WHERE name='C'"
            ).fetchone()
        ) == (0, 0)


def test_newcomer_registration_does_not_inherit_inactive_guest_credit(tmp_path):
    root = tmp_path / "room"
    app = create_app(
        root=root, password="admin", secret=b"fair-test", auto_prepare=False
    )
    app.state.events.create("Party", "123456")
    with TestClient(app) as a, TestClient(app) as b, TestClient(app) as c:
        join_and_register(a, "A")
        store = app.state.store
        store.db.execute("UPDATE guests SET plays=5,seconds=500 WHERE name='A'")
        store.db.commit()
        join_and_register(b, "B")
        store.db.execute("UPDATE guests SET plays=1,seconds=60 WHERE name='B'")
        store.db.commit()
        join_and_register(c, "C")
        # Registration is not a turn. Neither B nor C receives a credit
        # until their first playable song competes in the queue.
        assert tuple(
            store.db.execute(
                "SELECT fair_offset,fair_seconds FROM guests WHERE name='C'"
            ).fetchone()
        ) == (0, 0)
        assert tuple(
            store.db.execute(
                "SELECT plays,seconds FROM guests WHERE name='B'"
            ).fetchone()
        ) == (1, 60)
    reopened = Store(root / "sessions" / app.state.events.active_id)
    counts = {u["name"]: u for u in reopened.rows("SELECT * FROM guests")}
    assert counts["B"]["fair_offset"] == 0
    assert counts["B"]["fair_seconds"] == 0
    assert counts["C"]["fair_offset"] == 0
    assert counts["C"]["plays"] == 0


def test_legacy_guest_table_gets_zero_offsets(tmp_path):
    store = Store(tmp_path / "old")
    store.db.execute(
        "INSERT INTO guests(id,name,plays,seconds) VALUES('old','Old',4,350)"
    )
    store.db.commit()
    reopened = Store(tmp_path / "old")
    old = reopened.rows("SELECT * FROM guests WHERE id='old'")[0]
    assert old["fair_offset"] == 0 and old["fair_seconds"] == 0
    assert old["plays"] == 4 and old["seconds"] == 350
