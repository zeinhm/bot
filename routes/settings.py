import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_auth, encrypt, decrypt, get_trading_mode, require_2fa, backup_codes_remaining
from app.bot import get_bot_for_user, make_broadcast_fn, build_user_config
from config import SYMBOLS, STRATEGY_PARAMS, LEVERAGE
from exchange import validate_api_key
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
    active_symbols = await db.get_state("active_symbols", SYMBOLS, user_id=user.id, is_paper=is_paper)
    active_sessions = await db.get_state("active_sessions", STRATEGY_PARAMS["sessions"], user_id=user.id, is_paper=is_paper)
    leverage = await db.get_state("leverage", LEVERAGE, user_id=user.id, is_paper=is_paper)
    skip_may = await db.get_state("skip_may", True, user_id=user.id, is_paper=is_paper)
    skip_tax = await db.get_state("skip_tax_deadline", True, user_id=user.id, is_paper=is_paper)

    cfg = await db.get_user_config(user.id)
    api_key_set = cfg is not None and cfg.binance_api_key_enc is not None
    api_permissions = await db.get_state("api_permissions", {}, user_id=user.id, is_paper=False)

    api_key_preview = ""
    if api_key_set:
        try:
            raw_key = decrypt(cfg.binance_api_key_enc)
            if raw_key and len(raw_key) >= 8:
                api_key_preview = raw_key[:8] + "••••••••"
            else:
                api_key_preview = "••••••••••••"
        except Exception as e:
            log.warning("Failed to decrypt API key preview for user: %s", e)
            api_key_preview = "••••••••••••"

    saved = request.query_params.get("saved")
    error = request.query_params.get("error")

    ctx = await get_global_context(user.id, mode)
    ctx.update({
        "user": user,
        "bot_enabled": bot_enabled,
        "risk_mode": risk_mode,
        "risk_value": risk_value,
        "rr_ratio": rr_ratio,
        "api_key_set": api_key_set,
        "api_key_preview": api_key_preview,
        "api_permissions": api_permissions,
        "backup_remaining": backup_codes_remaining(getattr(user, "totp_backup_codes", None)),
        "leverage": leverage,
        "default_leverage": LEVERAGE,
        "skip_may": skip_may,
        "skip_tax": skip_tax,
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
    leverage: int = Form(LEVERAGE),
    skip_may: str = Form(None),
    skip_tax_deadline: str = Form(None),
):
    user = await require_auth(request)
    chal = require_2fa(request, user)
    if chal:
        return chal
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

    if not (1 <= leverage <= 20):
        errors.append("Leverage must be 1–20")

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
    await db.set_state("active_symbols", symbols, user_id=user.id, is_paper=is_paper)
    await db.set_state("active_sessions", sessions, user_id=user.id, is_paper=is_paper)
    await db.set_state("leverage", leverage, user_id=user.id, is_paper=is_paper)
    await db.set_state("skip_may", skip_may == "on", user_id=user.id, is_paper=is_paper)
    await db.set_state("skip_tax_deadline", skip_tax_deadline == "on", user_id=user.id, is_paper=is_paper)

    # Apply leverage to a running worker immediately (no restart needed).
    worker = get_bot_for_user(user.id, mode)
    if worker and getattr(worker.exchange, "client", None):
        try:
            await worker.exchange.set_leverage(leverage)
        except Exception as e:
            log.warning("Failed to apply leverage live for user %d: %s", user.id, e)

    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/reset")
async def reset_settings(request: Request):
    """Reset all bot-control settings for the current mode back to validated defaults."""
    user = await require_auth(request)
    chal = require_2fa(request, user)
    if chal:
        return chal
    mode = get_trading_mode(request)
    is_paper = (mode == "paper")

    await db.set_state("bot_enabled", True, user_id=user.id, is_paper=is_paper)
    await db.set_state("risk_mode", "dynamic", user_id=user.id, is_paper=is_paper)
    await db.set_state("risk_value", 2.0, user_id=user.id, is_paper=is_paper)
    await db.set_state("rr_ratio", STRATEGY_PARAMS["rrr"], user_id=user.id, is_paper=is_paper)
    await db.set_state("active_symbols", SYMBOLS, user_id=user.id, is_paper=is_paper)
    await db.set_state("active_sessions", STRATEGY_PARAMS["sessions"], user_id=user.id, is_paper=is_paper)
    await db.set_state("leverage", LEVERAGE, user_id=user.id, is_paper=is_paper)
    await db.set_state("skip_may", True, user_id=user.id, is_paper=is_paper)
    await db.set_state("skip_tax_deadline", True, user_id=user.id, is_paper=is_paper)

    worker = get_bot_for_user(user.id, mode)
    if worker and getattr(worker.exchange, "client", None):
        try:
            await worker.exchange.set_leverage(LEVERAGE)
        except Exception as e:
            log.warning("Failed to apply leverage on reset for user %d: %s", user.id, e)

    return JSONResponse({"ok": True})


@router.post("/settings/api-keys")
async def save_api_keys(
    request: Request,
    api_key: str = Form(...),
    api_secret: str = Form(...),
):
    user = await require_auth(request)
    chal = require_2fa(request, user)
    if chal:
        return chal

    # Validate against Binance before persisting — rejects bad keys, Futures-
    # disabled keys, and (for safety) keys with withdrawals enabled.
    result = await validate_api_key(api_key.strip(), api_secret.strip())
    if not result["ok"]:
        return JSONResponse({"ok": False, "error": result["error"]}, status_code=400)

    api_key_enc = encrypt(api_key.strip())
    api_secret_enc = encrypt(api_secret.strip())

    await db.save_user_config(
        user_id=user.id,
        api_key_enc=api_key_enc,
        api_secret_enc=api_secret_enc,
    )
    await db.set_state("api_permissions", result["permissions"], user_id=user.id, is_paper=False)

    manager = request.app.state.bot_manager
    worker = manager.get_worker(user.id, "live")
    if worker:
        try:
            await manager.stop_bot(user.id, "live")
        except Exception:
            pass

    config = build_user_config(api_key.strip(), api_secret.strip())
    shared_market = request.app.state.shared_market
    await manager.start_bot(user.id, "live", config, broadcast_fn=make_broadcast_fn(user.id, "live"),
                            shared_market=shared_market)

    log.info("User %d updated API keys", user.id)
    return JSONResponse({"ok": True, "permissions": result["permissions"]})


@router.post("/settings/api-keys/validate")
async def validate_api_keys_route(
    request: Request,
    api_key: str = Form(...),
    api_secret: str = Form(...),
):
    """Validate keys without persisting — used by the inline form + onboarding wizard."""
    await require_auth(request)
    result = await validate_api_key(api_key.strip(), api_secret.strip())
    return JSONResponse(result)


@router.post("/settings/api-keys/refresh")
async def refresh_api_permissions(request: Request):
    """Re-read the permission flags for the ALREADY-STORED key and refresh the cached
    `api_permissions` — so changing permissions on Binance (e.g. enabling Futures) is
    reflected without re-entering the key. Read-only against the key; not 2FA-gated."""
    user = await require_auth(request)
    cfg = await db.get_user_config(user.id)
    if cfg is None or cfg.binance_api_key_enc is None:
        return JSONResponse({"ok": False, "error": "No API key configured."}, status_code=400)

    try:
        api_key = decrypt(cfg.binance_api_key_enc)
        api_secret = decrypt(cfg.binance_api_secret_enc)
    except Exception:
        return JSONResponse({"ok": False, "error": "Could not read the stored key."}, status_code=500)

    result = await validate_api_key(api_key, api_secret)
    # Only overwrite the cache when we actually READ fresh flags — a transient Binance
    # error returns empty permissions, and we don't want that to wipe good badges.
    # (withdrawals-enabled returns ok=False but WITH flags, so the red pill still updates.)
    if result["permissions"]:
        await db.set_state("api_permissions", result["permissions"], user_id=user.id, is_paper=False)
    return JSONResponse({"ok": result["ok"], "permissions": result["permissions"], "error": result["error"]})


@router.post("/settings/api-keys/delete")
async def delete_api_keys(request: Request):
    user = await require_auth(request)
    chal = require_2fa(request, user)
    if chal:
        return chal

    manager = request.app.state.bot_manager
    worker = manager.get_worker(user.id, "live")
    if worker:
        try:
            await manager.stop_bot(user.id, "live")
        except Exception:
            pass

    await db.delete_user_api_keys(user.id)
    # No key = no account: zero the live balance so it doesn't show a stale figure.
    from app.bot.websocket import reset_live_balance
    await reset_live_balance(user.id)
    log.info("User %d deleted API keys", user.id)
    return JSONResponse({"ok": True})


@router.post("/api/emergency-close")
async def emergency_close(request: Request):
    # Intentionally NOT 2FA-gated: this is a time-critical "get me out now" action;
    # the confirm dialog is the safeguard. Speed matters more than step-up here.
    user = await require_auth(request)
    mode = get_trading_mode(request)
    is_paper = (mode == "paper")
    bot = get_bot_for_user(user.id, mode)
    if not bot or not bot.exchange.client:
        return JSONResponse({"ok": False, "error": "Bot not connected to exchange"}, status_code=503)

    closed = []
    closed_fills = {}
    errors = []

    try:
        positions = await bot.exchange.get_all_positions()
        for pos in positions:
            try:
                close_side = "SELL" if pos["side"] == "long" else "BUY"
                pos_side = "LONG" if pos["side"] == "long" else "SHORT"
                order = await bot.exchange.place_market_order(pos["symbol"], close_side, pos["quantity"], position_side=pos_side)
                await bot.exchange.cancel_all_orders(pos["symbol"])
                closed.append(pos["symbol"])
                closed_fills[pos["symbol"]] = float(order.get("avgPrice") or 0)
            except Exception as e:
                errors.append(f"{pos['symbol']}: {e}")

        from config import COMMISSION_PCT
        for open_trade in await db.get_open_trades(user.id, is_paper):
            exit_price = closed_fills.get(open_trade.symbol, 0) or None

            if exit_price and exit_price > 0:
                if open_trade.direction == "long":
                    raw_pnl = (exit_price - open_trade.entry_price) * open_trade.quantity
                else:
                    raw_pnl = (open_trade.entry_price - exit_price) * open_trade.quantity
                exit_comm = exit_price * open_trade.quantity * COMMISSION_PCT
                total_comm = (open_trade.commission or 0) + exit_comm
                pnl = raw_pnl - total_comm
            else:
                pnl = 0.0
                total_comm = open_trade.commission or 0

            await db.update_trade(open_trade.id, {
                "result": "loss",
                "r_value": -1.0,
                "exit_time": datetime.now(timezone.utc),
                "exit_price": exit_price,
                "pnl_usdt": pnl,
                "commission": total_comm,
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
