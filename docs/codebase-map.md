# Codebase Map — Detailed Reference

Function-level documentation for every source file in the ZENITH trading bot platform. For the concise overview, see `CLAUDE.md` in the project root.

---

## Core Engine

### `config.py`
Central configuration loaded from environment variables.

| Constant | Value / Source | Purpose |
|----------|---------------|---------|
| `BINANCE_API_KEY/SECRET` | env | Legacy single-user Binance credentials |
| `DATABASE_URL` | env | PostgreSQL connection string |
| `TELEGRAM_BOT_TOKEN` | env | Telegram alert bot token |
| `TELEGRAM_CHAT_ID` | env | Public channel for trade alerts |
| `TELEGRAM_OWNER_ID` | env | Private chat for status alerts |
| `GOOGLE_CLIENT_ID/SECRET` | env | Google OAuth (enables multi-user mode) |
| `SESSION_SECRET` | env (default "change-me-in-production") | Starlette session signing |
| `ENCRYPTION_KEY` | env | Fernet key for API key encryption |
| `RESEND_API_KEY` | env | Resend email service |
| `EMAIL_FROM` | env | Sender email address |
| `SYMBOLS` | `["BTCUSDT","ETHUSDT","SOLUSDT"]` | Traded pairs |
| `TICK_SIZE` | `{BTC:0.10, ETH:0.01, SOL:0.01}` | Price step per symbol |
| `LOT_SIZE` | `{BTC:0.001, ETH:0.01, SOL:0.1}` | Quantity step per symbol |
| `LEVERAGE` | `5` | Exchange leverage |
| `COMMISSION_PCT` | `0.0004` | 0.04% taker fee |
| `SLIPPAGE_TICKS` | `2` | Ticks of slippage applied on entry |
| `CANDLE_BUFFER_SIZE` | `1000` | Max klines to fetch on startup |
| `STRATEGY_PARAMS` | dict | The 15m strategy body (see below); also `STRATEGIES["amd_15m"]` |
| `STRATEGIES` | dict | Strategy registry: `amd_15m` (15m) + `trend_5m` (5m). Each is self-contained with its own `interval` + adaptive params |
| `ACC_RANGE_MODE` | `{BTC:"body", ETH:"wick", SOL:"wick"}` | Per-asset accumulation mode |

**STRATEGY_PARAMS** defaults: `tf_minutes=15`, `acc_len=60`, `acc_mode="atr"`, `atr_mult_acc=5`, `man_look=10`, `fvg_threshold=0.1`, `atr_len=14`, `atr_mult=1.5`, `rrr=2.0`, `dynamic_rr=True`, `rrr_trend=3.0`, `rrr_range=2.0`, `htf_hours=6`, `htf_adx_period=14`, `htf_adx_threshold=40`, sessions=all four, `sweep_filter=True`, `sweep_len=5`, `sweep_max_bars=300`, `skip_months=[5]`, `skip_weeks={4:[2,4]}`, `manip_min_mode="atr"`, `manip_min_val=0.4`, `adx_filter=True`, `adx_period=42`, `adx_threshold=35`, `loss_streak_threshold=4`, `reduced_risk_pct=0.25`, `wins_to_recover=2`

**STRATEGIES["trend_5m"]** overrides: `interval="5m"`, `tf_minutes=5`, `acc_len=20`, `atr_mult=1.5`, `rrr=2.0`, `dynamic_rr=False`, `trend_only=True`, `htf_adx_threshold=50`, `manip_min_val=1.5` (rest mirror the 15m body). The engine/worker are strategy-agnostic — `backtest_combine.py` overlays them (gating + per-strategy adaptive sizing).

---

### `main.py`
FastAPI app entry point.

- `lifespan(app)` — Async context manager: init DB → create SharedMarketData → load history + start kline stream → create BotManager → auto-start paper/live bots for all approved users → start WS push tasks. On shutdown: cancel tasks, stop all workers, close market.
- Two modes: multi-user (Google OAuth) or single-user/legacy (env API keys)
- Exception handlers: `AuthRequired` → `/login`, `PendingApproval` → `/pending`, `AccountRejected` → `/rejected`, `AdminNotFound` → 404
- `landing_page()` — Serves static `landing-page/landing-page.html` at `/`
- Mounts 11 route modules + static files at `/static` and `/landing`
- Middleware stack (outermost → innermost): SessionMiddleware → CSRFMiddleware → app
- Session middleware: `max_age=86400`, `same_site="lax"`, `https_only` in production
- Session secret: auto-generates random if default `"change-me-in-production"` detected

---

### `strategy.py`
Live signal detector. Processes a candle buffer and returns a signal dict if the LAST bar triggers entry.

- `check_signal(candles, params)` → `{direction, entry_price, sl, tp, sl_distance, atr, timestamp}` or `None`
- `_in_session(ts_ms, sessions)` — Checks if timestamp is in active trading session (NY timezone)
- `_in_skip_period(ts_ms, skip_months, skip_weeks)` — Checks for skip months/weeks
- `_rolling_max(values, period)` / `_rolling_min(values, period)` — O(n) monotone deque
- `_atr(highs, lows, closes, period)` — Wilder's ATR (RMA smoothing, matches Pine Script)
- `_adx(highs, lows, closes, period)` — ADX with +DI/-DI, Wilder's smoothing
- `_compute_sweep_zones(highs, lows, closes, sweep_len, sweep_max_bars)` — Liquidity sweep zone detection using pivot highs/lows

