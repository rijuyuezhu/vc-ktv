"""Cancelling a track also stops its subprocesses, never another song's."""

import subprocess
import threading
import time

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from vc_ktv.application.factory import create_app
from vc_ktv.core.store import Store
from vc_ktv.processing.worker import AutoPrep


def eventually(condition, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_admin_cancel_kills_background_download_process(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="pw",
        secret=b"cancel-process",
        auto_prepare=False,
    )
    app.state.events.create("Test", "123456")

    async def lookup(bvid, page):
        return {
            "bvid": bvid,
            "page": page,
            "title": "A",
            "uploader": "Uploader",
            "cover": "",
            "duration": 50,
        }

    app.state.lookup = lookup
    spawned = []
    ready = threading.Event()

    def download(_bvid, _page, _directory):
        child = subprocess.Popen(["sleep", "30"])
        spawned.append(child)
        ready.set()
        child.wait()
        raise RuntimeError("download cancelled")

    worker = app.state.autoprep
    worker.download = download
    worker.start()
    try:
        with TestClient(app) as guest, TestClient(app) as admin:
            join_and_register(guest, "Singer")
            song_id = guest.post("/api/songs", json={"bvid": "BV0000000002"}).json()[
                "id"
            ]
            assert ready.wait(4)
            assert (
                admin.post("/api/admin/login", json={"password": "pw"}).status_code
                == 200
            )
            response = admin.post(f"/api/admin/songs/{song_id}/cancel")
            assert response.status_code == 200, response.text
            assert eventually(lambda: spawned[0].poll() is not None)
            assert spawned[0].returncode == -9
            assert eventually(lambda: worker.active_claims == 0)
            with app.state.store.lock:
                assert (
                    app.state.store.db.execute(
                        "SELECT status FROM songs WHERE id=?", (song_id,)
                    ).fetchone()[0]
                    == "cancelled"
                )
    finally:
        worker.stop()


def test_cancelling_one_of_two_workers_preserves_other_song(tmp_path):
    store = Store(tmp_path / "room")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Singer')")
        for song_id in (1, 2):
            store.db.execute(
                "INSERT INTO songs(id,bvid,page,title,uploader,cover,duration,requester,requested_at,status,prep_state) "
                "VALUES(?,?,1,'A','Uploader','',20,'g',?,'waiting','pending')",
                (song_id, f"BV{song_id:010d}", song_id),
            )
        store.db.commit()
    processes = {}
    started = threading.Event()
    lock = threading.Lock()

    def download(bvid, page, directory):
        proc = subprocess.Popen(["sleep", "30"])
        with lock:
            processes[bvid] = proc
            if len(processes) == 2:
                started.set()
        proc.wait()
        raise RuntimeError("download cancelled")

    worker = AutoPrep(store, lambda _: 20, download=download, max_workers=2)
    worker.start()
    try:
        assert started.wait(4)
        with store.lock:
            store.db.execute("UPDATE songs SET status='cancelled' WHERE id=1")
            store.db.commit()
        assert worker.cancel_song(1)
        assert eventually(lambda: processes["BV0000000001"].poll() is not None)
        assert processes["BV0000000001"].returncode == -9
        assert processes["BV0000000002"].poll() is None
        with store.lock:
            store.db.execute("UPDATE songs SET status='cancelled' WHERE id=2")
            store.db.commit()
        assert worker.cancel_song(2)
        assert eventually(lambda: worker.active_claims == 0)
    finally:
        for proc in processes.values():
            if proc.poll() is None:
                proc.kill()
        worker.stop()


def test_admin_cancel_kills_manual_ffmpeg_render(tmp_path, monkeypatch):
    import importlib

    server = importlib.import_module("vc_ktv.processing.manual_render")
    app = create_app(
        root=tmp_path / "room",
        password="pw",
        secret=b"cancel-manual",
        auto_prepare=False,
    )
    app.state.events.create("Manual", "123456")
    store = app.state.events.active()
    source = store.media / "source.mp4"
    source.write_bytes(b"source")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Singer')")
        store.db.execute(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,status,"
            "media_file,source_file,lyrics,prep_state) "
            "VALUES('BV0000000002',1,'A','Uploader','',20,'g',1,'ready',?,?,?,'manual')",
            (
                source.name,
                source.name,
                "[00:01.00]第一句歌词你好世界\n[00:03.00]第二句歌词你好世界",
            ),
        )
        store.db.commit()
    spawned = []
    ready = threading.Event()

    def render(_source, _lyrics, _destination, _duration, _lead):
        proc = subprocess.Popen(["sleep", "30"])
        spawned.append(proc)
        ready.set()
        assert proc.wait() == 0

    monkeypatch.setattr(server, "render_video", render)
    try:
        with TestClient(app) as admin:
            assert (
                admin.post("/api/admin/login", json={"password": "pw"}).status_code
                == 200
            )
            render_response = admin.post("/api/admin/songs/1/render", json={})
            assert render_response.status_code == 200, render_response.text
            assert ready.wait(4)
            assert admin.post("/api/admin/songs/1/cancel").status_code == 200
            assert eventually(lambda: spawned[0].poll() is not None)
            assert spawned[0].returncode == -9
            with store.lock:
                assert (
                    store.db.execute("SELECT status FROM songs WHERE id=1").fetchone()[
                        0
                    ]
                    == "cancelled"
                )
    finally:
        for proc in spawned:
            if proc.poll() is None:
                proc.kill()


def test_200_gib_budget_stops_manual_render_too(tmp_path, monkeypatch):
    import importlib

    server = importlib.import_module("vc_ktv.processing.manual_render")
    app = create_app(
        root=tmp_path / "room",
        password="pw",
        secret=b"budget-manual",
        auto_prepare=False,
    )
    app.state.events.create("Manual", "123456")
    store = app.state.events.active()
    source = store.media / "source.mp4"
    source.write_bytes(b"source")
    with store.lock:
        store.db.execute("INSERT INTO guests(id,name) VALUES('g','Singer')")
        store.db.execute(
            "INSERT INTO songs(bvid,page,title,uploader,cover,duration,requester,requested_at,status,"
            "media_file,source_file,lyrics,prep_state) "
            "VALUES('BV0000000002',1,'A','Uploader','',20,'g',1,'ready',?,?,?,'manual')",
            (
                source.name,
                source.name,
                "[00:01.00]第一句歌词你好世界\n[00:03.00]第二句歌词你好世界",
            ),
        )
        store.db.commit()
    store.disk_budget.limit_bytes = store.disk_budget.used_bytes() + 100_000
    spawned = []
    ready = threading.Event()

    def render(_source, _lyrics, destination, _duration, _lead):
        destination.write_bytes(b"x" * 250_000)
        proc = subprocess.Popen(["sleep", "30"])
        spawned.append(proc)
        ready.set()
        assert proc.wait() == 0

    monkeypatch.setattr(server, "render_video", render)
    worker = app.state.autoprep
    worker.start()
    try:
        with TestClient(app) as admin:
            assert (
                admin.post("/api/admin/login", json={"password": "pw"}).status_code
                == 200
            )
            assert admin.post("/api/admin/songs/1/render", json={}).status_code == 200
            assert ready.wait(4)
            assert eventually(lambda: spawned[0].poll() is not None, seconds=7)
            assert spawned[0].returncode == -9
            assert eventually(
                lambda: (
                    store.db.execute("SELECT status FROM songs WHERE id=1").fetchone()[
                        0
                    ]
                    == "ready"
                )
            )
            row = store.db.execute(
                "SELECT status,render_error FROM songs WHERE id=1"
            ).fetchone()
            assert row["status"] == "ready" and "200 GiB" in row["render_error"]
    finally:
        for proc in spawned:
            if proc.poll() is None:
                proc.kill()
        worker.stop()
