import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

import app.db as db

router = APIRouter()
templates = Jinja2Templates(directory="templates")
log = logging.getLogger(__name__)


@router.get("/admin")
async def admin_dashboard(request: Request):
    from routes.admin import require_admin
    user = await require_admin(request)

    manager = request.app.state.bot_manager
    all_statuses = manager.get_all_statuses()

    approved_users = await db.get_all_approved_users()
    pending_users = await db.get_pending_users()
    year_pnl = await db.get_platform_year_pnl()
    total_trades = await db.get_platform_trade_count()
    require_approval = await db.get_state("require_approval", True)

    active_bots = sum(1 for running in all_statuses.values() if running)

    # Platform equity: sum of live balances + paper balances
    total_equity = 0.0
    for u in approved_users:
        live_worker = manager.get_worker(u.id, "live")
        if live_worker and live_worker.exchange.client:
            try:
                total_equity += await live_worker.exchange.get_balance()
            except Exception:
                bal = await db.get_state("last_balance", 0, user_id=u.id, is_paper=False)
                total_equity += bal or 0
        else:
            bal = await db.get_state("last_balance", 0, user_id=u.id, is_paper=False)
            total_equity += bal or 0

    user_rows = []
    for u in approved_users:
        summary = await db.get_user_trade_summary(u.id, is_paper=False)
        live_running = all_statuses.get((u.id, "live"), False)
        paper_running = all_statuses.get((u.id, "paper"), False)

        bots = []
        if live_running:
            bots.append({"label": "Live", "color": "green"})
        if paper_running:
            bots.append({"label": "Paper", "color": "amber"})
        if not bots:
            bots.append({"label": "Off", "color": "gray"})

        user_rows.append({
            "user": u,
            "bots": bots,
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
        "year_pnl": year_pnl,
        "total_trades": total_trades,
        "total_equity": total_equity,
        "user_rows": user_rows,
        "require_approval": require_approval,
    })


@router.post("/admin/settings/require-approval")
async def set_require_approval(request: Request):
    from routes.admin import require_admin
    await require_admin(request)

    body = await request.json()
    enabled = bool(body.get("enabled"))
    await db.set_state("require_approval", enabled)

    log.info("Admin set require_approval=%s", enabled)
    await db.log_event(
        f"New-user approval requirement {'enabled' if enabled else 'disabled (open registration)'}",
        category="system",
    )
    return JSONResponse({"ok": True, "enabled": enabled})
