from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from config import BINANCE_API_KEY, BINANCE_TESTNET, SYMBOLS
import database as db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/settings")
async def settings_page(request: Request):
    bot_enabled = await db.get_state("bot_enabled", True)
    risk_mode = await db.get_state("risk_mode", "static")
    risk_value = await db.get_state("risk_value", 10.0)
    active_symbols = await db.get_state("active_symbols", SYMBOLS)

    return templates.TemplateResponse(request, "settings.html", {
        "bot_enabled": bot_enabled,
        "risk_mode": risk_mode,
        "risk_value": risk_value,
        "testnet": BINANCE_TESTNET,
        "api_key_set": bool(BINANCE_API_KEY),
        "all_symbols": SYMBOLS,
        "active_symbols": active_symbols,
        "page": "settings",
    })


@router.post("/settings")
async def update_settings(
    request: Request,
    bot_enabled: str = Form("off"),
    risk_mode: str = Form("static"),
    risk_value: float = Form(10.0),
):
    form = await request.form()
    active = [s for s in SYMBOLS if form.get(f"symbol_{s}") == "on"]
    if not active:
        active = SYMBOLS

    await db.set_state("bot_enabled", bot_enabled == "on")
    await db.set_state("risk_mode", risk_mode)
    await db.set_state("risk_value", risk_value)
    await db.set_state("active_symbols", active)

    return RedirectResponse(url="/settings", status_code=303)
