import os

from dotenv import load_dotenv

load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
DATABASE_URL = os.getenv("DATABASE_URL", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", "")

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
    "sessions": ["sydney", "tokyo", "london", "ny"],
    "sweep_filter": True,
    "sweep_len": 5,
    "sweep_max_bars": 300,
    "skip_months": [5],
}

COMMISSION_PCT = 0.0004
SLIPPAGE_TICKS = 2

CANDLE_BUFFER_SIZE = 400
