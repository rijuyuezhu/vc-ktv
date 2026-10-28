"""Temporary, per-event account passwords. No global user profiles."""

from __future__ import annotations

import hashlib
import hmac
import secrets


def password_hash(password: str) -> str:
    if not 6 <= len(password) <= 72:
        raise ValueError("密码长度须为 6–72 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ":" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":")
        salt, digest = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        if len(salt) != 16 or len(digest) != 64:
            return False
        candidate = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
        return hmac.compare_digest(candidate, digest)
    except (ValueError, TypeError):
        return False
