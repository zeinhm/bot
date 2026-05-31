"""
Authentication helpers: session-based auth dependency, API key encryption.

When GOOGLE_CLIENT_ID is not set, auth is bypassed (single-user legacy mode).
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet
from fastapi import Request

import database as db
from database import User
from config import ENCRYPTION_KEY, GOOGLE_CLIENT_ID

log = logging.getLogger(__name__)

_fernet: Fernet | None = None

_LEGACY_USER = User(id=1, google_id="legacy", email="", name="Owner")


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        if not ENCRYPTION_KEY:
            raise RuntimeError("ENCRYPTION_KEY not set")
        _fernet = Fernet(ENCRYPTION_KEY.encode())
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()


class AuthRequired(Exception):
    pass


async def get_current_user(request: Request) -> User | None:
    if not GOOGLE_CLIENT_ID:
        return _LEGACY_USER
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return await db.get_user(user_id)


async def require_auth(request: Request) -> User:
    user = await get_current_user(request)
    if not user:
        raise AuthRequired()
    return user
