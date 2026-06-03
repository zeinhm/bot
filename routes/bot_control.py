import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.auth import require_auth, get_trading_mode
from app.bot import get_bot_for_user, make_broadcast_fn, build_user_config
from app.auth import decrypt
import app.db as db

router = APIRouter()
log = logging.getLogger(__name__)


@router.post("/bot/start")
async def start_bot(request: Request):
    user = await require_auth(request)
    mode = get_trading_mode(request)
    manager = request.app.state.bot_manager

    if mode == "paper":
        return JSONResponse({"ok": False, "error": "Paper bot starts automatically"}, status_code=400)

    if manager.get_worker(user.id, "live") is not None:
        return JSONResponse({"ok": False, "error": "Bot already running"}, status_code=409)

    cfg = await db.get_user_config(user.id)
    if not cfg or not cfg.binance_api_key_enc:
        return JSONResponse({"ok": False, "error": "API keys not configured"}, status_code=400)

    config = build_user_config(
        decrypt(cfg.binance_api_key_enc),
        decrypt(cfg.binance_api_secret_enc),
        cfg.binance_testnet,
    )
    await manager.start_bot(user.id, "live", config, broadcast_fn=make_broadcast_fn(user.id, "live"))
    log.info("User %d started live bot via API", user.id)
    return JSONResponse({"ok": True})


@router.post("/bot/stop")
async def stop_bot(request: Request):
    user = await require_auth(request)
    mode = get_trading_mode(request)
    manager = request.app.state.bot_manager

    if mode == "paper":
        return JSONResponse({"ok": False, "error": "Paper bot cannot be stopped manually"}, status_code=400)

    if manager.get_worker(user.id, "live") is None:
        return JSONResponse({"ok": False, "error": "Bot not running"}, status_code=404)

    await manager.stop_bot(user.id, "live")
    log.info("User %d stopped live bot via API", user.id)
    return JSONResponse({"ok": True})


@router.get("/bot/status")
async def bot_status(request: Request):
    user = await require_auth(request)
    mode = get_trading_mode(request)
    manager = request.app.state.bot_manager
    status = manager.get_status(user.id, mode)
    return JSONResponse({"status": status})


@router.post("/api/switch-mode")
async def switch_mode(request: Request):
    user = await require_auth(request)
    body = await request.json()
    mode = body.get("mode", "paper")
    if mode not in ("paper", "live"):
        return JSONResponse({"ok": False, "error": "Invalid mode"}, status_code=400)
    request.session["trading_mode"] = mode
    return JSONResponse({"ok": True, "mode": mode})
