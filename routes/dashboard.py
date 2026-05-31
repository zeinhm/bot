import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from auth import require_auth
from bot import get_bot_for_user
from config import SYMBOLS, BINANCE_TESTNET, LEVERAGE
import database as db
from template_context import get_global_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/")
async def dashboard(request: Request):
    user = await require_auth(request)
    bot = get_bot_for_user(user.id)
    ctx = await get_global_context(user.id)

    open_position = None
    open_trade = await db.get_open_trade()
    if open_trade and bot:
        try:
            pos = await bot.exchange.get_position(open_trade.symbol)
            if pos:
                open_position = {
                    "trade": open_trade,
                    "unrealized_pnl": pos["unrealized_pnl"],
                }
        except Exception:
            open_position = {"trade": open_trade, "unrealized_pnl": 0.0}

    today_pnl = await db.get_today_pnl()
    today_trade_count = await db.get_today_trade_count()
    recent_trades = await db.get_recent_trades(10)

    all_trades = await db.get_all_trades()
    closed = [t for t in all_trades if t.result in ("win", "loss")]
    wins = sum(1 for t in closed if t.result == "win")
    losses = len(closed) - wins
    total_r = sum(t.r_value or 0 for t in closed)
    win_rate = (wins / len(closed) * 100) if closed else 0

    pnls = [t.pnl_usdt or 0 for t in closed]
    peak = 0.0
    max_dd = 0.0
    cumulative = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    equity_data = []
    cum = 0.0
    for t in closed:
        cum += t.pnl_usdt or 0
        if t.exit_time:
            equity_data.append({"time": int(t.exit_time.timestamp()), "value": round(cum, 2)})

    risk_mode = await db.get_state("risk_mode", "static")
    risk_value = await db.get_state("risk_value", 10.0)

    cfg = await db.get_user_config(user.id)
    testnet = cfg.binance_testnet if cfg else BINANCE_TESTNET

    ctx.update({
        "user": user,
        "open_position": open_position,
        "today_pnl": today_pnl,
        "today_trade_count": today_trade_count,
        "recent_trades": recent_trades,
        "wins": wins,
        "losses": losses,
        "total_r": total_r,
        "win_rate": win_rate,
        "max_dd": max_dd_pct,
        "testnet": testnet,
        "leverage": LEVERAGE,
        "risk_mode": risk_mode,
        "risk_value": risk_value,
        "equity_data": json.dumps(equity_data),
        "page": "dashboard",
    })
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/api/live-candles")
async def live_candles(
    request: Request,
    symbol: str = Query("BTCUSDT"),
    interval: str = Query("15m"),
    limit: int = Query(500, ge=1, le=1500),
    endTime: int = Query(None),
):
    user = await require_auth(request)
    bot = get_bot_for_user(user.id)
    if bot and bot.exchange.market_client:
        try:
            kwargs = dict(symbol=symbol, interval=interval, limit=limit)
            if endTime:
                kwargs["endTime"] = endTime
            raw = await bot.exchange.market_client.futures_klines(**kwargs)
            candles = [
                {"time": int(int(k[0]) / 1000), "open": float(k[1]),
                 "high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
                 "volume": float(k[5])}
                for k in raw
            ]
            return JSONResponse(candles)
        except Exception:
            pass
    candles = await db.get_candles(symbol, limit)
    return JSONResponse(candles)