State machine: Accumulation → Manipulation → FVG → Entry. Only fires on bar `i == n - 1`.

Key details:
- Candle timestamps are **milliseconds**
- Smart SL: wider of ATR-based SL and manipulation extremum
- Accumulation supports "atr" mode (range ≤ ATR * mult) and "fixed %" mode
- `acc_range_mode` controls body-based vs wick-based range calculation
- ADX filter skips counter-trend entries when ADX > threshold

---

### `amd_engine.py`
Backtest/simulation engine. Runs strategy on historical data bar-by-bar.

- `run(data, cfg)` → list of setup dicts. Takes candle list `{time, open, high, low, close}` (time in **seconds**) and config with **camelCase** keys. When `cfg["dynamicRR"]` is on, picks RR per entry from the trend regime (`rrrTrend` vs `rrrRange`); when `cfg["trendOnly"]` is on, enters ONLY where the HTF regime is trending (the 5m strategy — note: a fixed-RR trend-only config must set `rrrTrend==rrrRange`, since all entries are in-trend). Each setup carries `regime` + `rrrUsed`
- `_htf_trend_mask(data, cfg)` → per-bar bool: resamples 15m→`htfHours` (6h) candles, computes ADX(`htfAdxPeriod`), flags bars where the **previous completed** HTF bar's ADX ≥ `htfAdxThreshold` (no lookahead) — drives the adaptive 3:1 regime
- `compute_stats(setups, rrr, equity_cfg)` → stats dict (trades, wins, losses, win_rate, total_r). R is derived per trade, so variable RR is handled unchanged
- `simulate_equity(setups, rrr, cfg, with_trades=False)` → equity curve with adaptive sizing, commission, drawdown tracking. With `with_trades=True`, also returns a per-trade `trades` ledger (symbol from `_symbol`, entry/exit, result, r, targetRr, pnl, running equity) — powers `/api/backtest/tradelog`
- `_close_trade(trade, idx, time, price, result)` — Marks trade dict as closed
- `_make_setup(...)` — Creates detailed setup dict with 18+ fields (incl. `regime`/`rrrUsed`)

Config uses **camelCase** keys (e.g., `accLen`, `fvgThreshold`), while `config.py` uses **underscore** keys. `seed_trades.py` and `routes/backtester.py._build_cfg()` bridge this mapping (both per-strategy).

---

### `backtest_combine.py`
Overlays multiple strategies into one backtest — the single source of truth for how `amd_15m` and
`trend_5m` combine. Used by `routes/backtester.py` (combined/tradelog endpoints) and `seed_trades.py`.

- `gate(setups)` → cross-strategy position gating: one open position per symbol across all strategies (first-come). Setups carry `_symbol`, `_strategy`, `entryTime`, `exitTime`
- `adaptive_params(strategy)` → that strategy's loss-streak / reduced-risk / wins-to-recover
- `simulate(setups, cfg, with_trades=False)` → gates, then walks equity with a **separate adaptive streak per strategy** over one shared wallet. Same output shape as `amd_engine.simulate_equity` plus a per-trade `strategy` tag (and quantity/commission/sl/tp when `with_trades`)
- `compute_stats(setups, cfg=None)` → combined headline stats over the gated set (+ `perStrategy` breakdown, + `equity` when cfg given)

---

### `exchange.py`
Live Binance Futures API wrapper.

- `BinanceExchange(api_key, api_secret)`
- `connect()` — Creates AsyncClient + BinanceSocketManager, detects position mode, sets leverage
- `_detect_position_mode()` — Queries hedge/one-way mode
- `get_balance()` → USDT futures balance
- `get_position(symbol)` → `{symbol, side, quantity, entry_price, unrealized_pnl}` or None
- `get_all_positions()` → list of non-zero positions
- `place_market_order(symbol, side, quantity, position_side)` — Hedge mode auto-sets positionSide
- `place_stop_loss(symbol, side, quantity, stop_price)` — STOP_MARKET, reduceOnly in one-way mode. **NOTE:** python-binance auto-routes STOP/TP to Binance's conditional/algo endpoint; the response has `algoId` (no `orderId`)
- `place_take_profit(symbol, side, quantity, price)` — TAKE_PROFIT_MARKET (also a conditional/algo order)
- `get_open_orders(symbol)` — **regular** open orders only (SL/TP are NOT here)
- `get_conditional_orders(symbol, strict=False)` — conditional/algo open orders (this is where SL/TP live; `conditional=True`). `strict=True` re-raises on error so a safety-critical caller can distinguish "no SL" from "couldn't fetch"
- `get_order(symbol, order_id)` / `get_trades_for_order(symbol, order_id)` — Fill data queries
- `cancel_order()` / `cancel_all_orders()` — Order cancellation. `cancel_all_orders` clears **both** the regular and conditional/algo buckets
- `get_klines(symbol, interval, limit)` → list of candle dicts
- `start_kline_socket(symbols, interval, callback)` — Multiplex kline websocket
- `start_user_socket(callback)` — User data stream (ORDER_TRADE_UPDATE events)
- `_format_qty(symbol, qty)` / `_format_price(symbol, price)` — Precision formatting

