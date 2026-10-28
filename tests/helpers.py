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
