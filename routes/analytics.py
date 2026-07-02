import json
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.auth import require_auth, get_trading_mode
import app.db as db
from app.core.context import get_global_context
from app.core.metrics import compute_drawdown, load_capital_events
from app.core.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="templates")
register_filters(templates)

SESSIONS_EST = [
    ("sydney", 17, 2),
    ("tokyo", 19, 4),
    ("london", 3, 12),
    ("ny", 8, 17),
]

SESSION_ORDER = ["ny", "london", "sydney", "tokyo"]


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
    fastest_win_hold = _format_hold(min(win_holds)) if win_holds else "—"
    fastest_loss_hold = _format_hold(min(loss_holds)) if loss_holds else "—"
    longest_win_hold = _format_hold(max(win_holds)) if win_holds else "—"
    longest_loss_hold = _format_hold(max(loss_holds)) if loss_holds else "—"

    # Longest win / loss streaks (chronological by close)
    longest_win_streak = longest_loss_streak = 0
    cur_win = cur_loss = 0
    for t in sorted(closed, key=lambda t: t.exit_time or t.entry_time or datetime.min.replace(tzinfo=timezone.utc)):
        if t.result == "win":
            cur_win += 1
            cur_loss = 0
            longest_win_streak = max(longest_win_streak, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            longest_loss_streak = max(longest_loss_streak, cur_loss)

    # Drawdown = real-balance high-water-mark: replay real capital events (Binance
    # transfers; paper's $10k start) + closed-trade PnL in time order. No notional.
    # None when there's no real capital base yet → UI shows '—'.
    dd_closed = sorted([t for t in closed if t.exit_time], key=lambda t: t.exit_time)
    capital_events = await load_capital_events(user.id, is_paper, dd_closed)
    dd = compute_drawdown(dd_closed, capital_events)
    max_drawdown = dd["max_pct"] if dd else None
    drawdown_data = dd["series"] if dd else []

    from zoneinfo import ZoneInfo
    ny_tz = ZoneInfo("America/New_York")
    session_stats = defaultdict(lambda: {"pnl": 0.0, "total": 0})
    for t in closed:
        if t.entry_time:
            hour = t.entry_time.astimezone(ny_tz).hour
            sess = _session_for_hour(hour)
            session_stats[sess]["total"] += 1
            session_stats[sess]["pnl"] += t.pnl_usdt or 0
    # Always show every session (0 trades if none) in a fixed order; "off" only
    # appears if it actually has trades.
    session_pnl = {}
    for s in SESSION_ORDER:
        d = session_stats.get(s, {"pnl": 0.0, "total": 0})
        session_pnl[s] = {"pnl": round(d["pnl"], 2), "total": d["total"]}
    if session_stats.get("off", {}).get("total", 0) > 0:
        d = session_stats["off"]
        session_pnl["off"] = {"pnl": round(d["pnl"], 2), "total": d["total"]}

    monthly_stats = defaultdict(lambda: {"pnl": 0.0, "total": 0, "wins": 0, "losses": 0})
    for t in closed:
        if t.exit_time:
            key = t.exit_time.strftime("%Y-%m")
            monthly_stats[key]["pnl"] += t.pnl_usdt or 0
            monthly_stats[key]["total"] += 1
            monthly_stats[key]["wins" if t.result == "win" else "losses"] += 1

    monthly_year = int(request.query_params.get("my", datetime.now(timezone.utc).year))
    monthly_data = []
    monthly_net = 0.0
    for m in range(1, 13):
        key = f"{monthly_year}-{m:02d}"
        s = monthly_stats.get(key, {"pnl": 0.0, "total": 0, "wins": 0, "losses": 0})
        monthly_net += s["pnl"]
        monthly_data.append({
            "month": key, "pnl": round(s["pnl"], 2),
            "total": s["total"], "wins": s["wins"], "losses": s["losses"],
        })
    available_years = sorted({k[:4] for k in monthly_stats}, reverse=True)

    ctx = await get_global_context(user.id, mode)
    ctx.update({
        "user": user,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_hold": avg_hold_str,
        "avg_win_hold": avg_win_hold,
        "avg_loss_hold": avg_loss_hold,
        "fastest_win_hold": fastest_win_hold,
        "fastest_loss_hold": fastest_loss_hold,
        "longest_win_hold": longest_win_hold,
        "longest_loss_hold": longest_loss_hold,
        "longest_win_streak": longest_win_streak,
        "longest_loss_streak": longest_loss_streak,
        "total_trades": len(closed),
        "wins": wins,
        "losses": losses,
        "total_r": total_r,
        "drawdown_data": json.dumps(drawdown_data),
        "session_pnl": session_pnl,
        "monthly_data": json.dumps(monthly_data),
        "monthly_net": monthly_net,
        "monthly_year": monthly_year,
        "available_years": available_years,
        "max_drawdown": round(max_drawdown, 1) if max_drawdown is not None else None,
        "page": "analytics",
    })
    return templates.TemplateResponse(request, "analytics.html", ctx)
