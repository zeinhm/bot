import asyncio
import logging

from binance import AsyncClient
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import decrypt
import app.db as db

router = APIRouter()
templates = Jinja2Templates(directory="templates")
log = logging.getLogger(__name__)


async def _real_live_balance(u, manager) -> float:
    """The account's ACTUAL live USDT balance. A running bot → its worker; an off
    account with keys → a short-lived client fetching the real balance (so a stale
    stored value can't inflate platform equity). Unreachable → 0, not a ghost."""
    worker = manager.get_worker(u.id, "live")
    if worker and worker.exchange.client:
        try:
            return await asyncio.wait_for(worker.exchange.get_balance(), timeout=8)
        except Exception:
            pass
    cfg = await db.get_user_config(u.id)
    if not cfg or not cfg.binance_api_key_enc:
        return 0.0
    client = None
    try:
        client = await asyncio.wait_for(AsyncClient.create(
            api_key=decrypt(cfg.binance_api_key_enc),
            api_secret=decrypt(cfg.binance_api_secret_enc)), timeout=8)
        bals = await asyncio.wait_for(client.futures_account_balance(), timeout=8)
        for b in bals:
            if b["asset"] == "USDT":
                return float(b["balance"])
        return 0.0
    except Exception as e:
        log.warning("Platform equity: couldn't fetch live balance for user %d: %s", u.id, e)
        return 0.0
    finally:
        if client is not None:
            try:
                await client.close_connection()
            except Exception:
                pass


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

    # Platform equity: each account's REAL live balance (fetched, running or not) so
    # a stale stored value can't inflate it. Parallel with per-account timeouts.
    balances = await asyncio.gather(*[_real_live_balance(u, manager) for u in approved_users])
    total_equity = sum(balances)

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
