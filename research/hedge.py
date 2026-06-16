"""Hedge-strategy exploration: at each AMD signal, open BOTH a long and a short.
One pair at a time per symbol — a new signal is skipped until BOTH legs of the
previous pair have closed.

SL schemes (--scheme):
  sym   : symmetric, both SL = ATR*1.5 from entry (baseline; whipsaws in chop).
  range : structural — favored leg SL = the directional smart-SL (manip extreme);
          counter leg SL = the OPPOSITE accumulation edge. Whipsaw then needs price
          to break the whole range both ways. Each leg has its own risk distance;
          TP = rr * (that leg's distance).

Run from bot/:  python -m research.hedge [--scheme sym|range] [--rr 2,3,4,5] [--risk 2]

Always prints the full per-asset table + baseline. Log the verdict to docs/research-log.md.
"""
import argparse
import asyncio

import amd_engine
from config import COMMISSION_PCT
from research.harness import Harness, base_cfg, INITIAL


def sim_pair(highs, lows, e, rr, long_sl, long_tp, short_sl, short_tp):
    """Simulate one long+short pair from bar e forward. SL checked before TP within a
    bar. Returns (long_r, short_r, close_idx, both_resolved). Unresolved leg → 0."""
    lr = sr = None
    n = len(highs)
    for i in range(e + 1, n):
        if lr is None:
            if lows[i] <= long_sl:
                lr = -1.0
            elif highs[i] >= long_tp:
                lr = rr
        if sr is None:
            if highs[i] >= short_sl:
                sr = -1.0
            elif lows[i] <= short_tp:
                sr = rr
        if lr is not None and sr is not None:
            return lr, sr, i, True
    return (lr or 0.0), (sr or 0.0), n - 1, (lr is not None and sr is not None)


def levels(scheme, s, entry, atr_e, rr):
    """Return (long_sl, long_tp, short_sl, short_tp, d_long, d_short) or None if invalid."""
    if scheme == "sym":
        d = atr_e * 1.5
        if d <= 0:
            return None
        return (entry - d, entry + rr * d, entry + d, entry - rr * d, d, d)
    # range: structural stops outside the accumulation range
    if s["direction"] == "short":
        short_sl = s["sl"]            # favored: smart-SL above the manip sweep
        long_sl = s["accLow"]         # counter: opposite (lower) range edge
    else:
        long_sl = s["sl"]             # favored: smart-SL below the manip sweep
        short_sl = s["accHigh"]       # counter: opposite (upper) range edge
    d_long = entry - long_sl
    d_short = short_sl - entry
    if d_long <= 0 or d_short <= 0:
        return None
    return (long_sl, entry + rr * d_long, short_sl, entry - rr * d_short, d_long, d_short)


