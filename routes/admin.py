import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_auth
from app.email import send_approval_email, send_rejection_email
import app.db as db
from app.core.context import get_global_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")
log = logging.getLogger(__name__)


async def require_admin(request: Request):
    user = await require_auth(request)
    if not user.is_admin:
        from app.auth import AuthRequired
        raise AuthRequired()
    return user


@router.get("/admin")
async def admin_page(request: Request):
    user = await require_admin(request)

    from sqlalchemy import select
    from app.db.models import User

    async with db.get_session() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()))
        all_users = list(result.scalars().all())

    pending = [u for u in all_users if not u.is_approved and not u.is_rejected]
    approved = [u for u in all_users if u.is_approved]

    from sqlalchemy import select as sa_select
    from app.db.models import RejectionLog
    async with db.get_session() as session:
        result = await session.execute(
            sa_select(RejectionLog).order_by(RejectionLog.rejected_at.desc())
        )
        rejection_log = list(result.scalars().all())

    ctx = await get_global_context(user.id)
    ctx.update({
        "user": user,
        "pending_users": pending,
        "approved_users": approved,
        "rejection_log": rejection_log,
        "total_users": len(all_users),
        "page": "admin",
    })
    return templates.TemplateResponse(request, "admin.html", ctx)


@router.post("/admin/approve/{user_id}")
async def approve_user(request: Request, user_id: int):
    await require_admin(request)

    from sqlalchemy import update
    from app.db.models import User

    async with db.get_session() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(is_approved=True)
        )
        await session.commit()

    target = await db.get_user(user_id)
    log.info("Admin approved user %d (%s)", user_id, target.email if target else "?")
    await db.log_event(f"Approved user: {target.email if target else user_id}", category="system")

    if target:
        send_approval_email(target.email, target.name)

    return JSONResponse({"ok": True})


@router.post("/admin/reject/{user_id}")
async def reject_user(request: Request, user_id: int):
    await require_admin(request)

    from sqlalchemy import update
    from app.db.models import User, RejectionLog

    target = await db.get_user(user_id)
    async with db.get_session() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(is_rejected=True, is_approved=False)
        )
        session.add(RejectionLog(
            user_id=user_id,
            email=target.email if target else "",
            name=target.name if target else "",
            avatar_url=target.avatar_url if target else "",
        ))
        await session.commit()

    log.info("Admin rejected user %d (%s)", user_id, target.email if target else "?")
    await db.log_event(f"Rejected user: {target.email if target else user_id}", category="system")

    if target:
        send_rejection_email(target.email, target.name)

    return JSONResponse({"ok": True})


@router.post("/admin/disable/{user_id}")
async def disable_user(request: Request, user_id: int):
    admin = await require_admin(request)
    if user_id == admin.id:
        return JSONResponse({"ok": False, "error": "Cannot disable yourself"}, status_code=400)

    from sqlalchemy import update
    from app.db.models import User

    async with db.get_session() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(is_approved=False)
        )
        await session.commit()

    manager = request.app.state.bot_manager
    if manager.get_worker(user_id):
        try:
            await manager.stop_bot(user_id)
        except Exception:
            pass

    target = await db.get_user(user_id)
    log.info("Admin disabled user %d (%s)", user_id, target.email if target else "?")
    await db.log_event(f"Disabled user: {target.email if target else user_id}", category="system")
    return JSONResponse({"ok": True})


@router.post("/admin/toggle-admin/{user_id}")
async def toggle_admin(request: Request, user_id: int):
    admin = await require_admin(request)
    if user_id == admin.id:
        return JSONResponse({"ok": False, "error": "Cannot change your own admin status"}, status_code=400)

    target = await db.get_user(user_id)
    if not target:
        return JSONResponse({"ok": False, "error": "User not found"}, status_code=404)

    from sqlalchemy import update
    from app.db.models import User

    new_val = not target.is_admin
    async with db.get_session() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(is_admin=new_val)
        )
        await session.commit()

    log.info("Admin toggled admin for user %d: %s", user_id, new_val)
    return JSONResponse({"ok": True, "is_admin": new_val})


@router.post("/admin/allow-reregister/{log_id}")
async def allow_reregister(request: Request, log_id: int):
    await require_admin(request)

    from datetime import datetime, timezone
    from sqlalchemy import update, select
    from app.db.models import User, RejectionLog

    async with db.get_session() as session:
        entry = await session.get(RejectionLog, log_id)
        if not entry or entry.status != "rejected":
            return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)

        await session.execute(
            update(RejectionLog).where(RejectionLog.id == log_id)
            .values(status="allowed", allowed_at=datetime.now(timezone.utc))
        )
        # Don't reset is_rejected here — it stays True until the user logs in again.
        # The login callback checks for "allowed" log entries to reset the flag.
        await session.commit()

    log.info("Admin allowed re-register for log %d (user %d)", log_id, entry.user_id)
    await db.log_event(f"Allowed re-register: {entry.email}", category="system")
    return JSONResponse({"ok": True})
