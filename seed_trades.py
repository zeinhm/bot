"""
Seed the trades table with backtest results.

Runs the AMD FVG strategy against historical candles in the DB,
then inserts the results as Trade rows so the dashboard, trades,
analytics, and track record pages all show real data.

Usage:
    python seed_trades.py                  # all 3 assets
    python seed_trades.py --symbol BTCUSDT # single asset
    python seed_trades.py --clear          # wipe trades first
"""

import asyncio
import sys
from datetime import datetime, timezone

from config import (
    STRATEGY_PARAMS, ACC_RANGE_MODE, SYMBOLS,
    COMMISSION_PCT, SLIPPAGE_TICKS, TICK_SIZE, LEVERAGE,
)
import amd_engine
import database as db

INITIAL_CAPITAL = 10000.0
RISK_PCT = 0.02

ADAPTIVE = {
    "threshold": STRATEGY_PARAMS.get("loss_streak_threshold", 4),
    "reduced_pct": STRATEGY_PARAMS.get("reduced_risk_pct", 0.25) / 100.0,
    "wins_to_recover": STRATEGY_PARAMS.get("wins_to_recover", 2),
}


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


def _ts_to_dt(epoch_sec: int) -> datetime:
    return datetime.fromtimestamp(epoch_sec, tz=timezone.utc)


def _apply_slippage(price: float, direction: str, is_entry: bool, symbol: str) -> float:
    tick = TICK_SIZE.get(symbol, 0.01)
    slip = SLIPPAGE_TICKS * tick
    if direction == "long":
        return price + slip if is_entry else price - slip
    else:
        return price - slip if is_entry else price + slip


async def seed(symbols: list[str], clear: bool = False):
    from dotenv import load_dotenv
    import os
    load_dotenv()
    await db.init_db(os.getenv("DATABASE_URL", ""))

    if clear:
        async with db.get_session() as session:
            await session.execute(db.delete(db.Trade))
            await session.commit()
        print("Cleared existing trades")

    rrr = STRATEGY_PARAMS["rrr"]
    all_setups = []

    for symbol in symbols:
        data = await db.get_all_historical_candles(symbol, "15m")
        if not data:
            print(f"  {symbol}: no historical candles, skipping")
            continue

        cfg = _build_cfg(symbol)
        setups = amd_engine.run(data, cfg)
        closed = [s for s in setups if s["result"] in ("win", "loss")]
        print(f"  {symbol}: {len(closed)} trades ({sum(1 for s in closed if s['result'] == 'win')}W / {sum(1 for s in closed if s['result'] == 'loss')}L)")

        for s in closed:
            s["_symbol"] = symbol
        all_setups.extend(closed)

    all_setups.sort(key=lambda s: s["entryTime"])
    print(f"\n  Total: {len(all_setups)} trades across {len(symbols)} assets")

    # Simulate equity with adaptive sizing to compute pnl_usdt and quantity
    capital = INITIAL_CAPITAL
    loss_streak = 0
    consec_wins = 0
    adaptive_active = False
    rows = []

    for s in all_setups:
        symbol = s["_symbol"]
        direction = s["direction"]
        entry_raw = s["entryPrice"]
        sl_raw = s["sl"]
        tp_raw = s["tp"]
        result = s["result"]

        exit_raw = tp_raw if result == "win" else sl_raw

        current_risk_pct = ADAPTIVE["reduced_pct"] if adaptive_active else RISK_PCT
        risk_amount = capital * current_risk_pct
        sl_dist = abs(entry_raw - sl_raw)
        if sl_dist == 0:
            continue

        quantity = risk_amount / sl_dist
        position_value = risk_amount / sl_dist * entry_raw
        commission = position_value * COMMISSION_PCT * 2

        if result == "win":
            tp_dist = abs(tp_raw - entry_raw)
            actual_rrr = tp_dist / sl_dist if sl_dist > 0 else rrr
            pnl = risk_amount * actual_rrr - commission
            r_value = actual_rrr
        else:
            pnl = -risk_amount - commission
            r_value = -1.0

        # Adaptive sizing state
        if result == "loss":
            loss_streak += 1
            consec_wins = 0
            if loss_streak >= ADAPTIVE["threshold"] and not adaptive_active:
                adaptive_active = True
        else:
            loss_streak = 0
            if adaptive_active:
                consec_wins += 1
                if consec_wins >= ADAPTIVE["wins_to_recover"]:
                    adaptive_active = False
                    consec_wins = 0

        capital += pnl

        rows.append({
            "symbol": symbol,
            "direction": direction,
            "entry_time": _ts_to_dt(s["entryTime"]),
            "exit_time": _ts_to_dt(s["exitTime"]),
            "entry_price": round(entry_raw, 8),
            "exit_price": round(exit_raw, 8),
            "sl_price": round(sl_raw, 8),
            "tp_price": round(tp_raw, 8),
            "quantity": round(quantity, 8),
            "result": result,
            "r_value": round(r_value, 4),
            "pnl_usdt": round(pnl, 4),
            "commission": round(commission, 4),
        })

    # Batch insert
    async with db.get_session() as session:
        for row in rows:
            session.add(db.Trade(**row))
        await session.commit()

    print(f"  Inserted {len(rows)} trades into DB")
    print(f"  Final equity: ${capital:,.2f} ({(capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100:+.1f}%)")


def main():
    clear = "--clear" in sys.argv
    symbol_arg = None
    if "--symbol" in sys.argv:
        idx = sys.argv.index("--symbol")
        symbol_arg = sys.argv[idx + 1]

    symbols = [symbol_arg] if symbol_arg else SYMBOLS
    asyncio.run(seed(symbols, clear=clear))


if __name__ == "__main__":
    main()
