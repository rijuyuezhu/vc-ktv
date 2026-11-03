"""Account writes must not leave partial changes in the shared SQLite connection."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.test_manual_casting_queue import new_app


class FaultDB:
    def __init__(self, db, *, statement="", fail_commit=False):
        self.db = db
        self.statement = statement
        self.fail_commit = fail_commit
        self.failures = 0
        self.rollbacks = 0

    def execute(self, sql, *args):
        if self.statement and self.statement in sql and not self.failures:
            self.failures += 1
            raise sqlite3.OperationalError("injected account SQL failure")
        return self.db.execute(sql, *args)

    def commit(self):
        if self.fail_commit and not self.failures:
            self.failures += 1
            raise sqlite3.OperationalError("injected account COMMIT failure")
        return self.db.commit()

    def rollback(self):
        self.rollbacks += 1
        return self.db.rollback()

    def __getattr__(self, name):
        return getattr(self.db, name)


def admin_login(client):
    assert (
        client.post("/api/admin/login", json={"password": "test-admin"}).status_code
        == 200
    )


def admin_account(client, name):
    return client.post(
        "/api/admin/accounts",
        json={"username": name, "password": "temporary-pass-123"},
    )


@pytest.mark.parametrize(
    "fault",
    ("guest_insert", "user_insert", "commit"),
)
def test_register_sqlite_failure_rolls_back_both_account_rows(tmp_path, fault):
    app = new_app(tmp_path)
    store = app.state.store
    with TestClient(app, raise_server_exceptions=False) as guest:
        assert guest.post("/api/join", json={"code": "123456"}).status_code == 200
        before = store.rows("SELECT id,name FROM guests ORDER BY id")
        db = store.db
        bad = FaultDB(
            db,
            statement=(
                "INSERT INTO guests"
                if fault == "guest_insert"
                else "INSERT INTO users"
                if fault == "user_insert"
                else ""
            ),
            fail_commit=fault == "commit",
        )
        store.db = bad
        try:
            response = guest.post(
                "/api/users/register",
                json={
                    "username": "Alice",
                    "password": "temporary-password",
                    "confirm_password": "temporary-password",
                },
            )
        finally:
            store.db = db
        assert response.status_code == 500
        assert bad.failures == 1
        assert bad.rollbacks == 1
        assert not db.in_transaction
        assert store.rows("SELECT id,name FROM guests ORDER BY id") == before
        assert store.rows("SELECT id,username FROM users") == []
        assert guest.get("/api/entry").json()["authenticated"] is False
        retry = guest.post(
            "/api/users/register",
            json={
                "username": "Alice",
                "password": "temporary-password",
                "confirm_password": "temporary-password",
            },
        )
        assert retry.status_code == 200
        assert len(store.rows("SELECT id FROM users")) == 1


@pytest.mark.parametrize("fault", ("insert", "commit"))
def test_event_admin_create_rolls_back_on_sqlite_failure(tmp_path, fault):
    app = new_app(tmp_path)
    store = app.state.store
    with TestClient(app, raise_server_exceptions=False) as owner:
        admin_login(owner)
        db = store.db
        bad = FaultDB(
            db,
            statement="INSERT INTO event_admins" if fault == "insert" else "",
            fail_commit=fault == "commit",
        )
        store.db = bad
        try:
            response = admin_account(owner, "operator")
        finally:
            store.db = db
        assert response.status_code == 500
        assert bad.failures == 1
        assert bad.rollbacks == 1
        assert not db.in_transaction
        assert store.rows("SELECT id FROM event_admins") == []
        assert admin_account(owner, "operator").status_code == 201
        assert len(store.rows("SELECT id FROM event_admins")) == 1


def test_duplicate_event_admin_rejects_without_leaving_transaction(tmp_path):
    app = new_app(tmp_path)
    store = app.state.store
    with TestClient(app, raise_server_exceptions=False) as owner:
        admin_login(owner)
        assert admin_account(owner, "operator").status_code == 201
        db = store.db
        spy = FaultDB(db)
        store.db = spy
        try:
            response = admin_account(owner, "operator")
        finally:
            store.db = db
        assert response.status_code == 409
        assert spy.rollbacks == 1
        assert not db.in_transaction
        assert len(store.rows("SELECT id FROM event_admins")) == 1
        assert admin_account(owner, "second").status_code == 201


@pytest.mark.parametrize("action", ("reset", "revoke"))
@pytest.mark.parametrize("fault", ("execute", "commit"))
def test_event_admin_mutation_sqlite_failure_preserves_credentials(
    tmp_path, action, fault
):
    app = new_app(tmp_path)
    store = app.state.store
    with TestClient(app, raise_server_exceptions=False) as owner:
        admin_login(owner)
        account_id = admin_account(owner, "operator").json()["id"]
        before = store.rows(
            "SELECT id,password_hash,revision FROM event_admins WHERE id=?",
            (account_id,),
        )
        db = store.db
        bad = FaultDB(
            db,
            statement=(
                "UPDATE event_admins"
                if action == "reset"
                else "DELETE FROM event_admins"
            )
            if fault == "execute"
            else "",
            fail_commit=fault == "commit",
        )
        method = lambda: (
            owner.put(
                f"/api/admin/accounts/{account_id}/password",
                json={"password": "changed-temporary-pass"},
            )
            if action == "reset"
            else owner.delete(f"/api/admin/accounts/{account_id}")
        )
        store.db = bad
        try:
            response = method()
        finally:
            store.db = db
        assert response.status_code == 500
        assert bad.failures == 1
        assert bad.rollbacks == 1
        assert not db.in_transaction
        assert (
            store.rows(
                "SELECT id,password_hash,revision FROM event_admins WHERE id=?",
                (account_id,),
            )
            == before
        )
        assert method().status_code == 200
