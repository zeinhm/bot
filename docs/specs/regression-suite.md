# ZENITH Baseline Regression Suite — Specification

**Purpose:** A permanent test suite covering the critical money-and-access paths of the
platform. It runs after EVERY feature implementation; any failure means the change broke
existing behavior and must not ship.

**Instructions for the implementer:** Build these tests under `tests/regression/`.
Use `pytest` + `pytest-asyncio`. NEVER call the real Binance API — mock `AsyncClient` /
`BinanceExchange` everywhere. Use a scratch/SQLite-or-throwaway Postgres DB via fixture,
never the production DB. Do not modify any application code to make a test pass — if a
test exposes a real bug, STOP and report it instead. Every test must assert exact values,
not just "no exception".

After building, add to the tester agent's mandatory checks:
`pytest tests/regression/ — any failure is automatic FAIL.`

---

## R1 — Auth & approval gate (`app/auth/service.py`, `routes/auth.py`, `routes/admin/users.py`)

- R1.1 An unauthenticated request to any protected route (e.g. `/dashboard`) is rejected/redirected — never renders content.
- R1.2 A user with pending (unapproved) status cannot reach the dashboard; they land on the pending-approval screen.
- R1.3 An approved non-admin user CANNOT access any `/admin/*` route.
- R1.4 Admin approve → user gains access; admin disable → an active session/user loses access.
- R1.5 When the require-approval toggle is OFF, Google callback auto-approves; when ON, new users are pending.

## R2 — API key handling (`app/auth/service.py`, `exchange.py::validate_api_key`)

- R2.1 Fernet round-trip: `decrypt(encrypt(key)) == key`; the stored value is NOT the plaintext.
- R2.2 `validate_api_key` returns `ok=False` with the withdrawal-specific error when the key has `enableWithdrawals=True` (mock `get_account_api_permissions`). This is a hard security invariant.
- R2.3 `validate_api_key` maps error codes correctly: `-2015` → IP/futures message, `-2014`/`-1022`/signature → invalid key/secret message.
- R2.4 Key delete triggers `reset_live_balance()` path (no stale balance served after disconnect).

## R3 — R-value semantics (`exchange.py::r_value_for_exit`)

- R3.1 `is_sl=True` → exactly `-1.0` regardless of prices.
- R3.2 Win with `fallback_r=3.0` → exactly `3.0` (planned RR, NOT geometry).
- R3.3 Win with `fallback_r=None` and valid prices → geometry `|tp-entry|/|entry-sl|` rounded to 2dp.
- R3.4 The `-1.0 or 2.0` truthiness trap: a loss must never come back as `2.0`. Explicit test.
- R3.5 Degenerate prices (zero sl_dist, None fields) → falls back to `2.0` without raising.

## R4 — Adaptive sizing state machine (`app/bot/worker.py::_on_trade_result`, `_get_effective_risk`)

- R4.1 Exactly 4 consecutive losses (default threshold) → `adaptive_active=True`; 3 losses → still False.
- R4.2 While active, `_get_effective_risk` returns the reduced pct (0.25); while inactive → None (normal risk).
- R4.3 Exactly 2 consecutive wins while active → deactivates and resets counters; 1 win → still active.
- R4.4 A win resets `current_streak` to 0 even when not active.
- R4.5 Per-strategy isolation: losses on `amd_15m` never move `trend_5m`'s streak, and each strategy uses its own thresholds with fallback to BotConfig defaults.
- R4.6 An `adaptive_sizing` WebSocket broadcast fires on both activation and deactivation with correct `streak`/`wins_needed` payloads (capture via mock broadcast_fn).

## R5 — Position sizing & PnL math (`app/bot/worker.py`, `backtest_combine.py::simulate`)

- R5.1 Position value = `(equity * risk_pct) / sl_dist * entry_price` — assert an exact number for a fixed input set.
- R5.2 Commission = `position_value * COMMISSION_PCT * 2` (round trip) — exact number.
- R5.3 With adaptive active, the same trade sizes at 0.25% instead of 2% — exact ratio 1:8.
- R5.4 Zero sl_dist is skipped, never divides by zero.

## R6 — Trade exit resolution (`exchange.py::resolve_trade_exit`)

Mock `futures_account_trades` fill lists. All assertions on returned dict values.

