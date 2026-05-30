import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from bot import get_bot
from config import BINANCE_API_KEY, BINANCE_TESTNET, SYMBOLS, STRATEGY_PARAMS, LEVERAGE
import database as db
from template_context import get_global_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")
log = logging.getLogger(__name__)

ALL_SESSIONS = ["sydney", "tokyo", "london", "ny"]


@router.get("/settings")
async def settings_page(request: Request):
    bot_enabled = await db.get_state("bot_enabled", True)
    risk_mode = await db.get_state("risk_mode", "static")
    risk_value = await db.get_state("risk_value", 10.0)
    rr_ratio = await db.get_state("rr_ratio", STRATEGY_PARAMS["rrr"])
    max_trades = await db.get_state("max_trades_per_day", 99)
    active_symbols = await db.get_state("active_symbols", SYMBOLS)
    active_sessions = await db.get_state("active_sessions", STRATEGY_PARAMS["sessions"])

    saved = request.query_params.get("saved")
    error = request.query_params.get("error")

    ctx = await get_global_context()
    ctx.update({
        "bot_enabled": bot_enabled,
        "risk_mode": risk_mode,
        "risk_value": risk_value,
        "rr_ratio": rr_ratio,
        "max_trades": max_trades,
        "testnet": BINANCE_TESTNET,
        "api_key_set": bool(BINANCE_API_KEY),
        "leverage": LEVERAGE,
        "all_symbols": SYMBOLS,
        "active_symbols": active_symbols,
        "all_sessions": ALL_SESSIONS,
        "active_sessions": active_sessions,
        "saved": saved == "1",
        "error": error or "",
        "page": "settings",
    })
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings")
async def save_settings(
    request: Request,
    bot_enabled: str = Form(None),
    risk_mode: str = Form("static"),
    risk_value: float = Form(10.0),
    rr_ratio: float = Form(2.0),
    max_trades: int = Form(99),
):
    form = await request.form()

    errors = []

    if risk_mode not in ("static", "dynamic"):
        errors.append("Invalid risk mode")

    if risk_mode == "dynamic":
        if not (0.1 <= risk_value <= 5.0):
            errors.append("Dynamic risk must be 0.1–5.0%")
    else:
        if not (1 <= risk_value <= 1000):
            errors.append("Static risk must be $1–$1,000")

    if not (1.0 <= rr_ratio <= 10.0):
        errors.append("RR ratio must be 1.0–10.0")

    if not (1 <= max_trades <= 10):
        errors.append("Max trades must be 1–10")

    symbols = [s for s in SYMBOLS if form.get(f"symbol_{s}")]
    if not symbols:
        errors.append("Select at least one symbol")

    sessions = [s for s in ALL_SESSIONS if form.get(f"session_{s}")]
    if not sessions:
        errors.append("Select at least one session")

    if errors:
        return RedirectResponse(f"/settings?error={errors[0]}", status_code=303)

    await db.set_state("bot_enabled", bot_enabled == "on")
    await db.set_state("risk_mode", risk_mode)
    await db.set_state("risk_value", risk_value)
    await db.set_state("rr_ratio", rr_ratio)
    await db.set_state("max_trades_per_day", max_trades)
    await db.set_state("active_symbols", symbols)
    await db.set_state("active_sessions", sessions)

    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/api/emergency-close")
async def emergency_close():
    bot = get_bot()
    if not bot or not bot.exchange.client:
        return JSONResponse({"ok": False, "error": "Bot not connected to exchange"}, status_code=503)

    closed = []
    errors = []

    try:
        positions = await bot.exchange.get_all_positions()
        for pos in positions:
            try:
                close_side = "SELL" if pos["side"] == "long" else "BUY"
                await bot.exchange.place_market_order(pos["symbol"], close_side, pos["quantity"])
                await bot.exchange.cancel_all_orders(pos["symbol"])
                closed.append(pos["symbol"])
            except Exception as e:
                errors.append(f"{pos['symbol']}: {e}")

        open_trade = await db.get_open_trade()
        if open_trade:
            await db.update_trade(open_trade.id, {
                "result": "loss",
                "r_value": -1.0,
                "exit_time": datetime.now(timezone.utc),
            })

        await db.log_event(
            f"Emergency close: {', '.join(closed) or 'no positions'}",
            level="warn",
            category="trade",
        )
    except Exception as e:
        log.exception("Emergency close failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    return JSONResponse({
        "ok": True,
        "closed": closed,
        "errors": errors,
    })
