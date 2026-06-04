# Architecture

## Overview

Zenith is a FastAPI application that runs an automated AMD FVG trading strategy on Binance Futures. It supports multiple users, each with independent paper (demo) and live trading bots. A single process runs the web dashboard, REST API, WebSocket server, and all bot workers concurrently using asyncio.

Deployed on Railway with PostgreSQL. Domain: zenithbot.org.

---

## Process Model

One uvicorn process handles everything:

```
uvicorn main:app
  ├── FastAPI (HTTP routes + static files)
  ├── WebSocket server (/ws/{user_id})
  ├── BotManager
  │   ├── BotWorker(user=1, mode=live)   → BinanceExchange
  │   ├── BotWorker(user=1, mode=paper)  → PaperExchange
  │   ├── BotWorker(user=2, mode=paper)  → PaperExchange
  │   └── ...
  ├── SharedMarketData (single unauthenticated Binance connection)
  └── Background WS push tasks (price stream, heartbeat, position poll, balance poll, orderbook, agg trades)
```

Everything starts in the `lifespan` context manager in `main.py`:
1. `init_db()` — ensure schema columns + `create_all` for new tables
2. `SharedMarketData.connect()` — one public Binance WebSocket for all paper bots
3. `BotManager()` — created and stored on `app.state`
4. Auto-start bots for approved users (paper if `paper_bot_started=True`, live if API keys configured)
5. `start_ws_tasks()` — 6 background asyncio tasks for real-time data push

On shutdown: cancel WS tasks, stop all bot workers, close SharedMarketData.

---

## Module Layout

```
bot/
├── main.py                    # FastAPI app, lifespan, router mounting
├── config.py                  # All env vars + strategy params + trading constants
├── strategy.py                # check_signal() — live signal detection from candle buffer
├── amd_engine.py              # run() + compute_stats() — full backtest over historical data
├── exchange.py                # BinanceExchange — authenticated Binance Futures client
├── paper_exchange.py          # PaperExchange — simulated exchange using SharedMarketData + DB
├── telegram_alert.py          # Telegram notifications (trade alerts → channel, status → owner)
├── import_candles.py          # CLI tool: import 1m CSVs into historical_candles, resample to 15m
├── seed_trades.py             # CLI tool: run backtest and seed backtest_results table
│
├── app/
│   ├── auth/
│   │   └── service.py         # Google OAuth gate: require_auth, encrypt/decrypt, trading mode
│   ├── bot/
│   │   ├── __init__.py        # build_paper_config, build_user_config, make_broadcast_fn, get_bot_for_user
│   │   ├── manager.py         # BotManager — start/stop workers, keyed by (user_id, mode)
│   │   ├── worker.py          # BotWorker — kline stream, signal detection, trade execution, crash recovery
│   │   ├── shared_market.py   # SharedMarketData — single Binance public API connection for paper bots
│   │   └── websocket.py       # ConnectionManager + 6 background push tasks (price, heartbeat, positions, balance, orderbook, agg trades)
│   ├── core/
│   │   └── context.py         # get_global_context() — shared template context (bot status, balance, mode)
│   ├── db/
│   │   ├── engine.py          # init_db, _ensure_schema, get_session
│   │   ├── models.py          # SQLAlchemy models (12 tables)
│   │   └── queries/           # trades, users, state, candles, events, paper, backtest
│   └── email.py               # Resend email for user approve/reject
│
├── routes/
│   ├── auth.py                # Google OAuth login/callback, setup page, pending/rejected pages
│   ├── dashboard.py           # Main dashboard with stats, equity curve, recent trades
│   ├── trades.py              # Trade history with filtering
│   ├── position.py            # Live position view with chart
│   ├── analytics.py           # Performance analytics (monthly breakdown, per-symbol stats)
│   ├── settings.py            # User settings (API keys, risk mode, bot enable/disable)
│   ├── bot_control.py         # Start/stop live bot, start paper bot, switch mode
│   ├── backtester.py          # On-the-fly backtester + seeded trade log
│   ├── alerts.py              # Bot event log viewer
│   ├── track_record.py        # Public track record page
│   ├── admin.py               # User management (approve, reject, disable, toggle admin)
│   └── ws.py                  # Legacy WebSocket endpoint (/ws, no user scoping)
│
├── templates/                 # Jinja2 HTML templates (base.html + 13 page templates)
├── static/
│   ├── css/app.css            # Full dashboard CSS (dark terminal aesthetic)
│   └── js/websocket.js        # BotWebSocket client (auto-reconnect, ping/pong)
│
├── auth.py, bot.py,           # Root shim files — re-export from app/ for backward compat
│   database.py,               # Used by strategy.py, exchange.py, and other root-level modules
│   template_context.py        # that import from the old flat structure
│
├── alembic/                   # Alembic migrations (5 versions, not run on prod — _ensure_schema handles it)
└── docs/                      # Architecture docs, order safety docs
```

