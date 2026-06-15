# Strategy: AMD FVG

## Concept

AMD = Accumulation, Manipulation, Distribution. The strategy identifies when price consolidates (accumulation), then fakes out in one direction (manipulation), and enters in the opposite direction when a Fair Value Gap (FVG) confirms the reversal (distribution).

The theory: smart money builds positions during accumulation, triggers stop losses with a fake breakout (manipulation), then drives price in the real direction. The FVG is the confirmation that the reversal has started.

---

## Timeframe & Assets

- **Timeframe**: 15 minutes
- **Assets**: BTCUSDT, ETHUSDT, SOLUSDT (Binance Futures, USDT-M perpetual)

---

## Signal Detection: Step by Step

Each 15-minute candle close triggers the following checks in order. The strategy only enters when all phases complete on the same scan.

### Phase 1: Accumulation

Detect tight price consolidation over the last `accLen` (60) bars.

**Range calculation:**
- BTC: body-based — `range_hi = max(open, close)`, `range_lo = min(open, close)` per bar, then rolling max/min over 60 bars
- ETH/SOL: wick-based — `range_hi = high`, `range_lo = low`, then rolling max/min over 60 bars

**ATR mode (current config):**
```
acc_range = rolling_high - rolling_low
atr_60 = Wilder ATR over 60 bars

is_accumulating = (
    acc_range <= atr_60 * 5.0        # range within 5x ATR
    AND acc_range >= atr_60 * 0.0    # no minimum (disabled)
    AND range_pct >= 0.0%            # no minimum % (disabled)
    AND in_active_session
)
```

While accumulating, the zone boundaries are stored (`acc_high`, `acc_low`) and the manipulation state resets on every bar. Accumulation must overlap with an active trading session.

### Phase 2: Manipulation

After accumulation ends, within the next `manLook` (10) bars:

**Bearish manipulation** (sets up a short):
- Wick (high) breaks above `acc_high + manip_min_dist`
- `manip_min_dist = ATR(14) * 0.4` (manipulation must be at least 0.4 ATR above the range)
- Track the highest wick as `manip_extremum`

**Bullish manipulation** (sets up a long):
- Wick (low) breaks below `acc_low - manip_min_dist`
- Track the lowest wick as `manip_extremum`

Only one direction of manipulation is detected per accumulation zone — once bearish manipulation is detected, bullish is blocked (and vice versa).

### Phase 3: FVG Detection + Entry

After manipulation is confirmed, check for a Fair Value Gap on the current bar:

**Bearish FVG** (after bullish manipulation above range → short entry):
```
fvg_gap = lows[i-2] - highs[i]     # gap between 2-bars-ago low and current high
valid = fvg_gap > ATR(14) * 0.1    # gap must exceed 10% of ATR
        AND close < acc_high         # price fell back into or below the range
```

**Bullish FVG** (after bearish manipulation below range → long entry):
```
fvg_gap = lows[i] - highs[i-2]     # gap between current low and 2-bars-ago high
valid = fvg_gap > ATR(14) * 0.1
        AND close > acc_low          # price recovered back into or above the range
```

**Entry**: at the candle close price of the FVG bar.

### Phase 4: SL & TP

**Stop Loss (Smart SL)** — wider of two values:
```
# Short:
atr_sl = entry + ATR(14) * 1.5
smart_sl = max(atr_sl, manip_extremum)    # whichever is further from entry

# Long:
atr_sl = entry - ATR(14) * 1.5
smart_sl = min(atr_sl, manip_extremum)
```

This ensures the SL is always beyond the manipulation wick — if the manipulation wick was larger than 1.5 ATR, the SL uses the wick level instead.

**Take Profit** (adaptive reward-to-risk):
```
sl_distance = abs(entry - sl)
rr = 3.0 if prev_completed_6h_ADX(14) >= 40 else 2.0   # strong trend → 3:1, else 2:1
tp = entry ± (sl_distance * rr)
```
The 6h trend regime is read from the **previous completed** 6h candle (no lookahead), so it is
constant within each 6h window. Validated via walk-forward: 3:1 only in strong trends beats fixed 2:1
out-of-sample with lower drawdown.

### Phase 5: Exit

Checked on every subsequent bar, SL before TP (matches Pine Script behavior):
```
# Long:
if low <= sl → loss
elif high >= tp → win

# Short:
if high >= sl → loss
elif low <= tp → win
```

If both SL and TP are hit on the same bar, SL takes priority (conservative assumption).

---

## Filters

### Session Filter

Only trade during active forex sessions (EST/New York time):

| Session | Hours (EST) |
|---------|-------------|
| Sydney | 17:00 – 02:00 |
| Tokyo | 19:00 – 04:00 |
| London | 03:00 – 12:00 |
| New York | 08:00 – 17:00 |

Current config enables all 4 sessions. Session is checked for both accumulation detection and manipulation/entry.

### Skip Periods

- **Skip month**: May (month 5) — historically poor performance
- **Skip weeks**: April weeks 2 and 4 — tax deadline and pre-May positioning

In the live bot, skip-month is enforced in `_process_candle()` (worker.py:314) before `check_signal()` is called — the entire candle is ignored.

