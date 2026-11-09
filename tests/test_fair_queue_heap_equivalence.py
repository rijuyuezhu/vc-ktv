"""The faster queue preview must exactly match historical fair-turn ordering."""

import random

from vc_ktv.core.store import Store


def reference_queue(pool, settings, users, current, previous):
    """Reference pre-optimization scan used to validate the heap implementation."""
    ready = [s for s in pool if Store.playable(s, settings)]
    priority = [s for s in ready if s["priority_next"]]
    ready = [s for s in ready if not s["priority_next"]]
    if settings["queue_policy"] == "fcfs":
        return priority + ready
    tally = {u["id"]: u["plays"] + u["fair_offset"] for u in users}
    if current and not current["fair_exempt"]:
        tally[current["requester"]] += 1
    active = {s["requester_id"] for s in ready}
    if active:
        floor = max(0, max(tally[u] for u in active) - 2)
        for user in active:
            tally[user] = max(tally[user], floor)
    if current and not current["fair_exempt"]:
        previous = current["requester"]
    output = []
    while ready:
        head = {}
        for s in ready:
            head.setdefault(s["requester_id"], s)
        chosen = min(
            head.values(),
            key=lambda s: (
                tally[s["requester_id"]],
                s["requester_id"] == previous,
                s["requested_at"],
                s["id"],
            ),
        )
        output.append(chosen)
        tally[chosen["requester_id"]] += 1
        previous = chosen["requester_id"]
        ready.remove(chosen)
    return priority + output


def test_heap_matches_reference_across_song_counts_users_and_ties(
    tmp_path, monkeypatch
):
    rng = random.Random(341)
    store = Store(tmp_path / "queue")
    state = {"users": [], "current": None}
    original_rows = store.rows

    def rows(sql, *args):
        if sql == "SELECT * FROM guests":
            return state["users"]
        return original_rows(sql, *args)

    monkeypatch.setattr(store, "rows", rows)
    monkeypatch.setattr(store, "current", lambda: state["current"])
    try:
        for iteration in range(400):
            n_users = rng.randint(1, 45)
            users = [
                {
                    "id": f"u{j}",
                    "plays": rng.randint(0, 40),
                    "fair_offset": rng.randint(0, 5),
                }
                for j in range(n_users)
            ]
            state["users"] = users
            current_uid = rng.choice(users)["id"]
            state["current"] = (
                {"requester": current_uid, "fair_exempt": rng.choice([False, True])}
                if rng.choice([False, True])
                else None
            )
            previous = rng.choice(["", current_uid, rng.choice(users)["id"]])
            store.db.execute(
                "INSERT INTO room_state(name,value) VALUES('last_requester',?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (previous,),
            )
            store.db.commit()
            settings = {
                "queue_policy": rng.choice(["fair", "fair", "fcfs"]),
                "allow_unsubtitled": rng.choice([True, False]),
            }
            pool = []
            for song_id in range(1, rng.randint(0, 150) + 1):
                uid = rng.choice(users)["id"]
                pool.append(
                    {
                        "id": song_id,
                        "requested_at": rng.randint(1, 30),
                        "requester_id": uid,
                        "requester": uid,
                        "priority_next": rng.random() < 0.08,
                        "status": rng.choice(["ready", "ready", "ready", "waiting"]),
                        "has_media": rng.choice([True, True, True, False]),
                        "skip_subtitles": rng.choice([True, False]),
                        "prep_state": rng.choice(["done", "original", "aligning"]),
                    }
                )
            pool.sort(key=lambda song: (song["requested_at"], song["id"]))
            expected = reference_queue(
                pool, settings, users, state["current"], previous
            )
            actual = store.fair_queue(pool, settings)
            assert [song["id"] for song in actual] == [
                song["id"] for song in expected
            ], f"mismatched reference at iteration {iteration}"
    finally:
        store.db.close()


def test_heap_preserves_previous_singer_tiebreak_with_thousand_users(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "large")
    users = [{"id": f"u{i}", "plays": 0, "fair_offset": 0} for i in range(1000)]
    original_rows = store.rows
    monkeypatch.setattr(
        store,
        "rows",
        lambda sql, *args: (
            users if sql == "SELECT * FROM guests" else original_rows(sql, *args)
        ),
    )
    songs = [
        {
            "id": i + 1,
            "requested_at": i,
            "requester_id": f"u{i}",
            "requester": f"u{i}",
            "priority_next": False,
            "status": "ready",
            "has_media": True,
            "prep_state": "done",
        }
        for i in range(1000)
    ]
    settings = {"queue_policy": "fair", "allow_unsubtitled": True}
    store.db.execute("INSERT INTO room_state(name,value) VALUES('last_requester','u0')")
    store.db.commit()
    try:
        expected = reference_queue(songs, settings, users, None, "u0")
        actual = store.fair_queue(songs, settings)
        assert [s["id"] for s in actual] == [s["id"] for s in expected]
    finally:
        store.db.close()
