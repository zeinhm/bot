"""
AMD FVG Strategy Engine — exact port of choosen/amd-fvg-15m-v1/strategy.py

Matches the validated config: wick-based accumulation, Wilder ATR,
smart SL (wider of ATR or manipulation wick), sweep filter, session filter.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")

SESSION_RANGES = {
    "sydney": (17 * 60, 2 * 60),
    "tokyo": (19 * 60, 4 * 60),
    "london": (3 * 60, 12 * 60),
    "ny": (8 * 60, 17 * 60),
}

DEFAULT_CFG = {
    "accLen": 60,
    "accMode": "atr",
    "accWidth": 0.2,
    "accWidthMin": 0.0,
    "atrMultAcc": 5.0,
    "atrMultAccMin": 0.0,
    "manLook": 10,
    "fvgThreshold": 0.1,
    "atrLen": 14,
    "atrMult": 1.5,
    "rrr": 2.0,
    "sessions": ["sydney", "tokyo", "london", "ny"],
    "sweepFilter": True,
    "sweepLen": 5,
    "sweepMaxBars": 300,
    "skipMonths": [5],
}


def _rolling_max(values, period):
    n = len(values)
    out = [0.0] * n
    dq = deque()
    for i in range(n):
        while dq and dq[0] < i - period + 1:
            dq.popleft()
        while dq and values[dq[-1]] <= values[i]:
            dq.pop()
        dq.append(i)
        out[i] = values[dq[0]]
    return out


def _rolling_min(values, period):
    n = len(values)
    out = [0.0] * n
    dq = deque()
    for i in range(n):
        while dq and dq[0] < i - period + 1:
            dq.popleft()
        while dq and values[dq[-1]] >= values[i]:
            dq.pop()
        dq.append(i)
        out[i] = values[dq[0]]
    return out


def _wilder_atr(highs, lows, closes, period):
    """Wilder's ATR (RMA smoothing), matches Pine's ta.atr()."""
    n = len(highs)
    tr = [0.0] * n
    atr = [0.0] * n
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    if n < period:
        return atr
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def _session_mask(times_sec, sessions):
    mask = [False] * len(times_sec)
    for i, ts in enumerate(times_sec):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(NY_TZ)
        t = dt.hour * 60 + dt.minute
        for sess in sessions:
            start, end = SESSION_RANGES[sess]
            if start < end:
                if start <= t < end:
                    mask[i] = True
                    break
            else:
                if t >= start or t < end:
                    mask[i] = True
                    break
    return mask


def _month_mask(times_sec, skip_months):
    if not skip_months:
        return [True] * len(times_sec)
    mask = [True] * len(times_sec)
    for i, ts in enumerate(times_sec):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if dt.month in skip_months:
            mask[i] = False
    return mask


def _compute_sweep_zones(highs, lows, closes, sweep_len, sweep_max_bars):
    n = len(highs)
    in_bull = [False] * n
    in_bear = [False] * n
    active_ph = []
    active_pl = []
    boxes = []

    for i in range(n):
        j = i - sweep_len
        if j >= sweep_len:
            is_ph = True
            for k in range(j - sweep_len, j + sweep_len + 1):
                if k != j and highs[k] > highs[j]:
                    is_ph = False
                    break
            if is_ph:
                active_ph.append([j, highs[j], False, False])

            is_pl = True
            for k in range(j - sweep_len, j + sweep_len + 1):
                if k != j and lows[k] < lows[j]:
                    is_pl = False
                    break
            if is_pl:
                active_pl.append([j, lows[j], False, False])

        for ph in active_ph:
            if ph[2] or ph[3]:
                continue
            if closes[i] > ph[1]:
                ph[2] = True
                continue
            if highs[i] > ph[1] and closes[i] < ph[1]:
                boxes.append([highs[i], ph[1], i, 1])
                ph[3] = True

        for pl in active_pl:
            if pl[2] or pl[3]:
                continue
            if closes[i] < pl[1]:
                pl[2] = True
                continue
            if lows[i] < pl[1] and closes[i] > pl[1]:
                boxes.append([pl[1], lows[i], i, -1])
                pl[3] = True

        if i % 500 == 0:
            active_ph = [p for p in active_ph if not p[2] and not p[3] and i - p[0] <= 2000]
            active_pl = [p for p in active_pl if not p[2] and not p[3] and i - p[0] <= 2000]

        kept = []
        for bx in boxes:
            top, bot, start_i, dr = bx
            if i - start_i > sweep_max_bars:
                continue
            if dr == -1 and closes[i] < bot:
                continue
            if dr == 1 and closes[i] > top:
                continue
            kept.append(bx)
            if bot <= closes[i] <= top:
                if dr == -1:
                    in_bull[i] = True
                else:
                    in_bear[i] = True
        boxes = kept

    return in_bull, in_bear


def run(data: list[dict], cfg: dict | None = None) -> list[dict]:
    """Run AMD FVG strategy on candle data (time in seconds)."""
    c = {**DEFAULT_CFG, **(cfg or {})}
    n = len(data)
    if n < 2:
        return []

    acc_len = int(c["accLen"])
    acc_mode = c.get("accMode", "atr")
    acc_width = float(c.get("accWidth", 0.2))
    acc_width_min = float(c.get("accWidthMin", 0.0))
    atr_mult_acc = float(c["atrMultAcc"])
    atr_mult_acc_min = float(c.get("atrMultAccMin", 0.0))
    man_look = int(c["manLook"])
    fvg_threshold = float(c["fvgThreshold"])
    atr_len = int(c["atrLen"])
    atr_mult = float(c["atrMult"])
    rrr = float(c["rrr"])
    sessions = c.get("sessions", ["sydney", "tokyo", "london", "ny"])
    sweep_filter = c.get("sweepFilter", True)
    sweep_len = int(c.get("sweepLen", 5))
    sweep_max_bars = int(c.get("sweepMaxBars", 300))
    skip_months = c.get("skipMonths", [5])
    acc_range_mode = c.get("accRangeMode", "wick")

    # Extract arrays
    times = [d["time"] for d in data]
    highs = [d["high"] for d in data]
    lows = [d["low"] for d in data]
    closes = [d["close"] for d in data]
    opens = [d["open"] for d in data]

    # Precompute
    if acc_range_mode == "body":
        range_hi = [max(opens[i], closes[i]) for i in range(n)]
        range_lo = [min(opens[i], closes[i]) for i in range(n)]
    else:
        range_hi = highs
        range_lo = lows
    acc_bhi = _rolling_max(range_hi, acc_len)
    acc_blo = _rolling_min(range_lo, acc_len)
    atr = _wilder_atr(highs, lows, closes, atr_len)
    atr_acc = _wilder_atr(highs, lows, closes, acc_len)
    in_sess = _session_mask(times, sessions)
    month_ok = _month_mask(times, skip_months)

    if sweep_filter:
        in_bull_sweep, in_bear_sweep = _compute_sweep_zones(
            highs, lows, closes, sweep_len, sweep_max_bars
        )
    else:
        in_bull_sweep = in_bear_sweep = [False] * n

    setups: list[dict] = []
    acc_high = None
    acc_low = None
    acc_end = None
    acc_start = None
    m_high = False
    m_low = False
    m_ext = None
    m_idx = None

    active = False
    is_long = False
    entry_px = 0.0
    sl_px = 0.0
    tp_px = 0.0

    start = max(acc_len, atr_len) - 1

    for i in range(start, n):
        # --- 1. Accumulation (wick-based) ---
        acc_range = acc_bhi[i] - acc_blo[i]
        rng_pct = acc_range / acc_blo[i] * 100 if acc_blo[i] > 0 else 0

        if acc_mode == "atr":
            is_acc = (
                atr_acc[i] > 0
                and acc_range <= atr_acc[i] * atr_mult_acc
                and acc_range >= atr_acc[i] * atr_mult_acc_min
                and rng_pct >= acc_width_min
                and in_sess[i]
                and month_ok[i]
            )
        else:
            is_acc = (
                rng_pct <= acc_width
                and rng_pct >= acc_width_min
                and in_sess[i]
                and month_ok[i]
            )

        if is_acc:
            acc_high = acc_bhi[i]
            acc_low = acc_blo[i]
            acc_end = i
            acc_start = max(0, i - acc_len + 1)
            m_high = False
            m_low = False
            m_ext = None
            m_idx = None

        # --- 2. Exit (SL before TP, same as validated) ---
        if active:
            if is_long:
                if lows[i] <= sl_px:
                    _close_trade(setups[-1], i, times[i], sl_px, "loss")
                    active = False
                elif highs[i] >= tp_px:
                    _close_trade(setups[-1], i, times[i], tp_px, "win")
                    active = False
            else:
                if highs[i] >= sl_px:
                    _close_trade(setups[-1], i, times[i], sl_px, "loss")
                    active = False
                elif lows[i] <= tp_px:
                    _close_trade(setups[-1], i, times[i], tp_px, "win")
                    active = False

        # --- 3. Manipulation + FVG + Entry ---
        if (
            not is_acc
            and acc_high is not None
            and acc_end is not None
            and i <= acc_end + man_look
            and in_sess[i]
            and month_ok[i]
            and i >= 2
            and atr[i] > 0
        ):
            cur_atr = atr[i]

            # Bearish manipulation (wick above acc high → sets up short)
            if not m_high and not m_low and highs[i] > acc_high:
                m_high = True
                m_ext = highs[i]
                m_idx = i

            if m_high:
                if highs[i] > m_ext:
                    m_ext = highs[i]
                    m_idx = i
                fvg_gap = lows[i - 2] - highs[i]
                if (
                    fvg_gap > cur_atr * fvg_threshold
                    and closes[i] < acc_high
                    and not active
                    and not in_bull_sweep[i]
                ):
                    entry = closes[i]
                    atr_sl = entry + cur_atr * atr_mult
                    smart_sl = max(atr_sl, m_ext)
                    sl_d = smart_sl - entry
                    tp_val = entry - sl_d * rrr

                    setups.append(_make_setup(
                        "short", acc_start, acc_end, acc_high, acc_low,
                        m_idx, m_ext, data, i, entry, smart_sl, tp_val,
                    ))
                    active = True
                    is_long = False
                    entry_px = entry
                    sl_px = smart_sl
                    tp_px = tp_val
                    acc_high = None
                    m_high = False

            # Bullish manipulation (wick below acc low → sets up long)
            if not m_low and not m_high and lows[i] < acc_low:
                m_low = True
                m_ext = lows[i]
                m_idx = i

            if m_low:
                if lows[i] < m_ext:
                    m_ext = lows[i]
                    m_idx = i
                fvg_gap = lows[i] - highs[i - 2]
                if (
                    fvg_gap > cur_atr * fvg_threshold
                    and closes[i] > acc_low
                    and not active
                    and not in_bear_sweep[i]
                ):
                    entry = closes[i]
                    atr_sl = entry - cur_atr * atr_mult
                    smart_sl = min(atr_sl, m_ext)
                    sl_d = entry - smart_sl
                    tp_val = entry + sl_d * rrr

                    setups.append(_make_setup(
                        "long", acc_start, acc_end, acc_high, acc_low,
                        m_idx, m_ext, data, i, entry, smart_sl, tp_val,
                    ))
                    active = True
                    is_long = True
                    entry_px = entry
                    sl_px = smart_sl
                    tp_px = tp_val
                    acc_high = None
                    m_low = False

    # Close open trade at last bar
    if active and setups:
        last = setups[-1]
        if last["exitIdx"] is None:
            _close_trade(last, n - 1, times[-1], closes[-1], "open")

    return setups


def _close_trade(trade, idx, time, price, result):
    trade["exitIdx"] = idx
    trade["exitTime"] = time
    trade["exitPrice"] = price
    trade["result"] = result


def _make_setup(direction, acc_start, acc_end, acc_high, acc_low,
                manip_idx, manip_ext, data, i, entry, sl, tp):
    return {
        "direction": direction,
        "accStartIdx": acc_start,
        "accEndIdx": acc_end,
        "accStartTime": data[acc_start]["time"],
        "accEndTime": data[acc_end]["time"],
        "accHigh": acc_high,
        "accLow": acc_low,
        "manipIdx": manip_idx,
        "manipTime": data[manip_idx]["time"],
        "manipExtremum": manip_ext,
        "fvgIdx": i,
        "entryIdx": i,
        "entryTime": data[i]["time"],
        "entryPrice": entry,
        "sl": sl,
        "tp": tp,
        "exitIdx": None,
        "exitTime": None,
        "exitPrice": None,
        "result": None,
    }


def compute_stats(setups: list[dict], rrr: float) -> dict:
    wins = losses = 0
    total_r = 0.0
    for s in setups:
        if s["result"] == "win":
            sl_d = abs(s["entryPrice"] - s["sl"])
            tp_d = abs(s["tp"] - s["entryPrice"])
            actual_rrr = tp_d / sl_d if sl_d > 0 else rrr
            wins += 1
            total_r += actual_rrr
        elif s["result"] == "loss":
            losses += 1
            total_r -= 1
    total = wins + losses
    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "winRate": round(wins / total * 100) if total else 0,
        "totalR": round(total_r, 1),
    }
