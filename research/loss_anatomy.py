"""Anatomy of losing trades: was the stop just wicked (price then reversed to our TP),
or a valid loss (price ran far the opposite way)?

For each LOSS, re-simulate from entry IGNORING the stop and see which happens first:
  - price reaches the original TP            → "recoverable" (stop cut a winner)
  - price runs K * SL-distance adverse        → "valid" loss (ran far opposite)
  - neither before data end                   → "unresolved"
Within a bar that hits both, the adverse side is counted first (conservative — never
overstates 'recoverable'). K is the "how far is far" knob (K=2 = one full SL past the stop).

Run from bot/:  python -m research.loss_anatomy [--k 1.5,2,3]
"""
import argparse
import asyncio

import amd_engine
from research.harness import Harness, base_cfg


def classify(highs, lows, e, direction, entry, d, tp, K):
    """Return ('recoverable'|'valid'|'unresolved', bars_to_resolution)."""
    adverse = entry - K * d if direction == "long" else entry + K * d
    n = len(highs)
    for i in range(e + 1, n):
        if direction == "long":
            hit_adv = lows[i] <= adverse
            hit_tp = highs[i] >= tp
        else:
            hit_adv = highs[i] >= adverse
            hit_tp = lows[i] <= tp
        if hit_adv:                # conservative: adverse wins ties
            return "valid", i - e
        if hit_tp:
            return "recoverable", i - e
    return "unresolved", n - 1 - e


async def main():
    ap = argparse.ArgumentParser(description="Loss-trade anatomy")
    ap.add_argument("--k", default="1.5,2,3", help="comma list of adverse multiples")
    args = ap.parse_args()
    Ks = [float(x) for x in args.k.split(",")]

    h = await Harness.create()
    market = {}
    for sym, data in h._candles.items():
        highs = [c["high"] for c in data]; lows = [c["low"] for c in data]
        setups = [s for s in amd_engine.run(data, base_cfg(sym)) if s["result"] == "loss"]
        market[sym] = (highs, lows, setups)

    total_losses = sum(len(v[2]) for v in market.values())
    print(f"Loss-trade anatomy — production config. Total losing trades: {total_losses}")
    print("recoverable = stop wicked us, price then hit our TP | valid = ran K×SL adverse first\n")

    for K in Ks:
        print(f"K = {K}  (valid = price ran {K}× the SL distance against us, i.e. {K-1:.1f}× past the stop)")
        print(f"{'asset':>8} {'losses':>6} {'recover%':>9} {'valid%':>7} {'unres%':>7} {'med-bars→TP':>12}")
        agg = dict(n=0, rec=0, val=0, un=0); rec_bars_all = []
        for sym, (highs, lows, setups) in market.items():
            n = rec = val = un = 0; rec_bars = []
            for s in setups:
                cls, bars = classify(highs, lows, s["entryIdx"], s["direction"],
                                     s["entryPrice"], abs(s["entryPrice"] - s["sl"]), s["tp"], K)
                n += 1
                if cls == "recoverable":
                    rec += 1; rec_bars.append(bars)
                elif cls == "valid":
                    val += 1
                else:
                    un += 1
            agg["n"] += n; agg["rec"] += rec; agg["val"] += val; agg["un"] += un
            rec_bars_all += rec_bars
            med = sorted(rec_bars)[len(rec_bars) // 2] if rec_bars else 0
            print(f"{sym:>8} {n:>6} {rec/n*100 if n else 0:>8.1f}% {val/n*100 if n else 0:>6.1f}% "
                  f"{un/n*100 if n else 0:>6.1f}% {med:>12}")
        n = agg["n"] or 1
        med = sorted(rec_bars_all)[len(rec_bars_all) // 2] if rec_bars_all else 0
        print(f"{'ALL':>8} {agg['n']:>6} {agg['rec']/n*100:>8.1f}% {agg['val']/n*100:>6.1f}% "
              f"{agg['un']/n*100:>6.1f}% {med:>12}\n")


if __name__ == "__main__":
    asyncio.run(main())
