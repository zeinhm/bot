import json
from collections import defaultdict

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import database as db
from template_context import get_global_context

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
    all_trades = await db.get_all_trades()
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

    # Drawdown series
    drawdown_data = []
    peak = 0.0
    cumulative = 0.0
    for t in closed:
        cumulative += t.pnl_usdt or 0
        if cumulative > peak:
            peak = cumulative
        dd_pct = ((peak - cumulative) / peak * 100) if peak > 0 else 0
        if t.exit_time:
            drawdown_data.append({"time": int(t.exit_time.timestamp()), "value": round(-dd_pct, 2)})

    # Session win rate
    session_stats = defaultdict(lambda: {"wins": 0, "total": 0})
    for t in closed:
        if t.entry_time:
            hour = t.entry_time.hour
            sess = _session_for_hour(hour)
            session_stats[sess]["total"] += 1
            if t.result == "win":
                session_stats[sess]["wins"] += 1
    session_wr = {
        s: {"wr": round(d["wins"] / d["total"] * 100) if d["total"] else 0, "total": d["total"]}
        for s, d in session_stats.items()
    }

    # Day of week win rate
    day_stats = defaultdict(lambda: {"wins": 0, "total": 0})
    for t in closed:
        if t.entry_time:
            day = t.entry_time.weekday()
            day_stats[day]["total"] += 1
            if t.result == "win":
                day_stats[day]["wins"] += 1
    day_wr = {
        DAY_NAMES[d]: {"wr": round(v["wins"] / v["total"] * 100) if v["total"] else 0, "total": v["total"]}
        for d, v in sorted(day_stats.items())
    }

    # Monthly P&L heatmap
    monthly_pnl = defaultdict(float)
    for t in closed:
        if t.exit_time and t.pnl_usdt:
            key = t.exit_time.strftime("%Y-%m")
            monthly_pnl[key] += t.pnl_usdt
    monthly_data = [{"month": k, "pnl": round(v, 2)} for k, v in sorted(monthly_pnl.items())]

    ctx = await get_global_context()
    ctx.update({
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
        "session_wr": session_wr,
        "day_wr": day_wr,
        "monthly_data": json.dumps(monthly_data),
        "page": "analytics",
    })
    return templates.TemplateResponse(request, "analytics.html", ctx)
