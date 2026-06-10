# Tech Plan — Backtester result cache + setup pagination

Status: **Implemented (Step 1 cache + Step 2 setup pagination) — pending live-chart
verification + DB migration.** Goal: make the backtester page **faster** (stop re-running
the strategy on every call) and **smoother** (load only the setups being shown), without
breaking the existing chart/UX.

---

## 1. Problem & goals

### Current behavior (verified)
- `/api/backtest` calls `amd_engine.run(data, cfg)` **fresh on every request** over the
  full candle history (~210k 15m bars), then `compute_stats`. Returns **all** setups + stats.
- `/api/backtest/combined` runs the engine **3×** (once per symbol) every call.
- A single page load fires `/api/backtest` (1 run) + `/combined` (3 runs) = **4 full sims**.
  Asset-switch and the "Run Backtest" button trigger more.
- Only the **candle data** is cached (`_candle_cache`, in `routes/backtester.py`); the
  **computation result is not** cached. `_invalidate_cache` exists but is never called.
- The chart already lazy-loads candles (left-edge `subscribeVisibleLogicalRangeChange`
  → `fetchOlder`); **setups are the exception** — all are loaded up front so they can draw.

### Goals
1. **Faster** — run the engine **once** per `(strategy, params, data)` combo; persist the
   result so it survives Railway redeploys.
2. **Smoother** — serve setups in pages / by visible time range (chosen storage: child
   table, future-proofed for thousands of setups).
3. **Non-breaking** — chart navigation, setup boxes, stats cards keep working; keep the
   `ensureLoaded` safety net.

### Non-goals (for now)
- Per-request / per-user strategy params (params come from server `config.py`).
- Forward (right-edge) candle loading past the most recent setup.
- Changing strategy logic or stats math.

---

## 2. Architecture

### Cache signature (the key idea)
The result is fully determined by **strategy + params + the candle data**. So the cache key
must include a **data fingerprint** — otherwise a new `import_candles.py` run would serve
stale setups. This makes invalidation automatic (no manual busting).

```
strategy_id   = "amd_fvg_v1"                       # future: multiple strategies
params_hash   = sha256(json.dumps(_build_cfg(symbol), sort_keys=True, default=str))
fingerprint   = (data_first_ts, data_last_ts, candle_count)   # cheap indexed queries
signature     = sha256(f"{strategy_id}|{symbol}|{interval}|{params_hash}|{first}|{last}|{count}")
```

When candles change → `last`/`count` change → new signature → cache miss → re-run. Old rows
are pruned on save (see §4).

### Two-tier cache
- **In-memory** (process): hot layer.
  - Step 1: `{signature: {run_id, stats, total, setups:[full]}}` (same shape as today).
  - Step 2: `{signature: {run_id, stats, total}}` (drop the full setups; fetch pages from DB).
- **DB** (`backtest_runs` + `backtest_setups`): persistent; survives redeploys so the engine
  doesn’t re-run 4× after every deploy.

### Lookup flow (`_get_or_build_run(symbol, interval)`)
```
cfg = _build_cfg(symbol)
sig = _signature(...)                # needs only range+count queries (NO 210k load)
1. mem[sig]?           → return meta                          # instant
2. db run by sig?      → load meta into mem, return           # survives restart
3. miss:
     data   = _load_candles(symbol, interval)                 # cached candles
     setups = amd_engine.run(data, cfg)
     stats  = amd_engine.compute_stats(setups, cfg["rrr"])
     save_backtest_run(run_fields, setups)                    # insert + prune old
     cache, return
```
`/combined` is itself a cached entity (two levels):
1. **Per-symbol:** `_get_or_build_run` per symbol — engine runs only on a miss.
2. **Combined result:** signature = `sha256(sig_BTC + sig_ETH + sig_SOL + equity_cfg_hash)`.
   Checked first (mem → DB); on hit, return immediately (no per-symbol load, no `compute_stats`).
   On miss: build/reuse the 3 per-symbol runs (cached), `compute_stats(all, rrr, _equity_cfg())`,
   **persist** as a `backtest_runs` row (`symbol="COMBINED"`, `total_setups=0`, no setup rows —
   the frontend reads only `stats`/`equity.curve`/`perAsset` from `/combined`, never setups).
   `perAsset` is rebuilt from the 3 per-symbol `stats` (also cached). So with all runs warm,
   `/combined` runs the engine **0 times** and survives redeploys.

---

## 3. Data model (Option B — child table)

`app/db/models.py` (add `Index` to the existing sqlalchemy import line):