- R6.1 Standard case: entry fill + one closing fill on the opposite side → correct `exit_price`, `exit_qty`, summed `exit_commission`, `realized_pnl`.
- R6.2 Multiple partial closing fills → weighted-average exit price, quantities accumulated only up to entry qty.
- R6.3 Entry order's own fills are excluded (same orderId as entry must not count as exit).
- R6.4 `is_sl` decided by proximity: exit price near sl → True, near tp → False.
- R6.5 Hedge mode: fills with wrong `positionSide` excluded; `BOTH`/absent tolerated.
- R6.6 Empty fills / API error → returns None (caller fallback), never raises.

## R7 — Order safety invariants (`app/bot/worker.py::_place_sl_tp`, `_execute_trade`; mock exchange)

- R7.1 `_place_sl_tp` cancels all orders (BOTH buckets) before placing — assert cancel called before place.
- R7.2 SL/TP order IDs stored from `algoId` when present, falling back to `orderId` — no KeyError on algo responses.
- R7.3 Placement retries: independent 3x per order; SL success + TP failure retries only TP.
- R7.4 On exit (user-stream fill of STOP_MARKET/TAKE_PROFIT_MARKET), remaining orders on the symbol are cancelled and the trade row is closed with correct result (`STOP_MARKET`→loss, `TAKE_PROFIT_MARKET`→win).
- R7.5 One-trade-at-a-time: with an active trade id set, a new signal does NOT place an order.
- R7.6 Force-close breach gate fires ONLY when all conditions hold (strict conditional fetch confirms no SL, re-place failed, price past SL, unrealized_pnl ≤ −0.9R) — test each condition individually blocking the close.

## R8 — Cross-strategy gating (`backtest_combine.py::gate`)

- R8.1 Two setups on the same symbol with overlapping time windows → only the earlier is taken.
- R8.2 Overlapping setups on DIFFERENT symbols → both taken.
- R8.3 A setup entering exactly at another's exitTime is allowed (boundary semantics preserved).

## R9 — Paper/live isolation (`worker_paper.py`, `worker_live.py`, `app/db/queries/paper.py`)

- R9.1 PaperWorker never touches `BinanceExchange` order methods (mock and assert zero calls).
- R9.2 Paper trades write rows with `is_paper=True`; live queries (dashboard stats, platform equity) exclude them.
- R9.3 Platform equity counts live balances only.
- R9.4 `make_worker()` returns the correct subclass per mode.

## R10 — Emergency close (`routes/settings.py` / `routes/position.py`)

- R10.1 Emergency close records `exit_price`, `pnl_usdt`, and `commission` on the trade row (non-null, correct values from mocked fills).
- R10.2 All orders on the symbol are cancelled after the close.

## R11 — CSRF & mutation protection (`app/middleware/csrf.py`)

- R11.1 A POST without `X-CSRF-Token` is rejected (403) on a representative mutating endpoint.
- R11.2 Same POST with the session token succeeds.
- R11.3 GET routes are unaffected.

## R12 — WebSocket contract (`app/bot/websocket.py`, `templates/base.html`)

- R12.1 Static check: every message `type` emitted by the server (`price`, `balance`, `heartbeat`, `bot_status`, `position`, `positions`, `adaptive_sizing`, `trade_opened`, `trade_closed`, `orderbook`, `agg_trade`) has a matching `ws.on("<type>"...)` registration in client JS. Parse both sides and diff the sets — catches a renamed/added type with no handler.

## R13 — Migrations (`alembic/`)

- R13.1 `alembic upgrade head` then `downgrade -1` then `upgrade head` succeeds on a scratch DB.

## R14 — Research baselines (`research/harness.py`, `research/overlay.py`, `backtest_combine.py`)

Both marked `@pytest.mark.slow` (excluded from the default run; the Stop hook also covers these).

- R14.1 The 15m harness alone (`Harness.create(interval="15m")` + `run_all()`, no overrides) reproduces exactly **755 trades / $183,632.75**.
- R14.2 The combined production seed — 15m + 5m trend overlay (`CFG5M`), cross-strategy gating via `backtest_combine.gate`, per-strategy adaptive streaks over one wallet, mirroring `research/gen_trade_log.py` — reproduces exactly **886 trades / $383,441.27**.
- R14.3 Failure semantics: R14.1 broken = harness/engine regression; R14.2 broken while R14.1 passes = gating/overlay/combine regression. The test failure message must say which.

---

## Definition of done

- All tests pass locally via `pytest tests/regression/ -m "not slow"`.
- Zero real network calls (assert via a no-network fixture or socket guard).
- Zero writes to the production database.
- No application code modified.
- A `tests/regression/README.md` listing each R-group and what invariant it protects.
- Report any test that FAILS against current code as a candidate real bug — do not adjust the test to pass.
