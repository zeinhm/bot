import json
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import app.db as db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/admin/analytics")
async def admin_analytics(request: Request):
    from routes.admin import require_admin
    user = await require_admin(request)

    all_trades = await db.get_all_platform_trades(is_paper=False)
    approved_users = await db.get_all_approved_users()

    closed = [t for t in all_trades if t.result in ("win", "loss")]
    wins = [t for t in closed if t.result == "win"]
    losses = [t for t in closed if t.result == "loss"]
    total = len(closed)

    win_rate = (len(wins) / total * 100) if total > 0 else 0
    total_r = sum(t.r_value or 0 for t in closed)
    gross_wins = sum(t.pnl_usdt or 0 for t in wins)
    gross_losses = abs(sum(t.pnl_usdt or 0 for t in losses))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else 0

    if closed:
        first_trade = min(t.entry_time for t in closed if t.entry_time)
        days = max((datetime.now(timezone.utc) - first_trade).days, 1)
        avg_trades_day = total / days
    else:
        avg_trades_day = 0

    # Monthly PnL
    monthly = defaultdict(float)
    for t in closed:
        if t.exit_time:
            key = t.exit_time.strftime("%Y-%m")
            monthly[key] += (t.pnl_usdt or 0)
    monthly_data = [{"month": k, "pnl": round(v, 2)} for k, v in sorted(monthly.items())]

    # Per-asset breakdown
    by_asset = defaultdict(lambda: {"wins": 0, "losses": 0, "r": 0, "pnl": 0})
    for t in closed:
        sym = t.symbol or "UNKNOWN"
        if t.result == "win":
            by_asset[sym]["wins"] += 1
        else:
            by_asset[sym]["losses"] += 1
        by_asset[sym]["r"] += (t.r_value or 0)
        by_asset[sym]["pnl"] += (t.pnl_usdt or 0)
    asset_data = []
    for sym, d in sorted(by_asset.items()):
        t = d["wins"] + d["losses"]
        asset_data.append({
            "symbol": sym,
            "trades": t,
            "win_rate": (d["wins"] / t * 100) if t > 0 else 0,
            "total_r": round(d["r"], 1),
            "pnl": round(d["pnl"], 2),
        })

    # Per-user leaderboard
    user_map = {u.id: u for u in approved_users}
    by_user = defaultdict(lambda: {"wins": 0, "losses": 0, "r": 0, "pnl": 0})
    for t in closed:
        uid = t.user_id
        if t.result == "win":
            by_user[uid]["wins"] += 1
        else:
            by_user[uid]["losses"] += 1
        by_user[uid]["r"] += (t.r_value or 0)
        by_user[uid]["pnl"] += (t.pnl_usdt or 0)

    leaderboard = []
    for uid, d in by_user.items():
        u = user_map.get(uid)
        t = d["wins"] + d["losses"]
        gw = sum(tr.pnl_usdt or 0 for tr in closed if tr.user_id == uid and tr.result == "win")
        gl = abs(sum(tr.pnl_usdt or 0 for tr in closed if tr.user_id == uid and tr.result == "loss"))
        pf = (gw / gl) if gl > 0 else 0
        leaderboard.append({
            "user": u,
            "user_id": uid,
            "trades": t,
            "wins": d["wins"],
            "losses": d["losses"],
            "win_rate": (d["wins"] / t * 100) if t > 0 else 0,
            "total_r": round(d["r"], 1),
            "pnl": round(d["pnl"], 2),
            "profit_factor": round(pf, 2),
        })
    leaderboard.sort(key=lambda x: x["total_r"], reverse=True)

    # Equity curve
    equity_data = []
    cumulative = 0
    for t in closed:
        cumulative += (t.pnl_usdt or 0)
        ts = t.exit_time or t.entry_time
        if ts:
            equity_data.append({"time": ts.strftime("%Y-%m-%d"), "value": round(cumulative, 2)})

    return templates.TemplateResponse(request, "admin_analytics.html", {
        "user": user,
        "page": "admin_analytics",
        "admin_mode": True,
        "total": total,
        "win_rate": win_rate,
        "total_r": total_r,
        "profit_factor": profit_factor,
        "avg_trades_day": avg_trades_day,
        "monthly_data": monthly_data,
        "asset_data": asset_data,
        "leaderboard": leaderboard,
        "equity_json": json.dumps(equity_data),
        "monthly_json": json.dumps(monthly_data),
    })
