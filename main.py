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
from fastapi.templating import Jinja2Templates
import secrets as _secrets

from starlette.middleware.sessions import SessionMiddleware

from app.middleware.csrf import CSRFMiddleware
from config import (
    DATABASE_URL, BINANCE_API_KEY, BINANCE_API_SECRET,
    SESSION_SECRET, GOOGLE_CLIENT_ID,
)
import app.db as db
from app.bot import broadcast, set_bot_manager, make_broadcast_fn, build_user_config, build_paper_config
from app.auth import AuthRequired, PendingApproval, AccountRejected, decrypt
from app.bot.manager import BotManager
from app.bot.shared_market import SharedMarketData
from app.bot.websocket import router as ws_router, start_ws_tasks

from routes.auth import router as auth_router
from routes.dashboard import router as dashboard_router
from routes.trades import router as trades_router
from routes.settings import router as settings_router
from routes.position import router as position_router
from routes.analytics import router as analytics_router
from routes.backtester import router as backtester_router
from routes.alerts import router as alerts_router
from routes.track_record import router as track_record_router
from routes.bot_control import router as bot_control_router
from routes.admin import router as admin_router, AdminNotFound

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db(DATABASE_URL)

    shared_market = SharedMarketData()
    await shared_market.connect()
    from config import SYMBOLS as _symbols
    await shared_market.load_history(_symbols)
    await shared_market.start_kline_stream(_symbols)
    app.state.shared_market = shared_market

    manager = BotManager()
    set_bot_manager(manager)
    app.state.bot_manager = manager

    if GOOGLE_CLIENT_ID:
        from sqlalchemy import select
        from app.db.models import User
        async with db.get_session() as session:
            result = await session.execute(select(User).where(User.is_approved == True))
            approved_users = list(result.scalars().all())

        for user in approved_users:
            if user.paper_bot_started:
                try:
                    paper_config = build_paper_config()
                    await manager.start_bot(user.id, "paper", paper_config,
                                            broadcast_fn=make_broadcast_fn(user.id, "paper"),
                                            shared_market=shared_market)
                    log.info("Auto-started paper bot for user %d (%s)", user.id, user.email)
                except Exception as e:
                    log.error("Failed to start paper bot for user %d: %s", user.id, e)

            user_cfg = await db.get_user_config(user.id)
            if user_cfg and user_cfg.binance_api_key_enc:
                try:
                    api_key = decrypt(user_cfg.binance_api_key_enc)
                    api_secret = decrypt(user_cfg.binance_api_secret_enc)
                    config = build_user_config(api_key, api_secret)
                    await manager.start_bot(user.id, "live", config,
                                            broadcast_fn=make_broadcast_fn(user.id, "live"),
                                            shared_market=shared_market)
                    log.info("Auto-started live bot for user %d (%s)", user.id, user.email)
                except Exception as e:
                    log.error("Failed to start live bot for user %d: %s", user.id, e)

    elif BINANCE_API_KEY:
        paper_config = build_paper_config()
        await manager.start_bot(1, "paper", paper_config,
                                broadcast_fn=make_broadcast_fn(1, "paper"),
                                shared_market=shared_market)
        config = build_user_config(BINANCE_API_KEY, BINANCE_API_SECRET)
        await manager.start_bot(1, "live", config, broadcast_fn=broadcast, shared_market=shared_market)

    ws_tasks = await start_ws_tasks(manager, shared_market)

    yield

    for t in ws_tasks:
        t.cancel()

    for key in list(manager.workers.keys()):
        try:
            await manager.stop_bot(key[0], key[1])
        except Exception:
            pass

    await shared_market.close()


app = FastAPI(title="Trading Futures Bot", lifespan=lifespan)

if SESSION_SECRET == "change-me-in-production":
    log.critical("SESSION_SECRET is the default value! Generating a random one. Set SESSION_SECRET in .env for persistent sessions.")
    _session_secret = _secrets.token_hex(32)
else:
    _session_secret = SESSION_SECRET

app.add_middleware(CSRFMiddleware)

app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    max_age=604800,
    same_site="lax",
    https_only=SESSION_SECRET != "change-me-in-production",
)


@app.exception_handler(AuthRequired)
async def auth_required_handler(request: Request, exc: AuthRequired):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(PendingApproval)
async def pending_handler(request: Request, exc: PendingApproval):
    return RedirectResponse("/pending", status_code=303)


@app.exception_handler(AccountRejected)
async def rejected_handler(request: Request, exc: AccountRejected):
    return RedirectResponse("/rejected", status_code=303)


_404_templates = Jinja2Templates(directory="templates")


@app.exception_handler(AdminNotFound)
async def admin_not_found_handler(request: Request, exc: AdminNotFound):
    return _404_templates.TemplateResponse(request, "404.html", {}, status_code=404)


app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/landing", StaticFiles(directory="landing-page"), name="landing")


@app.get("/")
async def landing_page():
    from fastapi.responses import HTMLResponse
    with open("landing-page/landing-page.html") as f:
        return HTMLResponse(f.read())


@app.get("/sw.js")
async def service_worker():
    # Served from root so the service worker can control the whole-site scope
    # (a /static/ URL would scope it to /static/). Header allows the broad scope.
    from fastapi.responses import FileResponse
    return FileResponse(
        "static/sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(trades_router)
app.include_router(settings_router)
app.include_router(position_router)
app.include_router(analytics_router)
app.include_router(backtester_router)
app.include_router(alerts_router)
app.include_router(track_record_router)
app.include_router(bot_control_router)
app.include_router(admin_router)
app.include_router(ws_router)
