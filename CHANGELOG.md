# Changelog

All notable changes to the ZENITH Trading Bot platform.

Format: [Keep a Changelog](https://keepachangelog.com/). Grouped by date and feature area.

---

## [2026-06-10]

### Changed
- **Paper trading fully separated from live (worker refactor)**: Split the 950-line `BotWorker` (which forked on `if self.is_paper` in ~14 places) into a mode-agnostic `BaseWorker` (`worker.py`) plus `LiveWorker` (`worker_live.py`) and `PaperWorker` (`worker_paper.py`). All mode-specific behavior now goes through ~12 hook methods (`_create_exchange`, `_resolve_exit`, `_count_active_sltp`, `_entry_commission`, `_exit_fill`, `_alert_started/_alert_stopped/_alert_entry`, `_on_trade_closed`, `_sweep_orphan_positions`, `_prepare_entry`, `_before_place_sl_tp`, `_poll_open_position`) that the subclasses override — the shared loop has **zero** `is_paper` branches. Live-only logic (conditional/algo SL/TP counting, Telegram alerts, post-close self-heal, orphan sweep, force-close-breach) lives entirely in `LiveWorker`; the real-money path can no longer be affected by a paper change and vice-versa. `manager.py` builds the right class via `make_worker()`; `BotWorker` kept as an alias to `BaseWorker` for compatibility. Behavior-preserving (methods moved verbatim).
- **Paper positions are now DB-backed (persist across restarts)**: `PaperExchange` previously held open positions in an in-memory dict that was wiped on every restart, so the worker's recovery/poll force-closed paper trades (usually as losses) on restart and the position page showed nothing. `get_position`/`get_all_positions` now derive from the open paper `Trade` row (mirroring how Binance is the source of truth for live); `place_market_order` just simulates the fill; the SL/TP fill loop settles balance from the `Trade`. Paper positions now survive restarts and render live like real ones.

## [2026-06-09]

### Fixed
- **Profitable TP trades recorded as SL losses (live)**: A live trade that hit **Take Profit** was being logged as a **Loss at the SL price** with `r=-1` (confirmed on BTCUSDT shorts #779–782 across 4 users: real exit 62,132.6 ≈ TP, `realizedPnl +2.75`, but DB showed loss at 64,179.5). **Root cause:** SL/TP are placed on Binance's **conditional/algo** endpoint, so the stored `sl_order_id`/`tp_order_id` are `algoId`s. When such an order triggers it spawns a *new regular* order with its own `orderId`, so every `futures_get_order(algoId)` lookup returns `-2013 Order does not exist`. Every exit-resolution path relied on that lookup and, on failure, fell back to an **SL-biased candle guess** (`is_sl = sl_hit or not tp_hit`) — defaulting wins to losses. New shared helper `exchange.resolve_trade_exit()` reads the account's **actual closing fills** (`/fapi/v1/userTrades`, with `realizedPnl`) and decides win/loss by the real exit price's proximity to TP vs SL — independent of any order id. Wired into all six affected paths: `worker.py` `_poll_one_trade`, `_recover_one_trade`, `_self_heal_trade`; `websocket.py` `_trade_anomaly_scanner`; and `routes/admin/user_detail.py` reconcile. The last-resort candle fallback in the poll is now de-biased (price-proximity, never assumes SL). New helper `exchange.r_value_for_exit()` computes the win R from price levels and fixes a latent `-1.0 or 2.0` truthiness bug that kept negative R on corrected wins. The anomaly scanner (5-min cycle, per-user keys) and the admin reconcile button now self-correct mislabeled history. Paper path untouched

### Changed
- **Position SL/TP display simplified**: Removed the SL/TP progress bars and the "% away" percentage from position cards. Mobile cards now render SL/TP as neutral `status-cell`s (same small-label + mono-value format as Size/Leverage, no red/green); the desktop table shows SL/TP as plain neutral text instead of red/green spans. Applied across all render paths: `base.html` mobile card, `position.html` Jinja + JS mobile cards, and the desktop table (Jinja + JS). Also dropped the now-unused price-distance recompute in the mobile-card `price` handler (SL/TP prices are static per trade)

### Removed
- **Dead `position` WebSocket handler (`base.html`)**: Removed the `botWS.on('position', …)` single-position panel handler — it targeted a `position-panel` element that exists in no template and listened for a singular `position` message the backend never sends (superseded by the `positions` plural multi-position payload). It also referenced an undefined `sl_distance_pct`/`tp_distance_pct` field

### Added
- **Force-close safety net (SL breach with no stop order)**: If a live position ends up with **no STOP order** on the exchange AND the current price has already **breached the SL level**, the bot now market-closes it (a synthetic stop) instead of only alerting. Tightly gated to prevent false closes — it fires only when all hold: SL genuinely absent (confirmed by a *strict* conditional-orders fetch that raises on error rather than returning empty), the re-place attempt failed, a reliable live price (`get_latest_price`) is past `sl_price` in the trade's direction, and Binance's own `unrealized_pnl` is at the SL magnitude (≤ −0.9×`|entry−sl|×qty`, i.e. ~1R loss — not merely negative). `worker.py` → `_maybe_force_close_breach()` + `_force_close_breached()`; `exchange.py` → `get_conditional_orders(symbol, strict=True)`. Live-only (paper closes on breach via its own fill loop)

### Fixed
- **SL/TP conditional-order bucket (TRUE root cause of `-4045`)**: `python-binance` 1.0.37 auto-routes `STOP_MARKET`/`TAKE_PROFIT_MARKET` to Binance's **conditional/algo** order endpoint (`/fapi/v1/algoOrder`), a separate bucket from regular orders. The bot's `get_open_orders`/`cancel_all_orders` only touched the *regular* bucket, so it was blind to its own SL/TP — re-placing them every cycle and accumulating to the algo cap (200 observed) → `-4045`. Also, the algo response returns `algoId` (no `orderId`), so `order["orderId"]` raised `KeyError`, making every successful placement look like a failure and triggering more retries. Fixes: `exchange.py` adds `get_conditional_orders()` (`conditional=True`) and makes `cancel_all_orders()` clear **both** buckets; `place_stop_loss`/`place_take_profit` read `algoId`; `worker.py` detects SL/TP via `_count_sltp_orders()` scanning the conditional bucket (matched by `orderType` + hedge `positionSide`) and treats anything other than exactly 1 SL + 1 TP as needing a cancel-both-buckets + re-place, which self-cleans existing duplicates on deploy. Verified read-only against the live account (200 conditional orders → counts 102 SL / 98 TP)
- **SL/TP duplicate-order accumulation**: Re-placement always routes through `_place_sl_tp()`, which cancels orders first — so the bot can never hold more than one SL + one TP per symbol. Removed the `-4045` backoff workaround
- **SL/TP false-positive alerts**: Replaced fragile `get_open_orders` type scan with direct order-ID lookup via `get_order()` for SL/TP verification
- **WebSocket 403 on new sessions**: Converted CSRF middleware from `BaseHTTPMiddleware` to pure ASGI middleware — `BaseHTTPMiddleware` wraps the ASGI lifecycle in a way that breaks WebSocket upgrades; pure ASGI skips non-HTTP scopes entirely
- **`bot_state` upsert error**: Drop old `bot_state_key_key` single-column unique constraint before creating `uq_bot_state_scoped` (scoped by user_id + is_paper); deduplicates existing rows first
- **Position page mode flickering**: Added WebSocket mode filter so live/paper position data doesn't cross-render
- **WebSocket reconnect loop**: Auto-redirect to `/login` after 3 consecutive WS failures (verified via HEAD request) instead of retrying forever with expired session

### Changed
- **Per-asset concurrent trades**: The bot now holds one open trade *per asset* (BTC/ETH/SOL can run simultaneously) instead of one trade total. `BotWorker` tracks `_active_trades` (symbol→trade_id) with per-symbol SL/TP recovery timers; the entry guard blocks only the symbol that already has an open trade (checked against the DB as source of truth). Crash recovery, the position poll, and the order-fill handler all operate per-trade; the fill handler now resolves the trade by symbol (`get_open_trade_for_symbol`) so the correct trade closes. Position page + WebSocket enrich every concurrent position as bot-managed. Paper SL/TP and emergency-close also made per-symbol/multi-trade aware. New queries: `get_open_trades`, `get_open_trade_for_symbol`
- **Removed daily trade cap**: `max_trades_per_day` no longer gates new trades — adaptive sizing handles drawdown/loss-streaks
- **Rolling sessions**: Session cookie now refreshes on every HTTP request; active users never expire. Max age increased from 24 hours to 7 days

---

## [2026-06-08]

### Added
- **CSRF protection**: Session-based CSRF middleware validates `X-CSRF-Token` header on all POST/PUT/DELETE requests; exempt GET, WebSocket upgrades, and `/auth/callback`; token injected via `<meta>` tag in templates
- **Session secret validation**: Generates random secret on startup if default value detected, with CRITICAL log warning
- **Secure cookies**: `SameSite=Lax` and `Secure` flag (in production) on session cookies
- **Mobile position cards**: Responsive card layout on mobile with PnL/ROI display, SL/TP progress bars, and live WebSocket price updates
- **ROI column**: Added ROI percentage to desktop position table with live updates

### Changed
- **Settings redesign**: API keys now show as read-only masked view when configured; delete via confirmation modal + toast notification instead of page reload
- **Currency formatting**: Removed `$` signs from all price, PnL, balance, and equity displays globally

### Fixed
- **WebSocket auth bypass**: Fixed `/ws/{user_id}` allowing unauthenticated connections (changed `is not None and` to `is None or`)

### Removed
- **Legacy WebSocket endpoint**: Deleted `routes/ws.py` — zero-auth `/ws` endpoint that leaked all broadcast data

---

## [2026-06-06]

### Changed
- **Analytics redesign**: Drawdown + monthly PnL side-by-side, PnL by session/day with progress bars, hold time analysis, monthly PnL with year filter
- **Mode switching**: Now redirects to dashboard when switching between live and demo mode

### Added
- **Navigation progress bar**: Green bar at top of viewport during HTMX page loads with instant tab highlighting on mobile

### Fixed
- **Mobile scroll**: Enabled pull-to-refresh, fixed content clipped by bottom nav on Android gesture bar, backtester z-index overlap with topbar
- **Bottom nav polish** (5 iterations): Fixed padding, active state highlighting, hover override on touch devices, history back/forward support

---

## [2026-06-05]

### Added
- **Admin dashboard**: 5-page admin panel — platform overview, bot status with error tracking, user management (approve/reject/disable/toggle-admin), platform-wide analytics with leaderboard, filterable event logs
- **Landing page**: Public marketing page at `/` with strategy overview, stats, "how it works" diagrams, and CTA; dashboard moved to `/dashboard`
- **HTMX instant navigation**: SPA-like content swapping — only the page content replaces, sidebar/topbar/WebSocket persist across navigations
- **Trade data integrity pipeline**: Admin reconciliation endpoint against Binance data; self-healing after every trade close; background anomaly scanner every 5 minutes; full verification mode comparing all trades against actual Binance fills
- **Meta tags**: Open Graph image, Twitter card, favicon for link sharing

### Changed
- **Testnet removed**: Paper trading now uses local simulation instead of Binance testnet; `BINANCE_TESTNET` env var no longer used
- **SL/TP classification**: Position poll now uses candle high/low (not just close) with SL-priority check
- **EST/EDT handling**: Replaced hardcoded EST offset with `ZoneInfo("America/New_York")` for automatic DST
- **Hedge mode**: Auto-detection and `positionSide` parameter on all orders
- **Emergency close**: Now properly records exit_price, pnl_usdt, and commission
- **Platform equity**: Only counts live balances, not paper

### Fixed
- **Database race conditions**: Atomic upserts for candle buffer and bot state to prevent concurrent write crashes
- **Backtester speed**: Parallel loading and MIN/MAX query for date range; chart flicker eliminated on asset switch
- **Admin sidebar**: Overview tab no longer highlights on subroutes
- **Admin dashboard crash**: Fixed unsupported comma format in Jinja2

---

## [2026-06-04]

### Added
- **Paper trading (on-demand)**: Paper bot is now opt-in with modal popup on first demo switch; default mode changed to live
- **Email notifications**: Branded approval/rejection emails via Resend when admin manages users
- **Trade data accuracy**: Entry/exit prices and commissions now sourced from actual Binance fill data; SL/TP placement retries up to 3x with Telegram alerts on failure
- **Documentation**: Architecture, strategy, backtester, and order safety docs added to `docs/`

### Changed
- **Schema migrations**: Moved from Alembic to direct idempotent `ALTER TABLE` for Railway deployment reliability
- **App restructure**: Split monolithic `database.py` into `app/db/` package; auth to `app/auth/`; bot to `app/bot/`; added paper trading, user management, admin panel, per-user bot workers

---

## [2026-06-03]

### Added
- **ADX trend filter**: Skips counter-trend trades when ADX(42) > 35; improved backtest from +938% to +1079%, reduced max DD from 31.8% to 28.5%
- **Skip periods**: April weeks 2 and 4 now skipped (15% win rate over 6 years); combined result: 734 trades, +1173%, 24.6% max DD
- **Manipulation minimum filter**: Wick must break at least 0.4x ATR beyond accumulation boundary
- **Equity simulation**: Full capital growth model with adaptive sizing, commission on leveraged positions, and drawdown tracking
- **Combined backtester view**: Aggregate stats across all three assets with combined equity curve chart

### Changed
- **Backtester params locked**: Now always uses live bot config; removed interactive parameter sliders
- **Strategy description**: Replaced specific parameter displays with narrative prose to protect trading edge
- **Dashboard revamp**: Seeded trades from backtest results, equity-based max drawdown (24.6%), year PnL instead of today PnL
- **Dynamic data range**: Backtester shows actual date range from DB instead of hardcoded "2020-2025"
- **Railway IPs**: Added to onboarding and settings for Binance whitelist

### Removed
- Bot configuration section hidden from settings page (moved to admin)
- Bot events log hidden from alerts page

---

## [2026-06-02]

### Added
- **Manipulation minimum filter**: Configurable via `manip_min_mode` and `manip_min_val` in strategy params

---

## [2026-06-01]

### Changed
- **Local timezone**: All timestamps converted to user's browser timezone across the entire app
- **Position page layout**: Chart 65/35 split with open positions below, order book upgraded to 20 levels
- **Deploy alerts silenced**: Bot start alerts disabled; stop alerts suppressed during clean deploys
- **Strategy update**: BTC now uses body-based accumulation (improved from +21R to +94R); adaptive sizing threshold changed from 8→4 losses, recovery from 3→2 wins

---

## [2026-05-31]

### Added
- **Multi-user authentication**: Google SSO via authlib, per-user encrypted Binance API keys (Fernet), session-based auth with approval gate
- **UI redesign**: Complete overhaul with ZENITH brand, sidebar rail navigation, dark terminal aesthetic, custom CSS design system
- **Position page**: Binance-style 3-column layout with order book, candlestick chart (LightweightCharts v4.2.0), market trades feed
- **New pages**: Analytics, alerts, backtester (with chart visualization), track record, position
- **Backtest engine**: `amd_engine.py` — standalone strategy engine with Wilder ATR, sweep zones, session/month filtering, smart SL

### Fixed
- **Bot startup crash**: Reduced CANDLE_BUFFER_SIZE from 2000 to 1000 (Binance limit)
- **OAuth redirect**: Uses HTTPS when behind reverse proxy
- **Backtester undefined**: Added null checks when no historical data exists

---

## [2026-05-27]

### Fixed
- **Leverage**: Now explicitly set to 5x on startup (was using Binance default 20x — 4x more exposure than intended)
- **Lot sizing**: Rounds to exchange LOT_SIZE step with `math.floor` (was sending invalid quantities)
- **Order safety**: SL/TP orders use `reduceOnly` instead of `closePosition` (prevents accidental reverse positions)
- **Candle save performance**: Batch INSERT replacing N+1 individual queries

---

## [2026-05-26]

### Added
- **Initial release**: AMD FVG 15m trading bot with web dashboard, Binance Futures integration, PostgreSQL storage, real-time WebSocket updates
- **Telegram alerts**: Trade entry/exit notifications with balance display, split between public channel and private owner chat
- **Dual-client architecture**: Live Binance for market data, testnet for trading (real prices, fake money)

### Changed
- **Settings read-only**: Removed ability to change bot settings from web UI
- **Branding**: Renamed from "AMD FVG Bot" to "Trading Futures Bot"

### Fixed
- **Starlette compatibility**: Updated TemplateResponse API for newer versions
- **WebSocket protocol**: Auto-detects `wss://` when served over HTTPS
