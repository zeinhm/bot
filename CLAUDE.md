# ZENITH Trading Bot — Platform Codebase

Python FastAPI trading bot for the AMD FVG strategy on Binance Futures, with a full web dashboard, multi-user Google SSO, paper trading, admin panel, and real-time WebSocket data streaming. Deployed on Railway at zenithbot.org.

## Tech Stack

- **Backend**: FastAPI + Uvicorn, Python 3.x
- **Database**: PostgreSQL (asyncpg + SQLAlchemy async ORM, Alembic migrations)
- **Frontend**: Jinja2 templates + Tailwind CSS (CDN) + HTMX (SPA-like navigation)
- **Charts**: LightweightCharts v4.2.0
- **Exchange**: python-binance (Binance Futures USDT-M)
- **Auth**: Google OAuth 2.0 (authlib), Fernet encryption for API keys
- **Alerts**: Telegram Bot API (aiohttp)
- **Email**: Resend
- **Deployment**: Railway (nixpacks.toml + Procfile)

## How to Run

```bash
# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env  # then fill in values

# Run migrations + start server
alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port 8000
```

## Directory Structure

```
bot/
├── main.py                    # App entry point, route mounting, bot auto-start
├── config.py                  # All env vars, strategy params, asset configs
├── strategy.py                # Live signal detector (AMD FVG v1, candle buffer → signal)
├── amd_engine.py              # Backtest engine (bar-by-bar simulation, stats, equity sim)
├── exchange.py                # Binance Futures API wrapper (live trading)
├── paper_exchange.py          # Paper trading exchange (in-memory + DB, polls prices)
├── telegram_alert.py          # Telegram trade entry/exit alerts
├── import_candles.py          # CLI: import 1m CSVs → DB, resample to higher TFs
├── seed_trades.py             # CLI: run strategy on historical data → backtest_results
├── database.py                # Shim → app/db/
├── bot.py                     # Shim → app/bot/
├── auth.py                    # Shim → app/auth/
├── template_context.py        # Shim → app/core/context
│
├── app/
│   ├── auth/service.py        # Fernet encrypt/decrypt, require_auth, session helpers
│   ├── auth/twofa.py          # TOTP 2FA: enroll/verify, require_2fa step-up gate, backup codes, rate limit
│   ├── core/context.py        # get_global_context() for template rendering
│   ├── core/template_filters.py # Shared Jinja filters (num: comma+decimals); register_filters()
│   ├── email.py               # send_approval_email(), send_rejection_email() via Resend
│   ├── db/
│   │   ├── engine.py          # init_db(), get_session(), _ensure_schema() auto-migration
│   │   ├── models.py          # 14 SQLAlchemy models (User, Trade, BotState, BacktestRun, etc.)
│   │   └── queries/
│   │       ├── trades.py      # Trade CRUD, filtering, today PnL/count
│   │       ├── users.py       # User CRUD, Google upsert, API key management
│   │       ├── candles.py     # Candle buffer + historical candle queries
│   │       ├── admin.py       # Platform-wide stats, user summaries
│   │       ├── events.py      # Bot event logging and retrieval
│   │       ├── state.py       # Key-value state store (per user+mode)
│   │       ├── paper.py       # Paper account + order management
│   │       └── backtest.py    # Backtest result storage + run/setup cache (get/save_backtest_run, get_run_setups_*)
│   ├── middleware/
│   │   └── csrf.py            # CSRF protection middleware (session-based token)
│   └── bot/
│       ├── __init__.py        # Bot accessors, broadcast factory, config builders
│       ├── worker.py          # BaseWorker: shared loop (candle → signal → execute → monitor) + mode hooks + make_worker()
│       ├── worker_live.py     # LiveWorker: Binance hooks (real orders, conditional SL/TP, alerts, self-heal, force-close)
│       ├── worker_paper.py    # PaperWorker: simulated hooks (DB-backed PaperExchange, paper orders)
│       ├── manager.py         # Multi-user worker lifecycle (start/stop/status); builds Live/Paper via make_worker()
│       ├── shared_market.py   # Shared Binance connection for candle data (no API key); get_trend_regime() → 6h-ADX regime for adaptive RR
│       └── websocket.py       # WS connection manager + 7 background push tasks (authenticated)
│
├── routes/
│   ├── auth.py                # Google OAuth flow, /setup, /login, /logout
│   ├── dashboard.py           # GET /dashboard, GET /api/live-candles
│   ├── position.py            # GET /position (live positions from exchange)
│   ├── trades.py              # GET /trades (filtered trade history)
│   ├── settings.py            # GET/POST /settings, API key CRUD, emergency close
│   ├── bot_control.py         # Bot start/stop/status, mode switching
│   ├── twofa.py               # 2FA endpoints: enroll/verify/disable/challenge + backup-codes/regenerate
│   ├── analytics.py           # GET /analytics (session/day/monthly breakdowns)
│   ├── backtester.py          # GET /backtester, /api/backtest (cached {stats,total}), /api/backtest/setups (paged), /api/backtest/combined, /api/candles
│   ├── alerts.py              # GET /alerts (event log)
│   ├── track_record.py        # GET /track-record (public, no auth)
│   └── admin/
│       ├── __init__.py        # Admin router, require_admin middleware
│       ├── dashboard.py       # GET /admin (platform overview)
│       ├── users.py           # User approve/reject/disable/toggle-admin
│       ├── user_detail.py     # GET /admin/user/{id}, POST reconcile
│       ├── bots.py            # GET /admin/bots (all bot instances)
│       ├── analytics.py       # GET /admin/analytics (platform-wide)
│       └── logs.py            # GET /admin/logs (filterable event log)
│
├── templates/                 # 22 Jinja2 templates
│   ├── base.html              # Master layout: sidebar, topbar, bottom nav, WS handlers
│   ├── _trade_table.html      # Shared trade table partial (Trade History + dashboard Recent Trades)
│   ├── dashboard.html         # Stats, equity curve, recent trades
│   ├── position.html          # Chart, order book, positions, market trades (~800 lines JS)
│   ├── trades.html            # Filtered trade history table
│   ├── settings.html          # API keys (read-only/form), emergency close
│   ├── analytics.html         # Drawdown, monthly PnL, session/day/hold analysis
│   ├── backtester.html        # Combined equity, per-asset chart with setup visualization
│   ├── alerts.html            # Telegram link, bot events (commented out)
│   ├── track_record.html      # Public page (standalone, no base.html)
│   ├── login.html             # Google sign-in (standalone)
│   ├── setup.html             # First-run API key form (standalone)
│   ├── pending.html           # Awaiting approval (standalone)
│   ├── rejected.html          # Access declined (standalone)
│   ├── 404.html               # Not found (standalone)
│   ├── admin_dashboard.html   # Platform overview, user table
│   ├── admin_users.html       # Approve/reject/disable with tab UI
│   ├── admin_user_detail.html # Single user stats, equity, trades, events
│   ├── admin_bots.html        # All bot instances with status/uptime
│   ├── admin_analytics.html   # Platform equity, monthly, leaderboard
│   └── admin_logs.html        # Filterable event log
│
├── static/
│   ├── css/app.css            # Full design system (dark theme, components, responsive)
│   ├── js/websocket.js        # BotWebSocket class with auto-reconnect
│   ├── manifest.webmanifest   # PWA manifest (installable app, icons, theme)
│   ├── sw.js                  # Service worker (network-first pages, cache-first static, web-push handlers)
│   └── icons/                 # PWA app icons (192/512/maskable/apple-touch) + favicon.svg (green "Z", linked on every page)
│
├── landing-page/
│   ├── landing-page.html      # Marketing page (standalone HTML, no Jinja)
│   ├── landing-page.css       # Landing page styles
│   ├── landing-page.js        # SVG charts, candlestick diagrams, animations
│   ├── amd-scroll.js          # Pinned scroll engine for #how (canvas AMD trade, scroll-driven)
│   └── og-image.png           # Social preview image
│
├── alembic/
│   ├── env.py                 # Migration environment config
│   └── versions/              # 9 migrations (schema → paper trading → rejection → funding fee → backtest cache → 2FA → adaptive RR target_rr)
│
├── docs/
│   ├── architecture.md        # System design overview
│   ├── strategy.md            # Strategy rules
│   ├── backtester.md          # Backtester docs
│   ├── order-safety.md        # Order safety mechanisms
│   └── codebase-map.md        # Detailed function-level reference (see this file)
│
├── .env.example               # Environment variable template
├── requirements.txt           # Python dependencies (16 packages)
├── Procfile                   # Railway deployment command
├── nixpacks.toml              # Railway build config
├── alembic.ini                # Alembic configuration
└── BUILD_PLAN.md              # Original build specification
```

