from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_auth
from config import STRATEGY_PARAMS, ACC_RANGE_MODE, LEVERAGE, COMMISSION_PCT, SLIPPAGE_TICKS
import amd_engine
import app.db as db
from app.core.context import get_global_context

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

    first_ts, last_ts = await db.get_historical_candle_range("BTCUSDT", "15m")
    if first_ts and last_ts:
        from datetime import datetime, timezone
        first = datetime.fromtimestamp(first_ts, tz=timezone.utc)
        last = datetime.fromtimestamp(last_ts, tz=timezone.utc)
        data_range = f"{first.strftime('%b %Y')} – {last.strftime('%b %Y')}"
    else:
        data_range = "No data"

    from app.auth import get_trading_mode
    mode = get_trading_mode(request)

    bt_results = await db.get_backtest_results()

    ctx = await get_global_context(user.id, mode)
    ctx.update({
        "user": user,
        "params": STRATEGY_PARAMS,
        "acc_range_mode": ACC_RANGE_MODE,
        "leverage": LEVERAGE,
        "commission_pct": COMMISSION_PCT,
        "slippage_ticks": SLIPPAGE_TICKS,
        "data_range": data_range,
        "bt_results": bt_results,
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


def _build_cfg(symbol: str, ov: dict | None = None) -> dict:
    cfg = {
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
        "dynamicRR": STRATEGY_PARAMS.get("dynamic_rr", False),
        "rrrTrend": STRATEGY_PARAMS.get("rrr_trend", 3.0),
        "rrrRange": STRATEGY_PARAMS.get("rrr_range", STRATEGY_PARAMS["rrr"]),
        "htfHours": STRATEGY_PARAMS.get("htf_hours", 6),
        "htfAdxPeriod": STRATEGY_PARAMS.get("htf_adx_period", 14),
        "htfAdxThreshold": STRATEGY_PARAMS.get("htf_adx_threshold", 40),
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
    # Playground overrides — ONLY the risk knobs (rrr, sessions, seasonal). The
    # detection params above are never overridable (protect the edge). Unset = defaults.
    if ov:
        cfg.update(ov)
    return cfg


def _equity_cfg(ov: dict | None = None) -> dict:
    cfg = {
        "initialCapital": 10000.0,
        "riskPct": 0.02,
        "commissionRate": COMMISSION_PCT,
        "lossStreakThreshold": STRATEGY_PARAMS.get("loss_streak_threshold", 4),
        "reducedRiskPct": STRATEGY_PARAMS.get("reduced_risk_pct", 0.25) / 100.0,
        "winsToRecover": STRATEGY_PARAMS.get("wins_to_recover", 2),
    }
    if ov and "riskPct" in ov:
        cfg["riskPct"] = ov["riskPct"]
    return cfg


def _parse_setup_overrides(sessions, skip_may, skip_tax) -> dict:
    """Build the amd_engine cfg overrides from playground query params: the
    sessions and the two seasonal-event toggles (Sell-in-May → skip May;
    US-tax-deadline → skip the April tax weeks). These change the actual setups,
    so each combo gets its own cached run. RR is set by the strategy (adaptive
    2:1/3:1 via the 6h trend filter) and is not a playground knob."""
    ov: dict = {}
    if sessions:
        picked = [s for s in sessions.split(",") if s in ("sydney", "tokyo", "london", "ny")]
        if picked:
            ov["sessions"] = picked
    if skip_may is not None:
        ov["skipMonths"] = STRATEGY_PARAMS["skip_months"] if int(skip_may) else []
    if skip_tax is not None:
        ov["skipWeeks"] = ({int(k): v for k, v in STRATEGY_PARAMS.get("skip_weeks", {}).items()}
                           if int(skip_tax) else {})
    return ov


# ── Result cache (run the engine once per strategy+params+data signature) ──────
_STRATEGY_ID = "amd_fvg_v1"
_CACHE_VERSION = "3"                     # bump to invalidate all cached runs (v3: adaptive RR)
_run_cache: dict[str, dict] = {}        # signature -> {run_id, stats, total, signature}
_combined_cache: dict[str, dict] = {}   # combo signature -> {stats, perAsset}
_tradelog_cache: dict[str, dict] = {}   # combo signature -> {trades, total}
_build_locks: dict[str, asyncio.Lock] = {}  # signature -> lock (one builder per combo)


def _params_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()


def _signature(symbol: str, interval: str, params_hash: str,
               first_ts, last_ts, count) -> str:
    raw = f"{_CACHE_VERSION}|{_STRATEGY_ID}|{symbol}|{interval}|{params_hash}|{first_ts}|{last_ts}|{count}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def _get_or_build_run(symbol: str, interval: str, ov: dict | None = None) -> dict:
    """Return cached metadata {run_id, stats, total, signature} for this
    (strategy, params, data) combo — running amd_engine only on a cache miss.
    `ov` carries playground risk overrides (rrr/sessions/seasonal); each distinct
    combo becomes its own cached run. Setups are fetched by page/range from the DB."""
    cfg = _build_cfg(symbol, ov)
    params_hash = _params_hash(cfg)
    first_ts, last_ts = await db.get_historical_candle_range(symbol, interval)
    count = await db.get_historical_candle_count(symbol, interval)
    sig = _signature(symbol, interval, params_hash, first_ts, last_ts, count)

    # 1) hot in-memory (metadata only)
    if sig in _run_cache:
        return _run_cache[sig]

    # Serialize builds for this signature so concurrent requests (the page fires
    # /api/backtest, /combined and /setups at once) don't each run the engine and
    # race on the DB write.
    lock = _build_locks.setdefault(sig, asyncio.Lock())
    async with lock:
        if sig in _run_cache:                      # built while we waited
            return _run_cache[sig]

        # 2) persisted (survives redeploys)
        run = await db.get_backtest_run(sig)
        if run is not None:
            meta = {"run_id": run.id, "stats": json.loads(run.stats or "{}"),
                    "total": run.total_setups, "signature": sig}
            _run_cache[sig] = meta
            return meta

        # 3) miss — run the engine once, persist
        data = await _load_candles(symbol, interval)
        if not data:
            return {"run_id": None, "stats": {}, "total": 0, "signature": sig}
        setups = amd_engine.run(data, cfg)
        stats = amd_engine.compute_stats(setups, cfg["rrr"])
        run_id = await db.save_backtest_run({
            "signature": sig, "strategy": _STRATEGY_ID, "symbol": symbol, "interval": interval,
            "params_hash": params_hash, "data_first_ts": first_ts, "data_last_ts": last_ts,
            "candle_count": count, "total_setups": len(setups), "stats": json.dumps(stats),
        }, setups)
        meta = {"run_id": run_id, "stats": stats, "total": len(setups), "signature": sig}
        _run_cache[sig] = meta
        log.info("Backtest computed + cached: %s (%d setups)", symbol, len(setups))
        return meta


@router.get("/api/backtest")
async def run_backtest(
    request: Request,
    symbol: str = Query("BTCUSDT"),
    interval: str = Query("15m"),
    sessions: Optional[str] = Query(None),
    skip_may: Optional[int] = Query(None),
    skip_tax: Optional[int] = Query(None),
):
    """Stats + total setup count only. Setups are loaded lazily via /api/backtest/setups."""
    await require_auth(request)
    ov = _parse_setup_overrides(sessions, skip_may, skip_tax)
    meta = await _get_or_build_run(symbol, interval, ov)
    return JSONResponse({"stats": meta["stats"], "total": meta["total"]})


@router.get("/api/backtest/setups")
async def backtest_setups(
    request: Request,
    symbol: str = Query("BTCUSDT"),
    interval: str = Query("15m"),
    from_ts: Optional[int] = Query(None, alias="from"),
    to_ts: Optional[int] = Query(None, alias="to"),
    limit: int = Query(10, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sessions: Optional[str] = Query(None),
    skip_may: Optional[int] = Query(None),
    skip_tax: Optional[int] = Query(None),
):
    """Setups for a run, by time range (from/to, for the chart) or newest-first
    page (limit/offset, for nav). Each setup carries its `_ordinal` (global index)."""
    await require_auth(request)
    ov = _parse_setup_overrides(sessions, skip_may, skip_tax)
    meta = await _get_or_build_run(symbol, interval, ov)
    if meta["run_id"] is None:
        return JSONResponse({"setups": [], "total": 0})
    if from_ts is not None and to_ts is not None:
        setups = await db.get_run_setups_range(meta["run_id"], from_ts, to_ts)
    else:
        setups = await db.get_run_setups_page(meta["run_id"], limit, offset)
    return JSONResponse({"setups": setups, "total": meta["total"]})


@router.get("/api/backtest/combined")
async def run_backtest_combined(
    request: Request,
    sessions: Optional[str] = Query(None),
    skip_may: Optional[int] = Query(None),
    skip_tax: Optional[int] = Query(None),
    risk_pct: Optional[float] = Query(None),
):
    await require_auth(request)

    setup_ov = _parse_setup_overrides(sessions, skip_may, skip_tax)
    equity_ov = {"riskPct": risk_pct / 100.0} if risk_pct is not None else None
    eff_rrr = STRATEGY_PARAMS["rrr"]

    metas = {symbol: await _get_or_build_run(symbol, "15m", setup_ov) for symbol in SYMBOLS}
    combo_sig = hashlib.sha256(
        ("|".join(metas[s]["signature"] for s in SYMBOLS)
         + "|" + _params_hash(_equity_cfg(equity_ov))).encode()
    ).hexdigest()

    # combined result is itself cached (mem → persisted COMBINED run row)
    if combo_sig in _combined_cache:
        return JSONResponse(_combined_cache[combo_sig])

    lock = _build_locks.setdefault(combo_sig, asyncio.Lock())
    async with lock:
        if combo_sig in _combined_cache:
            return JSONResponse(_combined_cache[combo_sig])
        run = await db.get_backtest_run(combo_sig)
        if run is not None:
            payload = json.loads(run.stats or "{}")
            _combined_cache[combo_sig] = payload
            return JSONResponse(payload)

        all_setups = []
        per_asset = {}
        for symbol in SYMBOLS:
            m = metas[symbol]
            per_asset[symbol.replace("USDT", "")] = m["stats"]
            if m["run_id"] is not None:
                all_setups.extend(await db.get_run_setups_all(m["run_id"]))
        all_setups.sort(key=lambda s: s["entryTime"])
        combined_stats = amd_engine.compute_stats(all_setups, eff_rrr, _equity_cfg(equity_ov))
        payload = {"stats": combined_stats, "perAsset": per_asset}

        await db.save_backtest_run({
            "signature": combo_sig, "strategy": _STRATEGY_ID, "symbol": "COMBINED", "interval": "15m",
            "params_hash": _params_hash(_equity_cfg(equity_ov)), "data_first_ts": None, "data_last_ts": None,
            "candle_count": None, "total_setups": 0, "stats": json.dumps(payload),
        }, [])
        _combined_cache[combo_sig] = payload
        return JSONResponse(payload)


@router.get("/api/backtest/tradelog")
async def backtest_tradelog(
    request: Request,
    sessions: Optional[str] = Query(None),
    skip_may: Optional[int] = Query(None),
    skip_tax: Optional[int] = Query(None),
    risk_pct: Optional[float] = Query(None),
):
    """Param-aware combined trade ledger for the Playground's Trade Log: same
    setup overrides + risk as /api/backtest/combined, but returns the per-trade
    list (with adaptive-sizing PnL + running equity) instead of just stats."""
    await require_auth(request)

    setup_ov = _parse_setup_overrides(sessions, skip_may, skip_tax)
    equity_ov = {"riskPct": risk_pct / 100.0} if risk_pct is not None else None

    metas = {symbol: await _get_or_build_run(symbol, "15m", setup_ov) for symbol in SYMBOLS}
    combo_sig = hashlib.sha256(
        ("|".join(metas[s]["signature"] for s in SYMBOLS)
         + "|" + _params_hash(_equity_cfg(equity_ov)) + "|tradelog").encode()
    ).hexdigest()

    if combo_sig in _tradelog_cache:
        return JSONResponse(_tradelog_cache[combo_sig])

    lock = _build_locks.setdefault(combo_sig, asyncio.Lock())
    async with lock:
        if combo_sig in _tradelog_cache:
            return JSONResponse(_tradelog_cache[combo_sig])

        all_setups = []
        for symbol in SYMBOLS:
            m = metas[symbol]
            if m["run_id"] is not None:
                for s in await db.get_run_setups_all(m["run_id"]):
                    s["_symbol"] = symbol
                    all_setups.append(s)
        all_setups.sort(key=lambda s: s["entryTime"])

        eq = amd_engine.simulate_equity(
            all_setups, STRATEGY_PARAMS["rrr"], _equity_cfg(equity_ov), with_trades=True)
        trades = eq.get("trades", [])
        payload = {"trades": trades, "total": len(trades)}
        _tradelog_cache[combo_sig] = payload
        return JSONResponse(payload)
