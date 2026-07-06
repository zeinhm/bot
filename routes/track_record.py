import json

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import app.db as db
from app.core.context import get_global_context
from app.core.metrics import compute_drawdown, load_drawdown_events
from app.core.template_filters import register_filters
from config import TRACK_RECORD_EMAIL

router = APIRouter()
templates = Jinja2Templates(directory="templates")
register_filters(templates)


@router.get("/track-record")
async def track_record_page(request: Request):
    # Show a designated clean sample account's real live trades. Fall back to the
    # admin (user_id=1) if the configured account isn't found on this deployment.
    tr_user = await db.get_user_by_email(TRACK_RECORD_EMAIL)
    tr_user_id = tr_user.id if tr_user else 1
    all_trades = await db.get_all_trades(user_id=tr_user_id, is_paper=False)
    closed = [t for t in all_trades if t.result in ("win", "loss")]
    wins = sum(1 for t in closed if t.result == "win")
    total_r = sum(t.r_value or 0 for t in closed)
    win_rate = (wins / len(closed) * 100) if closed else 0

    # Cumulative PnL curve (starts at 0). Not anchored to account balance so that
    # deposits/withdrawals don't distort it. Drawdown = giveback from peak profit.
    # Curve = cumulative PnL from $0. Max drawdown = real-balance high-water-mark
    # from real capital events (Binance transfers) + closed-trade PnL. No notional;
    # None → template shows '—'.
    closed_sorted = sorted([t for t in closed if t.exit_time], key=lambda t: t.exit_time)
    equity_data = []
    cumulative = 0.0
    for t in closed_sorted:
        cumulative += t.pnl_usdt or 0
        equity_data.append({"time": int(t.exit_time.timestamp()), "value": round(cumulative, 2)})

    dd = compute_drawdown(closed_sorted, await load_drawdown_events(tr_user_id, False, closed_sorted))
    max_dd = dd["max_pct"] if dd else None

    recent = list(reversed(closed[-12:])) if closed else []

    ctx = await get_global_context()
    ctx.update({
        "total_r": total_r,
        "win_rate": win_rate,
        "total_trades": len(closed),
        "max_dd": max_dd,
        "equity_data": json.dumps(equity_data),
        "recent_trades": recent,
        "page": "public",
    })
    return templates.TemplateResponse(request, "track_record.html", ctx)
