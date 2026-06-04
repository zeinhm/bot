# Backtester

## Overview

The backtester runs the AMD FVG strategy against historical candle data to produce trade results. There are two execution paths that share the same core engine (`amd_engine.py`):

1. **On-the-fly API** — browser requests backtest via `/api/backtest`, results computed in real-time, not persisted
2. **Seeded trade log** — `seed_trades.py` runs the backtest offline, inserts results into `backtest_results` table for display on the dashboard

Both produce identical signals from the same engine. The difference is what happens after: the API returns JSON to the frontend chart, while the seeder computes equity simulation with adaptive sizing and stores full trade records in the DB.

---

## Data Pipeline

```
Binance 1m CSVs (data/*.csv)
  → import_candles.py (resample to 15m, insert into historical_candles table)
    → amd_engine.run() (signal detection + bar-by-bar simulation)
      → seed_trades.py (equity simulation → backtest_results table)
      → /api/backtest (JSON response → frontend chart)
```

### Historical Candle Data

Source: downloaded 1m Binance klines stored as CSVs in `data/` directory.

`import_candles.py` reads the CSVs, resamples to higher timeframes (15m, 1h, etc.), and bulk-inserts into the `historical_candles` table using psycopg2 (synchronous, runs as a CLI tool, not part of the web app).

Table schema: `symbol`, `interval`, `timestamp` (epoch seconds), OHLCV fields. Unique constraint on `(symbol, interval, timestamp)`.

Current data: ~650k rows across BTCUSDT, ETHUSDT, SOLUSDT at 15m intervals spanning 2020-01 to 2026-04.

---

## Core Engine: `amd_engine.py`

### `run(data, cfg) → list[dict]`

Scans all bars and returns every setup (entry + exit). This is the full backtest.

Input: list of candle dicts with keys `time` (epoch seconds), `open`, `high`, `low`, `close`. Config dict with strategy parameters.

Output: list of setup dicts, each containing:
- `direction` (long/short)
- `accStartIdx`, `accEndIdx`, `accStartTime`, `accEndTime`, `accHigh`, `accLow`
- `manipIdx`, `manipTime`, `manipExtremum`
- `entryIdx`, `entryTime`, `entryPrice`
- `sl`, `tp`
- `exitIdx`, `exitTime`, `exitPrice`, `result` (win/loss/open)

### Strategy Logic Per Bar

For each bar (starting from `max(accLen, atrLen) - 1`):

**1. Accumulation detection**
- Compute rolling high/low over `accLen` bars
- Body-based (BTC) or wick-based (ETH/SOL) depending on `accRangeMode`
- ATR mode: range must be within `[atrMultAccMin * ATR, atrMultAcc * ATR]`
- Must be in active session and not in skip month/week
- When accumulating: store zone boundaries, reset manipulation state

**2. Exit check (SL before TP)**
- If in a long trade: check `low <= SL` first (loss), then `high >= TP` (win)
- If in a short trade: check `high >= SL` first (loss), then `low <= TP` (win)
- SL checked before TP on same bar matches Pine Script behavior

**3. Manipulation + FVG + Entry**
- Only checked when: not accumulating, within `manLook` bars of accumulation end, in session, not in skip period
- Bearish manipulation: wick breaks above `accHigh + manipMinDist`
- Bullish manipulation: wick breaks below `accLow - manipMinDist`
- Manipulation extremum tracked (highest wick for bearish, lowest for bullish)
- FVG detection: gap between `lows[i-2]` and `highs[i]` (bearish) or `lows[i]` and `highs[i-2]` (bullish)
- FVG must exceed `fvgThreshold * ATR`
- Additional filters: sweep zone filter, ADX trend filter
- Entry at candle close price
- SL: wider of ATR-based SL and manipulation extremum (smart SL)
- TP: `slDist * rrr` from entry

### `compute_stats(setups, rrr, equity_cfg?) → dict`

Computes summary statistics from setups: trade count, wins, losses, win rate, total R. Optionally runs equity simulation if `equity_cfg` provided.

### `simulate_equity(setups, rrr, cfg) → dict`

Simulates equity curve with:
- Dynamic position sizing (% of equity)
- Commission (0.04% per side = 0.08% round trip)
- Adaptive sizing: after N consecutive losses, reduce risk % until M consecutive wins

Returns: initial capital, final capital, return %, max drawdown %, equity curve array.

---

## Seeder: `seed_trades.py`

CLI tool that populates the `backtest_results` table. Run manually when strategy params change or data is updated.

### Usage

```bash
cd bot/
venv/bin/python3 seed_trades.py --clear          # all 3 assets, clear first
venv/bin/python3 seed_trades.py --symbol BTCUSDT  # single asset
```

### How It Works

1. Connects to local DB via `DATABASE_URL` from `.env`
2. For each symbol: loads all historical 15m candles from DB, runs `amd_engine.run()`
3. Filters to closed trades (win/loss only)
4. Sorts all trades across assets by entry time (chronological order for equity simulation)
5. Simulates equity with adaptive sizing:
   - Initial capital: $10,000
   - Risk: 2% of equity per trade
   - Adaptive: after 4 consecutive losses → reduce to 0.25% until 2 consecutive wins
   - Commission: 0.04% per side on full position value
