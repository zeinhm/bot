"""Flip the AMD strategy: take the OPPOSITE direction of every signal (treat the
setup as a break-&-retest continuation instead of a reversal). Same entry, one
trade at a time. Compares flip vs the normal directional trade at the SAME rr.

SL schemes (--sl):
  mirror : flipped SL = original SL distance, opposite side (clean apples-to-apples).
  atr    : flipped SL = 1.5*ATR from entry.

Run from bot/:  python -m research.flip [--sl mirror|atr] [--rr 2,3]

Always prints the full per-asset table + baselines. Log the verdict to docs/research-log.md.
"""
import argparse
import asyncio

import amd_engine
from research.harness import Harness, base_cfg


def sim_exit(highs, lows, e, direction, sl, tp):
    """Walk from bar e+1; SL before TP within a bar. Returns 'win'/'loss'/'open'."""
    n = len(highs)
    for i in range(e + 1, n):
        if direction == "long":
            if lows[i] <= sl:
                return "loss"
            if highs[i] >= tp:
                return "win"
        else:
            if highs[i] >= sl:
                return "loss"
            if lows[i] <= tp:
                return "win"
    return "open"


def run_side(highs, lows, atr, setups, rr, sl_scheme, flip):
    """Return list of (entryTime, result, rr) for flipped (or normal) trades,
    one at a time."""
    out = []
    last_close = -1
    for s in setups:
        e = s["entryIdx"]
        if e <= last_close:
            continue
        entry = s["entryPrice"]
        d = (atr[e] * 1.5) if sl_scheme == "atr" else abs(entry - s["sl"])
        if d <= 0:
            continue
        direction = s["direction"]
        if flip:
            direction = "long" if direction == "short" else "short"
        if direction == "long":
            sl, tp = entry - d, entry + rr * d
        else:
            sl, tp = entry + d, entry - rr * d
        # find close index for one-at-a-time blocking
        n = len(highs); ci = n - 1; res = "open"
        for i in range(e + 1, n):
            if direction == "long":
                if lows[i] <= sl: res, ci = "loss", i; break
                if highs[i] >= tp: res, ci = "win", i; break
            else:
                if highs[i] >= sl: res, ci = "loss", i; break
                if lows[i] <= tp: res, ci = "win", i; break
        if res == "open":
            continue
        last_close = ci
        out.append((s["entryTime"], res, rr, entry, d))
    return out


def equity(trades):
    """Adaptive-sizing equity sim (with commission) on (time,result,rr,entry,d) tuples."""
    from config import COMMISSION_PCT
    INITIAL = 10000.0
    cap = INITIAL; ls = cw = 0; adaptive = False; peak = cap; mdd = 0.0; wins = 0; total_r = 0.0
    for _, res, rr, entry, d in sorted(trades, key=lambda t: t[0]):
        rp = 0.0025 if adaptive else 0.02
        ra = cap * rp
        comm = (ra / d * entry) * COMMISSION_PCT * 2
        if res == "win":
            cap += ra * rr - comm
            wins += 1; total_r += rr; ls = 0
            if adaptive:
                cw += 1
                if cw >= 2: adaptive = False; cw = 0
        else:
            cap += -ra - comm; ls += 1; cw = 0; total_r -= 1
            if ls >= 4: adaptive = True
        peak = max(peak, cap); mdd = max(mdd, (peak - cap) / peak * 100)
    n = len(trades)
    return dict(trades=n, win_rate=round(wins / n * 100, 1) if n else 0,
                total_r=round(total_r, 1), final=round(cap, 2),
                ret=round((cap - INITIAL) / INITIAL * 100, 1), dd=round(mdd, 1))


async def main():
    ap = argparse.ArgumentParser(description="Flip the AMD strategy")
    ap.add_argument("--sl", choices=["mirror", "atr"], default="mirror")
    ap.add_argument("--rr", default="2,3", help="comma list of fixed reward:risk")
    args = ap.parse_args()
    rrs = [float(x) for x in args.rr.split(",")]

    h = await Harness.create()
    market = {}
    for sym, data in h._candles.items():
        highs = [c["high"] for c in data]; lows = [c["low"] for c in data]; closes = [c["close"] for c in data]
        atr = amd_engine._wilder_atr(highs, lows, closes, 14)
        setups = [s for s in amd_engine.run(data, base_cfg(sym)) if s["result"] in ("win", "loss")]
        market[sym] = (highs, lows, atr, setups)

    print(f"FLIP vs NORMAL (SL scheme = {args.sl}, fixed rr, one trade at a time, adaptive sizing).")
    print("NORMAL here = directional trade re-simulated at the SAME fixed rr/SL (not the adaptive production).\n")

    for rr in rrs:
        for label, flip in (("NORMAL", False), ("FLIP", True)):
            print(f"=== {label}  rr={rr:.0f}  sl={args.sl} ===")
            print(f"{'asset':>8} {'trades':>6} {'WR%':>5} {'totalR':>7} {'final$':>10} {'ret%':>7} {'maxDD%':>6}")
            allt = []
            for sym, (highs, lows, atr, setups) in market.items():
                t = run_side(highs, lows, atr, setups, rr, args.sl, flip)
                allt += t
                st = equity(t)
                print(f"{sym:>8} {st['trades']:>6} {st['win_rate']:>5} {st['total_r']:>+7.0f} "
                      f"{st['final']:>10,.0f} {st['ret']:>+7.0f} {st['dd']:>6.1f}")
            st = equity(allt)
            print(f"{'ALL':>8} {st['trades']:>6} {st['win_rate']:>5} {st['total_r']:>+7.0f} "
                  f"{st['final']:>10,.0f} {st['ret']:>+7.0f} {st['dd']:>6.1f}\n")


if __name__ == "__main__":
    asyncio.run(main())
