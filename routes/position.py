import time

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.auth import require_auth, get_trading_mode
from app.bot import get_bot_for_user
from config import LEVERAGE
import app.db as db
from app.core.context import get_global_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/position")
async def position_page(request: Request):
    user = await require_auth(request)
    mode = get_trading_mode(request)
    is_paper = (mode == "paper")
    bot = get_bot_for_user(user.id, mode)
    bot_trade = await db.get_open_trade(user.id, is_paper)

    positions = []

    if bot and bot.running:
        try:
            all_pos = await bot.exchange.get_all_positions()
            for pos in all_pos:
                is_bot = (
                    bot_trade is not None
                    and bot_trade.symbol == pos["symbol"]
                    and bot_trade.direction == pos["side"]
                )
                p = {
                    "symbol": pos["symbol"],
                    "direction": pos["side"],
                    "entry_price": pos["entry_price"],
                    "quantity": pos["quantity"],
                    "unrealized_pnl": pos["unrealized_pnl"],
                    "source": "bot" if is_bot else "manual",
                }
                if is_bot:
                    p["sl_price"] = bot_trade.sl_price
                    p["tp_price"] = bot_trade.tp_price
                positions.append(p)
        except Exception:
            if bot_trade:
                positions.append({
                    "symbol": bot_trade.symbol,
                    "direction": bot_trade.direction,
                    "entry_price": bot_trade.entry_price,
                    "quantity": bot_trade.quantity,
                    "unrealized_pnl": 0.0,
                    "source": "bot",
                    "sl_price": bot_trade.sl_price,
                    "tp_price": bot_trade.tp_price,
                })

    risk_mode = await db.get_state("risk_mode", "static", user_id=user.id, is_paper=is_paper)
    risk_value = await db.get_state("risk_value", 10.0, user_id=user.id, is_paper=is_paper)

    bot_status = "stopped"
    api_connected = False
    uptime_secs = None
    if bot:
        bot_status = bot.status
        api_connected = bot.exchange.client is not None
        if bot.started_at:
            uptime_secs = int(time.time() - bot.started_at)

    last_trade_ts = None
    recent = await db.get_recent_trades(1, user_id=user.id, is_paper=is_paper)
    if recent and recent[0].exit_time:
        last_trade_ts = int(recent[0].exit_time.timestamp())

    ctx = await get_global_context(user.id, mode)
    ctx.update({
        "user": user,
        "positions": positions,
        "leverage": LEVERAGE,
        "risk_mode": risk_mode,
        "risk_value": risk_value,
        "bot_status": bot_status,
        "api_connected": api_connected,
        "uptime_secs": uptime_secs,
        "last_trade_ts": last_trade_ts,
        "page": "position",
    })
    return templates.TemplateResponse(request, "position.html", ctx)
