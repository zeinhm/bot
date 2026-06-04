import time

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import app.db as db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/admin/bots")
async def admin_bots(request: Request):
    from routes.admin import require_admin
    user = await require_admin(request)

    manager = request.app.state.bot_manager
    bot_info = manager.get_all_bot_info()
    approved_users = await db.get_all_approved_users()
    user_map = {u.id: u for u in approved_users}

    rows = []
    seen = set()

    for info in bot_info:
        u = user_map.get(info["user_id"])
        rows.append({
            "user": u,
            "user_id": info["user_id"],
            "mode": info["mode"],
            "status": info["status"],
            "running": info["running"],
            "uptime_secs": info["uptime_secs"],
            "last_error": info["last_error"],
            "last_error_time": info["last_error_time"],
            "symbols": info["symbols"],
        })
        seen.add((info["user_id"], info["mode"]))

    for u in approved_users:
        for mode in ("live", "paper"):
            if (u.id, mode) not in seen:
                rows.append({
                    "user": u,
                    "user_id": u.id,
                    "mode": mode,
                    "status": "off",
                    "running": False,
                    "uptime_secs": None,
                    "last_error": None,
                    "last_error_time": None,
                    "symbols": [],
                })

    status_order = {"error": 0, "starting": 1, "running": 2, "stopped": 3, "off": 4}
    rows.sort(key=lambda r: (status_order.get(r["status"], 5), r["user_id"], r["mode"]))

    total = len(rows)
    running = sum(1 for r in rows if r["running"])
    errors = sum(1 for r in rows if r["status"] == "error")
    off = sum(1 for r in rows if r["status"] in ("off", "stopped"))

    now = time.time()
    for r in rows:
        if r["uptime_secs"] is not None:
            h, rem = divmod(r["uptime_secs"], 3600)
            m = rem // 60
            r["uptime_fmt"] = f"{h}h {m}m" if h > 0 else f"{m}m"
        else:
            r["uptime_fmt"] = None

        if r["last_error_time"] is not None:
            ago = int(now - r["last_error_time"])
            if ago < 60:
                r["error_ago"] = f"{ago}s ago"
            elif ago < 3600:
                r["error_ago"] = f"{ago // 60}m ago"
            else:
                r["error_ago"] = f"{ago // 3600}h {(ago % 3600) // 60}m ago"
        else:
            r["error_ago"] = None

    return templates.TemplateResponse(request, "admin_bots.html", {
        "user": user,
        "page": "admin_bots",
        "admin_mode": True,
        "rows": rows,
        "total": total,
        "running": running,
        "errors": errors,
        "off": off,
    })
