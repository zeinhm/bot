"""Recompute AMD setup geometry (accumulation + manipulation zones) for a
signal that the live detector just produced — WITHOUT touching the frozen
``strategy.check_signal``.

``strategy.py`` is frozen (see CLAUDE.md) and its ``check_signal`` returns only
entry/sl/tp, discarding the acc/manip box geometry. This module re-walks the
SAME accumulation + manipulation state machine over the candle buffer to recover
the box corners, so the live Position chart can draw the exact same
acc / manipulation / TP-SL setup that the Backtester draws (``amd_engine`` keeps
this geometry via ``_make_setup``; the live path threw it away).

It reuses ``strategy.py``'s own primitives (rolling max/min, ATR, session/skip
filters) so only the lightweight state tracking is mirrored here — the signal
logic itself stays in the frozen detector. Output shape matches the Backtester
setup object the frontend consumes: ``accStartTime/accEndTime/accHigh/accLow``,
``manipTime/manipExtremum``, ``entryTime/entryPrice/sl/tp``, ``direction`` — with
times in unix **seconds** to match ``/api/live-candles``.
"""

from __future__ import annotations

from strategy import (
    _rolling_max,
    _rolling_min,
    _atr,
    _in_session,
    _in_skip_period,
)


def build_setup_geometry(candles: list[dict], params: dict, signal: dict) -> dict | None:
    """Rebuild the acc/manip geometry for the signal ``check_signal`` returned on
    the LAST bar of ``candles``. Returns the setup dict, or ``None`` if it can't be
    reconstructed (state machine left no live accumulation + manipulation).

    Walks the identical loop as ``strategy.check_signal`` but only tracks the zone
    state — no FVG/entry checks — then reads the state at the final bar, which is
    exactly the geometry behind the fired signal.
    """
    n = len(candles)
    min_bars = max(params["acc_len"], params["atr_len"], 60) + 10
    if n < min_bars:
        return None

    ts = [c["timestamp"] for c in candles]
    opens = [c["open"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]

    if params.get("acc_range_mode") == "body":
        range_hi = [max(opens[i], closes[i]) for i in range(n)]
        range_lo = [min(opens[i], closes[i]) for i in range(n)]
    else:
        range_hi = highs
        range_lo = lows
    acc_bhi = _rolling_max(range_hi, params["acc_len"])
    acc_blo = _rolling_min(range_lo, params["acc_len"])
    atr = _atr(highs, lows, closes, params["atr_len"])
    atr_acc = _atr(highs, lows, closes, params["acc_len"])

    acc_high = None
    acc_low = None
    acc_end = None
    acc_start = None
    m_high = False
    m_low = False
    m_ext = None
    m_idx = None

    start = max(params["acc_len"], params["atr_len"]) - 1

    for i in range(start, n):
        acc_range = acc_bhi[i] - acc_blo[i]
        rng_pct = acc_range / acc_blo[i] * 100 if acc_blo[i] > 0 else 0

        if params["acc_mode"] == "atr":
            is_acc = (
                acc_range <= atr_acc[i] * params["atr_mult_acc"]
                and acc_range >= atr_acc[i] * params.get("atr_mult_acc_min", 0.0)
                and rng_pct >= params.get("acc_width_min", 0.0)
                and _in_session(ts[i], params["sessions"])
            )
        else:
            is_acc = (
                rng_pct <= params["acc_width"]
                and rng_pct >= params.get("acc_width_min", 0.0)
                and _in_session(ts[i], params["sessions"])
            )

        if is_acc:
            acc_high = acc_bhi[i]
            acc_low = acc_blo[i]
            acc_end = i
            acc_start = max(0, i - params["acc_len"] + 1)
            m_high = False
            m_low = False
            m_ext = None
            m_idx = None

        if (
            not is_acc
            and acc_high is not None
            and acc_end is not None
            and i <= acc_end + params["man_look"]
            and _in_session(ts[i], params["sessions"])
            and not _in_skip_period(ts[i], params.get("skip_months", []), params.get("skip_weeks", {}))
            and i >= 2
            and atr[i] > 0
        ):
            cur_atr = atr[i]

            manip_min_mode = params.get("manip_min_mode", "off")
            manip_min_val = params.get("manip_min_val", 0.0)
            if manip_min_mode == "atr":
                manip_min_dist = cur_atr * manip_min_val
            elif manip_min_mode == "acc_atr":
                manip_min_dist = atr_acc[i] * manip_min_val
            elif manip_min_mode == "range":
                manip_min_dist = (acc_high - acc_low) * manip_min_val
            else:
                manip_min_dist = 0.0

            if not m_high and not m_low and highs[i] > acc_high + manip_min_dist:
                m_high = True
                m_ext = highs[i]
                m_idx = i
            if m_high and highs[i] > m_ext:
                m_ext = highs[i]
                m_idx = i

            if not m_low and not m_high and lows[i] < acc_low - manip_min_dist:
                m_low = True
                m_ext = lows[i]
                m_idx = i
            if m_low and lows[i] < m_ext:
                m_ext = lows[i]
                m_idx = i

    if acc_high is None or acc_end is None or acc_start is None or m_ext is None or m_idx is None:
        return None

    return {
        "direction": signal["direction"],
        "accStartTime": int(ts[acc_start] // 1000),
        "accEndTime": int(ts[acc_end] // 1000),
        "accHigh": acc_high,
        "accLow": acc_low,
        "manipTime": int(ts[m_idx] // 1000),
        "manipExtremum": m_ext,
        "entryTime": int(ts[n - 1] // 1000),
        "entryPrice": signal["entry_price"],
        "sl": signal["sl"],
        "tp": signal["tp"],
    }
