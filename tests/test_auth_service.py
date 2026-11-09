"""Per-app token and proxy trust boundaries after auth extraction."""

from fastapi import Request

from vc_ktv.application.factory import create_app


def test_signed_tokens_are_app_local_and_purpose_scoped(tmp_path):
    first = create_app(
        root=tmp_path / "first",
        password="admin",
        secret=b"first-app",
        auto_prepare=False,
    )
    second = create_app(
        root=tmp_path / "second",
        password="admin",
        secret=b"second-app",
        auto_prepare=False,
    )
    token = first.state.auth.sign("guest", "event:user", 3600)
    assert first.state.auth.read_signed(token, "guest") == "event:user"
    assert first.state.auth.read_signed(token, "admin") is None
    assert second.state.auth.read_signed(token, "guest") is None
    assert (
        first.state.auth.read_signed(first.state.auth.sign("guest", "x", -1), "guest")
        is None
    )
    assert first.state.auth.read_signed("invalid", "guest") is None
    first.state.auth.failed_login[("ip", "user")] = [1]
    assert second.state.auth.failed_login == {}


def test_proxy_ip_is_trusted_only_for_configured_peers(tmp_path, monkeypatch):
    monkeypatch.setenv("VC_KTV_TRUSTED_PROXY_IPS", "127.0.0.1")
    app = create_app(
        root=tmp_path / "room",
        password="admin",
        secret=b"proxy-test",
        auto_prepare=False,
    )

    def request(peer: str, forwarded: str):
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [(b"x-real-ip", forwarded.encode())],
                "client": (peer, 31415),
            }
        )

    assert (
        app.state.auth.client_ip(request("127.0.0.1", "198.51.100.7")) == "198.51.100.7"
    )
    assert (
        app.state.auth.client_ip(request("203.0.113.8", "198.51.100.7"))
        == "203.0.113.8"
    )
    assert app.state.auth.client_ip(request("127.0.0.1", "invalid")) == "127.0.0.1"
