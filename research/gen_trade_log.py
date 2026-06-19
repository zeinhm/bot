"""Regenerate the reference trade-log CSV at
choosen/amd-fvg-15m-v1/results/trade_log.csv from the CURRENT production config:
the COMBINED two-strategy overlay (amd_15m + trend_5m) — cross-strategy position
gating + SEPARATE per-strategy adaptive sizing over one shared wallet.

Full 30-column per-trade equity ledger (adaptive sizing, drawdown, streaks) with a
`strategy` column. Times are UTC. Reproduces the production seed:
886 trades, final equity $383,441.27 (+3,734%).

Run from bot/:  python -m research.gen_trade_log
"""
import asyncio
import csv
import os

from datetime import datetime, timezone

from research.harness import Harness
from research.overlay import CFG5M
import backtest_combine
from config import COMMISSION_PCT

OUT = os.path.join(os.path.dirname(__file__), "..", "..",
                   "choosen", "amd-fvg-15m-v1", "results", "trade_log.csv")

INITIAL = 10000.0
RISK = 0.02            # 2% base risk per trade
COMM = COMMISSION_PCT * 2   # round-trip commission on position value

FIELDS = ["trade_no", "symbol", "strategy", "direction", "result", "entry_time", "exit_time",
          "entry_price", "sl", "tp", "exit_price", "sl_dist", "tp_dist", "actual_rrr",
          "r_value", "cumulative_r", "equity_before", "risk_pct", "adaptive_active",
          "risk_amount", "position_value", "commission", "gross_pnl", "net_pnl",
          "equity_after", "equity_peak", "drawdown", "drawdown_pct", "win_streak", "lose_streak"]


def _t(sec):
    return datetime.fromtimestamp(sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


async def main():
    # Same setup generation as research/overlay.py (the validated combined model).
    h15 = await Harness.create(interval="15m")
    h5 = await Harness.create(interval="5m")
    s15 = h15.run_all()             # production 15m (adaptive 2:1/3:1)
    s5 = h5.run_all(CFG5M)          # 5m trend overlay (fixed 2:1, ADX-50 gate)
    for s in s15:
        s["_strategy"] = "amd_15m"
    for s in s5:
        s["_strategy"] = "trend_5m"

    # Cross-strategy gating: one open position per symbol across both (first-come).
    taken = backtest_combine.gate(s15 + s5)

    # Walk equity with SEPARATE per-strategy adaptive streaks over one wallet.
    streaks = {name: {"loss": 0, "win": 0, "consec_wins": 0, "active": False}
               for name in ("amd_15m", "trend_5m")}
    equity = INITIAL
    peak = INITIAL
    cum_r = 0.0
    rows = []

    for i, s in enumerate(sorted(taken, key=lambda x: x["entryTime"]), 1):
        strat = s["_strategy"]
        st = streaks[strat]
        adp = backtest_combine.adaptive_params(strat)

        entry = s["entryPrice"]; sl = s["sl"]; tp = s["tp"]; result = s["result"]
        exit_price = tp if result == "win" else sl
        sl_dist = abs(entry - sl); tp_dist = abs(tp - entry)
        if sl_dist == 0:
            continue
        actual_rrr = tp_dist / sl_dist
        r_value = actual_rrr if result == "win" else -1.0

        adaptive_at_trade = st["active"]
        risk_pct = adp["reduced_pct"] if st["active"] else RISK
        equity_before = equity
        risk_amount = equity_before * risk_pct
        position_value = risk_amount / sl_dist * entry
        commission = position_value * COMM
        gross_pnl = risk_amount * r_value          # win: risk×rrr · loss: −risk
        net_pnl = gross_pnl - commission
        equity = equity_before + net_pnl
        peak = max(peak, equity)
        drawdown = peak - equity
        drawdown_pct = drawdown / peak * 100 if peak > 0 else 0.0
        cum_r += r_value

        # Per-strategy adaptive-sizing state machine.
        if result == "loss":
            st["loss"] += 1; st["win"] = 0; st["consec_wins"] = 0
            if st["loss"] >= adp["threshold"] and not st["active"]:
                st["active"] = True
        else:
            st["win"] += 1; st["loss"] = 0
            if st["active"]:
                st["consec_wins"] += 1
                if st["consec_wins"] >= adp["wins_to_recover"]:
                    st["active"] = False; st["consec_wins"] = 0

        rows.append({
            "trade_no": i,
            "symbol": s["_symbol"].replace("USDT", ""),
            "strategy": strat,
            "direction": s["direction"], "result": result,
            "entry_time": _t(s["entryTime"]), "exit_time": _t(s["exitTime"]),
            "entry_price": f"{entry:.4f}", "sl": f"{sl:.4f}", "tp": f"{tp:.4f}",
            "exit_price": f"{exit_price:.4f}", "sl_dist": f"{sl_dist:.4f}",
            "tp_dist": f"{tp_dist:.4f}", "actual_rrr": f"{actual_rrr:.1f}",
            "r_value": f"{r_value:.1f}", "cumulative_r": f"{cum_r:.1f}",
            "equity_before": f"{equity_before:.2f}", "risk_pct": f"{risk_pct * 100:.2f}",
            "adaptive_active": adaptive_at_trade,
            "risk_amount": f"{risk_amount:.2f}", "position_value": f"{position_value:.2f}",
            "commission": f"{commission:.2f}", "gross_pnl": f"{gross_pnl:.2f}",
            "net_pnl": f"{net_pnl:.2f}", "equity_after": f"{equity:.2f}",
            "equity_peak": f"{peak:.2f}", "drawdown": f"{drawdown:.2f}",
            "drawdown_pct": f"{drawdown_pct:.2f}", "win_streak": st["win"],
            "lose_streak": st["loss"],
        })

    path = os.path.abspath(OUT)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    n15 = sum(1 for r in rows if r["strategy"] == "amd_15m")
    n5 = len(rows) - n15
    print(f"Wrote {len(rows)} trades ({n15} amd_15m / {n5} trend_5m) → {path}")
    print(f"Final equity: ${equity:,.2f} ({(equity - INITIAL) / INITIAL * 100:+.1f}%)  "
          f"peak ${peak:,.2f}  cumR {cum_r:+.0f}")


if __name__ == "__main__":
    asyncio.run(main())