---

## Database (PostgreSQL)

12 tables, all defined in `app/db/models.py`:

| Table | Purpose | Scoped by |
|-------|---------|-----------|
| `users` | Google OAuth users with approval/admin flags | — |
| `user_configs` | Encrypted Binance API keys per user | `user_id` |
| `trades` | Live + paper trade records | `user_id`, `is_paper` |
| `bot_state` | Key-value settings (risk mode, bot enabled, etc.) | `user_id`, `is_paper` |
| `bot_events` | Log entries (trade events, system events) | `user_id`, `is_paper` |
| `historical_candles` | Downloaded 1m/15m candle data for backtester | `symbol`, `interval` |
| `candle_buffer` | Recent candles from live kline stream | `symbol` |
| `backtest_results` | Seeded backtest trade results for display | — |
| `paper_accounts` | Virtual balance per user ($10,000 default) | `user_id` |
| `paper_orders` | Simulated SL/TP orders for paper trading | `user_id` |
| `rejection_log` | Admin rejection history (supports re-register cycles) | `user_id` |

Schema management: `_ensure_schema()` in `engine.py` adds missing columns idempotently via `ALTER TABLE ADD COLUMN IF NOT EXISTS`, then `Base.metadata.create_all` creates any missing tables. Alembic migrations exist but are not run on Railway due to timeout issues.

---

## Bot Architecture

### BotManager (`app/bot/manager.py`)

Registry of running bot workers, keyed by `(user_id, mode)` where mode is `"live"` or `"paper"`. Each user can have up to 2 concurrent workers.

- `start_bot()` — creates BotWorker, wraps `worker.start()` in an asyncio Task
- `stop_bot()` — calls `worker.stop()`, cancels the Task, removes from registry
- `get_worker()` — lookup by (user_id, mode)

### BotWorker (`app/bot/worker.py`)

The core trading loop. Each worker runs 3 async tasks:

1. **Kline stream** (`_run_kline_stream`) — WebSocket subscription to 15m candles for all symbols. On each candle close, calls `strategy.check_signal()` with the candle buffer. If signal found and no active trade, executes entry.

2. **User stream** (`_run_user_stream`) — WebSocket subscription to account events. Detects SL/TP fill events (`ORDER_TRADE_UPDATE` with `FILLED` status) and records the trade exit.

3. **Position poll** (`_run_position_poll`) — Every 30s fallback. Detects positions that closed without a WebSocket event. Also runs SL/TP health checks on live trades (10s interval when orders are missing, Telegram alert after 60s).

On startup, runs `_crash_recovery()` to handle trades left open from a previous crash.

### BinanceExchange (`exchange.py`)

Authenticated Binance Futures client. Two connection modes:

- **Testnet mode**: trading client → testnet API, market data client → live API (separate connections)
- **Live mode**: single client for both trading and market data

Key operations: `place_market_order`, `place_stop_loss` (STOP_MARKET), `place_take_profit` (TAKE_PROFIT_MARKET), `get_order`, `get_trades_for_order`, `cancel_all_orders`.

### PaperExchange (`paper_exchange.py`)

Simulated exchange with the same interface as BinanceExchange. Uses `SharedMarketData` for price feeds (no API key needed). Positions are tracked in memory (`_positions` dict), SL/TP orders stored in `paper_orders` table. Simulates fills by polling `SharedMarketData` prices every 1s against pending orders.

### SharedMarketData (`app/bot/shared_market.py`)

Single unauthenticated Binance connection shared by all paper bots. Provides `market_client` and `market_bsm` (BinanceSocketManager) for kline streams and market data. Also stores latest prices updated by the price stream in `websocket.py`.

---

## Authentication & User Management

### Flow

```
User visits site
  → Not logged in → Redirect to /login
  → Google OAuth → /auth/callback
    → User created/updated in DB
    → is_rejected? → /rejected page
    → !is_approved? → /pending page (waiting for admin)
    → is_approved → Check API keys
      → No keys → /setup page (enter Binance API keys or skip)
      → Has keys → / (dashboard)
```

### Auth Gate (`app/auth/service.py`)

- `require_auth()` — called by every protected route. Raises `AuthRequired`, `PendingApproval`, or `AccountRejected` (caught by FastAPI exception handlers → redirect).
- `get_trading_mode()` — reads `trading_mode` from session, defaults to `"live"`.
- API keys encrypted with Fernet (`ENCRYPTION_KEY` env var).
- Legacy mode: if `GOOGLE_CLIENT_ID` not set, returns a hardcoded user (single-user mode).

### Admin Panel (`routes/admin.py`)

- Approve/reject/disable users
- Toggle admin status
- Allow re-registration after rejection
- Email notifications on approve/reject via Resend