async def main():
    ap = argparse.ArgumentParser(description="Hedge (long+short) strategy sim")
    ap.add_argument("--scheme", choices=["sym", "range"], default="range")
    ap.add_argument("--rr", default="2,3,4,5", help="comma list of reward:risk targets")
    ap.add_argument("--risk", type=float, default=2.0, help="total risk %% per signal (½ per leg)")
    args = ap.parse_args()
    rrs = [float(x) for x in args.rr.split(",")]
    risk_side = args.risk / 100.0 / 2.0

    h = await Harness.create()

    dir_setups = h.run_all()
    dstats = h.equity(dir_setups)
    print("DIRECTIONAL BASELINE (production config):")
    print(f"{'asset':>8} {'trades':>6} {'WR%':>5} {'totalR':>7}")
    for sym in h._candles:
        st = h.equity([s for s in dir_setups if s.get("_symbol") == sym])
        print(f"{sym:>8} {st['trades']:>6} {st['win_rate']:>5} {st['total_r']:>+7.0f}")
    print(f"{'ALL':>8} {dstats['trades']:>6} {dstats['win_rate']:>5} {dstats['total_r']:>+7.0f}"
          f"   → ${dstats['final']:,.0f} ({dstats['ret_pct']:+.0f}%) DD {dstats['max_dd']}%\n")

    market = {}
    for sym, data in h._candles.items():
        highs = [c["high"] for c in data]; lows = [c["low"] for c in data]; closes = [c["close"] for c in data]
        atr = amd_engine._wilder_atr(highs, lows, closes, 14)
        setups = [s for s in amd_engine.run(data, base_cfg(sym)) if s["result"] in ("win", "loss")]
        market[sym] = (highs, lows, atr, setups)

    print(f"HEDGE scheme={args.scheme}, both legs, one pair at a time, {args.risk}% risk/signal (½/leg).")
    print("bothW=both legs TP · 1leg=one leg ran to TP · whip=both stopped · unres=leg unresolved at data end · hold=avg bars to both-close.\n")
    hdr = (f"{'rr':>3} {'asset':>8} | {'pairs':>5} {'bothW%':>6} {'1leg%':>6} {'whip%':>6} {'unres%':>6} {'hold':>5} | "
           f"{'grossR':>7} {'R/pair':>7} | {'final$':>10} {'ret%':>6} {'maxDD%':>6}")

    for rr in rrs:
        print(hdr); print("-" * len(hdr))
        events = []
        agg = dict(pairs=0, both=0, ran=0, whip=0, unres=0, grossR=0.0, hold=0)
        for sym, (highs, lows, atr, setups) in market.items():
            ps = dict(pairs=0, both=0, ran=0, whip=0, unres=0, grossR=0.0, hold=0)
            last_close = -1
            for s in setups:
                e = s["entryIdx"]
                if e <= last_close:
                    continue
                lv = levels(args.scheme, s, s["entryPrice"], atr[e], rr)
                if lv is None:
                    continue
                long_sl, long_tp, short_sl, short_tp, dl, ds = lv
                lr, sr, ci, both = sim_pair(highs, lows, e, rr, long_sl, long_tp, short_sl, short_tp)
                last_close = ci
                ps["pairs"] += 1; ps["grossR"] += lr + sr; ps["hold"] += ci - e
                if lr == rr and sr == rr: ps["both"] += 1
                if lr == rr or sr == rr: ps["ran"] += 1
                if lr == -1.0 and sr == -1.0: ps["whip"] += 1
                if not both: ps["unres"] += 1
                events.append((s["entryTime"], lr, sr, s["entryPrice"], dl, ds))
            for k in agg: agg[k] += ps[k]
            p = ps["pairs"] or 1
            print(f"{rr:>3.0f} {sym:>8} | {ps['pairs']:>5} {ps['both']/p*100:>5.1f}% {ps['ran']/p*100:>5.1f}% "
                  f"{ps['whip']/p*100:>5.1f}% {ps['unres']/p*100:>5.1f}% {ps['hold']/p:>5.0f} | "
                  f"{ps['grossR']:>+7.0f} {ps['grossR']/p:>+7.2f} |")
        events.sort(key=lambda x: x[0])
        cap = INITIAL; peak = cap; mdd = 0.0
        for _, lr, sr, entry, dl, ds in events:
            for r, d in ((lr, dl), (sr, ds)):
                ra = cap * risk_side
                comm = (ra / d * entry) * COMMISSION_PCT * 2
                cap += ra * r - comm
            peak = max(peak, cap); mdd = max(mdd, (peak - cap) / peak * 100)
        p = agg["pairs"] or 1
        ret = (cap - INITIAL) / INITIAL * 100
        print(f"{rr:>3.0f} {'ALL':>8} | {agg['pairs']:>5} {agg['both']/p*100:>5.1f}% {agg['ran']/p*100:>5.1f}% "
              f"{agg['whip']/p*100:>5.1f}% {agg['unres']/p*100:>5.1f}% {agg['hold']/p:>5.0f} | "
              f"{agg['grossR']:>+7.0f} {agg['grossR']/p:>+7.2f} | {cap:>10,.0f} {ret:>+6.0f} {mdd:>6.1f}\n")


if __name__ == "__main__":
    asyncio.run(main())