6. Inserts into `backtest_results` table with: symbol, direction, entry/exit time, entry/exit price, SL, TP, quantity, result, R value, PnL, commission

### Verified Results (2020-01 to 2026-04)

```
BTCUSDT: 360 trades (158W / 202L)
ETHUSDT: 216 trades (90W / 126L)
SOLUSDT: 195 trades (82W / 113L)
Total:   771 trades
Final equity: $134,983.60 (+1249.8%)
```

These match the reference trade log at `choosen/amd-fvg-15m-v1/results/trade_log.csv` exactly — same trade count per asset, same entry/exit prices, same PnL per trade, same final equity.

---

## On-the-fly Backtester: `routes/backtester.py`

### Endpoints

**`GET /backtester`** — renders the backtester page. Loads historical candle data range and seeded backtest results from DB.

**`GET /api/backtest?symbol=BTCUSDT&interval=15m`** — runs backtest for a single symbol. Returns setups and stats as JSON. Uses in-memory candle cache (`_candle_cache`) to avoid re-reading from DB on every request.

**`GET /api/backtest/combined`** — runs backtest for all 3 symbols, merges and sorts by time, computes combined stats with equity simulation. Returns per-asset stats and combined stats.

**`GET /api/candles?symbol=BTCUSDT&interval=15m&limit=500`** — returns historical candle data for the frontend chart.

### Config Mapping

The backtester route builds its config from `config.py` → `STRATEGY_PARAMS` and `ACC_RANGE_MODE`, mapping snake_case to camelCase for `amd_engine`:

| config.py | amd_engine |
|-----------|-----------|
| `acc_len` | `accLen` |
| `acc_mode` | `accMode` |
| `atr_mult_acc` | `atrMultAcc` |
| `man_look` | `manLook` |
| `fvg_threshold` | `fvgThreshold` |
| `atr_len` | `atrLen` |
| `atr_mult` | `atrMult` |
| `rrr` | `rrr` |
| `sweep_filter` | `sweepFilter` |
| `sweep_len` | `sweepLen` |
| `sweep_max_bars` | `sweepMaxBars` |
| `skip_months` | `skipMonths` |
| `skip_weeks` | `skipWeeks` |
| `manip_min_mode` | `manipMinMode` |
| `manip_min_val` | `manipMinVal` |
| `adx_filter` | `adxFilter` |
| `adx_period` | `adxPeriod` |
| `adx_threshold` | `adxThreshold` |
| `ACC_RANGE_MODE[symbol]` | `accRangeMode` |

### Equity Simulation Config

Used by the combined backtest endpoint:

```python
{
    "initialCapital": 10000.0,
    "riskPct": 0.02,              # 2% per trade
    "commissionRate": 0.0004,     # 0.04% per side
    "lossStreakThreshold": 4,     # adaptive kicks in after 4L
    "reducedRiskPct": 0.0025,     # 0.25% during adaptive
    "winsToRecover": 2,           # 2W to exit adaptive
}
```

---

## Live Strategy vs Backtester

The live trading bot uses `strategy.py` → `check_signal()`, which is a separate implementation from `amd_engine.py` → `run()`. Both share the same logic but differ in interface:

| | `strategy.py` (live) | `amd_engine.py` (backtest) |
|---|---|---|
| Input | Rolling candle buffer (~1000 bars) | Full historical dataset |
| Output | Single signal or None (last bar only) | All setups across all bars |
| Exit handling | Done by `BotWorker` via Binance orders | Simulated bar-by-bar within `run()` |
| Timestamps | Milliseconds (Binance WebSocket format) | Seconds (DB `historical_candles` format) |
| Commission/slippage | Applied by `BotWorker` using Binance actual data | Simulated in `seed_trades.py` / `simulate_equity()` |
| ADX filter | Available | Available |
| Sweep filter | Available | Available |

Both use identical: session detection, accumulation logic (body/wick modes), manipulation detection, FVG gap calculation, smart SL (wider of ATR or manipulation wick), ATR computation (Wilder's RMA).

---

## Asset-Specific Configuration

| Asset | Accumulation Range Mode | Notes |
|-------|------------------------|-------|
| BTCUSDT | `body` (max/min of open,close) | Less noise from BTC wicks |
| ETHUSDT | `wick` (high/low) | Standard |
| SOLUSDT | `wick` (high/low) | Standard |

Defined in `config.py` → `ACC_RANGE_MODE` dict. Used by both live strategy and backtester.

---

## Trade Log Format

The reference trade log (`choosen/amd-fvg-15m-v1/results/trade_log.csv`) contains 29 columns per trade:

```
trade_no, symbol, direction, result, entry_time, exit_time,
entry_price, sl, tp, exit_price, sl_dist, tp_dist, actual_rrr,
r_value, cumulative_r, equity_before, risk_pct, adaptive_active,
risk_amount, position_value, commission, gross_pnl, net_pnl,
equity_after, equity_peak, drawdown, drawdown_pct, win_streak, lose_streak
```

The `backtest_results` DB table stores a subset: symbol, direction, entry/exit time, entry/exit price, SL, TP, quantity, result, R value, PnL, commission. The full trade log with equity tracking columns is generated by `seed_trades.py` during the equity simulation loop but only the core fields are persisted.
