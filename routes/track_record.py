from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import app.db as db
from app.core.context import get_global_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/track-record")
async def track_record_page(request: Request):
    all_trades = await db.get_recent_trades(1000, user_id=1, is_paper=False)
    closed = [t for t in all_trades if t.result in ("win", "loss")]
    wins = sum(1 for t in closed if t.result == "win")
    total_r = sum(t.r_value or 0 for t in closed)
    win_rate = (wins / len(closed) * 100) if closed else 0

    pnls = [t.pnl_usdt or 0 for t in closed]
    peak = 0.0
    max_dd = 0.0
    cumulative = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    recent = list(reversed(closed[-12:])) if closed else []

    ctx = await get_global_context()
    ctx.update({
        "total_r": total_r,
        "win_rate": win_rate,
        "total_trades": len(closed),
        "max_dd": max_dd_pct,
        "recent_trades": recent,
        "page": "public",
    })
    return templates.TemplateResponse(request, "track_record.html", ctx)
