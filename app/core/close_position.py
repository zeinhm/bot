"""Discretionary market-close of a single open position.

Shared by the owner's own close button (routes/position.py) and the admin
force-close (routes/admin/user_detail.py), so both book the trade identically.

Unlike the strategy's TP/SL exits, a discretionary close books the trade at its
ACTUAL realized R (pnl / risked amount), since it hit neither level.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import app.db as db
from config import COMMISSION_PCT

log = logging.getLogger(__name__)


class PositionNotFound(Exception):
    """No open position for that symbol (and direction) on the exchange."""


class AmbiguousPosition(Exception):
    """Hedge mode: the symbol has both a LONG and a SHORT leg open and the
    caller didn't say which to close. Never guessed — closing the wrong leg
    places a real market order on the wrong side."""


async def close_position(
    user_id: int,
    symbol: str,
    *,
    exchange,
    is_paper: bool,
    direction: str | None = None,
    worker=None,
    reason: str = "Manual close",
) -> dict:
    """Market-close `symbol` and resolve its tracked trade row at the real fill.

    `worker` is the running BotWorker when there is one: its per-symbol SL/TP
    recovery timers are cleared so a pending "failed to place SL/TP" alert loop
    stops immediately. `_active_trades` is left alone — the position poll
    re-syncs it from the DB's open trades every cycle.

    `direction` ("long"/"short") picks the leg in hedge mode. Pass it whenever
    the caller knows it; without it a two-legged symbol raises rather than
    closing a guess.

    Raises PositionNotFound when the exchange reports no such position, or
    AmbiguousPosition when the symbol has two legs and `direction` is absent.

    NOTE (hedge mode): `cancel_all_orders` is symbol-wide, so closing one leg
    also clears the OTHER leg's SL/TP. Pre-existing behavior shared with the
    worker's entry/SL-TP paths; see docs/order-safety.md.
    """
    # In hedge mode get_all_positions() returns one row per (symbol, side), so a
    # symbol alone can be ambiguous — match the side too and refuse to guess.
    positions = await exchange.get_all_positions()
    candidates = [
        p for p in positions
        if p["symbol"] == symbol and (direction is None or p["side"] == direction)
    ]
    if not candidates:
        side_txt = f" {direction}" if direction else ""
        raise PositionNotFound(f"No open{side_txt} position for {symbol}")
    if len(candidates) > 1:
        raise AmbiguousPosition(
            f"{symbol} has {len(candidates)} open legs "
            f"({', '.join(p['side'] for p in candidates)}) — specify direction")
    pos = candidates[0]

    close_side = "SELL" if pos["side"] == "long" else "BUY"
    pos_side = "LONG" if pos["side"] == "long" else "SHORT"
    order = await exchange.place_market_order(
        symbol, close_side, pos["quantity"], position_side=pos_side, reduce_only=True)
    await exchange.cancel_all_orders(symbol)
    exit_price = float(order.get("avgPrice") or 0) or None

    result = {
        "symbol": symbol,
        "direction": pos["side"],
        "quantity": pos["quantity"],
        "exit_price": exit_price,
        "trade_id": None,
        "pnl": None,
        "r_value": None,
        # False means the position is closed but its trade row could NOT be
        # booked (no fill price came back), so the row is still "open".
        "trade_resolved": False,
    }

    # If this position is a tracked bot trade, resolve its DB row at the real fill.
    open_trade = await db.get_open_trade_for_symbol(user_id, symbol, is_paper)
    if open_trade and exit_price and exit_price > 0:
        if open_trade.direction == "long":
            raw_pnl = (exit_price - open_trade.entry_price) * open_trade.quantity
        else:
            raw_pnl = (open_trade.entry_price - exit_price) * open_trade.quantity
        exit_comm = exit_price * open_trade.quantity * COMMISSION_PCT
        total_comm = (open_trade.commission or 0) + exit_comm
        pnl = raw_pnl - total_comm
        risk_amt = abs(open_trade.entry_price - (open_trade.sl_price or open_trade.entry_price)) * open_trade.quantity
        r_value = round(pnl / risk_amt, 2) if risk_amt > 0 else (1.0 if pnl > 0 else -1.0)
        await db.update_trade(open_trade.id, {
            "result": "win" if pnl > 0 else "loss",
            "r_value": r_value,
            "exit_time": datetime.now(timezone.utc),
            "exit_price": exit_price,
            "pnl_usdt": pnl,
            "commission": total_comm,
        })
        result.update({
            "trade_id": open_trade.id, "pnl": pnl, "r_value": r_value, "trade_resolved": True,
        })
    elif open_trade:
        # Closed on the exchange but we have no fill price to book it at. A
        # running worker's position poll will resolve the row from account
        # fills; a stopped one will not, so say so loudly rather than leaving a
        # silently-open row.
        result["trade_id"] = open_trade.id
        log.error(
            "%s: closed %s for user %d but got no fill price — trade #%d left open",
            reason, symbol, user_id, open_trade.id,
        )

    if worker is not None:
        worker._sltp_missing_since.pop(symbol, None)
        worker._sltp_last_alert.pop(symbol, None)

    await db.log_event(
        f"{reason}: {symbol}", level="warn", category="trade",
        user_id=user_id, is_paper=is_paper,
    )
    return result
