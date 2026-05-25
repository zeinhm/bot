from fastapi import APIRouter, Request
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
