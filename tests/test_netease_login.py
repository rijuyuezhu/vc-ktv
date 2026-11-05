"""Global-admin-only QR login, in-memory credentials, expiration and worker handoff."""

import base64
import stat
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from vc_ktv.app import create_app
from vc_ktv.netease_login import NeteaseLogin, NeteaseLoginError
from vc_ktv.netease_media import fetch_audio_url

KEY = "test_QR_KEY_for_netease_00001"
COOKIE = "test_MUSIC_U_cookie_private_123456789"


def fake_transport(steps):
    def handler(request):
        assert request.url.host == "music.163.com"
        assert request.method == "POST"
        assert request.headers.get("origin") == "https://music.163.com"
        form = parse_qs(request.content.decode("ascii"))
        assert len(form["params"][0]) >= 40
        assert len(form["encSecKey"][0]) == 256
        assert KEY not in request.content.decode("ascii")
        if request.url.path == "/weapi/login/qrcode/unikey":
            return httpx.Response(200, json={"code": 200, "unikey": KEY})
        assert request.url.path == "/weapi/login/qrcode/client/login"
        return httpx.Response(200, json=steps.pop(0))

    return httpx.MockTransport(handler)


def test_qr_lifecycle_persists_across_restart(tmp_path):
    now = [1000.0]
    login = NeteaseLogin(
        transport=fake_transport(
            [
                {"code": 801},
                {"code": 802},
                {"code": 803, "cookie": f"MUSIC_U={COOKIE}; __csrf=unrelated"},
            ]
        ),
        clock=lambda: now[0],
        storage_path=tmp_path / "private" / "netease-session.json",
    )
    qr = login.start_qr()
    assert qr["pending"] and not qr["connected"]
    assert qr["qr_image"].startswith("data:image/svg+xml;base64,")
    assert b"<svg" in base64.b64decode(qr["qr_image"].split(",", 1)[1])
    assert login.start_qr()["qr_image"] == qr["qr_image"]
    assert login.poll()["phase"] == "waiting"
    assert login.poll()["phase"] == "confirm"
    done = login.poll()
    assert done == {
        "connected": True,
        "pending": False,
        "qr_expires_in": 0,
        "phase": "connected",
    }
    assert COOKIE not in str(done)
    assert login.cookies() == {"MUSIC_U": COOKIE}
    file = tmp_path / "private" / "netease-session.json"
    assert file.is_file()
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert stat.S_IMODE(file.parent.stat().st_mode) == 0o700
    now[0] += 4 * 3600 * 15
    assert login.status()["connected"] is True
    login.close()
    restored = NeteaseLogin(storage_path=file)
    assert restored.cookies() == {"MUSIC_U": COOKIE}
    assert restored.start_qr()["connected"] is True
    assert restored.disconnect()["connected"] is False
    assert not file.exists()
    restored.close()


def test_weapi_set_cookie_header_is_persisted(tmp_path):
    def upstream(request):
        if request.url.path.endswith("/unikey"):
            return httpx.Response(200, json={"code": 200, "unikey": KEY})
        return httpx.Response(
            200,
            json={"code": 803},
            headers={"Set-Cookie": f"MUSIC_U={COOKIE}; Path=/; HttpOnly; Secure"},
        )

    login = NeteaseLogin(
        transport=httpx.MockTransport(upstream),
        storage_path=tmp_path / "private" / "netease-session.json",
    )
    login.start_qr()
    assert login.poll()["phase"] == "connected"
    assert login.cookies() == {"MUSIC_U": COOKIE}
    login.close()
    persisted = NeteaseLogin(storage_path=tmp_path / "private" / "netease-session.json")
    assert persisted.cookies() == {"MUSIC_U": COOKIE}
    persisted.close()


def test_qr_expiry_and_disconnect():
    now = [1234.0]
    login = NeteaseLogin(
        transport=fake_transport([{"code": 800}]), clock=lambda: now[0]
    )
    login.start_qr()
    assert login.poll()["phase"] == "expired"
    assert not login.status()["pending"]
    login.start_qr()
    now[0] += 181
    assert login.poll()["phase"] == "expired"
    assert login.disconnect() == {
        "connected": False,
        "pending": False,
        "qr_expires_in": 0,
    }
    login.close()


def test_global_only_routes_do_not_expose_cookies(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="global-pass",
        secret=b"netease-login-test",
        auto_prepare=False,
    )
    fake = app.state.netease_login
    fake.client.close()
    fake.client = httpx.Client(
        transport=fake_transport(
            [{"code": 801}, {"code": 803, "cookie": f"MUSIC_U={COOKIE}"}]
        ),
        follow_redirects=False,
    )
    with (
        TestClient(app) as owner,
        TestClient(app) as operator,
        TestClient(app) as stranger,
    ):
        urls = [
            ("GET", "/api/admin/netease/status"),
            ("POST", "/api/admin/netease/qr"),
            ("POST", "/api/admin/netease/check"),
            ("POST", "/api/admin/netease/disconnect"),
        ]
        for method, url in urls:
            assert stranger.request(method, url).status_code == 401
        assert (
            owner.post("/api/admin/login", json={"password": "global-pass"}).status_code
            == 200
        )
        assert (
            owner.post(
                "/api/admin/events", json={"name": "Connected trial"}
            ).status_code
            == 200
        )
        created = owner.post(
            "/api/admin/accounts",
            json={"username": "operator", "password": "operator-pass-1"},
        )
        assert created.status_code == 201, created.text
        assert (
            operator.post(
                "/api/admin/login",
                json={"username": "operator", "password": "operator-pass-1"},
            ).status_code
            == 200
        )
        for method, url in urls:
            assert operator.request(method, url).status_code == 403
        status = owner.get(urls[0][1])
        assert (
            status.status_code == 200 and status.headers["cache-control"] == "no-store"
        )
        qr = owner.post(urls[1][1])
        assert qr.status_code == 200, qr.text
        assert qr.json()["qr_image"].startswith("data:image/svg+xml;base64,")
        assert owner.post(urls[2][1]).json()["phase"] == "waiting"
        checked = owner.post(urls[2][1])
        assert checked.status_code == 200 and checked.json()["connected"] is True
        assert COOKIE not in checked.text
        assert COOKIE not in owner.get(urls[0][1]).text
        assert app.state.autoprep.netease_cookies() == {"MUSIC_U": COOKIE}
        assert owner.post(urls[3][1]).json()["connected"] is False
        assert app.state.autoprep.netease_cookies() is None
        assert COOKIE not in owner.post(urls[3][1]).text


