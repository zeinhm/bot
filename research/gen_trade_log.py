"""Regenerate the reference trade-log CSV at
choosen/amd-fvg-15m-v1/results/trade_log.csv from the CURRENT production config
(adaptive 2:1/3:1 RR), with the full 29-column per-trade equity ledger
(adaptive sizing, drawdown, streaks). Times are UTC (matches the original).

Run from bot/:  python -m research.gen_trade_log
Reproduces the production seed: 755 trades, final equity $183,632.75.
"""
import asyncio
import csv
import os
from datetime import datetime, timezone

from research.harness import Harness

OUT = os.path.join(os.path.dirname(__file__), "..", "..",
                   "choosen", "amd-fvg-15m-v1", "results", "trade_log.csv")

INITIAL = 10000.0
RISK = 0.02            # 2%
REDUCED = 0.0025       # 0.25% during adaptive
THRESH = 4             # 4 losses → adaptive
RECOVER = 2            # 2 wins → normal
COMM = 0.0008          # 0.04% × 2 (round trip) on position value

FIELDS = ["trade_no", "symbol", "direction", "result", "entry_time", "exit_time",
          "entry_price", "sl", "tp", "exit_price", "sl_dist", "tp_dist", "actual_rrr",
          "r_value", "cumulative_r", "equity_before", "risk_pct", "adaptive_active",
          "risk_amount", "position_value", "commission", "gross_pnl", "net_pnl",
          "equity_after", "equity_peak", "drawdown", "drawdown_pct", "win_streak", "lose_streak"]


def _t(sec):
    return datetime.fromtimestamp(sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


async def main():
    h = await Harness.create()
    setups = h.run_all()   # production adaptive config, sorted by entryTime

    equity = INITIAL; peak = INITIAL; cum_r = 0.0
    loss_streak = win_streak = consec_wins = 0
    adaptive = False
    rows = []

    for i, s in enumerate(setups, 1):
        entry = s["entryPrice"]; sl = s["sl"]; tp = s["tp"]; result = s["result"]
        exit_price = tp if result == "win" else sl
        sl_dist = abs(entry - sl); tp_dist = abs(tp - entry)
        if sl_dist == 0:
            continue
        actual_rrr = tp_dist / sl_dist
        r_value = actual_rrr if result == "win" else -1.0

        adaptive_at_trade = adaptive
        risk_pct = REDUCED if adaptive else RISK
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

        if result == "loss":
            loss_streak += 1; win_streak = 0; consec_wins = 0
            if loss_streak >= THRESH and not adaptive:
                adaptive = True
        else:
            win_streak += 1; loss_streak = 0
            if adaptive:
                consec_wins += 1
                if consec_wins >= RECOVER:
                    adaptive = False; consec_wins = 0

        rows.append({
            "trade_no": i,
            "symbol": s["_symbol"].replace("USDT", ""),
            "direction": s["direction"], "result": result,
            "entry_time": _t(s["entryTime"]), "exit_time": _t(s["exitTime"]),
            "entry_price": f"{entry:.2f}", "sl": f"{sl:.2f}", "tp": f"{tp:.2f}",
            "exit_price": f"{exit_price:.2f}", "sl_dist": f"{sl_dist:.2f}",
            "tp_dist": f"{tp_dist:.2f}", "actual_rrr": f"{actual_rrr:.1f}",
            "r_value": f"{r_value:.1f}", "cumulative_r": f"{cum_r:.1f}",
            "equity_before": f"{equity_before:.2f}", "risk_pct": f"{risk_pct * 100:.2f}",
            "adaptive_active": adaptive_at_trade,
            "risk_amount": f"{risk_amount:.2f}", "position_value": f"{position_value:.2f}",
            "commission": f"{commission:.2f}", "gross_pnl": f"{gross_pnl:.2f}",
            "net_pnl": f"{net_pnl:.2f}", "equity_after": f"{equity:.2f}",
            "equity_peak": f"{peak:.2f}", "drawdown": f"{drawdown:.2f}",
            "drawdown_pct": f"{drawdown_pct:.2f}", "win_streak": win_streak,
            "lose_streak": loss_streak,
        })

    path = os.path.abspath(OUT)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    print(f"Wrote {len(rows)} trades → {path}")
    print(f"Final equity: ${equity:,.2f} ({(equity - INITIAL) / INITIAL * 100:+.1f}%)  "
          f"peak ${peak:,.2f}  cumR {cum_r:+.0f}")


if __name__ == "__main__":
    asyncio.run(main())
