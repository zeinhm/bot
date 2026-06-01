import logging
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from auth import require_auth
from config import STRATEGY_PARAMS, ACC_RANGE_MODE, LEVERAGE, COMMISSION_PCT, SLIPPAGE_TICKS
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
    ctx.update({
        "user": user,
        "params": STRATEGY_PARAMS,
        "acc_range_mode": ACC_RANGE_MODE,
        "leverage": LEVERAGE,
        "commission_pct": COMMISSION_PCT,
        "slippage_ticks": SLIPPAGE_TICKS,
        "page": "backtester",
    })
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
):
    await require_auth(request)
    data = await _load_candles(symbol, interval)
    if not data:
        return JSONResponse({"stats": {}, "setups": []})

    cfg = {
        "accLen": STRATEGY_PARAMS["acc_len"],
        "atrMultAcc": STRATEGY_PARAMS["atr_mult_acc"],
        "manLook": STRATEGY_PARAMS["man_look"],
        "fvgThreshold": STRATEGY_PARAMS["fvg_threshold"],
        "atrLen": STRATEGY_PARAMS["atr_len"],
        "atrMult": STRATEGY_PARAMS["atr_mult"],
        "rrr": STRATEGY_PARAMS["rrr"],
        "sweepFilter": STRATEGY_PARAMS["sweep_filter"],
        "skipMonths": STRATEGY_PARAMS["skip_months"],
        "accRangeMode": ACC_RANGE_MODE.get(symbol, "wick"),
    }

    setups = amd_engine.run(data, cfg)
    stats = amd_engine.compute_stats(setups, cfg["rrr"])

    return JSONResponse({"stats": stats, "setups": setups})
