import json
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.auth import require_auth, get_trading_mode
import app.db as db
from app.core.context import get_global_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")

SESSIONS_EST = [
    ("sydney", 17, 2),
    ("tokyo", 19, 4),
    ("london", 3, 12),
    ("ny", 8, 17),
]

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _format_hold(mins: float) -> str:
    if mins >= 60:
        return f"{mins / 60:.1f}h"
    return f"{mins:.0f}m"


def _session_for_hour(hour: int) -> str:
    for name, start, end in SESSIONS_EST:
        if start < end:
            if start <= hour < end:
                return name
        else:
            if hour >= start or hour < end:
                return name
    return "off"


@router.get("/analytics")
async def analytics_page(request: Request):
    user = await require_auth(request)
    mode = get_trading_mode(request)
    is_paper = (mode == "paper")

    all_trades = await db.get_all_trades(user.id, is_paper)
    closed = [t for t in all_trades if t.result in ("win", "loss")]
    wins = sum(1 for t in closed if t.result == "win")
    losses = len(closed) - wins
    total_r = sum(t.r_value or 0 for t in closed)
    win_rate = (wins / len(closed) * 100) if closed else 0

    gross_win = sum(t.pnl_usdt for t in closed if t.result == "win" and t.pnl_usdt)
    gross_loss = abs(sum(t.pnl_usdt for t in closed if t.result == "loss" and t.pnl_usdt))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else 0

    win_holds = []
    loss_holds = []
    for t in closed:
        if t.entry_time and t.exit_time:
            mins = (t.exit_time - t.entry_time).total_seconds() / 60
            if t.result == "win":
                win_holds.append(mins)
            else:
                loss_holds.append(mins)
    all_holds = win_holds + loss_holds
    avg_hold_str = _format_hold(sum(all_holds) / len(all_holds)) if all_holds else "—"
    avg_win_hold = _format_hold(sum(win_holds) / len(win_holds)) if win_holds else "—"
    avg_loss_hold = _format_hold(sum(loss_holds) / len(loss_holds)) if loss_holds else "—"
    avg_win_mins = (sum(win_holds) / len(win_holds)) if win_holds else 0
    avg_loss_mins = (sum(loss_holds) / len(loss_holds)) if loss_holds else 0
    hold_ratio = round(avg_loss_mins / avg_win_mins, 1) if avg_win_mins > 0 else 0

    BASE_CAPITAL = 10000
    drawdown_data = []
    cumulative = 0.0
    peak_equity = BASE_CAPITAL
    max_drawdown = 0.0
    for t in closed:
        cumulative += t.pnl_usdt or 0
        equity = BASE_CAPITAL + cumulative
        if equity > peak_equity:
            peak_equity = equity
        dd_pct = ((peak_equity - equity) / peak_equity * 100) if peak_equity > 0 else 0
        if dd_pct > max_drawdown:
            max_drawdown = dd_pct
        if t.exit_time:
            drawdown_data.append({"time": int(t.exit_time.timestamp()), "value": round(-dd_pct, 2)})

    from zoneinfo import ZoneInfo
    ny_tz = ZoneInfo("America/New_York")
    session_stats = defaultdict(lambda: {"pnl": 0.0, "total": 0})
    for t in closed:
        if t.entry_time:
            hour = t.entry_time.astimezone(ny_tz).hour
            sess = _session_for_hour(hour)
            session_stats[sess]["total"] += 1
            session_stats[sess]["pnl"] += t.pnl_usdt or 0
    session_pnl = {
        s: {"pnl": round(d["pnl"], 2), "total": d["total"]}
        for s, d in session_stats.items()
    }

    day_stats = defaultdict(lambda: {"pnl": 0.0, "total": 0})
    for t in closed:
        if t.entry_time:
            day = t.entry_time.weekday()
            day_stats[day]["total"] += 1
            day_stats[day]["pnl"] += t.pnl_usdt or 0
    day_pnl = {
        DAY_NAMES[d]: {"pnl": round(v["pnl"], 2), "total": v["total"]}
        for d, v in sorted(day_stats.items())
    }

    monthly_pnl = defaultdict(float)
    for t in closed:
        if t.exit_time and t.pnl_usdt:
            key = t.exit_time.strftime("%Y-%m")
            monthly_pnl[key] += t.pnl_usdt

    monthly_year = int(request.query_params.get("my", datetime.now(timezone.utc).year))
    monthly_data = []
    monthly_net = 0.0
    for m in range(1, 13):
        key = f"{monthly_year}-{m:02d}"
        val = monthly_pnl.get(key, 0.0)
        monthly_net += val
        monthly_data.append({"month": key, "pnl": round(val, 2)})
    available_years = sorted({k[:4] for k in monthly_pnl}, reverse=True)

    ctx = await get_global_context(user.id, mode)
    ctx.update({
        "user": user,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_hold": avg_hold_str,
        "avg_win_hold": avg_win_hold,
        "avg_loss_hold": avg_loss_hold,
        "total_trades": len(closed),
        "wins": wins,
        "losses": losses,
        "total_r": total_r,
        "drawdown_data": json.dumps(drawdown_data),
        "session_pnl": session_pnl,
        "day_pnl": day_pnl,
        "monthly_data": json.dumps(monthly_data),
        "monthly_net": monthly_net,
        "monthly_year": monthly_year,
        "available_years": available_years,
        "max_drawdown": round(max_drawdown, 1),
        "hold_ratio": hold_ratio,
        "page": "analytics",
    })
    return templates.TemplateResponse(request, "analytics.html", ctx)
