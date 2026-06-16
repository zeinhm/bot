"""Sweep one or two strategy params and print a results table.

Run from the bot/ dir:
    python -m research.sweep --param manipMinVal --values 0.4,0.8,1.2
    python -m research.sweep --param atrMult --values 1.0,1.5,2.0 --param2 rrr --values2 2,2.5,3
    python -m research.sweep --param htfAdxThreshold --values 30,35,40 --dynamic
    python -m research.sweep --param rrr --values 2,3 --risk 3

Params use amd_engine camelCase keys (accLen, manipMinVal, atrMult, rrr, htfAdxThreshold, ...).
ALWAYS log the conclusion to docs/research-log.md afterward so it's never re-derived.
"""
import argparse
import asyncio

from research.harness import Harness


def _coerce(v: str):
    v = v.strip()
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def _vals(s: str):
    return [_coerce(v) for v in s.split(",")]


async def main():
    ap = argparse.ArgumentParser(description="Strategy param sweep")
    ap.add_argument("--param", required=True, help="amd_engine cfg key (camelCase)")
    ap.add_argument("--values", required=True, help="comma list, e.g. 0.4,0.8,1.2")
    ap.add_argument("--param2", help="optional 2nd param for a 2D grid")
    ap.add_argument("--values2", help="comma list for --param2")
    ap.add_argument("--risk", type=float, default=2.0, help="risk %% per trade (default 2)")
    ap.add_argument("--dynamic", action="store_true", help="force dynamicRR on for the sweep")
    args = ap.parse_args()

    base = {"dynamicRR": True} if args.dynamic else {}
    v1 = _vals(args.values)
    v2 = _vals(args.values2) if args.values2 else [None]

    h = await Harness.create()
    p2 = args.param2 or ""
    print(f"{args.param:>18} {p2:>14} | {'trades':>6} {'WR%':>5} | "
          f"{'final$':>11} {'ret%':>7} {'maxDD%':>6} {'R/DD':>5}")
    print("-" * 78)
    for a in v1:
        for b in v2:
            ov = dict(base)
            ov[args.param] = a
            if args.param2 and b is not None:
                ov[args.param2] = b
            st = h.equity(h.run_all(ov), risk_pct=args.risk / 100.0)
            bcol = "" if b is None else b
            print(f"{a:>18} {bcol:>14} | {st['trades']:>6} {st['win_rate']:>5} | "
                  f"{st['final']:>11,.0f} {st['ret_pct']:>+7.0f} {st['max_dd']:>6.1f} {st['calmar']:>5.1f}")


if __name__ == "__main__":
    asyncio.run(main())
