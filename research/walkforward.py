"""Year-by-year + old/new walk-forward for a given config (honest OOS test).

Run from the bot/ dir:
    python -m research.walkforward                      # production default, year-by-year
    python -m research.walkforward --rrr 3              # fixed 3:1
    python -m research.walkforward --dynamic --htfAdxThreshold 40
    python -m research.walkforward --split 2023         # in-sample <=2023 vs OOS >=2024
    python -m research.walkforward --set manipMinVal=0.8 --set atrMult=2.0

Lesson from prior research (see docs/research-log.md): pick configs for RELIABILITY
(best worst-year), not best in-sample result — greedy picks fail OOS.
"""
import argparse
import asyncio

from research.harness import Harness


def _coerce(v: str):
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


async def main():
    ap = argparse.ArgumentParser(description="Walk-forward / year-by-year")
    ap.add_argument("--rrr", type=float, help="fixed reward:risk (disables dynamic)")
    ap.add_argument("--dynamic", action="store_true", help="enable dynamicRR")
    ap.add_argument("--htfAdxThreshold", type=float, help="6h ADX threshold for dynamic RR")
    ap.add_argument("--risk", type=float, default=2.0, help="risk %% per trade")
    ap.add_argument("--split", type=int, default=2023,
                    help="last in-sample year (OOS = year+1..end)")
    ap.add_argument("--set", action="append", default=[], metavar="key=val",
                    help="extra cfg override (repeatable), e.g. --set manipMinVal=0.8")
    args = ap.parse_args()

    ov: dict = {}
    if args.rrr is not None:
        ov["dynamicRR"] = False
        ov["rrr"] = args.rrr
    if args.dynamic:
        ov["dynamicRR"] = True
    if args.htfAdxThreshold is not None:
        ov["dynamicRR"] = True
        ov["htfAdxThreshold"] = args.htfAdxThreshold
    for kv in args.set:
        k, v = kv.split("=", 1)
        ov[k] = _coerce(v)

    h = await Harness.create()
    setups = h.run_all(ov)
    risk = args.risk / 100.0

    print(f"Config overrides: {ov or '(production default)'}   risk={args.risk}%\n")

    print("Year-by-year (fresh $10k each Jan):")
    print(f"{'year':>5} {'trades':>6} {'WR%':>5} {'ret%':>7} {'maxDD%':>6} {'streak':>6}")
    for y, ss in h.by_year(setups).items():
        st = h.equity(ss, risk_pct=risk)
        print(f"{y:>5} {st['trades']:>6} {st['win_rate']:>5} {st['ret_pct']:>+7.0f} "
              f"{st['max_dd']:>6.1f} {st['max_streak']:>6}")

    from research.harness import year_of
    ins = [s for s in setups if year_of(s) <= args.split]
    oos = [s for s in setups if year_of(s) > args.split]
    si = h.equity(ins, risk_pct=risk)
    so = h.equity(oos, risk_pct=risk)
    print(f"\nWalk-forward split at {args.split}:")
    print(f"  in-sample  (<= {args.split}): {si['trades']:>4} trades  ${si['final']:>9,.0f}  "
          f"ret {si['ret_pct']:>+5.0f}%  DD {si['max_dd']:>4.1f}%  R/DD {si['calmar']}")
    print(f"  OUT-SAMPLE (> {args.split}):  {so['trades']:>4} trades  ${so['final']:>9,.0f}  "
          f"ret {so['ret_pct']:>+5.0f}%  DD {so['max_dd']:>4.1f}%  R/DD {so['calmar']}")


if __name__ == "__main__":
    asyncio.run(main())
