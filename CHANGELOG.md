# Changelog

All notable changes to the ZENITH Trading Bot platform.

Format: [Keep a Changelog](https://keepachangelog.com/). Grouped by date and feature area.

---

## [2026-06-16]

### Added
- **Adaptive reward-to-risk (2:1 / 3:1 via 6h trend regime).** Take-profit is now adaptive: **3:1 when the previous completed 6h candle's ADX(14) ≥ 40** (strong directional trend), otherwise **2:1**. Validated via rolling/maximin walk-forward (beats fixed-2:1 out-of-sample with lower drawdown; the 6h/ADX-40 config never had a losing year). New `STRATEGY_PARAMS`: `dynamic_rr=True`, `rrr_trend=3.0`, `rrr_range=2.0`, `htf_hours=6`, `htf_adx_period=14`, `htf_adx_threshold=40`.
  - **Backtest engine** (`amd_engine.py`): new `_htf_trend_mask()` (resamples 15m→6h, ADX, previous-completed-bar, no lookahead); `run()` picks RR per entry when `dynamicRR` is on; each setup carries `regime`/`rrrUsed`. `compute_stats`/`simulate_equity` already derive R per trade, so they handle variable RR unchanged.
  - **Live** (`app/bot/shared_market.py` `get_trend_regime()` + `app/bot/worker.py _process_candle`): fetches Binance 6h klines (fail-safe → 2:1 on error), sets `params["rrr"]` per regime; `dynamic_rr_enabled` state key is a kill-switch. Real-money default is **on**.
  - **Trade log**: new `target_rr` column on `trades` + `backtest_results` (migration `f6b2d4e8a1c9`; also in `_ensure_schema`); backtester and shared live trade tables show a **Target** column (2:1 / 3:1).
  - **Re-seeded**: 755 trades, 41.3% win rate, +1,736% ($183.6k from $10k), 19% max DD; ~17% of trades hit the 3:1 trend regime.
  - **Copy refreshed** across landing page, settings, backtester, and docs (`strategy.md`, `backtester.md`, `codebase-map.md`) — no stale "2:1-only" claims remain.

### Changed
- **Backtester Trade Log now syncs to the Playground.** Previously the Trade Log was baked once from the fixed default-param seed and never changed when you moved the risk/sessions/seasonal knobs (only the stats bar + chart updated). Now changing any Playground knob re-renders the Trade Log too — the actual param-adjusted, all-assets-combined trade list with recomputed Target/R/PnL/Equity. New `GET /api/backtest/tradelog` (`routes/backtester.py`, cached per param combo like `/combined`) returns the per-trade ledger from an extended `amd_engine.simulate_equity(..., with_trades=True)` — same adaptive-sizing path as the equity curve, so the log, stats, and curve always agree. Frontend: the trade-log IIFE is now API-driven (`setRows` + `window.__btReloadTradeLog`), refreshed from the chart IIFE's `btReload()` and once on load. Validated against the local DB: default = 755 trades / $183,632.75 / 19% DD; 3% risk keeps 755 trades and scales to $657k.
- **Backtester Playground layout** (`templates/backtester.html`): on **desktop** the Playground card is now capped to the chart column's height (`max-height` synced via JS to `#bt-results-col`, re-synced on resize) and scrolls internally, so its bottom aligns 1:1 with the chart instead of running far past it. On **mobile** (≤767px) the layout is unchanged except the **strategy explanation** (Strategy / Risk / Filters / Adaptive Sizing) now collapses behind a single **"Strategy explanation"** toggle (one expander for the whole block, collapsed by default); the risk controls above stay visible. Desktop always shows the full text.

## [2026-06-15]

### Added
- **Interactive "How it works" section** (`landing-page/`): the `#how` section is now a pinned, scroll-driven canvas that builds a full AMD trade as you scroll — accumulation range → liquidity sweep → FVG + entry → SL/TP execution, the four step cards lighting up in sync. The chart header shows only the current step label (e.g. "03 · FVG confirms — entry fires"). New `amd-scroll.js` (vanilla JS + `<canvas>`, no libraries/build step); styles appended to `landing-page.css` (`.amd-*`, cache-bust `?v=4`); markup replaces the old static four-card grid. Desktop (>980px) gets the pinned side-by-side effect (steps left, chart right). Mobile (≤980px) reflows to a **card-deck**: the chart stays pinned below while the four phase cards stack on top — the active card in front, the rest peeking behind, each scroll step sending the front card back and bringing the next forward, so the explanation and chart are always on screen together (`amd-scroll.js` sets a per-card `--depth`/`z-index`; CSS animates the stack). `prefers-reduced-motion` falls back to a plain static stack. The strategy filter chips (liquidity-sweep / trend / seasonal / sessions) are preserved as a section directly below.

## [2026-06-14]

### Added
- **Account — API-key validation (Phase 4a)**: `exchange.validate_api_key()` validates a Binance key before saving — confirms it works + Futures is enabled, and **rejects keys with withdrawals enabled** (security). Gates both save paths (Settings `/settings/api-keys` and onboarding `/setup`); bad keys never persist or start the bot. Permission flags (Reading / Futures / Withdrawals / IP-restricted) are stored on save and shown as pills when connected. New `POST /settings/api-keys/validate` (no-persist) for inline/wizard validation. The Settings form is now "Validate & Connect" with an inline error; `/setup` shows validation errors.
- **Account — TOTP two-factor auth (Phase 4b)**: opt-in **step-up** 2FA. New `app/auth/twofa.py` (pyotp) + `routes/twofa.py` (`/settings/2fa/enroll|verify|disable|challenge` + `/backup-codes/regenerate`). Once enabled, `require_2fa` gates the non-urgent high-value actions — **settings save/reset, API-key save/delete, live bot start/stop** — re-verified every 15 min. **Emergency close is intentionally not gated** (time-critical; the confirm dialog is the safeguard). Global 2FA challenge modal + `guardedFetch` in `base.html` auto-prompt on `401 twofa_required` and retry once. Settings has a new **Security tab** for the 2FA card (enable → QR + copyable key → verify → enabled; disable; recovery-code status + regenerate). **Brute-force protection:** 5 failed code attempts / 5 min → `429`, per user.
- **2FA recovery**: **one-time backup codes** (10, sha256-hashed, single-use) generated at enrollment, shown once with Copy/Download; accepted anywhere a TOTP code is (challenge/disable/regenerate); "N remaining" shown with a regenerate option. Plus an admin **Reset 2FA** action on the user-detail page (clears 2FA + codes for a lost device).
- Schema: nullable `users.totp_secret_enc` / `totp_enabled` / `totp_backup_codes` via `_ensure_schema` + alembic `e5a1c2d3f4b7`. New deps `pyotp`, `qrcode`. 2FA is opt-in, so existing users are unaffected.

## [2026-06-13]

### Changed
- **Bot Control + backtester refined** (supersedes parts of the 2026-06-12 Bot Control entry):
  - **Reward:Risk locked at the validated 2:1** — the 2:1/3:1 toggle is gone in both Bot Control and the Playground; the bot always uses 2:1.
  - **Risk per trade is dynamic-% only**, shown as a **1–3% slider** (was static $ / dynamic % up to 5%). Hidden `risk_mode=dynamic`; Reset → 2%.
  - **Leverage removed from the backtester** — position sizing is risk-%-based, so leverage never affects backtest PnL (it stays in Bot Control, where it gates live margin).
  - **Seasonal filter split into two independent, clearly-named event toggles** — **"Sell in May"** (`skip_may` → skip May) and **"US tax deadline"** (`skip_tax_deadline` → skip the April tax weeks) — replacing the single `seasonal_filter` key and the curve-fitting-sounding "skip historically underperforming periods" copy (now named events) everywhere (settings, backtester, landing chip/FAQ, docs). Worker + backtester (`_parse_setup_overrides` → `skip_may`/`skip_tax`) updated. Verified both-on reproduces the validated **771** trades; each toggle independently adds trades when off.
  - **Removed the broken "max trades" rule** — it defaulted to 99 and failed the 1–10 check, erroring on every settings save.
- **Settings UX**: save-confirmation modal; **Copy IPs** button (space-separated one-liner); equal-width Save/Reset buttons; install card no longer claims "offline-ready".
- **Landing polish**: green accent on the closing phrase of five section headings; install heading stays on one line on desktop; trimmed/smaller install subtitle; `?v=2` cache-bust on the landing CSS/JS.

### Added
- **App favicon** (green "Z") on every page — `static/icons/favicon.svg`, linked from `base.html` and the six standalone templates (login/setup/pending/rejected/track-record/404). The app pages previously had no favicon.

## [2026-06-12]

### Changed
- **Settings page tabbed**: Bot Control and Exchange are now two tabs (the active one persists via `localStorage`), while the **Install App** card and **Danger Zone** sit below, outside the tabs (always visible). New `.settings-tabs`/`.settings-tab` component; `app.css` bumped to `?v=14`.

### Added
- **PWA install prompts**: A dedicated **Install** section on the landing page (`#install`, with nav + footer links) and an **Install App** card in Settings. Both capture the browser's `beforeinstallprompt` (Chrome/Edge/Android) and offer one-click native install. The landing section uses a **device-aware tabbed layout** — Android / iOS / Desktop — that **auto-selects the tab from the visitor's device** (UA + iPadOS touch detection) and shows the matching step-by-step instructions, mirroring a clean install-UX pattern in the ZENITH green theme. The Settings card handles iOS "Add to Home Screen" steps and an unsupported-browser fallback. Both **hide entirely when the app is already installed** (running in `display-mode: standalone`) — the landing also hides its nav/footer Install links. The landing page links the manifest + registers the service worker so it's installable directly from `/`.
- **Bot Control (per-user risk settings UI)**: Re-introduced user-facing bot controls (the settings form + backtester sliders that were locked away on 2026-06-03), but scoped strictly to the **risk/personal knobs** — the strategy's detection logic stays fixed and hidden to protect the edge.
  - **Settings → Bot Control card** (`templates/settings.html`): master enable toggle, risk mode ($ static / % dynamic 1–5%), Reward:Risk (2:1 / 3:1), **leverage** (1–20), active **sessions** (Sydney/Tokyo/London/NY), **seasonal filter** toggle (skip May + April W2/W4), and symbol selection — plus a **Reset to default** button (`POST /settings/reset`). Scoped per `(user, mode)` via `bot_state`; saves via fetch+CSRF. New `.chip-toggle` component in `app.css` (bumped `?v=13`).
  - **Leverage + seasonal filter are now per-user.** Previously global (`config.LEVERAGE` / `STRATEGY_PARAMS['skip_months']`). `BinanceExchange` takes a `leverage` and gains `set_leverage()` (applied on connect from `bot_state`, and live on settings-save without a restart); `worker._process_candle` reads `seasonal_filter` from state and injects the skip months/weeks into the per-candle params (same pattern as `rr_ratio`/`sessions`). **Behavior-preserving: unset state = today's exact defaults.** Verified against local history — seasonal **on** reproduces the validated **771** trades, **off** yields 868.
  - **Backtester playground** (`/backtester`): a **Playground** panel exposes the same five risk knobs (RR, risk %, leverage, sessions, seasonal) + Reset. Each combo folds into the existing result-cache signature (`routes/backtester.py` `_build_cfg`/`_equity_cfg`/`_parse_setup_overrides`), so the engine runs once per combo then serves from cache — no perf regression. RR/sessions/seasonal change the setups; risk % only the equity sim; leverage is informational (sizing is risk-based). The locked detection params remain read-only prose. Verified: default reproduces BTC 360 trades; rrr/sessions/seasonal shift results as expected.

### Added
- **PWA (installable + offline shell)**: ZENITH is now an installable Progressive Web App. New `static/manifest.webmanifest` (standalone display, `start_url=/dashboard`, theme `#0F1117`, brand-green "Z" icons at 192/512 + maskable in `static/icons/`) and a `static/sw.js` service worker registered from `base.html`. The SW is **network-first for page navigations** (live trade data never goes stale) and **cache-first for `/static` + `/landing` assets**, and explicitly never intercepts `/api/*`, `/ws*`, `/auth/*`, `/bot/*`, or `/push/*`; offline navigations fall back to the cached page (or `/dashboard`). Served via a new root-scoped `GET /sw.js` route (`Service-Worker-Allowed: /`) so it can control the whole site. The SW also ships ready-to-use `push`/`notificationclick` handlers for the upcoming Web Push feature. Second entry in the missing-features build plan
- **Landing page FAQ**: New expandable FAQ section on the marketing page (`#faq`, between Performance and Risk) with five edge-safe entries — *What is ZENITH Bot? · How does the strategy work? · Why Binance Futures? · Is it secure? · Who is this for?*. Built on native `<details>` (works with no JS) and progressively enhanced to a single-open accordion (opening one closes the others) with a rotating chevron + slide-in animation; ties into the existing scroll-reveal. Added `FAQ` links to the nav and the footer Product column. New `.faq`/`.faq-item`/`.faq-q`/`.faq-a` styles in `landing-page.css` reuse the existing token set. Copy is drawn from the public strategy summary and the security posture (encrypted keys, Futures-only/withdrawals-off, IP whitelist) — no detection parameters exposed. First entry in the **missing-features build plan** (`~/.claude/plans/expressive-sauteeing-whisper.md`)

## [2026-06-11]

### Fixed
- **Railway deploy failure (not a code issue)**: a railpack builder upgrade broke deploys two ways. (1) It defaulted to Python `3.13.14`, which has no precompiled mise binary, failing the build — pinned `.python-version` to `3.13.13` (the exact version the last working deploy used; only `.14` lacks a binary). (2) It began actually running the Procfile's `alembic upgrade head &&` prefix, which fails on prod: prod has **never** had an `alembic_version` table (the schema is built by `create_all` + `_ensure_schema`, not alembic), so `alembic upgrade head` tries to create the base schema over existing tables and errors out before `uvicorn` starts. Dropped the alembic prefix from the start command (Procfile + nixpacks) — `create_all` creates the two new `backtest_*` tables on boot, matching how prod has always been managed

### Added
- **Backtester result cache + lazy setup pagination** (`docs/backtest-cache-plan.md`):
  - **Step 1 — cache.** `amd_engine.run` used to re-simulate ~210k bars on **every** `/api/backtest` call and **3×** on every `/api/backtest/combined` call (≈4 full sims per page load). Results are now cached by a `(strategy, params, candle-data fingerprint)` signature — two new tables `backtest_runs` + `backtest_setups` (migration `d4f9b2c1a8e3`) + an in-memory hot layer. The engine runs **once** per combo, persists (survives Railway redeploys), and the data fingerprint (`first_ts, last_ts, count`) auto-invalidates on candle import. `/combined` is itself a cached `symbol="COMBINED"` row. New queries: `get_backtest_run`, `save_backtest_run` (concurrency-safe on the unique signature), `get_run_setups_all/page/range`, `get_historical_candle_count`. Builds are serialized per signature with an `asyncio.Lock` so the page's concurrent `/api/backtest` + `/combined` + `/setups` requests don't each run the engine or race on the write (old runs are not pruned — deleting a `run_id` still referenced elsewhere dropped its setups and corrupted the combined total).
  - **Step 2 — pagination.** `/api/backtest` now returns `{stats, total}` (no setups); a new `/api/backtest/setups?symbol&interval&[from&to]|[offset&limit]` serves setups by time range (chart) or newest-first page (nav), each carrying its `_ordinal`. The backtester chart loads only the **newest 10 setups** on open and lazy-loads older pages as you navigate `‹` or scroll left (alongside the candle lazy-load) — instead of shipping all ~360. The setup counter shows the true `X / total`; `ensureLoaded` remains the safety net.

### Changed
- **Backtester loads candles in one call instead of two**: on page load (and asset switch) it used to fetch the most recent 2000 candles and then back-fill ~5000 older ones to bring the most-recent setup (which it auto-jumps to) into view. Now it fetches `/api/backtest` first, then makes a **single** candle request anchored at the last setup (`end = setup.exit + 30 bars`, limit 2000); `ensureLoaded()` remains the safety net. De-duplicated the setup-mapping/stats logic into `mapSetups()` / `applyBtStats()` / `loadCandlesForSetups()`. Trade-off: the chart no longer preloads candles to the right of the last setup (scrolling left still lazy-loads)

### Fixed
- **Trade History filters rendered broken**: the direction (All/Long/Short) and result (All/Wins/Losses) segmented toggles collapsed into unstyled text ("AllLongShort") because `.seg` only styled `<button>` while the filters use `<a>`. Extended `.seg` styling to `.seg a` and removed the inline `color:inherit` that fought it — they now render as proper padded pills with the active segment highlighted

### Added
- **Trade History filter toolbar + pagination**: moved the filters from the page header into a toolbar at the top of the table, added a **Symbol** filter (All/BTC/ETH/SOL) and a **Reset** button, and added server-side **pagination** fixed at **10/page** with a first/prev/numbered/next/last pager (`« ‹ 1 2 3 › »`, matching the backtester Trade Log) + an `X–Y of Z` range. Stat cards still reflect the full filtered set; the table shows one page. Extended `.pager` styling to links (was `<button>`-only). The backtester **Trade Log** table also now defaults to **10/page** (was 25)

### Changed
- **Analytics page overhaul**:
  - Removed the left accent border on the stat cards; P&L numbers (Monthly net, P&L by Session) use comma separators + 2 decimals (were rounded to whole numbers on prod)
  - **Monthly P&L** bars no longer print a value label on top — hovering a bar shows a tooltip with the month's P&L, total trades, and W/L
  - **Removed P&L by Day** (low signal / curve-fit risk) and added **Longest Streak** (longest consecutive win and loss runs, chronological by close) as a 5th card in the top stat row (`9W / 8L`)
  - **Hold Time Analysis** now shows Average / Fastest / Longest split by Winners & Losers (neutral colors, no progress bars; the "held N× longer" insight line was removed)
  - **P&L by Session** always lists every session (NY, London, Sydney, Tokyo) even with 0 trades; "NY" renders uppercase; the card title has a tooltip noting trades are grouped by the session they were **opened** in (entry time). Session + Hold Time sit side-by-side below
- **Equity curve anchored to the real balance**: the dashboard equity curve no longer starts at a hardcoded `$10,000` — it's anchored so the curve **ends at the user's actual balance** (start = balance − all trade PnL; falls back to `last_balance`, then a flat baseline). Drawdown % uses the same real-balance equity. Chart hover/header use the first data point as the baseline (not a fixed 10k), show 2-decimal precision, and default to the current equity + total %. Title simplified to just **"Equity Curve"**. This Year P&L shows `+7,671.94 USDT` (no `$`, comma separators, USDT unit)
- **Shared trade table**: extracted the Trade History table into a partial `templates/_trade_table.html` (Pair + leverage pill, Open/Closed dates, Entry/Closed/Size, R/ROI/Commission/Funding Fee/Nett PnL, comma separators) used by both Trade History and the dashboard's Recent Trades so they can't drift. Shared `num` Jinja filter moved to `app/core/template_filters.py`. The **backtester Trade Log** now matches the same look (Pair + pill, Open/Closed dates, result pills, comma separators, R tooltip), keeping its Equity column and omitting Commission/Funding/Size/ROI (no backtest data for those)

## [2026-06-10]

### Fixed
- **Mobile: page header overlapped by content**: the sticky topbar and the position page's `.ticker-bar` were both `z-index: 50`, so on scroll the ticker-bar painted over the topbar. Bumped the mobile topbar to `z-index: 100` (still below modals/nav-progress)

### Added
- **Tooltips on R**: hover explainer on the R column header and the Total R stat card (reusable `[data-tip]` on `th`/`.lbl`); lighter tooltip background (`--card-2`)

### Added
- **Income-based net PnL (matches Binance Position History to the cent)**: Trade PnL is now sourced from Binance's **income ledger** instead of being computed from prices. New `exchange.position_pnl_breakdown(client, symbol, entry_time, exit_time)` sums `REALIZED_PNL + FUNDING_FEE + COMMISSION` over the position window, converting BNB-paid commission to USDT via the BNB price at the fee time (matching how Binance displays it). Verified against the live account: position #782 → net **2.64** / ROI **10.40%**, exact match. Wired into `LiveWorker._self_heal_trade`, the `websocket.py` anomaly scanner, and admin reconcile, so `pnl_usdt` becomes the **net** realized PnL, `commission` the USDT trading fee, and the new `funding_fee` column the funding. The previous price-derived PnL (which also mishandled BNB fees) is removed. **`entry_time`/`exit_time` are now also sourced from the actual Binance fills** (`resolve_trade_exit` returns the close-fill time; `exchange.fills_time()` reads the entry-fill time) — previously they recorded when the *bot* acted/detected the close (~10s late), now they match Binance's fill timestamps to the second. Trade History shows the net P&L plus visible **Commission**, **Funding Fee**, and **ROI** columns. New `trades.funding_fee` column (migration `c3e8a1f2b4d6` + `_ensure_schema`)

### Changed
- **Trade History UI**: Pair moved to the first column with a leverage pill (e.g. `5x`); Date shows both open and close (with seconds); Exit column renamed to Closed; added Size (base-asset volume), ROI, Commission, and Funding Fee columns; comma thousand separators on all numbers; removed the `$` sign, the left win/loss row border, and the ID column

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
