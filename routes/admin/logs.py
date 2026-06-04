from typing import Optional

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import app.db as db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/admin/logs")
async def admin_logs(
    request: Request,
    user_id: Optional[str] = None,
    level: Optional[str] = None,
    category: Optional[str] = None,
    mode: Optional[str] = None,
):
    from routes.admin import require_admin
    user = await require_admin(request)

    uid = int(user_id) if user_id and user_id.isdigit() else None

    is_paper = None
    if mode == "live":
        is_paper = False
    elif mode == "paper":
        is_paper = True

    events = await db.get_admin_events(
        limit=100,
        user_id=uid,
        level=level if level else None,
        category=category if category else None,
        is_paper=is_paper,
    )

    approved_users = await db.get_all_approved_users()

    return templates.TemplateResponse(request, "admin_logs.html", {
        "user": user,
        "page": "admin_logs",
        "admin_mode": True,
        "events": events,
        "all_users": approved_users,
        "filter_user_id": uid,
        "filter_level": level or "",
        "filter_category": category or "",
        "filter_mode": mode or "",
    })
