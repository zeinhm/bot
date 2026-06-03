import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_auth, encrypt, decrypt, get_trading_mode
from app.bot import get_bot_for_user, make_broadcast_fn, build_user_config
from config import SYMBOLS, STRATEGY_PARAMS, LEVERAGE
import app.db as db
from app.core.context import get_global_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")
log = logging.getLogger(__name__)

ALL_SESSIONS = ["sydney", "tokyo", "london", "ny"]


@router.get("/settings")
async def settings_page(request: Request):
    user = await require_auth(request)
    mode = get_trading_mode(request)
    is_paper = (mode == "paper")

    bot_enabled = await db.get_state("bot_enabled", True, user_id=user.id, is_paper=is_paper)
    risk_mode = await db.get_state("risk_mode", "static", user_id=user.id, is_paper=is_paper)
    risk_value = await db.get_state("risk_value", 10.0, user_id=user.id, is_paper=is_paper)
    rr_ratio = await db.get_state("rr_ratio", STRATEGY_PARAMS["rrr"], user_id=user.id, is_paper=is_paper)
    max_trades = await db.get_state("max_trades_per_day", 99, user_id=user.id, is_paper=is_paper)
    active_symbols = await db.get_state("active_symbols", SYMBOLS, user_id=user.id, is_paper=is_paper)
    active_sessions = await db.get_state("active_sessions", STRATEGY_PARAMS["sessions"], user_id=user.id, is_paper=is_paper)

    cfg = await db.get_user_config(user.id)
    api_key_set = cfg is not None and cfg.binance_api_key_enc is not None
    testnet = cfg.binance_testnet if cfg else True

    saved = request.query_params.get("saved")
    keys_saved = request.query_params.get("keys_saved")
    error = request.query_params.get("error")

    ctx = await get_global_context(user.id, mode)
    ctx.update({
        "user": user,
        "bot_enabled": bot_enabled,
        "risk_mode": risk_mode,
        "risk_value": risk_value,
        "rr_ratio": rr_ratio,
        "max_trades": max_trades,
        "testnet": testnet,
        "api_key_set": api_key_set,
        "leverage": LEVERAGE,
        "all_symbols": SYMBOLS,
        "active_symbols": active_symbols,
        "all_sessions": ALL_SESSIONS,
        "active_sessions": active_sessions,
        "saved": saved == "1",
        "keys_saved": keys_saved == "1",
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
    user = await require_auth(request)
    mode = get_trading_mode(request)
    is_paper = (mode == "paper")
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

    await db.set_state("bot_enabled", bot_enabled == "on", user_id=user.id, is_paper=is_paper)
    await db.set_state("risk_mode", risk_mode, user_id=user.id, is_paper=is_paper)
    await db.set_state("risk_value", risk_value, user_id=user.id, is_paper=is_paper)
    await db.set_state("rr_ratio", rr_ratio, user_id=user.id, is_paper=is_paper)
    await db.set_state("max_trades_per_day", max_trades, user_id=user.id, is_paper=is_paper)
    await db.set_state("active_symbols", symbols, user_id=user.id, is_paper=is_paper)
    await db.set_state("active_sessions", sessions, user_id=user.id, is_paper=is_paper)

    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/api-keys")
async def save_api_keys(
    request: Request,
    api_key: str = Form(...),
    api_secret: str = Form(...),
    testnet: str = Form("on"),
):
    user = await require_auth(request)

    api_key_enc = encrypt(api_key.strip())
    api_secret_enc = encrypt(api_secret.strip())

    await db.save_user_config(
        user_id=user.id,
        api_key_enc=api_key_enc,
        api_secret_enc=api_secret_enc,
        testnet=(testnet == "on"),
    )

    manager = request.app.state.bot_manager
    worker = manager.get_worker(user.id, "live")
    if worker:
        try:
            await manager.stop_bot(user.id, "live")
        except Exception:
            pass

    config = build_user_config(api_key.strip(), api_secret.strip(), testnet == "on")
    await manager.start_bot(user.id, "live", config, broadcast_fn=make_broadcast_fn(user.id, "live"))

    log.info("User %d updated API keys", user.id)
    return RedirectResponse("/settings?keys_saved=1", status_code=303)


@router.post("/api/emergency-close")
async def emergency_close(request: Request):
    user = await require_auth(request)
    mode = get_trading_mode(request)
    is_paper = (mode == "paper")
    bot = get_bot_for_user(user.id, mode)
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

        open_trade = await db.get_open_trade(user.id, is_paper)
        if open_trade:
            await db.update_trade(open_trade.id, {
                "result": "loss",
                "r_value": -1.0,
                "exit_time": datetime.now(timezone.utc),
            })

        await db.log_event(
            f"Emergency close: {', '.join(closed) or 'no positions'}",
            level="warn", category="trade",
            user_id=user.id, is_paper=is_paper,
        )
    except Exception as e:
        log.exception("Emergency close failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    return JSONResponse({
        "ok": True,
        "closed": closed,
        "errors": errors,
    })
