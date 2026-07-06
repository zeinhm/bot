from __future__ import annotations

import asyncio
import logging
import time

import app.db as db

log = logging.getLogger(__name__)

# Paper accounts start at a real $10,000 virtual balance (PaperAccount default).
PAPER_START = 10000.0


async def capture_drawdown_base(user_id: int, api_key: str, api_secret: str) -> None:
    """Capture the real starting balance as the drawdown base for a GENUINELY NEW
    live account, at first key-connect. This is the reliable, permanent source (the
    historical balance can't be recovered later — Binance's snapshot only reaches
    ~30 days). Skips if a base is already set, or if the account already has bot
    trades — an existing account is seeded from the snapshot instead, so we never
    overwrite its history with a 'now' balance."""
    if await db.get_state("dd_base", None, user_id=user_id, is_paper=False) is not None:
        return
    trades = await db.get_all_trades(user_id, is_paper=False)
    if any(t.result in ("win", "loss") for t in trades):
        return
    async def _fetch_balance() -> float:
        from binance import AsyncClient
        from exchange import BinanceExchange
        ex = BinanceExchange(api_key, api_secret)
        ex.client = await AsyncClient.create(api_key=api_key, api_secret=api_secret)
        try:
            return await ex.get_balance()
        finally:
            await ex.client.close_connection()

    try:
        # Bounded so a slow/hung Binance call can't stall the key-save request.
        bal = await asyncio.wait_for(_fetch_balance(), timeout=12)
        await db.set_state("dd_base", round(float(bal), 8), user_id=user_id, is_paper=False)
        await db.set_state("dd_base_time", int(time.time()) - 1, user_id=user_id, is_paper=False)
        log.info("Captured drawdown base $%.2f for new user %d", bal, user_id)
    except Exception as e:
        log.warning("capture_drawdown_base failed for user %d: %s", user_id, e)


async def load_drawdown_events(user_id: int, is_paper: bool, closed_trades) -> list[tuple[int, float]]:
    """Capital events that fund the drawdown curve: the account's REAL starting
    balance (base) plus any deposits DURING the tracked period.

    Only the base + in-period deposits + the bot's own trades move the curve.
    WITHDRAWALS and MANUAL (non-bot) trades are excluded — a withdrawal isn't a
    trading loss, and manual trades aren't in our `trades` table.

    - Paper: $10k virtual start (its real starting balance).
    - Live: the real base is captured at API-key connect, or seeded from the Binance
      FUTURES account snapshot (stored as `dd_base` + `dd_base_time`, seconds). Then
      positive `TRANSFER` deposits AFTER `dd_base_time` are added. If no base is set
      → [] → `compute_drawdown` returns None → UI shows '—' (never a guessed base).
    """
    if is_paper:
        first = min((int(t.exit_time.timestamp()) for t in closed_trades if t.exit_time is not None),
                    default=0)
        return [(first - 1, PAPER_START)]

    base = await db.get_state("dd_base", None, user_id=user_id, is_paper=False)
    base_time = await db.get_state("dd_base_time", None, user_id=user_id, is_paper=False)
    if base is None or base_time is None or float(base) <= 0:
        return []

    base_time = int(base_time)
    events: list[tuple[int, float]] = [(base_time, float(base))]
    # Deposits STRICTLY AFTER the base — the base already reflects everything up to
    # base_time (deposits, withdrawals, manual). Withdrawals (< 0) are excluded.
    transfers = await db.get_state("capital_transfers", None, user_id=user_id, is_paper=False) or []
    events += [(int(x["time"]) // 1000, float(x["amount"]))
               for x in transfers
               if float(x.get("amount", 0)) > 0 and int(x["time"]) // 1000 > base_time]
    return events


def compute_drawdown(closed_trades, deposits) -> dict | None:
    """High-water-mark drawdown on the real balance curve = deposits + net PnL.

    Replays, in time order, each deposit (raises the base) and each closed trade's
    net PnL (`pnl_usdt`, already net of commission + funding via the anomaly
    reconciler). Peak = running high; current DD = (peak − balance)/peak; max DD =
    the deepest current DD ever (monotonic — nothing lowers it). Deposits raise the
    peak (current DD can reset to 0; max is preserved). Withdrawals never appear.

    Returns {max_pct, current_pct, series:[{time, value:-dd%}]} or None when there's
    no funding to measure against.
    """
    if not deposits:
        return None

    # (time, kind, delta) — kind 0 = deposit, 1 = trade, so a deposit applies before
    # a same-second trade (base is funded before that second's PnL is measured).
    events: list[tuple[int, int, float]] = [(int(ts), 0, float(amt)) for ts, amt in deposits]
    for t in closed_trades:
        if t.exit_time is not None:
            events.append((int(t.exit_time.timestamp()), 1, float(t.pnl_usdt or 0.0)))
    events.sort(key=lambda e: (e[0], e[1]))

    balance = peak = max_dd = current_dd = 0.0
    series: list[dict] = []
    for ts, _kind, delta in events:
        balance += delta
        if balance > peak:
            peak = balance
        current_dd = (peak - balance) / peak * 100 if peak > 0 else 0.0
        if current_dd > max_dd:
            max_dd = current_dd
        series.append({"time": ts, "value": round(-current_dd, 2)})

    if peak <= 0:
        return None
    return {
        "max_pct": round(max_dd, 2),
        "current_pct": round(current_dd, 2),
        "series": series,
    }
