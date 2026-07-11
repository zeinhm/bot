import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_auth, get_trading_mode
from app.bot import get_bot_for_user
from config import LEVERAGE, COMMISSION_PCT
import app.db as db
from app.core.context import get_global_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")
log = logging.getLogger(__name__)


@router.get("/position")
async def position_page(request: Request):
    user = await require_auth(request)
    mode = get_trading_mode(request)
    is_paper = (mode == "paper")
    bot = get_bot_for_user(user.id, mode)
    bot_trades = {t.symbol: t for t in await db.get_open_trades(user.id, is_paper)}

    positions = []

    if bot and bot.running:
        try:
            all_pos = await bot.exchange.get_all_positions()
            for pos in all_pos:
                bt = bot_trades.get(pos["symbol"])
                is_bot = bt is not None and bt.direction == pos["side"]
                p = {
                    "symbol": pos["symbol"],
                    "direction": pos["side"],
                    "entry_price": pos["entry_price"],
                    "quantity": pos["quantity"],
                    "unrealized_pnl": pos["unrealized_pnl"],
                    "source": "bot" if is_bot else "manual",
                }
                if is_bot:
                    p["sl_price"] = bt.sl_price
                    p["tp_price"] = bt.tp_price
                    if bt.setup_json:
                        try:
                            p["setup"] = json.loads(bt.setup_json)
                        except (ValueError, TypeError):
                            pass
                positions.append(p)
        except Exception:
            for bt in bot_trades.values():
                fp = {
                    "symbol": bt.symbol,
                    "direction": bt.direction,
                    "entry_price": bt.entry_price,
                    "quantity": bt.quantity,
                    "unrealized_pnl": 0.0,
                    "source": "bot",
                    "sl_price": bt.sl_price,
                    "tp_price": bt.tp_price,
                }
                if bt.setup_json:
                    try:
                        fp["setup"] = json.loads(bt.setup_json)
                    except (ValueError, TypeError):
                        pass
                positions.append(fp)

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


@router.post("/api/position/close")
async def close_one_position(request: Request):
    """Market-close a single open position (live or paper) and cancel its resting
    SL/TP. Unlike the strategy's TP/SL exits, a discretionary close books the trade
    at its ACTUAL realized R (pnl / risked amount), since it hit neither level.
    Not 2FA-gated — same rationale as emergency-close: a fast get-out action."""
    user = await require_auth(request)
    mode = get_trading_mode(request)
    is_paper = (mode == "paper")
    bot = get_bot_for_user(user.id, mode)
    if not bot or not bot.exchange.client:
        return JSONResponse({"ok": False, "error": "Bot not connected to exchange"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        body = {}
    symbol = (body.get("symbol") or "").upper()
    if not symbol:
        return JSONResponse({"ok": False, "error": "Missing symbol"}, status_code=400)

    try:
        positions = await bot.exchange.get_all_positions()
        pos = next((p for p in positions if p["symbol"] == symbol), None)
        if pos is None:
            return JSONResponse({"ok": False, "error": f"No open position for {symbol}"}, status_code=404)

        close_side = "SELL" if pos["side"] == "long" else "BUY"
        pos_side = "LONG" if pos["side"] == "long" else "SHORT"
        order = await bot.exchange.place_market_order(symbol, close_side, pos["quantity"], position_side=pos_side)
        await bot.exchange.cancel_all_orders(symbol)
        exit_price = float(order.get("avgPrice") or 0) or None

        # If this position is a tracked bot trade, resolve its DB row at the real fill.
        open_trade = await db.get_open_trade_for_symbol(user.id, symbol, is_paper)
        if open_trade and exit_price and exit_price > 0:
            if open_trade.direction == "long":
                raw_pnl = (exit_price - open_trade.entry_price) * open_trade.quantity
            else:
                raw_pnl = (open_trade.entry_price - exit_price) * open_trade.quantity
            exit_comm = exit_price * open_trade.quantity * COMMISSION_PCT
            total_comm = (open_trade.commission or 0) + exit_comm
            pnl = raw_pnl - total_comm
            risk_amt = abs(open_trade.entry_price - (open_trade.sl_price or open_trade.entry_price)) * open_trade.quantity
            r_value = round(pnl / risk_amt, 2) if risk_amt > 0 else (1.0 if pnl > 0 else -1.0)
            await db.update_trade(open_trade.id, {
                "result": "win" if pnl > 0 else "loss",
                "r_value": r_value,
                "exit_time": datetime.now(timezone.utc),
                "exit_price": exit_price,
                "pnl_usdt": pnl,
                "commission": total_comm,
            })

        await db.log_event(
            f"Manual close: {symbol}", level="warn", category="trade",
            user_id=user.id, is_paper=is_paper,
        )
        return JSONResponse({"ok": True, "symbol": symbol, "exit_price": exit_price})
    except Exception as e:
        log.exception("Close position failed for %s", symbol)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