**Module-level helpers (shared by worker, anomaly scanner, and reconcile):**
- `resolve_trade_exit(client, symbol, direction, entry_order_id, entry_time, entry_quantity, sl_price, tp_price)` → `{exit_price, exit_commission, realized_pnl, is_sl, exit_qty}` or None. Determines how a *closed* position actually exited by reading the account's real closing fills (`futures_account_trades` / `/fapi/v1/userTrades`, which carries `realizedPnl`), isolating this trade's exit fills (opposite side, after entry, accumulated up to entry qty). Decides SL vs TP by the real exit price's proximity to each level. Sidesteps the `-2013` trap where `futures_get_order(algoId)` can't resolve conditional/algo SL/TP orders. Takes a python-binance `AsyncClient` (use `BinanceExchange.client`)
- `r_value_for_exit(is_sl, entry_price, sl_price, tp_price, fallback_r)` → R multiple. Loss = −1; win = level-implied `|tp-entry|/|entry-sl|` (falls back to `fallback_r` or 2.0). Guards the `-1.0 or 2.0` truthiness trap
- `position_pnl_breakdown(client, symbol, entry_time, exit_time)` → `{realized_pnl, funding_fee, commission, net_pnl}` or None. The **net realized PnL exactly as Binance's Position History shows it** — summed from the income ledger (`futures_income_history`: `REALIZED_PNL + FUNDING_FEE + COMMISSION`), never computed from prices. `commission` is a positive USDT cost; non-USDT (BNB) fees are converted via `_asset_usdt_price()` (1m kline at fee time). Used by `_self_heal_trade`, the anomaly scanner, and reconcile to set `trades.pnl_usdt`/`commission`/`funding_fee`

---

### `paper_exchange.py`
Paper trading exchange mimicking BinanceExchange interface. Uses SharedMarketData for real-time prices.

- `PaperExchange(user_id, shared_market)`
- `connect()` — Ensures paper account in DB
- `get_balance()` → reads from DB
- `place_market_order()` — Uses shared market price for fill, manages in-memory positions
- `place_stop_loss()` / `place_take_profit()` — Creates PaperOrder in DB
- `start_user_socket(callback)` — **Polling loop** (every 1s), checks pending orders against live prices. Fills orders, updates paper balance (PnL minus commission), emits synthetic ORDER_TRADE_UPDATE events.

Trigger logic: STOP_MARKET SELL triggers when price ≤ stop_price; BUY when price ≥. TAKE_PROFIT_MARKET SELL when price ≥; BUY when price ≤.

---

### `telegram_alert.py`
Telegram notifications via Bot API.

- `send_alert(text)` — Sends to channel (TELEGRAM_CHAT_ID)
- `send_private(text)` — Sends to owner (TELEGRAM_OWNER_ID)
- `alert_entry(symbol, direction, entry_price, sl, tp, quantity, balance)` — Formatted entry notification
- `alert_exit(symbol, direction, result, entry_price, exit_price, pnl, r_value, balance)` — Formatted exit notification
- `alert_bot_started()` — No-op (disabled)
- `alert_bot_stopped(reason)` — Private alert unless reason is "shutdown"

---

### `import_candles.py`
CLI tool: imports 1m CSV → PostgreSQL, resamples to higher TFs.

- `ensure_table(conn)` — Creates historical_candles table
- `import_1m(conn, symbol)` — Reads `{symbol}_1m.csv` from `../data/`, converts ms→s timestamps, batch inserts 10k rows
- `resample(conn, symbol, interval)` — Aggregates 1m → 5m/15m/30m/1h/4h/1d
- Uses raw psycopg2 (not SQLAlchemy) for performance

---

### `seed_trades.py`
CLI tool: runs the COMBINED two-strategy overlay on historical candles → inserts BacktestResult rows
(and optionally refreshes the seeded `trades` live history). Gitignored (local-only).

- `_build_cfg(symbol, scfg)` — Converts a `STRATEGIES` entry (underscore) to camelCase for amd_engine (fixed-RR trend-only strategies get `rrrTrend==rrrRange`)
- `seed(symbols, clear, live_history)` — For each strategy: loads its interval candles, runs `amd_engine.run()`, tags setups; then `backtest_combine.simulate(with_trades=True)` gates + sizes; writes `backtest_results` (tagged `strategy`). With `live_history`, also `db.clear_user_trades(1, False)` + `db.bulk_create_trades(...)`
- CLI: `--clear`, `--live-history`, `--symbol`. Combined baseline: 886 trades, $383,441

---

## App Layer

### `app/auth/service.py`
Authentication, encryption, and session utilities.

- `encrypt(plaintext)` / `decrypt(ciphertext)` — Fernet encryption for API keys
- `require_auth(request)` → User or raises AuthRequired/PendingApproval/AccountRejected
- `get_current_user(request)` → User from session, or `_LEGACY_USER` (id=1) if no Google OAuth
- `get_trading_mode(request)` → "live" or "paper"
- `get_admin_mode(request)` → bool

---

### `app/core/context.py`
Shared template context builder.

- `get_global_context(user_id, mode)` → `{bot_running, bot_status, bot_enabled, balance, trading_mode, is_paper}`

---

### `app/core/template_filters.py`
Shared Jinja filters registered onto a route's `Jinja2Templates` env.

- `_num(value, decimals=2, sign=False)` — comma thousand separators + fixed decimals; `—` for `None`; optional leading `+`
- `register_filters(templates)` — registers `num` on `templates.env.filters` (used by `trades.py`, `dashboard.py`, `analytics.py`)

---

