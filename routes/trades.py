from typing import Optional

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.auth import require_auth, get_trading_mode
from config import LEVERAGE, SYMBOLS
import app.db as db
from app.core.context import get_global_context

from app.core.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="templates")
register_filters(templates)


@router.get("/trades")
async def trades_page(
    request: Request,
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    result: Optional[str] = None,
    page: int = 1,
):
    user = await require_auth(request)
    mode = get_trading_mode(request)
    is_paper = (mode == "paper")

    trades = await db.get_trades_filtered(
        user_id=user.id,
        is_paper=is_paper,
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

    # Pagination — 10/page (stats above are over the full filtered set)
    PER_PAGE = 10
    total_pages = max(1, -(-total // PER_PAGE))  # ceil division
    page = max(1, min(page, total_pages))
    page_trades = trades[(page - 1) * PER_PAGE: page * PER_PAGE]

    ctx = await get_global_context(user.id, mode)
    ctx.update({
        "user": user,
        "trades": page_trades,
        "total": total,
        "cur_page": page,
        "per_page": PER_PAGE,
        "total_pages": total_pages,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_r": total_r,
        "total_pnl": total_pnl,
        "leverage": LEVERAGE,
        "symbols": SYMBOLS,
        "filter_symbol": symbol or "",
        "filter_direction": direction or "",
        "filter_result": result or "",
        "page": "trades",
    })
    return templates.TemplateResponse(request, "trades.html", ctx)
