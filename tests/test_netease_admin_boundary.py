"""Moving NetEase admin handlers must retain their routes and permissions."""

from fastapi.testclient import TestClient

from vc_ktv.app import create_app


def test_netease_login_route_methods_and_permissions(tmp_path):
    app = create_app(
        root=tmp_path / "room", password="test", secret=b"test", auto_prepare=False
    )
    routes = {
        (path, method.upper())
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    assert {
        ("/api/admin/netease/status", "GET"),
        ("/api/admin/netease/qr", "POST"),
        ("/api/admin/netease/check", "POST"),
        ("/api/admin/netease/import", "POST"),
        ("/api/admin/netease/disconnect", "POST"),
    } <= routes
    with TestClient(app) as client:
        assert client.get("/api/admin/netease/status").status_code == 401
        assert client.post("/api/admin/netease/qr").status_code == 401
