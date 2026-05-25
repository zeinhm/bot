# AMD FVG Trading Bot — Build Plan

## What to build

A live trading bot for the AMD FVG 15m strategy with a web dashboard. Single Python service that:
1. Connects to Binance Futures (testnet first, live later)
2. Watches 15m candles for BTC, ETH, SOL
3. Executes trades automatically when the strategy signals
4. Serves a web dashboard to monitor everything

---

## Strategy Reference

The complete validated strategy is in `/choosen/amd-fvg-15m-v1/`. Read these files for full details:

- `strategy.py` — **The frozen strategy logic. Port this directly.** Contains `run_strategy()`, `resample_bars()`, `_compute_sweep_zones()`, and all AMD+FVG detection logic.
- `config.json` — All parameters in machine-readable format.
- `README.md` — Full strategy explanation (accumulation, manipulation, FVG, sweep filter, sessions, etc.)
- `backtest.py` — Reference for how trades are evaluated (SL/TP checking, commission, slippage).

### Key Parameters (do NOT change these)

```
Timeframe:       15m
accLen:          60
accMode:         ATR
atrMultAcc:      5
manLook:         10
fvgThreshold:    0.1
atrLen:          14
atrMult (SL):    1.5
RRR:             2.0
sweepLen:        5
sweepMaxBars:    300
Skip months:     May (month == 5)
Sessions:        All 4 (Sydney, Tokyo, London, NY) — see strategy.py for exact hours
```

### Strategy Logic Summary

1. Detect accumulation (ATR-based tight range over 60 bars)
2. Detect manipulation (wick break above/below accumulation range within 10 bars)
3. Detect FVG (fair value gap after manipulation)
4. Enter at FVG candle close (market order)
5. SL = ATR(14) * 1.5, widened to manipulation extreme if needed
6. TP = SL distance * 2.0 RRR
7. Sweep filter: block shorts if candle close is inside bullish sweep zone, block longs if inside bearish sweep zone (entry bar only)
8. Skip May entirely
9. One trade at a time, only during active sessions

---

## Tech Stack (decided, do not change)

| Layer | Tech |
|-------|------|
| Bot engine | Python (port strategy.py logic) |
| API + Dashboard | FastAPI + Jinja2 templates |
| Styling | Tailwind CSS via CDN |
| Database | PostgreSQL (use SQLAlchemy + asyncpg) |
| Real-time updates | FastAPI WebSocket |
| Exchange | python-binance (Binance Futures API) |
| Deploy target | Railway (single service) |

---

## Project Structure

```
bot/
├── main.py              ← FastAPI app entry point, mounts routes, starts bot on startup
├── bot.py               ← Bot loop: connects to Binance WebSocket, processes 15m candles, executes trades
├── exchange.py          ← Binance API wrapper: connect, place orders, get balance, get position, cancel orders
├── strategy.py          ← Strategy logic ported from /choosen/amd-fvg-15m-v1/strategy.py
├── database.py          ← SQLAlchemy models + async DB queries
├── config.py            ← Strategy params, env vars (API keys, DB URL), settings
├── routes/
│   ├── dashboard.py     ← Dashboard page route
│   ├── trades.py        ← Trades history page route
│   ├── settings.py      ← Settings page route (API keys, risk %, bot on/off)
│   └── ws.py            ← WebSocket endpoint for live updates
├── templates/
│   ├── base.html        ← Base layout (nav, Tailwind CDN, WebSocket JS)
│   ├── dashboard.html   ← Main dashboard page
│   ├── trades.html      ← Trade history table with filters
│   └── settings.html    ← Bot configuration page
├── static/
│   └── (minimal, Tailwind via CDN)
├── requirements.txt
├── Procfile             ← Railway: `web: uvicorn main:app --host 0.0.0.0 --port $PORT`
├── .env.example         ← Template for env vars
└── BUILD_PLAN.md        ← This file
```

---

## Database Schema

### Table: trades

| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL PK | |
| symbol | VARCHAR | BTCUSDT, ETHUSDT, SOLUSDT |
| direction | VARCHAR | long / short |
| entry_time | TIMESTAMP | When position opened |
| exit_time | TIMESTAMP | When position closed (null if open) |
| entry_price | FLOAT | Entry price |
| exit_price | FLOAT | Exit price (null if open) |
| sl_price | FLOAT | Stop loss price |
| tp_price | FLOAT | Take profit price |
| quantity | FLOAT | Position size |
| result | VARCHAR | win / loss / open |
| r_value | FLOAT | R multiple achieved |
| pnl_usdt | FLOAT | Actual P&L in USDT |
| commission | FLOAT | Fees paid |
| created_at | TIMESTAMP | |

### Table: bot_state

| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL PK | |
| key | VARCHAR UNIQUE | Setting name |
| value | TEXT | Setting value (JSON) |

Store in bot_state: `bot_enabled` (true/false), `risk_mode` (static/dynamic), `risk_value` (dollar amount or percentage), API keys (encrypted or via env vars).

### Table: candle_buffer

| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL PK | |
| symbol | VARCHAR | |
| timestamp | TIMESTAMP | Candle open time |
| open | FLOAT | |
| high | FLOAT | |
| low | FLOAT | |
| close | FLOAT | |
| volume | FLOAT | |

Store recent candles so the strategy has history to work with on bot restart. Need at least 400 bars (accLen=60 + sweepMaxBars=300 + buffer) per symbol.

---

## Bot Logic (bot.py)

### Startup

1. Load historical 15m candles from Binance REST API (at least 400 bars per symbol)
2. Store in candle_buffer table
3. Check if there's an existing open position on Binance (crash recovery)
4. If open position exists, sync it to the trades table
5. Connect to Binance WebSocket for real-time 15m klines

### On Each 15m Candle Close

```
for each symbol (BTC, ETH, SOL):
    1. Append new candle to buffer
    2. If month == May → skip
    3. If already in a trade → skip (one trade at a time)
    4. Run strategy logic on candle buffer
    5. If strategy signals entry:
       a. Calculate position size based on risk mode
       b. Place market order
       c. Place SL and TP orders (OCO or two separate orders)
       d. Save trade to database
       e. Push update via WebSocket
```

### Position Monitoring

- Listen for order fill events via Binance user data WebSocket
- When SL or TP fills: update trade in DB, cancel the other order, push WebSocket update
- Alternative: poll position every 30 seconds as fallback

### Crash Recovery

On startup:
1. Check Binance for any open positions
2. Check trades table for any `result = 'open'`
3. Reconcile: if position exists on Binance but not in DB, create it. If in DB but not on Binance, mark it closed.
4. Ensure SL/TP orders are in place for any open position

---

## Exchange Layer (exchange.py)

Use `python-binance` library. Wrap these operations:

```python
class BinanceExchange:
    def __init__(self, api_key, api_secret, testnet=True):
        # If testnet=True, use testnet URL
    
    async def get_balance(self) -> float:
        # Return USDT balance
    
    async def get_position(self, symbol) -> dict | None:
        # Return open position or None
    
    async def place_market_order(self, symbol, side, quantity) -> dict:
        # Market buy/sell
    
    async def place_stop_loss(self, symbol, side, quantity, stop_price) -> dict:
        # Stop market order
    
    async def place_take_profit(self, symbol, side, quantity, price) -> dict:
        # Take profit limit order
    
    async def cancel_order(self, symbol, order_id) -> None:
        # Cancel specific order
    
    async def get_klines(self, symbol, interval, limit) -> list:
        # Get historical candles
    
    async def start_kline_socket(self, symbols, interval, callback):
        # WebSocket for real-time candles
    
    async def start_user_socket(self, callback):
        # WebSocket for order fills
```

### Testnet vs Live

```python
TESTNET_URL = "https://testnet.binancefuture.com"
LIVE_URL = "https://fapi.binance.com"
```

Only difference is the base URL and API keys. Controlled by `BINANCE_TESTNET=true` env var.

---

## Dashboard Pages

### 1. Dashboard (/)

Show:
- **Bot status**: Running / Stopped (green/red indicator)
- **Account balance**: Current USDT balance from Binance
- **Open position**: If in a trade, show symbol, direction, entry price, current P&L, SL, TP
- **Today's P&L**: Sum of today's closed trades
- **Recent trades**: Last 10 trades in a compact table

Auto-refresh via WebSocket — when a trade opens/closes, the page updates without reload.

### 2. Trades (/trades)