---

## Real-Time Data (WebSocket)

### Server → Client Push

6 background tasks in `app/bot/websocket.py`, started in lifespan:

| Task | Interval | Data |
|------|----------|------|
| `_price_stream` | Real-time (Binance WS) | Symbol prices + 24h change for BTC/ETH/SOL |
| `_heartbeat_loop` | 15s | Bot status, candle age, session, risk settings per user per mode |
| `_position_poll` | 2s | Open positions with unrealized PnL, SL/TP levels |
| `_balance_poll` | 30s | USDT balance per user per mode |
| `_orderbook_stream` | Real-time (throttled 0.5s) | Top 20 bids/asks per symbol |
| `_agg_trade_stream` | Real-time | Individual trades (price, qty, side) |

### Client

`static/js/websocket.js` — `BotWebSocket` class. Auto-reconnects with exponential backoff (1s → 30s max). Sends ping every 30s to keep connection alive.

`base.html` registers handlers for `price`, `balance`, `heartbeat`, `bot_status`, `position`, `adaptive_sizing`, `trade_opened`, `trade_closed` events. Updates topbar price, balance, bot status pill, and triggers page reload on trade open/close.

### Connection Scoping

`ConnectionManager` in `websocket.py` maps `user_id → set[WebSocket]`. `send_to_user()` only sends to that user's connections. `broadcast_all()` sends to everyone (prices, orderbook). Price stream also updates `SharedMarketData` so paper bots get latest prices.

---

## Paper Trading

### On-Demand Start

Paper bots don't auto-start for new users. Flow:
1. User clicks "Switch to Demo" in profile dropdown
2. If `paper_bot_started=False` → modal popup: "Start Paper Trading?"
3. User clicks Start → `POST /bot/start-paper` → starts PaperExchange worker, sets flag in DB
4. On subsequent switches or app restarts → auto-starts if flag is True

### How Paper Trades Execute

PaperExchange simulates the full trade lifecycle:
- Entry: uses latest price from SharedMarketData, tracks position in memory
- SL/TP: stored as PaperOrder rows in DB with status NEW
- Fill detection: `start_user_socket()` polls every 1s, checks prices against pending orders
- On fill: updates paper balance in `paper_accounts`, emits fake `ORDER_TRADE_UPDATE` event
- Worker processes the event identically to live (same `_on_user_event` handler)

---

## Strategy

Two implementations of the same AMD FVG strategy:

| Module | Purpose | Input |
|--------|---------|-------|
| `strategy.py` → `check_signal()` | Live trading — checks only the last bar | Rolling candle buffer (~1000 bars) |
| `amd_engine.py` → `run()` | Backtesting — scans all bars | Full historical dataset |

Both produce identical signals for the same data. The live version returns a signal only if the last bar triggers entry. The backtest version returns all setups.

Strategy parameters are defined in `config.py` → `STRATEGY_PARAMS` and `ACC_RANGE_MODE`.

---

## Notifications

| Channel | When | Module |
|---------|------|--------|
| Telegram (channel) | Trade entry/exit | `telegram_alert.py` → `send_alert()` |
| Telegram (private) | SL/TP placement failure, bot crash | `telegram_alert.py` → `send_private()` |
| Email (Resend) | User approved/rejected by admin | `app/email.py` |

---

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `GOOGLE_CLIENT_ID` | No | Google OAuth (multi-user mode). Omit for single-user |
| `GOOGLE_CLIENT_SECRET` | No | Google OAuth |
| `SESSION_SECRET` | Yes | Starlette session encryption |
| `ENCRYPTION_KEY` | Yes | Fernet key for encrypting stored API keys |
| `BINANCE_API_KEY` | No | Fallback for single-user mode (no OAuth) |
| `BINANCE_API_SECRET` | No | Fallback for single-user mode |
| `BINANCE_TESTNET` | No | Default `true`. Whether fallback keys use testnet |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot for alerts |
| `TELEGRAM_CHAT_ID` | No | Channel for trade alerts |
| `TELEGRAM_OWNER_ID` | No | Private chat for status/error alerts |
| `RESEND_API_KEY` | No | Email notifications via Resend |
| `EMAIL_FROM` | No | Sender address. Default: `ZENITH BOT <noreply@zenithbot.org>` |

---

## Deployment (Railway)

- **Web service**: Nixpacks auto-detect Python, runs Procfile: `alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port $PORT` (Alembic doesn't actually execute due to timeout — `_ensure_schema` handles schema changes instead)
- **PostgreSQL**: Railway managed Postgres, connected via `DATABASE_URL`
- Auto-deploys on push to `main` branch
- Domain: `zenithbot.org` (CNAME to Railway)
- DNS: Namecheap (DKIM + SPF for Resend email)
