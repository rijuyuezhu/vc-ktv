"""Account routes retain global-admin-only access and revision invalidation."""

from fastapi.testclient import TestClient

from vc_ktv.application.factory import create_app


def test_admin_account_routes_preserve_authorization(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="global-test",
        secret=b"accounts-test",
        auto_prepare=False,
    )
    app.state.events.create("Room", "123456")
    with TestClient(app) as global_admin, TestClient(app) as operator:
        assert operator.get("/api/admin/accounts").status_code == 401
        assert (
            global_admin.post(
                "/api/admin/login", json={"password": "global-test"}
            ).status_code
            == 200
        )
        assert global_admin.get("/api/admin/accounts").json() == {"accounts": []}
        created = global_admin.post(
            "/api/admin/accounts",
            json={"username": "helper", "password": "initial-pass"},
        )
        assert created.status_code == 201
        account_id = created.json()["id"]
        assert (
            operator.post(
                "/api/admin/login",
                json={"username": "helper", "password": "initial-pass"},
            ).status_code
            == 200
        )
        assert operator.get("/api/admin/accounts").status_code == 403
        assert operator.delete(f"/api/admin/accounts/{account_id}").status_code == 403
        assert (
            global_admin.put(
                f"/api/admin/accounts/{account_id}/password",
                json={"password": "new-pass-123"},
            ).status_code
            == 200
        )
        assert operator.get("/api/admin/state").status_code == 401
        assert (
            global_admin.delete(f"/api/admin/accounts/{account_id}").status_code == 200
        )
        assert global_admin.get("/api/admin/accounts").json() == {"accounts": []}
