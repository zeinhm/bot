import json

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import app.db as db
from app.core.context import get_global_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")

BASE_CAPITAL = 10000


@router.get("/track-record")
async def track_record_page(request: Request):
    all_trades = await db.get_all_trades(user_id=1, is_paper=False)
    closed = [t for t in all_trades if t.result in ("win", "loss")]
    wins = sum(1 for t in closed if t.result == "win")
    total_r = sum(t.r_value or 0 for t in closed)
    win_rate = (wins / len(closed) * 100) if closed else 0

    equity_data = []
    cumulative = 0.0
    peak_equity = BASE_CAPITAL
    max_dd_pct = 0.0
    for t in closed:
        cumulative += t.pnl_usdt or 0
        equity = BASE_CAPITAL + cumulative
        if equity > peak_equity:
            peak_equity = equity
        dd_pct = ((peak_equity - equity) / peak_equity * 100) if peak_equity > 0 else 0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
        if t.exit_time:
            equity_data.append({"time": int(t.exit_time.timestamp()), "value": round(equity, 2)})

    recent = list(reversed(closed[-12:])) if closed else []

    ctx = await get_global_context()
    ctx.update({
        "total_r": total_r,
        "win_rate": win_rate,
        "total_trades": len(closed),
        "max_dd": max_dd_pct,
        "equity_data": json.dumps(equity_data),
        "recent_trades": recent,
        "page": "public",
    })
    return templates.TemplateResponse(request, "track_record.html", ctx)
