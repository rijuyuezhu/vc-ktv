"""Event lifecycle router keeps the current worker and permissions in sync."""

from fastapi.testclient import TestClient

from vc_ktv.application.factory import create_app


def test_event_lifecycle_updates_worker_and_respects_admin_permissions(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="test-admin",
        secret=b"event-admin-api-contract",
        auto_prepare=False,
    )
    initial = app.state.autoprep
    lifecycle = app.state.lifecycle
    assert lifecycle.worker is initial
    assert not lifecycle.running
    with TestClient(app) as admin, TestClient(app) as outsider:
        assert lifecycle.running
        assert outsider.post("/api/admin/events", json={"name": "A"}).status_code == 401
        assert (
            admin.post("/api/admin/login", json={"password": "test-admin"}).status_code
            == 200
        )

        created = admin.post("/api/admin/events", json={"name": "A"})
        assert created.status_code == 200, created.text
        first_worker = app.state.autoprep
        assert lifecycle.worker is first_worker
        assert first_worker is not initial
        assert first_worker.store is app.state.events.current_store
        assert app.state.events.active_id == created.json()["id"]
        assert admin.get("/api/health").json()["autoprep_ready"] is False
        assert outsider.post("/api/admin/events/close").status_code == 401

        assert admin.post("/api/admin/events/close").status_code == 200
        assert app.state.events.active_id is None
        created_again = admin.post("/api/admin/events", json={"name": "B"})
        assert created_again.status_code == 200
        assert app.state.autoprep is not first_worker
        assert lifecycle.worker is app.state.autoprep
        assert app.state.autoprep.store is app.state.events.current_store
        assert (
            admin.delete(f"/api/admin/events/{created.json()['id']}").status_code == 200
        )
        assert app.state.events.active_id == created_again.json()["id"]
    assert not lifecycle.running