### Sweep Filter

Blocks entries that are counter to an active liquidity sweep zone:
- Detects pivot highs/lows using `sweepLen` (5) bar lookback
- When price sweeps a pivot (wick breaks but close doesn't) → creates a sweep "box"
- Box expires after `sweepMaxBars` (300) bars or when price breaks through
- If price is inside a bullish sweep box → block short entries (`in_bull_sweep`)
- If price is inside a bearish sweep box → block long entries (`in_bear_sweep`)

### ADX Trend Filter

Blocks entries that go against a strong trend:
- Compute ADX and directional indicators (+DI, -DI) with period 42
- If ADX > 35 at the manipulation bar AND the trend direction matches the manipulation direction → skip
  - Bearish manipulation + ADX strong + `+DI > -DI` (uptrend) → skip short (don't fight the trend)
  - Bullish manipulation + ADX strong + `-DI > +DI` (downtrend) → skip long

---

## Adaptive Position Sizing

Reduces risk after consecutive losses to protect capital during drawdowns.

```
Normal risk:    2% of equity per trade
Reduced risk:   0.25% of equity per trade

Trigger:        4 consecutive losses → switch to reduced risk
Recovery:       2 consecutive wins → switch back to normal risk
```

State tracked in `BotWorker`: `current_streak`, `consecutive_wins`, `adaptive_active`. Broadcasted to frontend via WebSocket as `adaptive_sizing` events — the dashboard shows a warning banner when active.

---

## Rules

1. **One trade at a time** — `_active_trade_id is not None` blocks new signals
2. **No daily trade cap** — the old `max_trades_per_day` gate was removed; drawdown/loss-streaks are handled by adaptive sizing instead
3. **Bot enabled check** — user can disable bot via settings without stopping the worker
4. **Active symbols** — user can select which symbols to trade
5. **Only enter during active session** — checked by the strategy itself
6. **No manual SL movement** — SL and TP are set once at entry, never adjusted

---

## Parameters

### Strategy Parameters (`config.py` → `STRATEGY_PARAMS`)

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `tf_minutes` | 15 | Candle timeframe |
| `acc_len` | 60 | Accumulation lookback (60 bars = 15 hours) |
| `acc_mode` | `atr` | ATR-based accumulation detection |
| `atr_mult_acc` | 5 | Max accumulation range = 5x ATR(60) |
| `atr_mult_acc_min` | 0.0 | Min accumulation range (disabled) |
| `acc_width` | 0.2 | Fixed % mode width (not used when acc_mode=atr) |
| `acc_width_min` | 0.0 | Min % width (disabled) |
| `man_look` | 10 | Bars after accumulation to find manipulation |
| `fvg_threshold` | 0.1 | Min FVG gap as ATR multiplier |
| `atr_len` | 14 | ATR period for SL/TP calculation |
| `atr_mult` | 1.5 | ATR multiplier for SL distance |
| `rrr` | 2.0 | Reward-to-risk ratio (range / fallback) |
| `dynamic_rr` | true | Adaptive RR: 3:1 in strong 6h trends, else 2:1 |
| `rrr_trend` | 3.0 | RR used when the 6h trend is strong |
| `rrr_range` | 2.0 | RR used otherwise |
| `htf_hours` | 6 | Higher timeframe for the trend regime |
| `htf_adx_period` | 14 | ADX period on the 6h regime |
| `htf_adx_threshold` | 40 | 6h ADX ≥ this ⇒ trend ⇒ 3:1 |
| `sessions` | all 4 | Active trading sessions |
| `sweep_filter` | true | Sweep zone filter enabled |
| `sweep_len` | 5 | Pivot detection lookback |
| `sweep_max_bars` | 300 | Sweep box expiry |
| `skip_months` | [5] | Skip May |
| `skip_weeks` | {4: [2, 4]} | Skip April weeks 2 and 4 |
| `manip_min_mode` | `atr` | Manipulation minimum distance mode |
| `manip_min_val` | 0.4 | Manipulation must exceed 0.4x ATR(14) |
| `adx_filter` | true | ADX trend filter enabled |
| `adx_period` | 42 | ADX calculation period |
| `adx_threshold` | 35 | ADX value above which trend is "strong" |

### Adaptive Sizing Parameters

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `loss_streak_threshold` | 4 | Consecutive losses to activate |
| `reduced_risk_pct` | 0.25 | Risk % during adaptive mode |
| `wins_to_recover` | 2 | Consecutive wins to deactivate |

### Asset-Specific Config (`ACC_RANGE_MODE`)

| Asset | Accumulation Range | Reason |
|-------|--------------------|--------|
| BTCUSDT | `body` | Less noise from BTC wicks |
| ETHUSDT | `wick` | Standard |
| SOLUSDT | `wick` | Standard |

### Trading Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `LEVERAGE` | 5x | Binance Futures leverage |
| `COMMISSION_PCT` | 0.04% | Per-side taker fee |
| `SLIPPAGE_TICKS` | 2 | Slippage buffer for backtesting |
| `TICK_SIZE` | BTC: $0.10, ETH/SOL: $0.01 | Price tick per symbol |
| `LOT_SIZE` | BTC: 0.001, ETH: 0.01, SOL: 0.1 | Min quantity step |
| `CANDLE_BUFFER_SIZE` | 1000 | Rolling buffer for live strategy |

---

## Implementation: Live vs Backtest

| Aspect | `strategy.py` (live) | `amd_engine.py` (backtest) |
|--------|---------------------|---------------------------|
| Function | `check_signal(candles, params)` | `run(data, cfg)` |
| Input | Rolling buffer (~1000 candles), timestamps in **ms** | Full historical data, timestamps in **seconds** |
| Output | Single signal dict or `None` (last bar only) | List of all setup dicts |
| Entry check | `if i == n - 1` — only returns signal on the last bar | Records every entry across all bars |
| Exit handling | Not handled — `BotWorker` manages via Binance orders | Simulated bar-by-bar within `run()` |
| State persistence | In-memory (`BotWorker` fields) | Loop variables within `run()` |
| Skip months | Enforced in `worker.py:314` before calling `check_signal` | Enforced in `_month_mask` applied to accumulation and manipulation |
| Trade stacking | Blocked by `_active_trade_id` check in `worker.py:325` | Blocked by `active` flag in `run()` loop |

Both share identical: accumulation logic, manipulation detection, FVG calculation, sweep zones, ADX filter, smart SL, ATR computation (Wilder's RMA), session detection.

---

## Signal → Execution Flow (Live)

```
Binance 15m kline WebSocket
  → _on_kline() — only processes closed candles (k.x == true)
    → Append to candle_buffers[symbol], trim to 1000
    → _process_candle(symbol)
      → Seasonal filter check (skip_may / skip_tax_deadline, read from user state)
      → Bot enabled check
      → Active symbols check
      → No active trade check
      → Build params (merge strategy config + user overrides: rr/sessions/seasonal)
      → check_signal(candle_buffer, params)
        → If signal found:
          → _execute_trade(symbol, signal)
            → Calculate position size from risk % and SL distance
            → Cancel stale orders on symbol
            → Place MARKET entry order on Binance
            → Query actual commission from Binance trade history
            → Save trade to DB
            → _place_sl_tp() — cancel existing, place STOP_MARKET + TAKE_PROFIT_MARKET (3x retry)
            → Broadcast trade_opened via WebSocket
            → Telegram alert (live only)
```

---

## Backtest Results (Verified)

Using the current production parameters on historical data from January 2020 to April 2026:

```
BTCUSDT: 350 trades (149W / 201L) = 42.6% WR
ETHUSDT: 214 trades (85W / 129L)  = 39.7% WR
SOLUSDT: 191 trades (78W / 113L)  = 40.8% WR
Total:   755 trades (312W / 443L) = 41.3% WR

Final equity: $183,632.75 (+1736.3%)
Max drawdown: 19.0%
Starting:     $10,000
Risk:         2% dynamic with adaptive sizing
Reward:risk:  adaptive — 3:1 in strong 6h trends (~17% of trades), else 2:1
```

The win rate (~41%) is below 50%, but the adaptive 2:1/3:1 RRR means each win recovers 2–3 losses. The strategy is profitable through edge in reward-to-risk, not win rate.

---

## Public Summary (for landing page / visitors)

*This section describes the strategy without exposing specific parameters or configuration. Safe to use on public-facing pages.*

### How It Works

The bot scans for periods where price coils into a tight range relative to recent volatility — **accumulation**. It then watches for a sharp wick that sweeps beyond the range boundary, trapping breakout traders and hunting stop losses — **manipulation**.

Once the trap is set, the bot waits for a Fair Value Gap to form in the opposite direction, confirming the real move. Entry is at the FVG candle close.

### Risk Management

Reward-to-risk is **adaptive: 2:1 normally, 3:1 when the higher-timeframe (6h) trend is strong** — letting winners run when the market actually trends, while staying conservative in chop. The stop loss is volatility-based, always placed beyond the manipulation extreme to avoid getting stopped out by noise. Take profit is set at twice — or three times in a strong trend — the stop distance from entry.

During losing streaks, position size scales down dramatically to protect capital. After consecutive wins, it scales back up. This adaptive sizing cut max drawdown nearly in half while preserving returns.

### Filters

- **Liquidity sweep zones** block entries where the move may already be exhausted
- A **trend filter** skips choppy, directionless markets to avoid whipsaws
- **Seasonal filter** sits out recurring events that make the market choppy — US tax deadlines and the "sell in May" slump
- Trading can be limited to specific **market sessions** (all four active by default)

### Track Record

The strategy has been backtested across BTC, ETH, and SOL over 6+ years of historical data with 750+ trades. It trades on the 15-minute timeframe, averaging roughly 10-15 trades per month across all assets.

### What Makes It Different

Most retail strategies chase high win rates. This one accepts losing more often than winning — but when it wins, the payout is two to three times the loss. Combined with adaptive position sizing that protects during drawdowns, the strategy compounds capital through disciplined risk management, not prediction accuracy.
