from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import app.db as db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/admin")
async def admin_dashboard(request: Request):
    from routes.admin import require_admin
    user = await require_admin(request)

    manager = request.app.state.bot_manager
    all_statuses = manager.get_all_statuses()

    approved_users = await db.get_all_approved_users()
    pending_users = await db.get_pending_users()
    today_pnl = await db.get_platform_today_pnl()
    total_trades = await db.get_platform_trade_count()

    active_bots = sum(1 for running in all_statuses.values() if running)

    user_rows = []
    for u in approved_users:
        summary = await db.get_user_trade_summary(u.id, is_paper=False)
        live_running = all_statuses.get((u.id, "live"), False)
        paper_running = all_statuses.get((u.id, "paper"), False)

        if live_running:
            bot_label = "Live"
            bot_color = "green"
        elif paper_running:
            bot_label = "Paper"
            bot_color = "amber"
        else:
            bot_label = "Off"
            bot_color = "gray"

        user_rows.append({
            "user": u,
            "bot_label": bot_label,
            "bot_color": bot_color,
            "trades": summary["trades"],
            "win_rate": summary["win_rate"],
            "today_pnl": summary["today_pnl"],
        })

    return templates.TemplateResponse(request, "admin_dashboard.html", {
        "user": user,
        "page": "admin_dashboard",
        "admin_mode": True,
        "total_users": len(approved_users),
        "pending_count": len(pending_users),
        "active_bots": active_bots,
        "today_pnl": today_pnl,
        "total_trades": total_trades,
        "user_rows": user_rows,
    })
