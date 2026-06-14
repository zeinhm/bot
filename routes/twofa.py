"""TOTP 2FA endpoints: enroll, verify, disable, the step-up challenge, and
one-time backup recovery codes."""

import logging

from fastapi import APIRouter, Request, Form
from fastapi.responses import JSONResponse
from sqlalchemy import update

from app.auth import (
    require_auth, encrypt, decrypt,
    verify_code, generate_secret, provisioning_uri, qr_svg, mark_2fa_verified,
    rate_limited, record_code_failure, clear_code_failures,
    generate_backup_codes, hash_backup_codes, backup_codes_remaining, verify_totp_or_backup,
)
from app.db.models import User
import app.db as db

router = APIRouter()
log = logging.getLogger(__name__)

_RATE_LIMIT_MSG = "Too many attempts. Wait a few minutes and try again."


@router.post("/settings/2fa/enroll")
async def enroll(request: Request):
    """Generate a pending secret (held in session until verified) + its QR."""
    user = await require_auth(request)
    if user.totp_enabled:
        return JSONResponse({"ok": False, "error": "Two-factor auth is already enabled."}, status_code=400)
    secret = generate_secret()
    request.session["pending_totp_secret"] = secret
    return JSONResponse({"ok": True, "secret": secret, "qr": qr_svg(provisioning_uri(secret, user.email))})


@router.post("/settings/2fa/verify")
async def verify(request: Request, code: str = Form(...)):
    """Confirm enrollment: validate a code against the pending secret, then persist + enable."""
    user = await require_auth(request)
    if rate_limited(user.id):
        return JSONResponse({"ok": False, "error": _RATE_LIMIT_MSG}, status_code=429)
    secret = request.session.get("pending_totp_secret")
    if not secret:
        return JSONResponse({"ok": False, "error": "Enrollment expired — start again."}, status_code=400)
    if not verify_code(secret, code):
        record_code_failure(user.id)
        return JSONResponse({"ok": False, "error": "Invalid code. Check your authenticator and try again."}, status_code=400)
    clear_code_failures(user.id)
    codes = generate_backup_codes()
    async with db.get_session() as session:
        await session.execute(update(User).where(User.id == user.id).values(
            totp_secret_enc=encrypt(secret), totp_enabled=True, totp_backup_codes=hash_backup_codes(codes)))
        await session.commit()
    request.session.pop("pending_totp_secret", None)
    mark_2fa_verified(request)
    log.info("User %d enabled 2FA", user.id)
    return JSONResponse({"ok": True, "backup_codes": codes})


@router.post("/settings/2fa/disable")
async def disable(request: Request, code: str = Form(...)):
    """Disable 2FA — requires a current valid code."""
    user = await require_auth(request)
    if rate_limited(user.id):
        return JSONResponse({"ok": False, "error": _RATE_LIMIT_MSG}, status_code=429)
    if not user.totp_enabled or not user.totp_secret_enc:
        return JSONResponse({"ok": False, "error": "Two-factor auth is not enabled."}, status_code=400)
    ok, _used, _new = verify_totp_or_backup(decrypt(user.totp_secret_enc), user.totp_backup_codes, code)
    if not ok:
        record_code_failure(user.id)
        return JSONResponse({"ok": False, "error": "Invalid code."}, status_code=400)
    clear_code_failures(user.id)
    async with db.get_session() as session:
        await session.execute(update(User).where(User.id == user.id).values(
            totp_secret_enc=None, totp_enabled=False, totp_backup_codes=None))
        await session.commit()
    request.session.pop("twofa_verified_at", None)
    log.info("User %d disabled 2FA", user.id)
    return JSONResponse({"ok": True})


@router.post("/settings/2fa/challenge")
async def challenge(request: Request, code: str = Form(...)):
    """Step-up: validate a code and mark the session as freshly 2FA-verified."""
    user = await require_auth(request)
    if not user.totp_enabled or not user.totp_secret_enc:
        return JSONResponse({"ok": True})
    if rate_limited(user.id):
        return JSONResponse({"ok": False, "error": _RATE_LIMIT_MSG}, status_code=429)
    ok, used_backup, new_json = verify_totp_or_backup(decrypt(user.totp_secret_enc), user.totp_backup_codes, code)
    if not ok:
        record_code_failure(user.id)
        return JSONResponse({"ok": False, "error": "Invalid code."}, status_code=400)
    clear_code_failures(user.id)
    if used_backup:
        async with db.get_session() as session:
            await session.execute(update(User).where(User.id == user.id).values(totp_backup_codes=new_json))
            await session.commit()
        log.info("User %d used a backup code", user.id)
    mark_2fa_verified(request)
    return JSONResponse({"ok": True, "used_backup": used_backup, "backup_remaining": backup_codes_remaining(new_json)})


@router.post("/settings/2fa/backup-codes/regenerate")
async def regenerate_backup_codes(request: Request, code: str = Form(...)):
    """Replace the backup codes — requires a current TOTP or remaining backup code."""
    user = await require_auth(request)
    if rate_limited(user.id):
        return JSONResponse({"ok": False, "error": _RATE_LIMIT_MSG}, status_code=429)
    if not user.totp_enabled or not user.totp_secret_enc:
        return JSONResponse({"ok": False, "error": "Two-factor auth is not enabled."}, status_code=400)
    ok, _used, _new = verify_totp_or_backup(decrypt(user.totp_secret_enc), user.totp_backup_codes, code)
    if not ok:
        record_code_failure(user.id)
        return JSONResponse({"ok": False, "error": "Invalid code."}, status_code=400)
    clear_code_failures(user.id)
    codes = generate_backup_codes()
    async with db.get_session() as session:
        await session.execute(update(User).where(User.id == user.id).values(totp_backup_codes=hash_backup_codes(codes)))
        await session.commit()
    log.info("User %d regenerated backup codes", user.id)
    return JSONResponse({"ok": True, "backup_codes": codes})
