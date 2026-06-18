"""Overlay test (Zein's model): ONE bot watches candles; the 15m and 5m strategies
each detect setups with their own params. SHARED position gating — one position per
SYMBOL across both strategies (first-come-first-served; a signal is skipped if that
symbol already has an open position from either strategy). One shared wallet.

Reports both adaptive-sizing models: GLOBAL (one streak — the natural single-bot model)
and SEPARATE (each strategy its own streak), so the sizing choice is visible.

5m config: 6h-ADX-50 gate, accLen 20, atrMultAcc 5, manipMin 1.5, RR 2:1, SL 1.5.
Run from bot/:  python -m research.overlay
"""
import asyncio

from research.harness import Harness, year_of, INITIAL, RISK_PCT
from config import COMMISSION_PCT

CFG5M = {"trendOnly": True, "dynamicRR": False, "rrrTrend": 2.0, "rrrRange": 2.0,
         "atrMult": 1.5, "accLen": 20, "atrMultAcc": 5.0, "htfAdxThreshold": 50,
         "manipMinVal": 1.5, "htfHours": 6}


def gate(setups):
    """One position per symbol across both strategies (first-come wins). Returns
    (taken, skipped_by_tf). `setups` must carry _symbol, entryTime, exitTime, _tf."""
    open_until = {}
    taken = []
    skipped = {"15m": 0, "5m": 0}
    for s in sorted(setups, key=lambda x: x["entryTime"]):
        sym = s["_symbol"]
        if open_until.get(sym, -1) > s["entryTime"]:   # symbol busy → skip
            skipped[s.get("_tf", "15m")] += 1
            continue
        open_until[sym] = s["exitTime"]
        taken.append(s)
    return taken, skipped


def equity(setups, mode="global"):
    """mode='global': one adaptive streak. mode='separate': per-_tf streak."""
    cap = INITIAL; peak = cap; mdd = 0.0; max_streak = 0
    streak = {}
    for s in sorted(setups, key=lambda x: x["entryTime"]):
        key = "ALL" if mode == "global" else s.get("_tf", "15m")
        d = streak.setdefault(key, {"ls": 0, "cw": 0, "ad": False})
        rp = 0.0025 if d["ad"] else RISK_PCT
        ra = cap * rp
        sld = abs(s["entryPrice"] - s["sl"])
        if sld == 0:
            continue
        comm = (ra / sld * s["entryPrice"]) * COMMISSION_PCT * 2
        if s["result"] == "win":
            cap += ra * (abs(s["tp"] - s["entryPrice"]) / sld) - comm
            d["ls"] = 0
            if d["ad"]:
                d["cw"] += 1
                if d["cw"] >= 2:
                    d["ad"] = False; d["cw"] = 0
        else:
            cap += -ra - comm
            d["ls"] += 1; d["cw"] = 0
            max_streak = max(max_streak, d["ls"])
            if d["ls"] >= 4:
                d["ad"] = True
        peak = max(peak, cap)
        mdd = max(mdd, (peak - cap) / peak * 100)
    ret = (cap - INITIAL) / INITIAL * 100
    return {"final": round(cap, 2), "ret_pct": round(ret, 1), "max_dd": round(mdd, 1),
            "calmar": round(ret / mdd, 1) if mdd > 0 else 0, "max_streak": max_streak,
            "trades": len(setups)}


def wf(setups, mode):
    i = equity([s for s in setups if year_of(s) <= 2023], mode)
    o = equity([s for s in setups if year_of(s) > 2023], mode)
    return i["ret_pct"], o["ret_pct"]


async def main():
    h15 = await Harness.create(interval="15m")
    h5 = await Harness.create(interval="5m")
    s15 = h15.run_all()
    s5 = h5.run_all(CFG5M)
    for s in s15:
        s["_tf"] = "15m"
    for s in s5:
        s["_tf"] = "5m"

    taken, skipped = gate(s15 + s5)
    n5_taken = sum(1 for s in taken if s["_tf"] == "5m")
    n15_taken = len(taken) - n5_taken
    print("=== OVERLAY (position-gated: one position per symbol across both) ===")
    print(f"15m signals: {len(s15)} → taken {n15_taken} (skipped {skipped['15m']})")
    print(f" 5m signals: {len(s5)} → taken {n5_taken} (skipped {skipped['5m']})")
    print(f"   total taken: {len(taken)}\n")

    def line(name, setups, mode):
        st = equity(setups, mode)
        wi, wo = wf(setups, mode)
        print(f"{name:>30}: ${st['final']:>10,.0f} ({st['ret_pct']:>+6.0f}%)  DD {st['max_dd']:>4.1f}%  "
              f"R/DD {st['calmar']:>5.1f}  strk {st['max_streak']:>2}  | WF in {wi:>+5.0f}% out {wo:>+6.0f}%")

    line("15m alone (baseline)", s15, "global")
    line("COMBINED — global adaptive", taken, "global")
    line("COMBINED — separate adaptive", taken, "separate")

    print("\nYear-by-year (combined, global adaptive vs 15m alone):")
    print(f"{'year':>5} | {'15m ret%':>9} {'15m DD%':>8} | {'comb ret%':>10} {'comb DD%':>9}")
    y15 = Harness.by_year(s15)
    ym = {}
    for s in taken:
        ym.setdefault(year_of(s), []).append(s)
    for y in sorted(ym):
        a = equity(y15.get(y, []), "global")
        c = equity(ym[y], "global")
        print(f"{y:>5} | {a['ret_pct']:>+8.0f}% {a['max_dd']:>7.1f}% | {c['ret_pct']:>+9.0f}% {c['max_dd']:>8.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