### `templates/_trade_table.html`
Shared trade-table partial. Expects `rows` (list of Trade) + `leverage` in context and the `num` filter. Columns: Pair (+leverage pill), Date (Open/Closed), Dir, Entry, Closed, Size, Result, R (tooltip), ROI, Commission, Funding Fee, Nett PnL. Included by `trades.html` and `dashboard.html` (Recent Trades) via `{% set rows = ... %}{% include %}`.

---

### `app/email.py`
Transactional emails via Resend.

- `send_approval_email(to, name)` — Branded HTML approval notification
- `send_rejection_email(to, name)` — Branded HTML rejection notification

---

### `app/db/engine.py`
Database initialization.

- `init_db(database_url)` — Creates async engine, runs `_ensure_schema()`, then `create_all`
- `_ensure_schema(eng)` — Idempotent `ALTER TABLE ADD COLUMN IF NOT EXISTS` for all user-scoping columns
- `get_session()` → AsyncSession

---

### `app/db/models.py`
12 SQLAlchemy ORM models. See CLAUDE.md for the table summary.

Key relationships:
- `Trade.user_id` → `User.id`
- `UserConfig.user_id` → `User.id` (unique)
- `PaperAccount.user_id` → `User.id` (unique)
- `PaperOrder.user_id` → `User.id`, `PaperOrder.trade_id` → `Trade.id`
- `RejectionLog.user_id` → `User.id`
- `BotState` unique on `(key, user_id, is_paper)`
- `HistoricalCandle` unique on `(symbol, interval, timestamp)`
- `CandleBuffer` unique on `(symbol, timestamp)`

---

### `app/db/queries/trades.py`

| Function | Returns | Notes |
|----------|---------|-------|
| `create_trade(trade_data)` | Trade | Insert new trade row |
| `update_trade(trade_id, updates)` | — | Update specific fields |
| `get_trade(trade_id)` | Trade or None | By ID |
| `get_open_trade(user_id, is_paper)` | Trade or None | First trade with result="open" |
| `get_open_trades(user_id, is_paper)` | list[Trade] | All open trades (per-asset concurrency: one per symbol) |
| `get_open_trade_for_symbol(user_id, symbol, is_paper)` | Trade or None | Open trade for a specific symbol (close the right trade on fill) |
| `get_recent_trades(limit, user_id, is_paper)` | list[Trade] | DESC by ID |
| `get_all_trades(user_id, is_paper)` | list[Trade] | ASC by ID |
| `get_trades_filtered(user_id, is_paper, symbol, direction, result_filter)` | list[Trade] | With optional filters |
| `get_today_pnl(user_id, is_paper)` | float | Sum of pnl_usdt for today's closed trades |
| `get_today_trade_count(user_id, is_paper)` | int | Trades entered today |

### `app/db/queries/users.py`

| Function | Returns | Notes |
|----------|---------|-------|
| `get_user(user_id)` | User | By ID |
| `get_user_by_google_id(google_id)` | User | By Google OAuth ID |
| `upsert_user_from_google(google_id, email, name, avatar_url)` | User | Create or update on login |
| `set_user_approved(user_id, approved=True)` | None | Set `is_approved` (used by admin approve + open-registration auto-approve) |
| `get_user_config(user_id)` | UserConfig | Encrypted API keys |
| `save_user_config(user_id, api_key_enc, api_secret_enc)` | — | Create or update |
| `delete_user_api_keys(user_id)` | — | Nulls out encrypted key/secret fields |
| `get_all_configured_users()` | list[(User, UserConfig)] | Users with API keys set |

### `app/db/queries/candles.py`

| Function | Returns | Notes |
|----------|---------|-------|
| `save_candles(symbol, candles)` | — | Bulk upsert with ON CONFLICT DO NOTHING |
| `get_candles(symbol, limit)` | list | Recent live candles from buffer |
| `trim_candle_buffer(symbol, keep)` | — | Delete old candles |
| `get_historical_candles(symbol, interval, end, limit)` | (candles, has_more) | Paginated fetch |
| `get_historical_candle_range(symbol, interval)` | (min_ts, max_ts) | Date range query |
| `get_all_historical_candles(symbol, interval)` | list | All candles ASC (for backtester) |

### `app/db/queries/state.py`

| Function | Returns | Notes |
|----------|---------|-------|
| `get_state(key, default, user_id, is_paper)` | any | JSON-parsed value, falls back to global |
| `set_state(key, value, user_id, is_paper)` | — | Atomic upsert (ON CONFLICT DO UPDATE) |

### `app/db/queries/admin.py`

| Function | Returns | Notes |
|----------|---------|-------|
| `get_all_approved_users()` | list[User] | Ordered by last login |
| `get_pending_users()` | list[User] | Neither approved nor rejected |
| `get_platform_today_pnl()` | float | Sum across all live trades today |
| `get_platform_year_pnl()` | float | Sum across all live trades this year |
| `get_platform_trade_count()` | int | All closed live trades |
| `get_user_trade_summary(user_id, is_paper)` | dict | trades, wins, losses, win_rate, total_r, total_pnl, today_pnl |
| `get_all_platform_trades(is_paper)` | list[Trade] | All closed trades across users |
| `get_admin_events(limit, user_id, level, category, is_paper)` | list[(BotEvent, User)] | With optional filters |

### `app/db/queries/events.py`

