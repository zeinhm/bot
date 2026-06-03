from __future__ import annotations

import logging

from cryptography.fernet import Fernet
from fastapi import Request

import app.db as db
from app.db.models import User
from config import ENCRYPTION_KEY, GOOGLE_CLIENT_ID

log = logging.getLogger(__name__)

_fernet: Fernet | None = None

_LEGACY_USER = User(id=1, google_id="legacy", email="", name="Owner", is_approved=True, is_admin=True)


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


class PendingApproval(Exception):
    pass


class AccountRejected(Exception):
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
    if user.is_rejected:
        raise AccountRejected()
    if not user.is_approved:
        raise PendingApproval()
    return user


def get_trading_mode(request: Request) -> str:
    return request.session.get("trading_mode", "paper")