def test_saved_cookie_survives_app_recreation_and_reaches_worker(tmp_path):
    root = tmp_path / "room"
    kwargs = dict(
        root=root, password="global-pass", secret=b"reopen-app", auto_prepare=False
    )
    app = create_app(**kwargs)
    login = app.state.netease_login
    login._save_cookie(COOKIE)
    login.music_cookie = COOKIE
    with TestClient(app) as owner:
        assert (
            owner.post("/api/admin/login", json={"password": "global-pass"}).status_code
            == 200
        )
        assert owner.get("/api/admin/netease/status").json()["connected"]
    app2 = create_app(**kwargs)
    assert app2.state.autoprep.netease_cookies() == {"MUSIC_U": COOKIE}
    with TestClient(app2) as owner:
        assert (
            owner.post("/api/admin/login", json={"password": "global-pass"}).status_code
            == 200
        )
        assert owner.get("/api/admin/netease/status").json()["connected"]
        assert owner.post("/api/admin/netease/disconnect").json()["connected"] is False
    assert not (root / ".private" / "netease-session.json").exists()
    app3 = create_app(**kwargs)
    assert app3.state.autoprep.netease_cookies() is None
    app3.state.netease_login.close()


def test_reject_exposed_cookie_file(tmp_path):
    session = tmp_path / "netease-session.json"
    session.write_text('{"MUSIC_U": "sensitive-cookie"}')
    session.chmod(0o644)
    with pytest.raises(NeteaseLoginError):
        NeteaseLogin(storage_path=session)


def test_authenticated_media_request_carries_restored_cookie():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": 42, "code": 200, "url": "https://m10.music.126.net/test.mp3"}
                ]
            },
        )

    url = fetch_audio_url(
        42, cookies={"MUSIC_U": COOKIE}, transport=httpx.MockTransport(handler)
    )
    assert url == "https://m10.music.126.net/test.mp3"
    assert len(requests) == 1
    assert requests[0].url.host == "music.163.com"
    assert requests[0].headers["cookie"] == f"MUSIC_U={COOKIE}"


def test_qr_failure_exposes_only_upstream_status_not_credentials(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="global-pass",
        secret=b"qr-rejected",
        auto_prepare=False,
    )
    login = app.state.netease_login
    login.client.close()
    login.client = httpx.Client(
        transport=fake_transport([{"code": 502, "cookie": f"MUSIC_U={COOKIE}"}]),
        follow_redirects=False,
    )
    with TestClient(app) as owner:
        assert (
            owner.post("/api/admin/login", json={"password": "global-pass"}).status_code
            == 200
        )
        assert owner.post("/api/admin/netease/qr").status_code == 200
        result = owner.post("/api/admin/netease/check")
        assert result.status_code == 409
        assert "502" in result.json()["detail"]
        assert COOKIE not in result.text
        assert login.cookies() is None
        assert not (tmp_path / "room" / ".private" / "netease-session.json").exists()


def test_qr_http_gateway_failure_identified(tmp_path):
    app = create_app(
        root=tmp_path / "room",
        password="global-pass",
        secret=b"qr-upstream-error",
        auto_prepare=False,
    )

    def upstream(request):
        if request.url.path.endswith("/unikey"):
            return httpx.Response(200, json={"code": 200, "unikey": KEY})
        return httpx.Response(502, json={"code": 502})

    login = app.state.netease_login
    login.client.close()
    login.client = httpx.Client(
        transport=httpx.MockTransport(upstream), follow_redirects=False
    )
    with TestClient(app) as owner:
        assert (
            owner.post("/api/admin/login", json={"password": "global-pass"}).status_code
            == 200
        )
        assert owner.post("/api/admin/netease/qr").status_code == 200
        result = owner.post("/api/admin/netease/check")
        assert result.status_code == 502
        assert "HTTP 502" in result.json()["detail"]
        assert login.cookies() is None


def test_qr_success_without_cookie_reports_missing_auth(tmp_path):
    login = NeteaseLogin(
        transport=fake_transport([{"code": 803}]),
        storage_path=tmp_path / "session.json",
    )
    login.start_qr()
    with pytest.raises(NeteaseLoginError, match="MUSIC_U"):
        login.poll()
    assert login.cookies() is None
    assert not (tmp_path / "session.json").exists()
    login.close()
