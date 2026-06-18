"""5m trend-sniper experiment: take the AMD setup on the 5m timeframe ONLY while
the 6h ADX-40 regime is trending, with high RRR (3:1 / 4:1) and wider SL options.

Hypothesis (Zein, live obs): in trends, accumulation+manipulation compress, so 5m
catches more/cleaner setups that run far. Compared against the 15m trend subset and
the full 15m strategy. See docs/research-log.md.

Run from bot/:  python -m research.tf5m_trend [--rr 3,4] [--atr 1.5,2,2.5] [--acclen 60] [--split 2023]
"""
import argparse
import asyncio
import itertools

from research.harness import Harness, year_of


def split_eq(h, setups, yr):
    ins = [s for s in setups if year_of(s) <= yr]
    oos = [s for s in setups if year_of(s) > yr]
    return h.equity(ins), h.equity(oos)


async def main():
    ap = argparse.ArgumentParser(description="5m trend-sniper sweep: ADX × accLen × atrMultAcc")
    ap.add_argument("--adx", default="40", help="6h ADX trend-gate threshold(s)")
    ap.add_argument("--acclen", default="30", help="accumulation length(s), e.g. 20,30,40")
    ap.add_argument("--accmult", default="5", help="accumulation tightness atrMultAcc(s), e.g. 2,3,4,5 (lower = tighter, less noise)")
    ap.add_argument("--manipmin", default="0.4", help="manipMinVal: min manipulation wick as ATR mult, comma list")
    ap.add_argument("--htfhours", default="6", help="trend-gate timeframe in HOURS, comma list (6,4,2,1,0.5,0.25 = 6h..15m)")
    ap.add_argument("--rr", type=float, default=3.0, help="reward:risk (fixed)")
    ap.add_argument("--atr", type=float, default=1.5, help="SL atrMult (fixed)")
    ap.add_argument("--nosweep", action="store_true", help="disable the liquidity-sweep filter (likely noise on 5m)")
    ap.add_argument("--noadxfilter", action="store_true", help="disable the 15m-level ADX-42 counter-trend entry filter")
    ap.add_argument("--byyear", action="store_true", help="print year-by-year breakdown per config")
    ap.add_argument("--split", type=int, default=2023)
    args = ap.parse_args()
    adxs = [float(x) for x in args.adx.split(",")]
    accls = [int(x) for x in args.acclen.split(",")]
    accmults = [float(x) for x in args.accmult.split(",")]
    manipmins = [float(x) for x in args.manipmin.split(",")]
    htfhrs = [float(x) for x in args.htfhours.split(",")]

    def tflabel(h):
        return f"{h:.0f}h" if h >= 1 else f"{h*60:.0f}m"

    h15 = await Harness.create(interval="15m")
    full15 = h15.equity(h15.run_all())
    trend15 = h15.equity(h15.run_all({"trendOnly": True, "dynamicRR": False, "rrrTrend": 3.0, "rrrRange": 3.0}))
    print(f"REF  full 15m strategy : {full15['trades']:>4} trades  ${full15['final']:>9,.0f} ({full15['ret_pct']:+.0f}%)  DD {full15['max_dd']}%")
    print(f"REF  15m trend-only rr3: {trend15['trades']:>4} trades  ${trend15['final']:>9,.0f} ({trend15['ret_pct']:+.0f}%)  DD {trend15['max_dd']}%\n")

    h5 = await Harness.create(interval="5m")
    print(f"5m TREND-ONLY  (rr={args.rr:.0f}, SL atr={args.atr}, "
          f"sweepFilter={'OFF' if args.nosweep else 'on'}, adxFilter={'OFF' if args.noadxfilter else 'on'}):")
    print(f"{'htf':>4} {'adx':>4} {'acc':>3} {'accM':>5} {'mMin':>5} | {'trades':>6} {'WR%':>5} {'totalR':>7} | "
          f"{'final$':>10} {'ret%':>6} {'maxDD%':>6} | {'WF in':>8} {'WF out':>8} {'(t)':>5}")
    print("-" * 102)
    for hh, adx, acc, accm, mm in itertools.product(htfhrs, adxs, accls, accmults, manipmins):
        ov = {"trendOnly": True, "dynamicRR": False, "rrrTrend": args.rr, "rrrRange": args.rr,
              "atrMult": args.atr, "accLen": acc, "atrMultAcc": accm, "htfAdxThreshold": adx,
              "manipMinVal": mm, "htfHours": hh}
        if args.nosweep:
            ov["sweepFilter"] = False
        if args.noadxfilter:
            ov["adxFilter"] = False
        allt = []
        for sym in h5._candles:
            allt += h5.run(sym, ov)
        allt.sort(key=lambda s: s["entryTime"])
        st = h5.equity(allt)
        ins, oos = split_eq(h5, allt, args.split)
        if args.byyear:
            print(f"\n=== ADX {adx:.0f} / accLen {acc} / atrMultAcc {accm:.0f} / manipMin {mm} / rr {args.rr:.0f} ===")
            print(f"{'year':>5} {'trades':>6} {'WR%':>5} {'totalR':>7} {'ret%':>7} {'maxDD%':>6}")
            for y, ss in h5.by_year(allt).items():
                ys = h5.equity(ss)
                print(f"{y:>5} {ys['trades']:>6} {ys['win_rate']:>5} {ys['total_r']:>+7.0f} {ys['ret_pct']:>+7.0f} {ys['max_dd']:>6.1f}")
            print(f"FULL {st['trades']:>6} {st['win_rate']:>5} {st['total_r']:>+7.0f} {st['ret_pct']:>+7.0f} {st['max_dd']:>6.1f}"
                  f"   |  WF in {ins['ret_pct']:+.0f}%  out {oos['ret_pct']:+.0f}% ({oos['trades']}t)")
            continue
        print(f"{tflabel(hh):>4} {adx:>4.0f} {acc:>3} {accm:>5.1f} {mm:>5.1f} | {st['trades']:>6} {st['win_rate']:>5} {st['total_r']:>+7.0f} | "
              f"{st['final']:>10,.0f} {st['ret_pct']:>+6.0f} {st['max_dd']:>6.1f} | "
              f"{ins['ret_pct']:>+7.0f}% {oos['ret_pct']:>+7.0f}% {oos['trades']:>5}")


if __name__ == "__main__":
    asyncio.run(main())
