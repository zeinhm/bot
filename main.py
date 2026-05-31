"""
FastAPI entry point.

Mounts routes, starts the bot via BotManager on startup, serves the dashboard.
"""

import asyncio
import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from config import (
    DATABASE_URL, BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    SYMBOLS, STRATEGY_PARAMS, COMMISSION_PCT, SLIPPAGE_TICKS,
    TICK_SIZE, CANDLE_BUFFER_SIZE, LEVERAGE, LOT_SIZE,
    SESSION_SECRET, GOOGLE_CLIENT_ID,
)
import database as db
from bot import broadcast, set_bot_manager, make_broadcast_fn
from auth import AuthRequired, decrypt
from app.bot.manager import BotManager
from app.bot.worker import BotConfig

from routes.auth import router as auth_router
from routes.dashboard import router as dashboard_router
from routes.trades import router as trades_router
from routes.settings import router as settings_router
from routes.position import router as position_router
from routes.analytics import router as analytics_router
from routes.backtester import router as backtester_router
from routes.alerts import router as alerts_router
from routes.track_record import router as track_record_router
from routes.ws import router as ws_legacy_router
from app.bot.websocket import router as ws_router, start_ws_tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger(__name__)


def _build_user_config(api_key: str, api_secret: str, testnet: bool) -> BotConfig:
    return BotConfig(
        api_key=api_key,
        api_secret=api_secret,
        testnet=testnet,
        symbols=SYMBOLS,
        strategy_params=STRATEGY_PARAMS,
        leverage=LEVERAGE,
        commission_pct=COMMISSION_PCT,
        slippage_ticks=SLIPPAGE_TICKS,
        tick_size=TICK_SIZE,
        lot_size=LOT_SIZE,
        candle_buffer_size=CANDLE_BUFFER_SIZE,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db(DATABASE_URL)

    manager = BotManager()
    set_bot_manager(manager)
    app.state.bot_manager = manager

    if GOOGLE_CLIENT_ID:
        configured = await db.get_all_configured_users()
        for user, user_cfg in configured:
            try:
                api_key = decrypt(user_cfg.binance_api_key_enc)
                api_secret = decrypt(user_cfg.binance_api_secret_enc)
                config = _build_user_config(api_key, api_secret, user_cfg.binance_testnet)
                await manager.start_bot(user.id, config, broadcast_fn=make_broadcast_fn(user.id))
                log.info("Auto-started bot for user %d (%s)", user.id, user.email)
            except Exception as e:
                log.error("Failed to start bot for user %d: %s", user.id, e)
    elif BINANCE_API_KEY:
        config = _build_user_config(BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET)
        await manager.start_bot(1, config, broadcast_fn=broadcast)

    ws_tasks = await start_ws_tasks(manager)

    yield

    for t in ws_tasks:
        t.cancel()

    for uid in list(manager.workers.keys()):
        try:
            await manager.stop_bot(uid)
        except Exception:
            pass


app = FastAPI(title="Trading Futures Bot", lifespan=lifespan)

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=86400 * 30)


@app.exception_handler(AuthRequired)
async def auth_required_handler(request: Request, exc: AuthRequired):
    return RedirectResponse("/login", status_code=303)


app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(trades_router)
app.include_router(settings_router)
app.include_router(position_router)
app.include_router(analytics_router)
app.include_router(backtester_router)
app.include_router(alerts_router)
app.include_router(track_record_router)
app.include_router(ws_legacy_router)
app.include_router(ws_router)
