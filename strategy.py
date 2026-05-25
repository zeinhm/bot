"""
AMD FVG v1 strategy — ported from /choosen/amd-fvg-15m-v1/strategy.py

Adapted for live trading: works with a rolling candle buffer instead of full CSV.
Produces identical signals to the backtest version.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

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


def _in_session(ts_ms: int, sessions: list[str]) -> bool:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(NY_TZ)
    t = dt.hour * 60 + dt.minute
    for sess in sessions:
        start, end = SESSION_RANGES[sess]
        if start < end:
            if start <= t < end:
                return True
        else:
            if t >= start or t < end:
                return True
    return False


def _rolling_max(values: list[float], period: int) -> list[float]:
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


def _rolling_min(values: list[float], period: int) -> list[float]:
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


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float]:
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


def _compute_sweep_zones(
    highs: list[float], lows: list[float], closes: list[float],
    sweep_len: int, sweep_max_bars: int
) -> tuple[list[bool], list[bool]]:
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
            top, bot, start, dr = bx
            if i - start > sweep_max_bars:
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


def check_signal(candles: list[dict], params: dict) -> dict | None:
    """
    Run strategy on a candle buffer and return a signal if the LAST bar triggers entry.

    candles: list of dicts with keys: timestamp, open, high, low, close, volume
    params: strategy parameters (from config.STRATEGY_PARAMS)

    Returns dict with entry details or None.
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

    acc_bhi = _rolling_max(highs, params["acc_len"])
    acc_blo = _rolling_min(lows, params["acc_len"])
    atr = _atr(highs, lows, closes, params["atr_len"])
    atr_acc = _atr(highs, lows, closes, params["acc_len"])

    if params.get("sweep_filter", False):
        in_bull_sweep, in_bear_sweep = _compute_sweep_zones(
            highs, lows, closes, params["sweep_len"], params["sweep_max_bars"]
        )
    else:
        in_bull_sweep = in_bear_sweep = [False] * n

    acc_high = None
    acc_low = None
    acc_end = None
    m_high = False
    m_low = False
    m_ext = None

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
            m_high = False
            m_low = False
            m_ext = None

        if (
            not is_acc
            and acc_high is not None
            and acc_end is not None
            and i <= acc_end + params["man_look"]
            and _in_session(ts[i], params["sessions"])
            and i >= 2
            and atr[i] > 0
        ):
            cur_atr = atr[i]

            if not m_high and not m_low and highs[i] > acc_high:
                m_high = True
                m_ext = highs[i]

            if m_high:
                m_ext = max(m_ext, highs[i])
                fvg_gap = lows[i - 2] - highs[i]
                if (
                    fvg_gap > cur_atr * params["fvg_threshold"]
                    and closes[i] < acc_high
                    and not in_bull_sweep[i]
                ):
                    if i == n - 1:
                        entry_px = closes[i]
                        atr_sl = entry_px + cur_atr * params["atr_mult"]
                        sl = max(atr_sl, m_ext)
                        sl_d = sl - entry_px
                        tp = entry_px - sl_d * params["rrr"]
                        return {
                            "direction": "short",
                            "entry_price": entry_px,
                            "sl": sl,
                            "tp": tp,
                            "sl_distance": sl_d,
                            "atr": cur_atr,
                            "timestamp": ts[i],
                        }

            if not m_low and not m_high and lows[i] < acc_low:
                m_low = True
                m_ext = lows[i]

            if m_low:
                m_ext = min(m_ext, lows[i])
                fvg_gap = lows[i] - highs[i - 2]
                if (
                    fvg_gap > cur_atr * params["fvg_threshold"]
                    and closes[i] > acc_low
                    and not in_bear_sweep[i]
                ):
                    if i == n - 1:
                        entry_px = closes[i]
                        atr_sl = entry_px - cur_atr * params["atr_mult"]
                        sl = min(atr_sl, m_ext)
                        sl_d = entry_px - sl
                        tp = entry_px + sl_d * params["rrr"]
                        return {
                            "direction": "long",
                            "entry_price": entry_px,
                            "sl": sl,
                            "tp": tp,
                            "sl_distance": sl_d,
                            "atr": cur_atr,
                            "timestamp": ts[i],
                        }

    return None
