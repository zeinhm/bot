from __future__ import annotations

import app.db as db


async def load_capital_events(user_id: int, is_paper: bool, closed_trades) -> list[tuple[int, float]]:
    """Capital events for the drawdown curve.

    Paper: its real $10k virtual start. Live, in priority order:
      1. Real Binance transfer history (synced into `capital_transfers` state by the
         balance poll) — the accurate source; handles deposits/withdrawals.
      2. Fallback: a single funding event = `balance − realized PnL` (the account's
         real starting capital, from the last-polled balance). Exact for accounts
         with no mid-stream deposits; used until transfers sync so the chart is never
         blank. NOT a $10k notional — it's derived from the real balance.
      3. Nothing known → [] → compute_drawdown returns None → UI shows '—'.
    """
    if is_paper:
        return paper_capital_events(closed_trades)

    transfers = await db.get_state("capital_transfers", None, user_id=user_id, is_paper=False) or []
    if transfers:
        return [(int(x["time"]) // 1000, float(x["amount"])) for x in transfers]

    bal = await db.get_state("last_balance", None, user_id=user_id, is_paper=False)
    if bal and bal > 0:
        total_pnl = sum(float(t.pnl_usdt or 0) for t in closed_trades)
        start_equity = bal - total_pnl
        if start_equity > 0:
            first = min((int(t.exit_time.timestamp()) for t in closed_trades if t.exit_time is not None),
                        default=0)
            return [(first - 1, start_equity)]
    return []


def compute_drawdown(closed_trades, capital_events) -> dict | None:
    """Real-balance high-water-mark drawdown, from real data only.

    Reconstructs the account's actual balance curve by replaying, in time order,
    every capital event (deposits/withdrawals) and every closed trade's realized
    PnL — then measures the decline from the running peak. A deposit raises the
    balance (and the peak), so future drawdown is measured against the larger
    account; that's the "deposits affect upcoming data" behaviour.

    Args:
        closed_trades: Trade rows (win/loss) with `.exit_time` and `.pnl_usdt`.
        capital_events: list of (unix_seconds, amount) — money in(+)/out(-) of the
            wallet, INCLUDING the initial funding. Live: Binance TRANSFER income.
            Paper: a single (start, 10000) virtual-funding event.

    Returns {max_pct, current_pct, series:[{time, value:-dd%}]} or None when there
    is no capital base to measure against (never a fabricated notional).
    """
    # No real capital base (e.g. a live account whose transfers haven't synced) →
    # we can't measure a % drawdown, so return None (UI shows '—'). Trades alone
    # must NOT be used as the base — that reconstructs a curve from $0 and produces
    # nonsense percentages.
    if not capital_events:
        return None

    events: list[tuple[int, float]] = [(int(ts), float(amt)) for ts, amt in capital_events]
    for t in closed_trades:
        if t.exit_time is not None:
            events.append((int(t.exit_time.timestamp()), float(t.pnl_usdt or 0.0)))

    if not events:
        return None

    # Capital events and a same-second trade: apply capital first so the deposit is
    # in the base before that second's PnL is measured.
    events.sort(key=lambda e: (e[0], 0 if e[1] is None else 0))
    events.sort(key=lambda e: e[0])

    balance = 0.0
    peak = 0.0
    max_dd = 0.0
    current_dd = 0.0
    series: list[dict] = []
    for ts, delta in events:
        balance += delta
        if balance > peak:
            peak = balance
        current_dd = (peak - balance) / peak * 100 if peak > 0 else 0.0
        if current_dd > max_dd:
            max_dd = current_dd
        series.append({"time": ts, "value": round(-current_dd, 2)})

    if peak <= 0:
        # No positive capital ever seen (e.g. only trades, no funding synced yet).
        return None

    return {
        "max_pct": round(max_dd, 2),
        "current_pct": round(current_dd, 2),
        "series": series,
    }


def paper_capital_events(closed_trades) -> list[tuple[int, float]]:
    """Virtual-funding event for a paper account: its real $10,000 starting balance
    placed just before the first trade. (Paper has no Binance transfers; $10k is the
    account's genuine start, not a fallback notional.)"""
    first = min((int(t.exit_time.timestamp()) for t in closed_trades if t.exit_time is not None),
                default=0)
    return [(first - 1, 10000.0)]
