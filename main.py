"""
FastAPI entry point.

Mounts routes, starts the bot on startup, serves the dashboard.
"""

import asyncio
import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import DATABASE_URL
import database as db
from bot import start_bot

from routes.dashboard import router as dashboard_router
from routes.trades import router as trades_router
from routes.settings import router as settings_router
from routes.ws import router as ws_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db(DATABASE_URL)

    bot_task = asyncio.create_task(start_bot())

    yield

    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Trading Futures Bot", lifespan=lifespan)

app.include_router(dashboard_router)
app.include_router(trades_router)
app.include_router(settings_router)
app.include_router(ws_router)
