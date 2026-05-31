import logging
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from auth import require_auth
from config import STRATEGY_PARAMS
import amd_engine
import database as db
from template_context import get_global_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")
log = logging.getLogger(__name__)

_candle_cache: dict[str, list[dict]] = {}


async def _load_candles(symbol: str, interval: str) -> list[dict]:
    key = f"{symbol}_{interval}"
    if key in _candle_cache:
        return _candle_cache[key]

    data = await db.get_all_historical_candles(symbol, interval)
    if data:
        _candle_cache[key] = data
        log.info("Loaded %d candles for %s from DB", len(data), key)
    return data


def _invalidate_cache(symbol: str = None, interval: str = None):
    if symbol and interval:
        _candle_cache.pop(f"{symbol}_{interval}", None)
    else:
        _candle_cache.clear()


@router.get("/backtester")
async def backtester_page(request: Request):
    user = await require_auth(request)
    ctx = await get_global_context(user.id)
    ctx.update({"user": user, "params": STRATEGY_PARAMS, "page": "backtester"})
    return templates.TemplateResponse(request, "backtester.html", ctx)


@router.get("/api/candles")
async def get_candles(
    request: Request,
    symbol: str = Query("BTCUSDT"),
    interval: str = Query("15m"),
    end: Optional[int] = Query(None),
    limit: int = Query(500, ge=1, le=5000),
):
    await require_auth(request)
    candles, has_more = await db.get_historical_candles(symbol, interval, end=end, limit=limit)
    if not candles:
        return JSONResponse({"candles": [], "hasMore": False})
    return JSONResponse({"candles": candles, "hasMore": has_more})


@router.get("/api/backtest")
async def run_backtest(
    request: Request,
    symbol: str = Query("BTCUSDT"),
    interval: str = Query("15m"),
    accLen: int = Query(60),
    atrMultAcc: float = Query(5.0),
    manLook: int = Query(10),
    fvgThreshold: float = Query(0.1),
    atrLen: int = Query(14),
    atrMult: float = Query(1.5),
    rrr: float = Query(2.0),
    sweepFilter: bool = Query(True),
    skipMay: bool = Query(True),
):
    await require_auth(request)
    data = await _load_candles(symbol, interval)
    if not data:
        return JSONResponse({"stats": {}, "setups": []})

    cfg = {
        "accLen": accLen, "atrMultAcc": atrMultAcc, "manLook": manLook,
        "fvgThreshold": fvgThreshold, "atrLen": atrLen,
        "atrMult": atrMult, "rrr": rrr,
        "sweepFilter": sweepFilter,
        "skipMonths": [5] if skipMay else [],
    }

    setups = amd_engine.run(data, cfg)
    stats = amd_engine.compute_stats(setups, rrr)

    return JSONResponse({"stats": stats, "setups": setups})
