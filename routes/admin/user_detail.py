import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import app.db as db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def compute_stats(trades):
    closed = [t for t in trades if t.result in ("win", "loss")]
    wins = [t for t in closed if t.result == "win"]
    losses = [t for t in closed if t.result == "loss"]
    total = len(closed)
    win_rate = (len(wins) / total * 100) if total > 0 else 0
    total_r = sum(t.r_value or 0 for t in closed)
    total_pnl = sum(t.pnl_usdt or 0 for t in closed)

    gross_wins = sum(t.pnl_usdt or 0 for t in wins)
    gross_losses = abs(sum(t.pnl_usdt or 0 for t in losses))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else 0

    hold_times = []
    for t in closed:
        if t.entry_time and t.exit_time:
            hold_times.append((t.exit_time - t.entry_time).total_seconds())
    avg_hold = (sum(hold_times) / len(hold_times)) if hold_times else 0

    peak = 0
    equity = 0
    max_dd = 0
    for t in closed:
        equity += (t.pnl_usdt or 0)
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    equity_data = []
    cumulative = 0
    for t in closed:
        cumulative += (t.pnl_usdt or 0)
        ts = t.exit_time or t.entry_time
        if ts:
            equity_data.append({"time": ts.strftime("%Y-%m-%d"), "value": round(cumulative, 2)})

    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_r": total_r,
        "total_pnl": total_pnl,
        "profit_factor": profit_factor,
        "avg_hold_secs": avg_hold,
        "max_dd_pct": max_dd_pct,
        "equity_data": equity_data,
    }


@router.get("/admin/user/{user_id}")
async def admin_user_detail(request: Request, user_id: int):
    from routes.admin import require_admin, AdminNotFound
    admin = await require_admin(request)

    target = await db.get_user(user_id)
    if not target:
        raise AdminNotFound()

    manager = request.app.state.bot_manager
    live_status = manager.get_status(user_id, "live")
    paper_status = manager.get_status(user_id, "paper")

    live_trades = await db.get_all_trades(user_id, is_paper=False)
    paper_trades = await db.get_all_trades(user_id, is_paper=True)

    live_stats = compute_stats(live_trades)
    paper_stats = compute_stats(paper_trades)

    config = await db.get_user_config(user_id)
    has_api_keys = config is not None and config.binance_api_key_enc is not None

    paper_balance = await db.get_paper_balance(user_id)

    live_balance = None
    live_worker = manager.get_worker(user_id, "live")
    if live_worker and live_worker.exchange.client:
        try:
            live_balance = await live_worker.exchange.get_balance()
        except Exception:
            pass
    if live_balance is None and has_api_keys:
        live_balance = await db.get_state("last_balance", None, user_id=user_id, is_paper=False)

    recent_trades = await db.get_recent_trades(20, user_id=user_id, is_paper=False)
    events = await db.get_recent_events(20, user_id=user_id)

    avg_hold_mins = int(live_stats["avg_hold_secs"] / 60) if live_stats["avg_hold_secs"] > 0 else 0

    return templates.TemplateResponse(request, "admin_user_detail.html", {
        "user": admin,
        "page": "admin_dashboard",
        "admin_mode": True,
        "target": target,
        "live_status": live_status,
        "paper_status": paper_status,
        "live_stats": live_stats,
        "paper_stats": paper_stats,
        "has_api_keys": has_api_keys,
        "live_balance": live_balance,
        "paper_balance": paper_balance,
        "recent_trades": recent_trades,
        "events": events,
        "avg_hold_mins": avg_hold_mins,
        "equity_json": json.dumps(live_stats["equity_data"]),
    })
