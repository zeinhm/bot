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

    data = await db.get_all_historical_candles("BTCUSDT", "15m")
    if data:
        from datetime import datetime, timezone
        first = datetime.fromtimestamp(data[0]["time"], tz=timezone.utc)
        last = datetime.fromtimestamp(data[-1]["time"], tz=timezone.utc)
        data_range = f"{first.strftime('%b %Y')} – {last.strftime('%b %Y')}"
    else:
        data_range = "No data"

    ctx = await get_global_context(user.id)
    ctx.update({
        "user": user,
        "params": STRATEGY_PARAMS,
        "acc_range_mode": ACC_RANGE_MODE,
        "leverage": LEVERAGE,
        "commission_pct": COMMISSION_PCT,
        "slippage_ticks": SLIPPAGE_TICKS,
        "data_range": data_range,
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


SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def _build_cfg(symbol: str) -> dict:
    return {
        "accLen": STRATEGY_PARAMS["acc_len"],
        "accMode": STRATEGY_PARAMS["acc_mode"],
        "atrMultAcc": STRATEGY_PARAMS["atr_mult_acc"],
        "atrMultAccMin": STRATEGY_PARAMS.get("atr_mult_acc_min", 0.0),
        "accWidth": STRATEGY_PARAMS.get("acc_width", 0.2),
        "accWidthMin": STRATEGY_PARAMS.get("acc_width_min", 0.0),
        "manLook": STRATEGY_PARAMS["man_look"],
        "fvgThreshold": STRATEGY_PARAMS["fvg_threshold"],
        "atrLen": STRATEGY_PARAMS["atr_len"],
        "atrMult": STRATEGY_PARAMS["atr_mult"],
        "rrr": STRATEGY_PARAMS["rrr"],
        "sweepFilter": STRATEGY_PARAMS["sweep_filter"],
        "sweepLen": STRATEGY_PARAMS.get("sweep_len", 5),
        "sweepMaxBars": STRATEGY_PARAMS.get("sweep_max_bars", 300),
        "skipMonths": STRATEGY_PARAMS["skip_months"],
        "skipWeeks": {int(k): v for k, v in STRATEGY_PARAMS.get("skip_weeks", {}).items()},
        "accRangeMode": ACC_RANGE_MODE.get(symbol, "wick"),
        "manipMinMode": STRATEGY_PARAMS.get("manip_min_mode", "off"),
        "manipMinVal": STRATEGY_PARAMS.get("manip_min_val", 0.0),
        "adxFilter": STRATEGY_PARAMS.get("adx_filter", False),
        "adxPeriod": STRATEGY_PARAMS.get("adx_period", 42),
        "adxThreshold": STRATEGY_PARAMS.get("adx_threshold", 35),
    }


def _equity_cfg() -> dict:
    return {
        "initialCapital": 10000.0,
        "riskPct": 0.02,
        "commissionRate": COMMISSION_PCT,
        "lossStreakThreshold": STRATEGY_PARAMS.get("loss_streak_threshold", 4),
        "reducedRiskPct": STRATEGY_PARAMS.get("reduced_risk_pct", 0.25) / 100.0,
        "winsToRecover": STRATEGY_PARAMS.get("wins_to_recover", 2),
    }


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

    cfg = _build_cfg(symbol)
    setups = amd_engine.run(data, cfg)
    stats = amd_engine.compute_stats(setups, cfg["rrr"])

    return JSONResponse({"stats": stats, "setups": setups})


@router.get("/api/backtest/combined")
async def run_backtest_combined(request: Request):
    await require_auth(request)

    all_setups = []
    per_asset = {}
    for symbol in SYMBOLS:
        data = await _load_candles(symbol, "15m")
        if not data:
            continue
        cfg = _build_cfg(symbol)
        setups = amd_engine.run(data, cfg)
        stats = amd_engine.compute_stats(setups, cfg["rrr"])
        short = symbol.replace("USDT", "")
        per_asset[short] = stats
        all_setups.extend(setups)

    all_setups.sort(key=lambda s: s["entryTime"])
    rrr = STRATEGY_PARAMS["rrr"]
    combined_stats = amd_engine.compute_stats(all_setups, rrr, _equity_cfg())

    return JSONResponse({"stats": combined_stats, "perAsset": per_asset})
