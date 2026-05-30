"""
FastAPI entry point.

Mounts routes, starts the bot via BotManager on startup, serves the dashboard.
"""

import asyncio
import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import (
    DATABASE_URL, BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    SYMBOLS, STRATEGY_PARAMS, COMMISSION_PCT, SLIPPAGE_TICKS,
    TICK_SIZE, CANDLE_BUFFER_SIZE, LEVERAGE, LOT_SIZE,
)
import database as db
from bot import broadcast, set_bot_manager
from app.bot.manager import BotManager
from app.bot.worker import BotConfig

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

PHASE1_USER_ID = 1


def _build_phase1_config() -> BotConfig:
    return BotConfig(
        api_key=BINANCE_API_KEY,
        api_secret=BINANCE_API_SECRET,
        testnet=BINANCE_TESTNET,
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

    config = _build_phase1_config()
    await manager.start_bot(PHASE1_USER_ID, config, broadcast_fn=broadcast)

    ws_tasks = await start_ws_tasks(manager)

    yield

    for t in ws_tasks:
        t.cancel()

    try:
        await manager.stop_bot(PHASE1_USER_ID)
    except Exception:
        pass


app = FastAPI(title="Trading Futures Bot", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

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