| Function | Returns | Notes |
|----------|---------|-------|
| `log_event(message, level, category, details, user_id, is_paper)` | — | Insert BotEvent |
| `get_recent_events(limit, user_id, is_paper)` | list[BotEvent] | DESC by ID |

### `app/db/queries/paper.py`

| Function | Returns | Notes |
|----------|---------|-------|
| `get_or_create_paper_account(user_id, default_balance)` | PaperAccount | Default $10,000 |
| `update_paper_balance(user_id, new_balance)` | — | |
| `get_paper_balance(user_id)` | float | |
| `create_paper_order(order_data)` | PaperOrder | |
| `get_pending_paper_orders(user_id, symbol)` | list[PaperOrder] | Status "NEW" |
| `get_paper_orders_for_trade(trade_id)` | list[PaperOrder] | |
| `fill_paper_order(order_id)` / `cancel_paper_order(order_id)` | — | |
| `cancel_all_paper_orders(user_id, symbol)` | — | Cancels all NEW orders |

### `app/db/queries/backtest.py`

| Function | Returns | Notes |
|----------|---------|-------|
| `get_backtest_results(symbol)` | list[BacktestResult] | Optional symbol filter (Trade Log) |
| `save_backtest_results(results)` | — | Bulk insert |
| `clear_backtest_results()` | — | Delete all |
| `get_backtest_run(signature)` | BacktestRun \| None | Cache lookup by signature |
| `save_backtest_run(run, setups)` | run_id | Prunes stale versions for (strategy,symbol,interval); concurrency-safe on unique signature; bulk-inserts setups |
| `get_run_setups_all(run_id)` | list[dict] | All setups, ordinal asc (Step 1 same-shape) |
| `get_run_setups_page(run_id, limit, offset)` | list[dict] | Newest-first page → ascending (Step 2 nav) |
| `get_run_setups_range(run_id, from_ts, to_ts)` | list[dict] | Setups in a time window (Step 2 chart) |

**Backtester result cache** (`routes/backtester.py`): `_get_or_build_run(symbol, interval)` builds a `sha256(strategy + symbol + interval + params_hash + (first_ts,last_ts,count))` signature, checks in-memory `_run_cache` → DB `BacktestRun` → else runs `amd_engine.run` once and persists. `/api/backtest` + `/api/backtest/combined` return the same shape as before, now from cache. Models: `BacktestRun` (one cached run; `symbol="COMBINED"` row holds combined stats) + `BacktestSetup` (one row per setup, indexed for nav + chart-range).

---

## Bot Layer

### `app/middleware/csrf.py`
CSRF protection middleware (session-based tokens).

- `CSRFMiddleware(BaseHTTPMiddleware)` — Generates `csrf_token` in session on first request; validates `X-CSRF-Token` header on POST/PUT/DELETE; exempts safe methods, WebSocket upgrades, `/auth/callback`; returns 403 on mismatch

### `app/bot/__init__.py`
Bot accessors and config builders.

- `set_bot_manager(manager)` — Stores global reference
- `broadcast(data)` — Sends to user 1 via ws_manager (legacy compat)
- `make_broadcast_fn(user_id, mode)` → closure that broadcasts to specific user/mode via ws_manager
- `get_bot()` → BotWorker for user 1 (legacy)
- `get_bot_for_user(user_id, mode)` → BotWorker for any user/mode
- `build_user_config(api_key, api_secret)` → BotConfig for live trading
- `build_paper_config(paper_balance)` → BotConfig with is_paper=True

### `app/bot/worker.py` — `BaseWorker`
Mode-agnostic trading loop for a single user/mode. **No `is_paper` branching** — every mode difference goes through a hook overridden by `LiveWorker` / `PaperWorker`.

**BotConfig** (dataclass): api_key, api_secret, symbols, strategy_params (15m body, back-compat), leverage, commission, slippage, tick_sizes, lot_sizes, buffer_size, telegram_*, is_paper, paper_balance, loss_streak_threshold, reduced_risk_pct, wins_to_recover, **strategies** (the `STRATEGIES` registry — falls back to a single 15m strategy from strategy_params)

**Module helpers**: `make_worker(user_id, config, ...)` builds `LiveWorker` or `PaperWorker` by `config.is_paper`. `BotWorker` is an alias of `BaseWorker` (back-compat).

