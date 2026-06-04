import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

import app.db as db

router = APIRouter()
templates = Jinja2Templates(directory="templates")
log = logging.getLogger(__name__)


@router.get("/admin/users")
async def admin_users_page(request: Request):
    from routes.admin import require_admin
    user = await require_admin(request)

    from sqlalchemy import select
    from app.db.models import User

    async with db.get_session() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()))
        all_users = list(result.scalars().all())

    pending = [u for u in all_users if not u.is_approved and not u.is_rejected]
    approved = [u for u in all_users if u.is_approved]

    from app.db.models import RejectionLog
    async with db.get_session() as session:
        result = await session.execute(
            select(RejectionLog).order_by(RejectionLog.rejected_at.desc())
        )
        rejection_log = list(result.scalars().all())

    return templates.TemplateResponse(request, "admin_users.html", {
        "user": user,
        "pending_users": pending,
        "approved_users": approved,
        "rejection_log": rejection_log,
        "total_users": len(all_users),
        "page": "admin_users",
        "admin_mode": True,
    })


@router.post("/admin/users/approve/{user_id}")
async def approve_user(request: Request, user_id: int):
    from routes.admin import require_admin
    await require_admin(request)

    from sqlalchemy import update
    from app.db.models import User
    from app.email import send_approval_email

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


@router.post("/admin/users/reject/{user_id}")
async def reject_user(request: Request, user_id: int):
    from routes.admin import require_admin
    await require_admin(request)

    from sqlalchemy import update
    from app.db.models import User, RejectionLog
    from app.email import send_rejection_email

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


@router.post("/admin/users/disable/{user_id}")
async def disable_user(request: Request, user_id: int):
    from routes.admin import require_admin
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
    for mode in ("live", "paper"):
        if manager.get_worker(user_id, mode):
            try:
                await manager.stop_bot(user_id, mode)
            except Exception:
                pass

    target = await db.get_user(user_id)
    log.info("Admin disabled user %d (%s)", user_id, target.email if target else "?")
    await db.log_event(f"Disabled user: {target.email if target else user_id}", category="system")
    return JSONResponse({"ok": True})


@router.post("/admin/users/toggle-admin/{user_id}")
async def toggle_admin(request: Request, user_id: int):
    from routes.admin import require_admin
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


@router.post("/admin/users/allow-reregister/{log_id}")
async def allow_reregister(request: Request, log_id: int):
    from routes.admin import require_admin
    await require_admin(request)

    from datetime import datetime, timezone
    from sqlalchemy import update, select
    from app.db.models import RejectionLog

    async with db.get_session() as session:
        entry = await session.get(RejectionLog, log_id)
        if not entry or entry.status != "rejected":
            return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)

        await session.execute(
            update(RejectionLog).where(RejectionLog.id == log_id)
            .values(status="allowed", allowed_at=datetime.now(timezone.utc))
        )
        await session.commit()

    log.info("Admin allowed re-register for log %d (user %d)", log_id, entry.user_id)
    await db.log_event(f"Allowed re-register: {entry.email}", category="system")
    return JSONResponse({"ok": True})
