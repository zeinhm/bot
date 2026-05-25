from typing import Optional

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import database as db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/trades")
async def trades_page(
    request: Request,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    result: Optional[str] = None,
):
    trades = await db.get_trades_filtered(
        symbol=symbol,
        direction=direction,
        result_filter=result,
    )

    total = len(trades)
    wins = sum(1 for t in trades if t.result == "win")
    losses = sum(1 for t in trades if t.result == "loss")
    win_rate = (wins / total * 100) if total > 0 else 0
    total_r = sum(t.r_value or 0 for t in trades if t.result != "open")
    total_pnl = sum(t.pnl_usdt or 0 for t in trades if t.result != "open")

    return templates.TemplateResponse(request, "trades.html", {
        "trades": trades,
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_r": total_r,
        "total_pnl": total_pnl,
        "filter_symbol": symbol or "",
        "filter_direction": direction or "",
        "filter_result": result or "",
        "page": "trades",
    })
