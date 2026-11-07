"""Global-admin-only accounts for a single KTV event."""

import secrets
import sqlite3

from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel, Field

from ..accounts import password_hash


class EventAdminCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=24)
    password: str = Field(min_length=6, max_length=72)


class EventAdminPassword(BaseModel):
    password: str = Field(min_length=6, max_length=72)


def create_admin_accounts_router(app, require_global_admin) -> APIRouter:
    router = APIRouter()
    store = app.state.store

    def bail(status: int, message: str):
        raise HTTPException(status_code=status, detail=message)

    @router.get("/api/admin/accounts")
    def list_event_admins(vc_admin: str | None = Cookie(default=None)):
        require_global_admin(vc_admin)
        with store.lock:
            return {
                "accounts": store.rows(
                    "SELECT id,username FROM event_admins ORDER BY username COLLATE NOCASE"
                )
            }

    @router.post("/api/admin/accounts", status_code=201)
    def create_event_admin(
        body: EventAdminCredentials, vc_admin: str | None = Cookie(default=None)
    ):
        require_global_admin(vc_admin)
        username = body.username.strip()
        if not username:
            bail(422, "管理员用户名不能为空")
        hashed = password_hash(body.password)
        with store.lock:
            account_id = secrets.token_hex(16)
            try:
                store.db.execute(
                    "INSERT INTO event_admins(id,username,password_hash) VALUES(?,?,?)",
                    (account_id, username, hashed),
                )
                store.db.commit()
            except sqlite3.IntegrityError:
                store.db.rollback()
                bail(409, "本场管理员用户名已存在")
            except sqlite3.Error:
                store.db.rollback()
                raise
        return {"id": account_id, "username": username}

    @router.put("/api/admin/accounts/{account_id}/password")
    def reset_event_admin_password(
        account_id: str,
        body: EventAdminPassword,
        vc_admin: str | None = Cookie(default=None),
    ):
        require_global_admin(vc_admin)
        hashed = password_hash(body.password)
        with store.lock:
            try:
                updated = store.db.execute(
                    "UPDATE event_admins SET password_hash=?,revision=revision+1 WHERE id=?",
                    (hashed, account_id),
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        if not updated.rowcount:
            bail(404, "管理员账号不存在")
        return {"ok": True}

    @router.delete("/api/admin/accounts/{account_id}")
    def revoke_event_admin(
        account_id: str, vc_admin: str | None = Cookie(default=None)
    ):
        require_global_admin(vc_admin)
        with store.lock:
            try:
                deleted = store.db.execute(
                    "DELETE FROM event_admins WHERE id=?", (account_id,)
                )
                store.db.commit()
            except sqlite3.Error:
                store.db.rollback()
                raise
        if not deleted.rowcount:
            bail(404, "管理员账号不存在")
        return {"ok": True}

    return router
