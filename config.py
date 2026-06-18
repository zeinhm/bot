import os

from dotenv import load_dotenv

load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", "")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-in-production")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "ZENITH BOT <noreply@zenithbot.org>")

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

TICK_SIZE = {"BTCUSDT": 0.10, "ETHUSDT": 0.01, "SOLUSDT": 0.01}

STRATEGY_PARAMS = {
    "tf_minutes": 15,
    "acc_len": 60,
    "acc_mode": "atr",
    "atr_mult_acc": 5,
    "atr_mult_acc_min": 0.0,
    "acc_width": 0.2,
    "acc_width_min": 0.0,
    "man_look": 10,
    "fvg_threshold": 0.1,
    "atr_len": 14,
    "atr_mult": 1.5,
    "rrr": 2.0,
    # Adaptive reward-to-risk: 3:1 when the previous completed 6h candle's
    # ADX(14) >= 40 (strong trend), else 2:1. Validated via walk-forward.
    "dynamic_rr": True,
    "rrr_trend": 3.0,
    "rrr_range": 2.0,
    "htf_hours": 6,
    "htf_adx_period": 14,
    "htf_adx_threshold": 40,
    "sessions": ["sydney", "tokyo", "london", "ny"],
    "sweep_filter": True,
    "sweep_len": 5,
    "sweep_max_bars": 300,
    "skip_months": [5],
    "skip_weeks": {4: [2, 4]},
    "manip_min_mode": "atr",
    "manip_min_val": 0.4,
    "adx_filter": True,
    "adx_period": 42,
    "adx_threshold": 35,
    "loss_streak_threshold": 4,
    "reduced_risk_pct": 0.25,
    "wins_to_recover": 2,
}

# --- Strategy registry -------------------------------------------------------
# Each entry is a fully self-contained, named strategy config carrying its own
# `interval` and its own adaptive-sizing params. The engine and worker stay
# strategy-agnostic: to add or change a strategy, edit only this dict (SOLID —
# Single-Responsibility per strategy, Open/Closed for the engine).
#
#   amd_15m  — the validated 15m production strategy (adaptive 2:1/3:1 RR).
#   trend_5m — the 5m trend-sniper: take the AMD setup on 5m ONLY while the 6h
#              ADX(14) >= 50 regime is trending, fixed 2:1, SL 1.5 ATR. Overlaid
#              on amd_15m it shares one wallet, one position per symbol, and its
#              OWN adaptive-sizing streak (see app/bot/worker.py).
STRATEGIES = {
    "amd_15m": {
        "interval": "15m",
        **STRATEGY_PARAMS,
    },
    "trend_5m": {
        "interval": "5m",
        "tf_minutes": 5,
        "acc_len": 20,
        "acc_mode": "atr",
        "atr_mult_acc": 5,
        "atr_mult_acc_min": 0.0,
        "acc_width": 0.2,
        "acc_width_min": 0.0,
        "man_look": 10,
        "fvg_threshold": 0.1,
        "atr_len": 14,
        "atr_mult": 1.5,
        "rrr": 2.0,
        # Trend-only: enter exclusively while the 6h ADX(14) >= htf_adx_threshold
        # regime is trending. Enforced in the worker (live check_signal has no
        # trend_only flag) and in the engine (trendOnly) for the backtest.
        "dynamic_rr": False,
        "trend_only": True,
        "htf_hours": 6,
        "htf_adx_period": 14,
        "htf_adx_threshold": 50,
        "sessions": ["sydney", "tokyo", "london", "ny"],
        "sweep_filter": True,
        "sweep_len": 5,
        "sweep_max_bars": 300,
        "skip_months": [5],
        "skip_weeks": {4: [2, 4]},
        "manip_min_mode": "atr",
        "manip_min_val": 1.5,
        "adx_filter": True,
        "adx_period": 42,
        "adx_threshold": 35,
        "loss_streak_threshold": 4,
        "reduced_risk_pct": 0.25,
        "wins_to_recover": 2,
    },
}

ACC_RANGE_MODE = {
    "BTCUSDT": "body",
    "ETHUSDT": "wick",
    "SOLUSDT": "wick",
}

LEVERAGE = 5

COMMISSION_PCT = 0.0004
SLIPPAGE_TICKS = 2

CANDLE_BUFFER_SIZE = 1000

LOT_SIZE = {"BTCUSDT": 0.001, "ETHUSDT": 0.01, "SOLUSDT": 0.1}
