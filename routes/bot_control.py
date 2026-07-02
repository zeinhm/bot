import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import update

from app.auth import require_auth, get_trading_mode, require_2fa
from app.bot import get_bot_for_user, make_broadcast_fn, build_user_config, build_paper_config
from app.auth import decrypt
from app.db.models import User
import app.db as db

router = APIRouter()
log = logging.getLogger(__name__)


@router.post("/bot/start")
async def start_bot(request: Request):
    user = await require_auth(request)
    chal = require_2fa(request, user)
    if chal:
        return chal
    mode = get_trading_mode(request)
    manager = request.app.state.bot_manager

    if mode == "paper":
        return JSONResponse({"ok": False, "error": "Use /bot/start-paper"}, status_code=400)

    if manager.get_worker(user.id, "live") is not None:
        return JSONResponse({"ok": False, "error": "Bot already running"}, status_code=409)

    cfg = await db.get_user_config(user.id)
    if not cfg or not cfg.binance_api_key_enc:
        return JSONResponse({"ok": False, "error": "API keys not configured"}, status_code=400)

    config = build_user_config(
        decrypt(cfg.binance_api_key_enc),
        decrypt(cfg.binance_api_secret_enc),
    )
    shared_market = request.app.state.shared_market
    await manager.start_bot(user.id, "live", config, broadcast_fn=make_broadcast_fn(user.id, "live"),
                            shared_market=shared_market)
    log.info("User %d started live bot via API", user.id)
    return JSONResponse({"ok": True})


@router.post("/bot/stop")
async def stop_bot(request: Request):
    user = await require_auth(request)
    chal = require_2fa(request, user)
    if chal:
        return chal
    mode = get_trading_mode(request)
    manager = request.app.state.bot_manager

    if mode == "paper":
        return JSONResponse({"ok": False, "error": "Paper bot cannot be stopped"}, status_code=400)

    if manager.get_worker(user.id, "live") is None:
        return JSONResponse({"ok": False, "error": "Bot not running"}, status_code=404)

    await manager.stop_bot(user.id, "live")
    # Balance is only meaningful while connected — zero it on disconnect.
    from app.bot.websocket import reset_live_balance
    await reset_live_balance(user.id)
    log.info("User %d stopped live bot via API", user.id)
    return JSONResponse({"ok": True})


@router.post("/bot/start-paper")
async def start_paper_bot(request: Request):
    user = await require_auth(request)
    manager = request.app.state.bot_manager

    if manager.get_worker(user.id, "paper") is not None:
        return JSONResponse({"ok": False, "error": "Paper bot already running"}, status_code=409)

    shared_market = request.app.state.shared_market
    paper_config = build_paper_config()
    await manager.start_bot(
        user.id, "paper", paper_config,
        broadcast_fn=make_broadcast_fn(user.id, "paper"),
        shared_market=shared_market,
    )

    async with db.get_session() as session:
        await session.execute(
            update(User).where(User.id == user.id).values(paper_bot_started=True)
        )
        await session.commit()

    log.info("User %d started paper bot", user.id)
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

    manager = request.app.state.bot_manager
    paper_bot_running = manager.get_worker(user.id, "paper") is not None

    fresh_user = await db.get_user(user.id)
    paper_bot_started = fresh_user.paper_bot_started if fresh_user else False

    return JSONResponse({
        "ok": True,
        "mode": mode,
        "paper_bot_running": paper_bot_running,
        "paper_bot_started": paper_bot_started,
    })
