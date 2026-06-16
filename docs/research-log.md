# Strategy Research Log

**Read this before running any strategy experiment — the answer may already be here.**
Append a row to the table (and a detail section for non-trivial results) after every
experiment, so nothing is re-derived. Reproduce commands use the harness in `research/`
(run from `bot/`): `python -m research.sweep ...` / `python -m research.walkforward ...`.

Current production config: **AMD FVG 15m, adaptive RR (3:1 when prev-completed 6h ADX(14) ≥ 40,
else 2:1)**, smart-SL ATR×1.5, sweep + ADX-42 filters, skip May + April wk 2&4, 2% dynamic risk +
adaptive sizing (4 losses → 0.25% until 2 wins). Baseline backtest: **755 trades, 41.3% WR,
+1,736% ($183,632.75 from $10k), 19% max DD** (~17% of trades hit the 3:1 regime).

## Summary (latest first)

| Date | Hypothesis / change | Result | Verdict | Reproduce |
|------|--------------------|--------|---------|-----------|
| 2026-06-16 | **Dynamic RR via HTF-ADX regime** (trend→3:1, else 2:1) | 4–6h + ADX 35–40 is a robust plateau; 6h/40 best floor (no losing year), $184k @ 19% DD; beats fixed-2:1 OOS | ✅ **SHIPPED** (6h/ADX-40) | `python -m research.walkforward --dynamic --htfAdxThreshold 40` |
| 2026-06-16 | Daily (24h) ADX regime for dynamic RR | in-sample-best threshold (20) is OOS-worst; only thr-40 squeaks a win, unselectable | ❌ too slow/noisy | `python -m research.sweep --param htfAdxThreshold --values 20,30,40 --dynamic` (24h needs htfHours=24) |
| 2026-06-16 | Rolling WF selection: greedy (best past) vs maximin (best worst-year) | greedy → $65k (loses to 2:1 $84k); maximin → $141k, converges to 6h/35-40 | ✅ **pick for reliability, not peak** | (selection logic; see detail) |
| 2026-06-16 | Fixed 3:1 RRR | in-sample looks 2–3× better, OOS ties 2:1 ($21.9k vs $20.2k) with worse DD & 14-loss streak | ❌ regime-dependent ghost | `python -m research.walkforward --rrr 3 --split 2023` |
| 2026-06-16 | Exit sweep: atrMult × rrr | atrMult barely changes trade count (it's a zoom); rrr=2 peaks compounded equity (volatility drag beats higher per-trade expectancy) | ✅ 2:1 / 1.5 ATR confirmed | `python -m research.sweep --param atrMult --values 1,1.5,2,2.5 --param2 rrr --values2 1.5,2,2.5,3` |
| 2026-06-16 | Adaptive-sizing tune (loss-streak trigger) | trig-6 looks great in-sample ($180k) but FAILS walk-forward; trig-4 wins OOS | ❌ keep 4 / 0.25% / 2-wins | (equity-sim params; see detail) |
| 2026-06-16 | Extended-entry filter (skip when FVG/displacement ran far) | mild decline (tight 48% → extended 41% WR) but all buckets profitable; any cut removes net +R | ❌ unfilterable | bucket by `|entry-manip|/ATR` |
| 2026-06-16 | Manip-min as % of box width | no separation; tiniest-manip bucket is best; require ≥0.5×box removes +145R | ❌ | bucket by overshoot/box |
| 2026-06-16 | Box-dilution filter (staircase 60-bar boxes) | diluted boxes perform same/slightly better; chart complaint was variance | ❌ | bucket by box60/box30 |
| 2026-06-16 | Adaptive accumulation window (snap to tight range) | WR collapses 43→36%; the wide loose box IS the quality gate | ❌ | (engine variant) |
| 2026-06-16 | Shorter fixed accLen (30/40/50 vs 60) | far worse — short windows let noise qualify as accumulation | ❌ | `python -m research.sweep --param accLen --values 30,40,50,60` |
| 2026-06-16 | Stricter min-manipulation (>0.4 ATR) | WR/equity drop; removing it also worse — 0.4 is the peak | ❌ | `python -m research.sweep --param manipMinVal --values 0.4,0.8,1.2` |

## Key detail

### Dynamic RR (the shipped win)
- Mechanism: TP = 3× risk only when the **previous completed** 6h candle's ADX(14) ≥ 40 (no lookahead),
  else 2× risk. Trends let winners run; chop stays conservative.
- The 4–6h × ADX-35–40 winners form a **contiguous plateau** (neighbors agree → real, not a spike).
  Low thresholds (20–30) flip to 3:1 too often and get chopped → lose OOS.
- 6h/40 = best worst-year (+9%, never a losing year); 6h/35 = best total ($237k) but +7% floor.
  Shipped **6h/40** for reliability.
- **Trade-count drop 771→755 is expected, not a bug:** 3:1 trades have the *same SL* but a *further TP*,
  so they stay open longer and the one-trade-at-a-time rule crowds out ~16 later setups. Proven:
  `dynamicRR` on with `rrrTrend=2` reproduces 771 exactly.

### Adaptive-sizing walk-forward (why we DON'T retune it)
- In-sample, trigger-6/recover-2 looked best ($180k full-sample). Out-of-sample (tune 2020-23, test
  2024-25) the current **trigger-4 / 0.25% / recover-2 beat it** (return/DD 4.8 vs 3.4). Sizing rules
  are the easiest thing to overfit (they don't change trade selection) — only trust them after a blind
  test. `recover-1` is bad everywhere; `recover-2` confirmed; `reducedPct` barely matters.

### The overarching lesson
Entry geometry AND exit RR ratio AND sizing are all near a well-defended optimum. Every "obvious"
tweak was a mirage that lost money or failed walk-forward — **except** dynamic RR, whose edge is a
*structural* regime effect, not a curve-fit. When evaluating any new idea: (1) bucket/measure whether
it separates winners from losers before filtering, (2) judge by **out-of-sample** return/drawdown, not
in-sample peak, (3) prefer the *reliable* config (best worst-case) over the highest number.

## Untested / open ideas
- Long vs short asymmetry (README notes shorts +131R vs longs +64R) — structural, not yet isolated.
- Per-session / per-asset dead-weight analysis.
- A real-data track-record + leaderboard from Binance income history (feasibility discussed; needs
  opt-in + per-user mainnet keys; copy-trading profile link can't be auto-discovered).