```python
class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (UniqueConstraint("signature", name="uq_backtest_run_sig"),)
    id            = Column(Integer, primary_key=True)
    signature     = Column(String(64), nullable=False, index=True)   # sha256 hex
    strategy      = Column(String(50), nullable=False, default="amd_fvg_v1")
    symbol        = Column(String(20), nullable=False)
    interval      = Column(String(5),  nullable=False)
    params_hash   = Column(String(64), nullable=False)
    data_first_ts = Column(Integer)
    data_last_ts  = Column(Integer)
    candle_count  = Column(Integer)
    total_setups  = Column(Integer, nullable=False, default=0)
    stats         = Column(Text)                                     # json.dumps(stats)
    created_at    = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class BacktestSetup(Base):
    __tablename__ = "backtest_setups"
    __table_args__ = (
        Index("ix_bt_setup_run_ordinal", "run_id", "ordinal"),
        Index("ix_bt_setup_run_entry",   "run_id", "entry_time"),
    )
    id         = Column(Integer, primary_key=True)
    run_id     = Column(Integer, ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False)
    ordinal    = Column(Integer, nullable=False)        # 0..N-1, time order
    entry_time = Column(Integer, nullable=False)        # seconds UTC — range queries
    data       = Column(Text, nullable=False)           # json.dumps(full setup dict)
```

- JSON stored as `Text` (matches existing convention — no `JSON` column type used in this codebase).
- `(run_id, ordinal)` index → nav pages (`‹ ›`, "X / total"). `(run_id, entry_time)` index → chart range queries.
- `ondelete="CASCADE"` so pruning a run drops its setups.

---

## 4. DB queries

**`app/db/queries/candles.py`** — add:
```python
async def get_historical_candle_count(symbol, interval) -> int   # select(func.count(HistoricalCandle.id)).where(...)
```
(`get_historical_candle_range` already returns first/last ts.)

**`app/db/queries/backtest.py`** — add (re-exported via the existing `from ...backtest import *`):
```python
async def get_backtest_run(signature) -> BacktestRun | None
async def save_backtest_run(run: dict, setups: list[dict]) -> int
    # 1) prune: delete existing runs for same (strategy, symbol, interval)  → cascade setups
    # 2) insert BacktestRun; on IntegrityError(unique signature) → rollback, re-query, return its id
    # 3) bulk-insert BacktestSetup rows (ordinal, entry_time, json.dumps(setup))
    # returns run_id
async def get_run_setups_all(run_id)                         -> list[dict]   # Step 1 (same shape)
async def get_run_setups_page(run_id, limit, offset)         -> list[dict]   # Step 2 nav (newest-first)
async def get_run_setups_range(run_id, from_ts, to_ts)       -> list[dict]   # Step 2 chart
```
- Setup rows store `data` as `json.dumps`; readers `json.loads` back to the exact dicts the
  frontend expects (so `mapSetups`/`drawSetup` are unchanged).
- Concurrency: portable `try insert / except IntegrityError → re-query` (avoids PG-only `ON CONFLICT`).
- Pruning keeps the table at ~`3 symbols × strategies` runs (params come from one server config).

---

## 5. Backend (`routes/backtester.py`)

Add near the cache section:
```python
import hashlib, json
_STRATEGY_ID = "amd_fvg_v1"
_run_meta_cache: dict[str, dict] = {}     # signature -> {run_id, stats, total[, setups]}
_combined_cache: dict[str, dict] = {}     # combo-sig -> combined stats

def _params_hash(cfg) -> str: ...
async def _signature(symbol, interval, cfg) -> str: ...     # uses range+count queries
async def _get_or_build_run(symbol, interval) -> dict: ...  # the lookup flow (§2)
```

### Step 1 endpoints (SAME response shape → no frontend change)
- `GET /api/backtest` → `_get_or_build_run` → `{"stats": meta.stats, "setups": <all setups>}`.
- `GET /api/backtest/combined` → combined-signature check (mem → DB row `symbol="COMBINED"`);
  hit → return stored. Miss → per-symbol `_get_or_build_run` (engine cached), combine setups,
  `compute_stats(all, rrr, _equity_cfg())`, persist + cache → `{"stats": combined, "perAsset": {...}}`.
- Empty data → `{"stats": {}, "setups": []}` (unchanged), not cached.

### Step 2 endpoints (pagination / range)
- `GET /api/backtest` → `{"stats", "total"}` (no setups).
- `GET /api/backtest/setups?symbol&interval&from&to` → setups in `[from,to]` (chart).
- `GET /api/backtest/setups?symbol&interval&before&limit=10` → newest-first page (nav).
- `/combined` unchanged (still needs the full set for the combined curve — cached).

