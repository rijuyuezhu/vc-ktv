"""State and active details must describe the same playback moment."""

import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import pytest
from fastapi.testclient import TestClient

from tests.helpers import join_and_register, primary_tv_token
from tests.test_manual_casting_queue import add_song, new_app
from vc_ktv.core.store import Store


@pytest.mark.parametrize("endpoint", ("admin", "tv"))
def test_state_and_active_are_one_snapshot_during_finish(
    tmp_path, monkeypatch, endpoint
):
    app = new_app(tmp_path)
    with TestClient(app) as admin, TestClient(app) as poller, TestClient(app) as guest:
        join_and_register(guest, "Singer")
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )
        store = app.state.store
        singer = store.rows("SELECT id FROM users")[0]["id"]
        song = add_song(store, singer, media="playing.mp4")
        assert admin.post("/api/admin/next").json()["active"]["id"] == song

        if endpoint == "admin":
            poller.cookies.update(admin.cookies)
            route = "/api/admin/state"
        else:
            token = primary_tv_token(admin)
            route = f"/api/tv/state?token={token}"

        started = threading.Event()
        release = threading.Event()
        original = Store.state

        def paused_state(self):
            snapshot = original(self)
            started.set()
            assert release.wait(5)
            return snapshot

        monkeypatch.setattr(Store, "state", paused_state)
        with ThreadPoolExecutor(max_workers=2) as executor:
            poll = executor.submit(poller.get, route)
            try:
                assert started.wait(3)
                finish = executor.submit(
                    admin.post, "/api/admin/finish", json={"song_id": song}
                )
                with pytest.raises(FutureTimeout):
                    finish.result(timeout=0.4)
            finally:
                release.set()
            response = poll.result(timeout=5)
            finished = finish.result(timeout=5)
        assert response.status_code == 200
        assert finished.status_code == 200
        state = response.json()
        assert state["current"]["id"] == song
        assert state["active"]["id"] == song
        latest = poller.get(route).json()
        assert latest["current"] is None and latest["active"] is None