## Feature → File Lookup

| To change...                        | Edit these files                                              |
|-------------------------------------|---------------------------------------------------------------|
| **Strategy logic / signal detection** | `strategy.py` (live), `amd_engine.py` (backtest)             |
| **Strategy parameters / defaults**  | `config.py` → `STRATEGY_PARAMS` (incl. adaptive-RR: `dynamic_rr`/`rrr_trend`/`htf_adx_threshold`), `ACC_RANGE_MODE` |
| **Adaptive reward-to-risk (2:1/3:1)** | `amd_engine.py` `_htf_trend_mask()` + `run()` (backtest); `app/bot/shared_market.py` `get_trend_regime()` + `app/bot/worker.py` `_process_candle()` (live); `target_rr` on `trades`/`backtest_results` |
| **Shared trade loop / signal→execute** | `app/bot/worker.py` (`BaseWorker`) → `_process_candle()`, `_execute_trade()`, `_place_sl_tp()` |
| **Live-only execution behavior**    | `app/bot/worker_live.py` (`LiveWorker`) → real orders, conditional SL/TP, alerts, `_self_heal_trade()`, force-close |
| **Paper-only execution behavior**   | `app/bot/worker_paper.py` (`PaperWorker`) → `_resolve_exit()`, `_count_active_sltp()`; `paper_exchange.py` (DB-backed) |
| **Add/change a mode difference**    | Add/override a hook in `BaseWorker` (default), then `LiveWorker` / `PaperWorker` — do NOT add `if self.is_paper` |
| **Crash recovery**                  | `app/bot/worker.py` → `_crash_recovery()`, `_recover_one_trade()` (delegates to mode hooks) |
| **Adaptive position sizing**        | `app/bot/worker.py` → `_get_effective_risk()`, `_on_trade_result()` |
| **Add a new page**                  | 1. `routes/newpage.py` 2. `templates/newpage.html` 3. `main.py` (mount router) 4. `templates/base.html` (add nav link) |
| **Add an admin page**               | 1. `routes/admin/newpage.py` 2. `templates/admin_newpage.html` 3. `routes/admin/__init__.py` (include router) 4. `templates/base.html` (add admin nav link) |
| **Add a DB model / table**          | `app/db/models.py` + new migration in `alembic/versions/`    |
| **Add a DB query**                  | `app/db/queries/` (existing or new file) + re-export in `app/db/queries/__init__.py` |
| **Sidebar / topbar / bottom nav**   | `templates/base.html`                                        |
| **Mobile layout**                   | `static/css/app.css` (breakpoints at 768px, 767px, 479px)    |
| **WebSocket push data**             | `app/bot/websocket.py` (server) + `templates/base.html` or page JS (client) |
| **Real-time price/position/balance**| `app/bot/websocket.py` → `_price_stream`, `_position_poll`, `_balance_poll` |
| **Telegram alerts**                 | `telegram_alert.py`                                          |
| **Email notifications**             | `app/email.py`                                               |
| **User auth / session**             | `app/auth/service.py`, `routes/auth.py`                      |
| **API key encryption**              | `app/auth/service.py` → `encrypt()`, `decrypt()`             |
| **API key validation**              | `exchange.py` → `validate_api_key()`; wired in `routes/settings.py` + `routes/auth.py` (setup) |
| **2FA (TOTP) / step-up gate**       | `app/auth/twofa.py` (`require_2fa`, backup codes), `routes/twofa.py`, Settings → Security tab (`templates/settings.html`), challenge modal + `guardedFetch` in `templates/base.html`; admin reset in `routes/admin/user_detail.py` |
| **User approval flow**              | `routes/admin/users.py`, `templates/admin_users.html`        |
| **Bot start/stop lifecycle**        | `app/bot/manager.py`, `routes/bot_control.py`                |
| **Landing page**                    | `landing-page/` (html/css/js + `amd-scroll.js` for the #how scroll section, no Jinja) |
| **PWA (install/offline/icons)**     | `static/manifest.webmanifest`, `static/sw.js`, `static/icons/`, `main.py` (`/sw.js` route), `templates/base.html` (head links + SW registration) |
| **Design tokens / colors**          | `static/css/app.css` → CSS custom properties at top           |
| **Historical data import**          | `import_candles.py` (CLI tool, raw psycopg2)                 |
| **Seed backtest results**           | `seed_trades.py` (CLI tool)                                  |
| **CSRF / security middleware**      | `app/middleware/csrf.py`, `main.py` (middleware order)        |
| **Session config / cookies**        | `main.py` → SessionMiddleware config                         |
| **Deployment config**               | `Procfile`, `nixpacks.toml`, `.env`                          |

## Key Patterns

**Routes**: Each route file creates a `router = APIRouter()`, defines endpoints, and is included in `main.py`. Routes call `require_auth(request)` first, get `trading_mode`, build context with `get_global_context()`, then render a template.

**Templates**: All authenticated pages extend `base.html` and override `{% block content %}`. They receive `user`, `page` (for active nav), and global context vars. HTMX `hx-get` on nav links enables SPA-like navigation.

**Queries**: Each query module in `app/db/queries/` uses `async with get_session() as session` pattern. All are re-exported via `app/db/queries/__init__.py` and accessible as `app.db.function_name`.

**WebSocket messages**: Server sends JSON `{"type": "...", ...}`. Client in `base.html` dispatches via `ws.on("type", handler)`. Types: `price`, `balance`, `heartbeat`, `bot_status`, `position`, `positions`, `adaptive_sizing`, `trade_opened`, `trade_closed`, `orderbook`, `agg_trade`.

**Two operational modes**: Multi-user (Google OAuth enabled) or single-user/legacy (env API keys). Controlled by `GOOGLE_CLIENT_ID` in config.

**CSRF protection**: All POST/PUT/DELETE requests require `X-CSRF-Token` header matching the session token. Token is injected via `<meta name="csrf-token">` in `base.html` and `setup.html`. Helper `csrfToken()` is available globally. Middleware in `app/middleware/csrf.py`, exempts GET/HEAD/OPTIONS, WebSocket upgrades, and `/auth/callback`.

**WebSocket auth**: `/ws/{user_id}` requires session authentication — rejects if no session or user ID mismatch (code 4003). No legacy unauthenticated WS endpoint.

**Live vs Paper**: Scoped by `(user_id, is_paper)` throughout DB queries and bot workers. `exchange.py` for live, `paper_exchange.py` for paper.

## Database Models (14 tables)

| Model | Table | Key fields |
|-------|-------|------------|
| `User` | `users` | google_id, email, name, is_approved, is_rejected, is_admin, paper_bot_started, totp_secret_enc, totp_enabled, totp_backup_codes |
| `UserConfig` | `user_configs` | user_id (FK), binance_api_key_enc, binance_api_secret_enc |
| `Trade` | `trades` | user_id (FK), is_paper, symbol, direction, entry/exit price/time, result, r_value, pnl_usdt (net), commission (USDT fee), funding_fee, target_rr (2:1/3:1 regime) |
| `BotState` | `bot_state` | key, value, user_id, is_paper — unique on (key, user_id, is_paper) |
| `BotEvent` | `bot_events` | user_id, is_paper, level, category, message, details |
| `HistoricalCandle` | `historical_candles` | symbol, interval, timestamp, OHLCV |
| `CandleBuffer` | `candle_buffer` | symbol, timestamp, OHLCV |
| `BacktestResult` | `backtest_results` | symbol, direction, entry/exit, result, r_value, pnl_usdt, target_rr |
| `PaperAccount` | `paper_accounts` | user_id (unique FK), balance (default 10000) |
| `PaperOrder` | `paper_orders` | user_id, trade_id (FK), symbol, side, order_type, stop_price, status |
| `RejectionLog` | `rejection_log` | user_id, email, name, status (rejected/allowed) |
| `BacktestRun` | `backtest_runs` | signature (unique), strategy, symbol, interval, params_hash, data fingerprint, total_setups, stats (JSON) — backtester result cache |
| `BacktestSetup` | `backtest_setups` | run_id (FK, cascade), ordinal, entry_time, data (JSON setup); indexed (run_id,ordinal) + (run_id,entry_time) |

## SOP — Keeping Docs Updated

These rules apply to every code change in this codebase:

### On any code change
- If a file's **purpose changed**, was **added**, or was **removed**: update the file map in this CLAUDE.md AND `docs/codebase-map.md`

### On feature, fix, or meaningful change
- Add an entry to `CHANGELOG.md` under today's date
- Small fixes (typos, one-liner tweaks, CSS adjustments) can be batched under a single date entry like "Minor fixes: ..."

### On new file creation
- Add to the directory tree and file map in this CLAUDE.md
- Add detailed function/class documentation to `docs/codebase-map.md`

### On file deletion
- Remove from both this CLAUDE.md and `docs/codebase-map.md`

### On new route or page
- Update the feature-to-file lookup table above
- Add template documentation to `docs/codebase-map.md`
- Add nav links to `templates/base.html` if needed

### On database schema change
- Update the database models table above
- Create a new Alembic migration
- Update `docs/codebase-map.md` migration history section