**Shared methods (BaseWorker)**:
- `start()` / `stop()` — Connect exchange (`_create_exchange`) → crash recovery → candle callback → user stream + poll; alerts via `_alert_started`/`_alert_stopped`
- `_crash_recovery()` / `_recover_one_trade(trade)` — Reconcile DB vs exchange across all open trades, then `_sweep_orphan_positions()` (hook). Closed-trade exits resolved via `_resolve_exit()` (hook); open-trade SL/TP via `_count_active_sltp()` (hook)
- `_on_shared_candle(symbol, interval, candle)` → `_process_candle(symbol, interval)`
- `_process_candle(symbol, interval)` — user gates (bot_enabled, active symbols) → per-symbol `asyncio.Lock` → cross-strategy one-position-per-symbol DB guard → loop the strategies on THIS interval, calling `_detect_for_strategy`; first signal wins the symbol → `_execute_trade`
- `_detect_for_strategy(symbol, name, scfg, skip_may, skip_tax)` — owns the strategy's seasonal calendar, trend regime (`get_trend_regime`), **trend-only gate** (skip if not trending — live `check_signal` has no such flag), and reward:risk (adaptive for 15m, fixed for 5m) → `check_signal()`; stamps `target_rr`
- `_execute_trade(symbol, signal, strategy)` — per-strategy sizing (`_get_effective_risk(strategy)`) → `_prepare_entry()` (hook) → market order → `_entry_commission()` (hook) → record (tagged with `strategy`) → `_place_sl_tp` → broadcast → `_alert_entry()` (hook)
- `_place_sl_tp(...)` — `_before_place_sl_tp()` (hook) then 3-retry place; returns `(sl_ok, tp_ok)`
- `_on_user_event(data)` — ORDER_TRADE_UPDATE: resolve trade by symbol, `_exit_fill()` (hook), close, route result to the trade's strategy streak, broadcast, `_on_trade_closed()` (hook)
- `_run_position_poll()` / `_poll_one_trade(trade)` — safety-net poll; closed → `_resolve_exit()` + de-biased fallback; still-open → `_poll_open_position()` (hook)
- `_resolve_strategy(trade)` — the trade's strategy (legacy/NULL rows → default 15m), for routing adaptive results
- `_get_effective_risk(strategy)` / `_on_trade_result(strategy, won)` — adaptive-sizing state machine, tracked **per strategy** in `self._adaptive`
- **Hooks** (defaults in base): data hooks `_create_exchange`, `_resolve_exit`, `_count_active_sltp` (abstract); value hooks `_entry_commission`, `_exit_fill` (computed default); side-effect hooks `_alert_started/_alert_stopped/_alert_entry`, `_on_trade_closed`, `_sweep_orphan_positions`, `_prepare_entry`, `_before_place_sl_tp`, `_poll_open_position` (no-op default)

### `app/bot/worker_live.py` — `LiveWorker(BaseWorker)`
Real-money Binance implementation of the hooks. Overrides: `_create_exchange` → BinanceExchange; `_resolve_exit` → `resolve_trade_exit` (account fills); `_entry_commission`/`_exit_fill` → real fills; all alert hooks → Telegram; `_sweep_orphan_positions`, `_prepare_entry`, `_before_place_sl_tp` (cancel both buckets), `_poll_open_position` (verify 1 SL+1 TP + force-close). Live-only methods: `_count_sltp_orders` (conditional/algo bucket), `_maybe_force_close_breach`, `_force_close_breached`, `_self_heal_trade`.

### `app/bot/worker_paper.py` — `PaperWorker(BaseWorker)`
Simulated implementation. Overrides only the data hooks: `_create_exchange` → DB-backed PaperExchange; `_resolve_exit` → first FILLED `PaperOrder`; `_count_active_sltp` → pending paper orders. Inherits computed commissions and all no-op side-effect hooks (no alerts/sweep/self-heal/force-close).

### `app/bot/manager.py`
Multi-user bot lifecycle. `start_bot()` builds the worker via `make_worker()` (Live/Paper by mode).

- `BotManager.start_bot(user_id, mode, config, broadcast_fn, shared_market)` — Create + start BotWorker as asyncio task
- `stop_bot(user_id, mode)` — Stop worker, cancel task
- `get_worker(user_id, mode)` / `get_any_worker(user_id)` → BotWorker or None
- `get_status(user_id, mode)` → status string
- `get_all_bot_info()` → list of dicts with full bot info

### `app/bot/shared_market.py`
Shared public Binance connection (no API key needed).

- `connect()` — Creates anonymous AsyncClient
- `load_history(symbols, interval, limit)` — Fetches klines, populates the `(symbol, interval)` buffer (drops last incomplete candle). Called once per interval (15m + 5m) from `main.py`
- `start_kline_stream(symbols, interval)` — Background multiplex kline WebSocket (one task per interval, collected in `_kline_tasks`)
- `_on_kline(data, interval)` — On closed candle: append to the `(symbol, interval)` buffer (max 400); **persist to DB only for 15m** (the `candle_buffer` table is symbol-keyed); fire 3-arg `(symbol, interval, candle)` callbacks
- `get_candles(symbol, interval="15m")` → copy of that interval's buffer
- `get_trend_regime(symbol, htf_hours, adx_period, adx_threshold)` → bool (trending) for the trend gate / adaptive RR: fetches recent 6h klines, computes ADX on the previous completed bar; cache keyed by `(symbol, htf_hours, adx_threshold)` (15m gates on 40, 5m on 50); **fail-safe → False (range / 2:1)** on any error
- `on_candle_close(callback)` / `remove_candle_callback(callback)` — Register/unregister (callbacks take `symbol, interval, candle`)

### `app/bot/websocket.py`
WebSocket connection manager + 7 background push tasks.

- `ConnectionManager` — Per-user WebSocket connections: `connect`, `disconnect`, `send_to_user`, `broadcast_all`
- `websocket_endpoint(ws, user_id)` — `/ws/{user_id}` route, rejects if no session or user ID mismatch (code 4003)
- `start_ws_tasks(bot_manager, shared_market)` → 7 asyncio tasks:
  1. `_price_stream` — Real-time ticker prices → all clients
  2. `_heartbeat_loop` — Every 15s: bot status, risk, session, candle age, uptime
  3. `_position_poll` — Every 2s: positions with unrealized PnL/R, SL/TP distance
  4. `_balance_poll` — Every 30s: USDT balance, persists to DB state
  5. `_orderbook_stream` — Top-20 depth at max 2Hz per symbol
  6. `_agg_trade_stream` — Aggregated trades (price, qty, side)
  7. `_trade_anomaly_scanner` — Every 5min: verify all trades from last 24h against Binance fills

