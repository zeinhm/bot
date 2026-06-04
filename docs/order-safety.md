# Order Safety & SL/TP Protection

How the bot handles SL/TP orders across all scenarios to prevent unprotected positions and duplicate orders.

---

## Order Types Used

| Order | Binance Type | Fill | Fee |
|-------|-------------|------|-----|
| Entry | `MARKET` | Immediate | Taker |
| Stop Loss | `STOP_MARKET` + `reduceOnly` | Market order when trigger price hit | Taker |
| Take Profit | `TAKE_PROFIT_MARKET` + `reduceOnly` | Market order when trigger price hit | Taker |

All SL/TP are **server-side on Binance** — they execute regardless of whether the bot is online.

All orders are **scoped per symbol** — operations on BTCUSDT never affect ETHUSDT orders.

---

## Trade Lifecycle

### 1. Entry

```
Signal detected
  → Cancel all existing orders on symbol (cleanup stale orders)
  → Place MARKET entry order
  → Save trade to DB
  → Place SL/TP (cancel all orders first, then place both)
```

**Handler:** `_execute_trade()` → `_place_sl_tp()`

### 2. SL/TP Placement

```
Cancel all existing orders on symbol (prevent duplicates)
  → Place SL (STOP_MARKET) — retry up to 3x
  → Place TP (TAKE_PROFIT_MARKET) — retry up to 3x
  → Save order IDs to DB
```

Each order retries independently — if SL succeeds but TP fails on attempt 1, only TP retries on attempt 2.

**Handler:** `_place_sl_tp()`

### 3. Exit via User Stream (normal path)

When Binance fills a SL or TP order, we receive the event via WebSocket:

```
STOP_MARKET or TAKE_PROFIT_MARKET filled
  → Calculate PnL
  → Update trade in DB (result, exit price, PnL)
  → Cancel ALL remaining orders on symbol (not just the counterpart)
  → Clear active trade
```

**Handler:** `_on_order_update()`

### 4. Exit via Position Poll (fallback)

Every 30s, checks if the position still exists on Binance. Catches cases where the WebSocket missed a fill event:

```
Position gone but trade still open in DB
  → Determine win/loss from last price
  → Update trade in DB
  → Cancel ALL orders on symbol
  → Clear active trade
```

**Handler:** `_run_position_poll()` (position gone branch)

---

## Safety Mechanisms

### A. Crash Recovery (on startup)

Runs once when the bot starts. Handles the case where the bot crashed mid-trade.

```
Check DB for open trade:
  → If trade exists but NO position on Binance:
      Query Binance for actual fill data (price, commission) from SL/TP orders
      Record accurate exit price, PnL, and commission
  → If trade exists AND position exists:
      Check if SL/TP orders exist on Binance
      If missing → cancel all orders → re-place both SL and TP
  → If NO trade but position exists on Binance:
      Close the orphan position with market order
      Cancel all orders on symbol
```

**Handler:** `_crash_recovery()`

### B. SL/TP Health Check (continuous, live trades only)

Runs inside the position poll loop. Detects SL/TP orders that were placed but later disappeared (Binance glitch, manual cancellation, etc.).

```
Position exists AND trade is open:
  → Query open orders on symbol
  → If SL missing → place only SL
  → If TP missing → place only TP
  → If re-placement fails:
      Poll interval drops from 30s → 10s
      After 60s of continuous failure:
        Send Telegram alert: "Failed to place SL/TP — check position manually"
        Repeat alert every 60s until resolved
  → If both present → reset to normal 30s polling
```

**Handler:** `_run_position_poll()` (position exists branch)

### C. Pre-Entry Cleanup

Before every new entry, cancel all existing orders on the symbol. Catches stale orders from previous trades that weren't properly cleaned up.

**Handler:** `_execute_trade()` (before market order)

### D. Pre-SL/TP Cleanup

Before placing SL/TP, cancel all existing orders on the symbol. Prevents duplicate SL/TP stacking.

**Handler:** `_place_sl_tp()` (before placement)

---

## Risk Windows

| Scenario | Duration | Mitigation |
|----------|----------|------------|
| Entry placed, SL/TP not yet placed | ~300-1000ms | Retry 3x, crash recovery on restart |
| Bot crashes during trade | ~30-60s (Railway restart) | Crash recovery re-places SL/TP |
| SL/TP placement keeps failing | Ongoing | 10s polling + Telegram alert after 60s |
| Binance overload (SL delayed) | Seconds to minutes | Server-side order still in queue, position sizing limits damage |

---

## Duplicate Order Prevention

Every path that places orders cancels existing orders first:

| Path | Cancels before placing? | Cancels on exit? |
|------|------------------------|-------------------|
| Normal entry | ✅ `cancel_all_orders` before entry + before SL/TP | — |
| Crash recovery | ✅ `cancel_all_orders` before re-placing SL/TP | — |
| Exit via user stream | — | ✅ `cancel_all_orders` |
| Exit via position poll | — | ✅ `cancel_all_orders` |
| Health check re-place | Only places missing order (no cancel needed) | — |
| Orphan cleanup | — | ✅ `cancel_all_orders` after closing |

---

## Data Accuracy (Binance ↔ Platform)

All live trade data is sourced from Binance's actual fill records, not calculated estimates.

| Data Point | Source | Method |
|-----------|--------|--------|
| Entry price | Binance order response | `avgPrice` from `futures_create_order` |
| Entry commission | Binance trade history | `futures_account_trades(orderId)` → sum of `commission` |
| Exit price | Binance trade history | Weighted average of `price * qty` from `futures_account_trades(orderId)` |
| Exit commission | Binance trade history | `futures_account_trades(orderId)` → sum of `commission` |
| Win/Loss | Binance order type | `STOP_MARKET` filled = loss, `TAKE_PROFIT_MARKET` filled = win |

**Fallback:** If Binance trade history is unavailable (API error, paper trading), the bot falls back to calculated values using `config.commission_pct` and theoretical SL/TP prices.

### Where data is fetched:

- **Entry** (`_execute_trade`): queries `get_trades_for_order()` after market fill for actual commission
- **Exit via user stream** (`_on_user_event`): queries `get_trades_for_order()` for actual fill price and commission
- **Exit via position poll** (`_run_position_poll`): queries `get_order()` + `get_trades_for_order()` for actual fill data
- **Crash recovery** (`_crash_recovery`): queries `get_order()` + `get_trades_for_order()` for accurate PnL on orphan trades
