"""Reusable strategy-research harness — the durable replacement for throwaway
scratch scripts. Findings live in `docs/research-log.md` (read it before running
a new experiment — the answer may already be there).

Run experiments from the `bot/` dir as modules so imports resolve:
    python -m research.sweep --param manipMinVal --values 0.4,0.8,1.2
    python -m research.walkforward --dynamic

Or ad-hoc:
    import asyncio
    from research.harness import Harness
    h = asyncio.run(Harness.create())
    setups = h.run_all({"rrr": 3.0})
    print(h.equity(setups))

The equity sim mirrors `seed_trades.py` exactly (2% dynamic risk + adaptive sizing:
4 losses -> 0.25% until 2 wins, 0.04% commission per side), so research numbers match
the production seed. Baseline (no overrides) reproduces 755 trades / $183,632.75.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from dotenv import load_dotenv

import amd_engine
from config import STRATEGY_PARAMS, ACC_RANGE_MODE, SYMBOLS, COMMISSION_PCT
import app.db as db

INITIAL = 10000.0
RISK_PCT = 0.02


def base_cfg(symbol: str, **overrides) -> dict:
    """Full amd_engine cfg from STRATEGY_PARAMS (camelCase), with optional overrides."""
    cfg = {
        "accLen": STRATEGY_PARAMS["acc_len"],
        "accMode": STRATEGY_PARAMS["acc_mode"],
        "atrMultAcc": STRATEGY_PARAMS["atr_mult_acc"],
        "atrMultAccMin": STRATEGY_PARAMS.get("atr_mult_acc_min", 0.0),
        "accWidth": STRATEGY_PARAMS.get("acc_width", 0.2),
        "accWidthMin": STRATEGY_PARAMS.get("acc_width_min", 0.0),
        "manLook": STRATEGY_PARAMS["man_look"],
        "fvgThreshold": STRATEGY_PARAMS["fvg_threshold"],
        "atrLen": STRATEGY_PARAMS["atr_len"],
        "atrMult": STRATEGY_PARAMS["atr_mult"],
        "rrr": STRATEGY_PARAMS["rrr"],
        "dynamicRR": STRATEGY_PARAMS.get("dynamic_rr", False),
        "rrrTrend": STRATEGY_PARAMS.get("rrr_trend", 3.0),
        "rrrRange": STRATEGY_PARAMS.get("rrr_range", STRATEGY_PARAMS["rrr"]),
        "htfHours": STRATEGY_PARAMS.get("htf_hours", 6),
        "htfAdxPeriod": STRATEGY_PARAMS.get("htf_adx_period", 14),
        "htfAdxThreshold": STRATEGY_PARAMS.get("htf_adx_threshold", 40),
        "sweepFilter": STRATEGY_PARAMS["sweep_filter"],
        "sweepLen": STRATEGY_PARAMS.get("sweep_len", 5),
        "sweepMaxBars": STRATEGY_PARAMS.get("sweep_max_bars", 300),
        "skipMonths": STRATEGY_PARAMS["skip_months"],
        "skipWeeks": {int(k): v for k, v in STRATEGY_PARAMS.get("skip_weeks", {}).items()},
        "accRangeMode": ACC_RANGE_MODE.get(symbol, "wick"),
        "manipMinMode": STRATEGY_PARAMS.get("manip_min_mode", "off"),
        "manipMinVal": STRATEGY_PARAMS.get("manip_min_val", 0.0),
        "adxFilter": STRATEGY_PARAMS.get("adx_filter", False),
        "adxPeriod": STRATEGY_PARAMS.get("adx_period", 42),
        "adxThreshold": STRATEGY_PARAMS.get("adx_threshold", 35),
    }
    cfg.update(overrides)
    return cfg


def year_of(s: dict) -> int:
    return datetime.fromtimestamp(s["entryTime"], tz=timezone.utc).year


class Harness:
    """Loads candles once, runs the engine + equity sim with arbitrary overrides."""

    def __init__(self, candles: dict):
        self._candles = candles

    @classmethod
    async def create(cls, interval: str = "15m", symbols: list[str] | None = None) -> "Harness":
        load_dotenv()
        await db.init_db(os.getenv("DATABASE_URL", ""))
        symbols = symbols or SYMBOLS
        candles = {s: await db.get_all_historical_candles(s, interval) for s in symbols}
        return cls(candles)

    def run(self, symbol: str, overrides: dict | None = None) -> list[dict]:
        cfg = base_cfg(symbol, **(overrides or {}))
        out = []
        for s in amd_engine.run(self._candles[symbol], cfg):
            if s["result"] in ("win", "loss"):
                s["_symbol"] = symbol
                out.append(s)
        return out

    def run_all(self, overrides: dict | None = None) -> list[dict]:
        out: list[dict] = []
        for sym in self._candles:
            out.extend(self.run(sym, overrides))
        out.sort(key=lambda s: s["entryTime"])
        return out

    @staticmethod
    def equity(setups: list[dict], risk_pct: float = RISK_PCT,
               threshold: int | None = None, reduced_pct: float | None = None,
               wins_rec: int | None = None) -> dict:
        """Adaptive-sizing equity sim. Returns trades/win_rate/final/ret_pct/max_dd/
        max_streak/total_r/calmar/curve. Defaults match the production config."""
        threshold = threshold if threshold is not None else STRATEGY_PARAMS.get("loss_streak_threshold", 4)
        reduced_pct = reduced_pct if reduced_pct is not None else STRATEGY_PARAMS.get("reduced_risk_pct", 0.25) / 100.0
        wins_rec = wins_rec if wins_rec is not None else STRATEGY_PARAMS.get("wins_to_recover", 2)

        cap = INITIAL; ls = cw = 0; adaptive = False; peak = cap
        mdd = 0.0; max_streak = 0; wins = 0; total_r = 0.0
        curve = []
        for s in setups:
            rp = reduced_pct if adaptive else risk_pct
            ra = cap * rp
            sld = abs(s["entryPrice"] - s["sl"])
            if sld == 0:
                continue
            comm = (ra / sld * s["entryPrice"]) * COMMISSION_PCT * 2
            if s["result"] == "win":
                rr = abs(s["tp"] - s["entryPrice"]) / sld
                cap += ra * rr - comm
                ls = 0; wins += 1; total_r += rr
                if adaptive:
                    cw += 1
                    if cw >= wins_rec:
                        adaptive = False; cw = 0
            else:
                cap += -ra - comm
                ls += 1; cw = 0; total_r -= 1
                max_streak = max(max_streak, ls)
                if ls >= threshold:
                    adaptive = True
            peak = max(peak, cap)
            mdd = max(mdd, (peak - cap) / peak * 100)
            curve.append((s["entryTime"], round(cap, 2)))

        n = len(setups)
        ret = (cap - INITIAL) / INITIAL * 100
        return {
            "trades": n, "wins": wins,
            "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "final": round(cap, 2), "ret_pct": round(ret, 1),
            "max_dd": round(mdd, 1), "max_streak": max_streak,
            "total_r": round(total_r, 1),
            "calmar": round(ret / mdd, 1) if mdd > 0 else 0.0,
            "curve": curve,
        }

    @staticmethod
    def by_year(setups: list[dict]) -> dict[int, list[dict]]:
        years: dict[int, list[dict]] = {}
        for s in setups:
            years.setdefault(year_of(s), []).append(s)
        return dict(sorted(years.items()))