---

## Routes

### `routes/auth.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/login` | `login.html` | Google sign-in page |
| GET | `/auth/google` | — | Initiate OAuth redirect |
| GET | `/auth/callback` | — | OAuth callback, upsert user, redirect |
| GET | `/logout` | — | Clear session |
| GET | `/setup` | `setup.html` | First-run API key form |
| POST | `/setup` | — | Save API keys, start bot |
| GET | `/pending` | `pending.html` | Awaiting approval page |
| GET | `/rejected` | `rejected.html` | Access declined page |

### `routes/dashboard.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/dashboard` | `dashboard.html` | Main dashboard with stats, equity curve, recent trades |
| GET | `/api/live-candles` | — | JSON OHLCV candles (Binance API → fallback to DB buffer) |

### `routes/position.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/position` | `position.html` | Live positions from exchange, SL/TP for bot trades |

### `routes/trades.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/trades` | `trades.html` | Filtered trade history (symbol, direction, result) |

### `routes/settings.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/settings` | `settings.html` | Bot settings, API key status |
| POST | `/settings` | — | Save bot settings (risk%/sessions/symbols/**leverage**/**skip_may**/**skip_tax_deadline**; RR is adaptive 2:1/3:1 via the 6h trend, not user-set); applies leverage live to a running worker. 2FA-gated |
| POST | `/settings/reset` | — | Reset all bot-control settings for the mode to validated defaults |
| POST | `/settings/api-keys` | — | Validate (`exchange.validate_api_key`) then save keys, store permission flags, restart bot. 2FA-gated |
| POST | `/settings/api-keys/validate` | — | Validate keys without persisting (inline form + wizard) |
| POST | `/settings/api-keys/delete` | — | Delete API keys, stop live bot. 2FA-gated |
| POST | `/api/emergency-close` | — | Close all positions, mark trades as loss. **Not** 2FA-gated (time-critical) |

Sensitive POSTs (`/settings`, `/settings/reset`, `/settings/api-keys`, `/settings/api-keys/delete`, `/bot/start`, `/bot/stop`) call `require_2fa(request, user)` — returns a `401 twofa_required` when the user has 2FA enabled and isn't freshly verified; the client's `guardedFetch` (base.html) prompts for a code and retries.

### `routes/twofa.py`
| Method | Path | Purpose |
|--------|------|---------|
| POST | `/settings/2fa/enroll` | Generate a pending TOTP secret (session) + QR; returns secret + SVG |
| POST | `/settings/2fa/verify` | Confirm enrollment, persist encrypted secret, enable, return **backup codes** (once) |
| POST | `/settings/2fa/disable` | Disable 2FA — requires a current TOTP or backup code |
| POST | `/settings/2fa/challenge` | Step-up: validate a TOTP/backup code, mark the session verified |
| POST | `/settings/2fa/backup-codes/regenerate` | Replace backup codes (requires a current code) |

`app/auth/twofa.py`: `generate_secret`, `provisioning_uri`, `verify_code`, `qr_svg`, `is_2fa_fresh`/`mark_2fa_verified`, `require_2fa` (the gate), `generate_backup_codes`/`hash_backup_codes`/`verify_totp_or_backup`/`backup_codes_remaining` (single-use sha256-hashed recovery codes), and a per-user `rate_limited`/`record_code_failure`/`clear_code_failures` (5 fails / 5 min). `exchange.validate_api_key(key, secret)` → `{ok, permissions, error}`.

### `routes/bot_control.py`
| Method | Path | Purpose |
|--------|------|---------|
| POST | `/bot/start` | Start live bot |
| POST | `/bot/stop` | Stop live bot |
| POST | `/bot/start-paper` | Start paper bot, mark user opted-in |
| GET | `/bot/status` | Current bot status JSON |
| POST | `/api/switch-mode` | Switch live/paper mode in session |

### `routes/analytics.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/analytics` | `analytics.html` | Win rate, profit factor, drawdown, session/day/monthly PnL, hold times |

### `routes/backtester.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/backtester` | `backtester.html` | Backtester page + **Playground** controls (risk knobs) |
| GET | `/api/candles` | — | Paginated historical candles from DB |
| GET | `/api/backtest` | — | Run backtest for single symbol; playground overrides `sessions`/`skip_may`/`skip_tax` (RR is adaptive 2:1/3:1 via the 6h trend; no leverage knob — sizing is risk-%-based) |
| GET | `/api/backtest/setups` | — | Setups by page/range; same overrides |
| GET | `/api/backtest/combined` | — | Combined backtest; overrides `sessions`/`skip_may`/`skip_tax`/`risk_pct`. Overrides fold into the cache signature (one cached run per combo) |
| GET | `/api/backtest/tradelog` | — | Param-aware **combined trade ledger** for the Playground's Trade Log (same overrides as `/combined`). Returns `{trades, total}` from `simulate_equity(..., with_trades=True)`; cached in `_tradelog_cache` per combo |

### `routes/alerts.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/alerts` | `alerts.html` | Telegram link, bot events (currently commented out) |

### `routes/track_record.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/track-record` | `track_record.html` | Public verified track record (no auth, standalone) |

### `routes/admin/__init__.py`
- `require_admin(request)` — Auth + admin check
- `POST /admin/toggle` — Toggle admin mode in session

### `routes/admin/dashboard.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/admin` | `admin_dashboard.html` | Platform overview: users, bots, equity, PnL + Access control toggle |
| POST | `/admin/settings/require-approval` | JSON | Set platform-wide `require_approval` state (`{enabled: bool}`); off = open registration |

### `routes/admin/users.py`
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/admin/users` | User management page (pending/approved/rejected tabs) |
| POST | `/admin/users/approve/{id}` | Approve user + send email |
| POST | `/admin/users/reject/{id}` | Reject user + create log + send email |
| POST | `/admin/users/disable/{id}` | Disable approved user, stop bots |
| POST | `/admin/users/toggle-admin/{id}` | Toggle admin status |
| POST | `/admin/users/allow-reregister/{id}` | Allow rejected user to re-register |

### `routes/admin/user_detail.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/admin/user/{id}` | `admin_user_detail.html` | Detailed user view with stats, equity, trades, events |
| POST | `/admin/user/{id}/reconcile` | — | Reconcile trades against Binance fill data |
| POST | `/admin/user/{id}/reset-2fa` | — | Clear a user's 2FA + backup codes (lost-device recovery) |

Helper functions: `compute_stats(trades)`, `_get_order()`, `_get_fills()`, `_get_all_fills()`

### `routes/admin/bots.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/admin/bots` | `admin_bots.html` | All bot instances with status/uptime/errors |

### `routes/admin/analytics.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/admin/analytics` | `admin_analytics.html` | Platform win rate, leaderboard, monthly PnL |

### `routes/admin/logs.py`
| Method | Path | Template | Purpose |
|--------|------|----------|---------|
| GET | `/admin/logs` | `admin_logs.html` | Filterable event log (user, level, category, mode) |

---

## Templates

### Inheritance
- **Extends base.html**: dashboard, position, trades, settings, analytics, backtester, alerts, all admin_* templates
- **Standalone** (own layout): login, setup, pending, rejected, 404, track_record

### base.html — Key blocks
- `{% block title_suffix %}` — appended to `<title>ZENITH...`
- `{% block head %}` — extra head content
- `{% block content_class %}` — extra CSS class on #page-content
- `{% block content %}` — main content area
- `{% block scripts %}` — scripts before </body>

### base.html — Context variables
All pages receive via `get_global_context()`: `user`, `admin_mode`, `page`, `is_paper`, `balance`, `bot_status`, `bot_enabled`, `trading_mode`

### base.html — JavaScript handlers
WebSocket message types: `price`, `balance`, `heartbeat`, `bot_status`, `position`, `adaptive_sizing`, `trade_opened`, `trade_closed`
Functions: `toggleAdminMode()`, `switchMode()`, `closePaperModal()`, `toggleProfileMenu()`, `toggleMorePanel()`, `formatLocalTimes()`, `updateNavActive()`
HTMX hooks: `beforeRequest` (progress bar), `afterSettle` (time format), `afterSwap` (title update), `pushedIntoHistory` (nav active state), `confirm` (prevent re-navigation)

### Template-specific heavy JS
- **position.html** (~800 lines): Chart with timeframe management, order book, market trades, position price lines, infinite scroll history
- **backtester.html** (~500 lines): Combined equity, per-asset chart with setup box visualization, trade log pagination
- **analytics.html**: Drawdown chart, monthly bars, session/day progress bars, hold time bars

---

## Static Assets

### `static/css/app.css`
Design system with CSS custom properties:
- Surfaces: `--bg` (#0F1117), `--card`, `--card-2`, `--card-3`, `--border`
- Colors: `--green` (#00C896), `--red` (#FF4D4D), `--amber`, `--blue`
- Fonts: `--sans` (Inter), `--mono` (JetBrains Mono)
- Layout: `--rail` (56px), `--header-h` (48px)

Responsive breakpoints: `768px` (hide sidebar, show bottom nav), `767px` (full mobile), `479px` (single-column stats)

### `static/js/websocket.js`
`BotWebSocket` class: auto-reconnect with exponential backoff (1s→30s), client-side ping every 30s, event-based `.on(type, handler)`.

---

## Alembic Migrations

| Version | Description |
|---------|-------------|
| `18cd39afc223` | Add is_approved, is_admin to users; drop historical candle index |
| `8adeceee7bf1` | Paper trading tables, user scoping on trades/events/state, backtest_results |
| `b1267890df2e` | Add is_rejected to users |
| `673da50d8570` | Create rejection_log table |
| `a2f1c3d5e7b9` | Add paper_bot_started to users, backfill approved users |
| `c3e8a1f2b4d6` | Add funding_fee to trades |
| `d4f9b2c1a8e3` | Add backtest_runs + backtest_setups (result cache) |
| `e5a1c2d3f4b7` | Add totp_secret_enc / totp_enabled / totp_backup_codes to users (2FA) |
| `f6b2d4e8a1c9` | Add target_rr to trades + backtest_results (adaptive 2:1/3:1) |
| `a7c9e1b3d5f2` | Add strategy (amd_15m/trend_5m) to trades + backtest_results (multi-strategy) |

Chain: `None → 18cd → 8ade → b126 → 673d → a2f1 → c3e8 → d4f9 → e5a1 → f6b2 → a7c9`

Note: `_ensure_schema()` in `engine.py` also runs idempotent ALTER TABLE statements on startup, so schema changes are applied even without running Alembic.
