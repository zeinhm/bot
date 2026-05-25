from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from bot import get_bot
from config import SYMBOLS
import database as db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/")
async def dashboard(request: Request):
    bot = get_bot()

    balance = 0.0
    open_position = None
    if bot and bot.exchange.client:
        try:
            balance = await bot.exchange.get_balance()
        except Exception:
            pass

    open_trade = await db.get_open_trade()
    if open_trade and bot:
        try:
            pos = await bot.exchange.get_position(open_trade.symbol)
            if pos:
                open_position = {
                    "trade": open_trade,
                    "unrealized_pnl": pos["unrealized_pnl"],
                }
        except Exception:
            open_position = {"trade": open_trade, "unrealized_pnl": 0.0}

    today_pnl = await db.get_today_pnl()
    recent_trades = await db.get_recent_trades(10)

    bot_enabled = await db.get_state("bot_enabled", True)

    return templates.TemplateResponse(request, "dashboard.html", {
        "bot_running": bot is not None and bot.running,
        "bot_enabled": bot_enabled,
        "balance": balance,
        "open_position": open_position,
        "today_pnl": today_pnl,
        "recent_trades": recent_trades,
        "page": "dashboard",
    })