Full trade history table:
- Date, symbol, direction, entry, exit, SL, TP, result, R value, P&L
- Filter by: symbol, direction, result (win/loss), date range
- Show totals at bottom: total trades, win rate, total R, total P&L

### 3. Settings (/settings)

- **Bot toggle**: Enable / Disable trading (bot still runs but won't place orders)
- **Risk mode**: Static (fixed USDT amount) or Dynamic (% of balance)
- **Risk value**: Dollar amount or percentage
- **Testnet mode**: Toggle (requires restart)
- **API key status**: Show if keys are configured (from env vars, not editable in UI)
- **Active symbols**: Check/uncheck BTC, ETH, SOL

Settings stored in bot_state table. Changes take effect on next candle.

---

## Configuration (config.py)

```python
# From environment variables
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
DATABASE_URL = os.getenv("DATABASE_URL")

# Strategy params (fixed, from validated config)
STRATEGY_PARAMS = {
    "tf_minutes": 15,
    "acc_len": 60,
    "acc_mode": "atr",
    "atr_mult_acc": 5,
    "man_look": 10,
    "fvg_threshold": 0.1,
    "atr_len": 14,
    "atr_mult": 1.5,
    "rrr": 2.0,
    "sessions": ["sydney", "tokyo", "london", "ny"],
    "sweep_filter": True,
    "sweep_len": 5,
    "sweep_max_bars": 300,
    "skip_months": [5],  # May
}
```

---

## .env.example

```
BINANCE_API_KEY=your_key_here
BINANCE_API_SECRET=your_secret_here
BINANCE_TESTNET=true
DATABASE_URL=postgresql://user:pass@host:5432/tradingbot
```

---

## requirements.txt

```
fastapi
uvicorn[standard]
jinja2
sqlalchemy[asyncio]
asyncpg
python-binance
python-dotenv
```

---

## Railway Deployment

- `Procfile`: `web: uvicorn main:app --host 0.0.0.0 --port $PORT`
- Add PostgreSQL plugin in Railway dashboard
- Set env vars in Railway dashboard (API keys, DATABASE_URL auto-set by Railway)
- Single service runs both the bot and dashboard

---

## Build Order

Build and test each step before moving to the next:

### Step 1: Project setup
- Create all files and directories
- Set up requirements.txt and .env.example
- Initialize database models

### Step 2: Exchange layer (exchange.py)
- Implement BinanceExchange class
- Test connection to testnet
- Test getting balance, klines, placing test orders

### Step 3: Strategy port (strategy.py)
- Copy and adapt strategy logic from /choosen/amd-fvg-15m-v1/strategy.py
- Must work with a rolling candle buffer (not full CSV load)
- All logic must produce same signals as the backtest version

### Step 4: Bot engine (bot.py)
- Implement candle loading, WebSocket connection
- Implement the main loop (on candle close → run strategy → place orders)
- Implement position monitoring and crash recovery
- Test with testnet: verify it detects setups and places correct orders

### Step 5: Database (database.py)
- Create tables (trades, bot_state, candle_buffer)
- CRUD operations for trades and settings
- Test locally with PostgreSQL (or SQLite for dev)

### Step 6: Dashboard (main.py + routes/ + templates/)
- Build FastAPI app with Jinja2
- Dashboard page with live data
- Trades page with history table
- Settings page with bot controls
- WebSocket for real-time updates

### Step 7: Integration + testing
- Run full bot with dashboard on testnet
- Verify trades appear in dashboard
- Verify settings changes take effect
- Test crash recovery (kill and restart)

### Step 8: Deploy prep
- Procfile, Railway config
- Environment variable documentation
- Final testing on Railway with testnet

---

## Important Rules

1. **Do NOT modify strategy parameters.** They are validated. Copy them exactly.
2. **Testnet first.** Always default to testnet. Live mode requires explicit env var change.
3. **One trade at a time.** The strategy rule — no stacking positions.
4. **Skip May.** If current month is May, do not open any trades.
5. **Crash safe.** Bot must recover gracefully — check Binance for open positions on startup.
6. **No charts for now.** Dashboard is tables and text only. Charts can be added later.
7. **Strategy logic source of truth is `/choosen/amd-fvg-15m-v1/strategy.py`.** Read it carefully before porting.
