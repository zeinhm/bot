import json
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_auth, get_trading_mode
from config import SYMBOLS, LEVERAGE
import app.db as db
from app.core.context import get_global_context
from app.core.template_filters import register_filters

router = APIRouter()
templates = Jinja2Templates(directory="templates")
register_filters(templates)


@router.get("/dashboard")
async def dashboard(request: Request):
    user = await require_auth(request)
    mode = get_trading_mode(request)
    is_paper = (mode == "paper")
    ctx = await get_global_context(user.id, mode)

    recent_trades = await db.get_recent_trades(10, user_id=user.id, is_paper=is_paper)
    all_trades = await db.get_all_trades(user.id, is_paper)
    closed = [t for t in all_trades if t.result in ("win", "loss")]
    wins = sum(1 for t in closed if t.result == "win")
    losses = len(closed) - wins
    total_r = sum(t.r_value or 0 for t in closed)
    win_rate = (wins / len(closed) * 100) if closed else 0

    now = datetime.now(timezone.utc)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    year_trades = [t for t in closed if t.exit_time and t.exit_time >= year_start]
    year_pnl = sum(t.pnl_usdt or 0 for t in year_trades)
    year_trade_count = len(year_trades)

    # Anchor the equity curve to the user's real balance: start so the curve ends
    # at the current balance (= balance − all trade PnL). Falls back to the last
    # known balance, then to a flat baseline if we have no reading yet.
    real_balance = ctx.get("balance") or 0.0
    if real_balance <= 0:
        real_balance = await db.get_state("last_balance", 0.0, user_id=user.id, is_paper=is_paper) or 0.0
    total_pnl_all = sum(t.pnl_usdt or 0 for t in closed)
    start_equity = (real_balance - total_pnl_all) if real_balance > 0 else 10000.0

    by_exit = sorted(closed, key=lambda t: t.exit_time or t.entry_time)
    equity = start_equity
    peak_equity = equity
    max_dd_pct = 0.0
    max_dd_date = None
    eq_map = {}
    # Seed the curve with the starting equity (at the first trade's entry) so it
    # shows the rise from the start, and the % baseline is the real start balance.
    if by_exit:
        first = by_exit[0]
        seed_t = first.entry_time or first.exit_time
        if seed_t:
            eq_map[int(seed_t.timestamp())] = round(start_equity, 2)
    for t in by_exit:
        equity += t.pnl_usdt or 0
        if equity > peak_equity:
            peak_equity = equity
        dd_pct = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            max_dd_date = t.exit_time
        if t.exit_time:
            eq_map[int(t.exit_time.timestamp())] = round(equity, 2)
    equity_data = [{"time": k, "value": v} for k, v in sorted(eq_map.items())]

    risk_mode = await db.get_state("risk_mode", "static", user_id=user.id, is_paper=is_paper)
    risk_value = await db.get_state("risk_value", 10.0, user_id=user.id, is_paper=is_paper)

    ctx.update({
        "user": user,
        "year_pnl": year_pnl,
        "year_trade_count": year_trade_count,
        "now_year": now.year,
        "recent_trades": recent_trades,
        "wins": wins,
        "losses": losses,
        "total_r": total_r,
        "win_rate": win_rate,
        "max_dd": max_dd_pct,
        "max_dd_date": max_dd_date,
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
    shared_market = request.app.state.shared_market
    if shared_market and shared_market.market_client:
        try:
            kwargs = dict(symbol=symbol, interval=interval, limit=limit)
            if endTime:
                kwargs["endTime"] = endTime
            raw = await shared_market.market_client.futures_klines(**kwargs)
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
