"""Test helpers for short-lived accounts scoped to one KTV event."""


def join_and_register(client, username="Test User", code="123456"):
    joined = client.post("/api/join", json={"code": code})
    assert joined.status_code == 200, joined.text
    registration = client.post(
        "/api/users/register",
        json={
            "username": username,
            "password": "test-login-password",
            "confirm_password": "test-login-password",
        },
    )
    assert registration.status_code == 200, registration.text
    assert client.get("/api/entry").json()["authenticated"]


def primary_tv_token(admin, *, replace=False):
    """Complete a one-use TV pairing and return its active control credential."""
    response = admin.get("/api/admin/tv-link", params={"replace": replace})
    assert response.status_code == 200, response.text
    link = response.json()["url"]
    assert "/tv.html?pair=" in link
    from urllib.parse import unquote

    pair = unquote(link.partition("?pair=")[2])
    response = admin.post("/api/tv/activate", json={"pair": pair})
    assert response.status_code == 200, response.text
    return response.json()["token"]
