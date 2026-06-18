"""Overlay multiple strategies into one backtest.

Single source of truth for how the 15m and 5m strategies combine: cross-strategy
position gating (one open position per symbol across all strategies, first-come) plus
SEPARATE per-strategy adaptive-sizing streaks over one shared wallet. Used by the
backtester route (routes/backtester.py) and by the seeder (seed_trades.py, which shares
gate() / adaptive_params()).

Setups passed in must carry `_symbol`, `_strategy`, `entryTime`, `exitTime`, plus the
usual `entryPrice`/`sl`/`tp`/`result`/`rrrUsed` fields produced by amd_engine.run().
"""
from __future__ import annotations

from config import STRATEGIES, COMMISSION_PCT

INITIAL_CAPITAL = 10000.0
RISK_PCT = 0.02


def adaptive_params(strategy: str) -> dict:
    """Per-strategy adaptive-sizing config (loss streak → reduced risk → recover)."""
    scfg = STRATEGIES.get(strategy, {})
    return {
        "threshold": scfg.get("loss_streak_threshold", 4),
        "reduced_pct": scfg.get("reduced_risk_pct", 0.25) / 100.0,
        "wins_to_recover": scfg.get("wins_to_recover", 2),
    }


def gate(setups: list[dict]) -> list[dict]:
    """One open position per symbol across ALL strategies (first-come-first-served):
    skip a setup if its symbol already has a position open at its entry time."""
    open_until: dict[str, int] = {}
    taken = []
    for s in sorted(setups, key=lambda x: x["entryTime"]):
        sym = s.get("_symbol")
        if open_until.get(sym, -1) > s["entryTime"]:
            continue
        open_until[sym] = s["exitTime"]
        taken.append(s)
    return taken


def simulate(setups: list[dict], cfg: dict | None = None, with_trades: bool = False) -> dict:
    """Gate the setups, then walk equity with per-strategy adaptive streaks over one
    wallet. Returns the same shape as amd_engine.simulate_equity (plus a per-trade
    `strategy` tag when with_trades)."""
    cfg = cfg or {}
    initial = cfg.get("initialCapital", INITIAL_CAPITAL)
    risk_pct = cfg.get("riskPct", RISK_PCT)
    commission_rate = cfg.get("commissionRate", COMMISSION_PCT)

    taken = gate(setups)
    streaks: dict[str, dict] = {}
    capital = initial
    peak = initial
    max_dd_pct = 0.0
    curve = []
    trades = [] if with_trades else None

    for s in taken:
        if s["result"] not in ("win", "loss"):
            continue
        strat = s.get("_strategy", "amd_15m")
        st = streaks.setdefault(strat, {"loss": 0, "wins": 0, "active": False})
        adp = adaptive_params(strat)

        cur_pct = adp["reduced_pct"] if st["active"] else risk_pct
        risk_amount = capital * cur_pct
        sl_dist = abs(s["entryPrice"] - s["sl"])
        if sl_dist == 0:
            continue
        position_value = risk_amount / sl_dist * s["entryPrice"]
        commission = position_value * commission_rate * 2

        if s["result"] == "win":
            actual_rrr = abs(s["tp"] - s["entryPrice"]) / sl_dist
            pnl = risk_amount * actual_rrr - commission
            r_value = actual_rrr
            st["loss"] = 0
            if st["active"]:
                st["wins"] += 1
                if st["wins"] >= adp["wins_to_recover"]:
                    st["active"] = False
                    st["wins"] = 0
        else:
            pnl = -risk_amount - commission
            r_value = -1.0
            st["loss"] += 1
            st["wins"] = 0
            if st["loss"] >= adp["threshold"]:
                st["active"] = True

        capital += pnl
        peak = max(peak, capital)
        dd_pct = (peak - capital) / peak * 100 if peak > 0 else 0
        max_dd_pct = max(max_dd_pct, dd_pct)
        curve.append({"time": s["entryTime"], "value": round(capital, 2)})

        if trades is not None:
            trades.append({
                "symbol": s.get("_symbol"),
                "strategy": strat,
                "direction": s.get("direction"),
                "entryTime": s.get("entryTime"),
                "exitTime": s.get("exitTime"),
                "entryPrice": s["entryPrice"],
                "exitPrice": s["tp"] if s["result"] == "win" else s["sl"],
                "slPrice": s["sl"],
                "tpPrice": s["tp"],
                "quantity": risk_amount / sl_dist,
                "commission": round(commission, 4),
                "result": s["result"],
                "r": round(r_value, 4),
                "targetRr": s.get("rrrUsed"),
                "pnl": round(pnl, 4),
                "equity": round(capital, 2),
            })

    out = {
        "initialCapital": initial,
        "finalCapital": round(capital, 2),
        "returnPct": round((capital - initial) / initial * 100, 1),
        "maxDdPct": round(max_dd_pct, 1),
        "curve": curve,
    }
    if trades is not None:
        out["trades"] = trades
    return out


def compute_stats(setups: list[dict], cfg: dict | None = None) -> dict:
    """Combined headline stats over the GATED setups (matches amd_engine.compute_stats
    shape). Pass cfg to also attach the equity sim under `equity`."""
    taken = gate(setups)
    wins = losses = 0
    total_r = 0.0
    per_strategy: dict[str, dict] = {}
    for s in taken:
        strat = s.get("_strategy", "amd_15m")
        ps = per_strategy.setdefault(strat, {"trades": 0, "wins": 0})
        if s["result"] == "win":
            sl_d = abs(s["entryPrice"] - s["sl"])
            tp_d = abs(s["tp"] - s["entryPrice"])
            total_r += tp_d / sl_d if sl_d > 0 else 0
            wins += 1
            ps["wins"] += 1
            ps["trades"] += 1
        elif s["result"] == "loss":
            losses += 1
            total_r -= 1
            ps["trades"] += 1
    total = wins + losses
    stats = {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "winRate": round(wins / total * 100) if total else 0,
        "totalR": round(total_r, 1),
        "perStrategy": {
            k: {**v, "winRate": round(v["wins"] / v["trades"] * 100) if v["trades"] else 0}
            for k, v in per_strategy.items()
        },
    }
    if cfg:
        stats["equity"] = simulate(setups, cfg)
    return stats
