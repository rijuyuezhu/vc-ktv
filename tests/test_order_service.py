"""Song admission stays atomic across concurrent orders and event changes."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from vc_ktv.application.factory import create_app
from vc_ktv.sources.base import SongInfo


def test_song_insert_rechecks_event_and_guest_capacity(tmp_path, monkeypatch):
    app = create_app(
        root=tmp_path / "room",
        password="admin",
        secret=b"order-test",
        auto_prepare=False,
    )
    event_id = app.state.events.create("Room", "123456")["id"]
    store = app.state.store
    with store.lock:
        store.db.execute(
            "INSERT INTO guests(id,name) VALUES (?,?)", ("singer", "Singer")
        )
        store.db.commit()
    song = SongInfo(
        kind="bilibili",
        source_id="",
        bvid="BV0000000001",
        page=1,
        title="Song",
        uploader="Uploader",
        cover="",
        duration=60,
    )
    orders = app.state.orders

    with pytest.raises(HTTPException) as stale:
        orders.submit("singer", "old-event", song, changed_message="场次已变化")
    assert stale.value.status_code == 409
    assert stale.value.detail == "场次已变化"
    assert store.rows("SELECT * FROM songs") == []

    monkeypatch.setattr("vc_ktv.application.orders.MAX_USER_SONGS", 1)

    def submit():
        try:
            return (
                "ok",
                orders.submit("singer", event_id, song, changed_message="changed"),
            )
        except HTTPException as error:
            return ("error", error.status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(result[0] for result in outcomes) == ["error", "ok"]
    assert next(result[1] for result in outcomes if result[0] == "error") == 409
    assert len(store.rows("SELECT id FROM songs")) == 1