---

## 6. Frontend (`templates/backtester.html`) — **Step 2 only**

Step 1 changes **nothing** here. Step 2:
- On load: `GET /api/backtest` → `{stats, total}`; set counter to `total`; fetch the **last page**
  of setups (`?before=<last_ts+1>&limit=10`), `mapSetups`, jump to the most recent.
- Setup `‹` past the loaded window → fetch the previous page, **prepend** to a local `setups`
  array (it stays a contiguous newest-first suffix → global index maps cleanly).
- Chart render: in `renderSetups`, when the visible range changes, fetch setups for that range
  (`?from&to`) and merge (dedupe by `entryTime`). Ties into the existing
  `subscribeVisibleLogicalRangeChange` / `fetchOlder` so candles + setups load together.
- `goToSetup` → `ensureLoaded` stays as the safety net (loads candles for the target; if the
  setup row isn’t loaded, fetch its range first).
- Keep `mapSetups` / `applyBtStats` / `loadCandlesForSetups` (already extracted).

---

## 7. Migration & rollout

1. **Models** added → `Base.metadata.create_all` (on app start, after `_ensure_schema`)
   creates the two new tables automatically in dev.
2. **Alembic migration** `<rev>_add_backtest_cache.py`, `down_revision='c3e8a1f2b4d6'`:
   `op.create_table('backtest_runs', ...)`, `op.create_table('backtest_setups', ...)`,
   `op.create_index(...)` ×2. `downgrade` drops both. Prod runs `alembic upgrade head` (Procfile)
   then `create_all` (idempotent backup). **No `_ensure_schema` entry needed** (that path is only
   for adding columns to existing tables).
3. **Local-first**: build + test against the dev Postgres only. **Do not run the migration on
   the Railway/prod DB without explicit approval** (per project rule). Review the migration in
   isolation first.

---

## 8. Edge cases & risks

| Case | Handling |
|---|---|
| Concurrent cold requests (same combo) | `try insert / except IntegrityError → re-query`; both return the same run |
| Candle import / data change | fingerprint changes → new signature → re-run; old run pruned on next save |
| `config.py` param change | `params_hash` changes → new signature → re-run |
| No candles for a symbol | return `{stats:{}, setups:[]}`, don’t cache (unchanged) |
| Process restart (redeploy) | DB hit → no re-run; only mem cache is cold |
| Setup JSON serialization | setup dict is ints/floats/strings/None → `json.dumps` safe |
| Table growth | prune-on-save keeps ~3 rows/symbol; setups cascade-delete |
| Multi-strategy future | `strategy` already in signature + a column |
| Chart range under-loads (Step 2) | `ensureLoaded` safety net re-fetches |

---

## 9. Testing

**Step 1 (must prove same output, just cached):**
- Unit-ish: call `_get_or_build_run(BTCUSDT,15m)` twice → 2nd is a cache hit (engine not called — assert via a counter/log).
- Snapshot: capture `/api/backtest` and `/combined` JSON **before** the change; assert byte-equal
  (modulo ordering) **after** (same stats + same setups).
- Manual: load `/backtester` → chart + boxes + nav identical; reload → instant; check DB has 1 run + N setups.

**Step 2:**
- `get_run_setups_page` / `get_run_setups_range` return correct subsets & ordering.
- Manual on live chart: last-setup load, `‹/›` across page boundaries, scroll-left loads older
  candles **and** setups, setup count `X / total` correct, `node --check` on the JS.

---

## 10. File-by-file change list

| File | Step 1 | Step 2 |
|---|---|---|
| `app/db/models.py` | + `BacktestRun`, `BacktestSetup` (+`Index` import) | — |
| `alembic/versions/<rev>_add_backtest_cache.py` | new migration | — |
| `app/db/queries/candles.py` | + `get_historical_candle_count` | — |
| `app/db/queries/backtest.py` | + run/setup cache queries | + page/range queries (already listed) |
| `routes/backtester.py` | signature + `_get_or_build_run` + cache; rewire `/api/backtest`, `/combined` | split setups into `/api/backtest/setups`; `/api/backtest` → `{stats,total}` |
| `templates/backtester.html` | **unchanged** | range/paged setup loading |
| `CLAUDE.md`, `docs/codebase-map.md`, `CHANGELOG.md` | doc the new models/queries | update |

---

## 11. Recommendation
Ship **Step 1** first (cache + tables + migration, identical API). It removes the real cost
(4 sims/load → 0 when warm) with **zero frontend risk**, and the migration can be reviewed in
isolation. Then do **Step 2** (range/paged setups) for the smoothness, with live-chart testing.
