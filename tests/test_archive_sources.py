"""CSV history preserves provider identities for Bilibili and NetEase."""

import csv
import io

from fastapi.testclient import TestClient

from tests.helpers import join_and_register
from tests.test_events import admin_login, make_app


def test_export_mixed_source_ids_in_active_and_closed_event(tmp_path):
    app = make_app(tmp_path / "ktv")
    with TestClient(app) as admin, TestClient(app) as guest:
        admin_login(admin)
        created = admin.post("/api/admin/events", json={"name": "Mixed"}).json()
        join_and_register(guest, "点歌者", created["code"])
        store = app.state.events.active()
        requester = store.db.execute("SELECT id FROM guests LIMIT 1").fetchone()[0]
        with store.lock:
            store.db.executemany(
                """INSERT INTO songs (bvid,page,title,uploader,cover,duration,requester,
                   requested_at,status,source_type,source_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        "BV0000000001",
                        2,
                        "B站标题",
                        "UP",
                        "",
                        110,
                        requester,
                        1,
                        "played",
                        "bilibili",
                        "",
                    ),
                    (
                        "",
                        1,
                        "=假公式",
                        "Singer",
                        "",
                        190,
                        requester,
                        2,
                        "played",
                        "netease",
                        "2613869782",
                    ),
                ],
            )
            store.db.commit()
        path = f"/api/admin/events/{created['id']}/songs.csv"
        assert guest.get(path).status_code == 401
        for _ in range(2):
            response = admin.get(path)
            assert response.status_code == 200, response.text
            assert response.content.startswith(b"\xef\xbb\xbf")
            rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
            assert list(rows[0]) == [
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
            assert rows[0]["bvid"] == "BV0000000001"
            assert rows[0]["page"] == "2"
            assert rows[0]["source_type"] == "bilibili"
            assert rows[0]["source_id"] == ""
            assert rows[1]["bvid"] == ""
            assert rows[1]["title"] == "'=假公式"
            assert rows[1]["source_type"] == "netease"
            assert rows[1]["source_id"] == "2613869782"
            if _ == 0:
                assert admin.post("/api/admin/events/close").status_code == 200
