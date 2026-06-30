"""
Pre-compute every Backtester Playground combination and persist each result to the
DB, keyed by its signature — so a user changing the Playground knobs gets the result
served straight from `backtest_runs` instead of waiting for the server to run the
engine + equity sim on the first hit.

It drives the SAME helpers the live routes use (`routes.backtester`), so the
signatures it warms are byte-for-byte the ones runtime requests look up.

Playground param space:
  - sessions:  every non-empty subset of [sydney, tokyo, london, ny]  → 15
  - skip_may:  0 / 1                                                   → 2
  - skip_tax:  0 / 1                                                   → 2
        ⇒ 60 setup-override combos  (drive the per-(strategy,symbol) runs)
  - risk_pct:  1, 1.5, 2, 2.5, 3   (combined + trade-log only)        → 5
        ⇒ 300 combined + 300 trade-log combos

Each setup combo persists 6 single runs (2 strategies × 3 symbols). Singles do NOT
depend on risk, so they are built once per setup combo and reused across all 5 risks.

Target DB:
    Defaults to local DATABASE_URL. To warm PRODUCTION from your laptop (Option A),
    add PROD_DATABASE_URL to .env and pass --prod — it reads prod's candles and
    writes prod's backtest_runs, so the signatures match prod requests exactly.

Usage (run from bot/):
    python warm_backtest_cache.py            # warm LOCAL (idempotent)
    python warm_backtest_cache.py --prod     # warm PRODUCTION (PROD_DATABASE_URL)
    python warm_backtest_cache.py --database-url postgres://...  # ad-hoc target
    python warm_backtest_cache.py --clear    # wipe backtest_runs first, then warm
    python warm_backtest_cache.py --dry-run  # just print the plan + combo count
    python warm_backtest_cache.py --prod -y  # skip the prod confirmation prompt

Warm a RUNNING instance via its HTTP API (Option B) — the deployed app computes +
persists each combo itself; no DB creds, just a logged-in `session` cookie:
    python warm_backtest_cache.py --api-base https://zenithbot.org --cookie "session=<value>"
    (the cookie can also come from WARM_SESSION_COOKIE in the environment)
"""

from __future__ import annotations

import asyncio
import itertools
import os
import sys
import time
from urllib.parse import urlsplit

from dotenv import load_dotenv

import app.db as db
from routes import backtester as bt

# Canonical session order — MUST match the DOM/order the frontend sends in
# `btSetupParams()` (sydney, tokyo, london, ny), or signatures won't line up.
SESSIONS = ["sydney", "tokyo", "london", "ny"]
# Risk slider: min=1 max=3 step=0.5 (templates/backtester.html #bt-risk).
RISK_VALUES = [1.0, 1.5, 2.0, 2.5, 3.0]


def _session_combos() -> list[list[str]]:
    """Every non-empty subset, each ordered by the canonical session order."""
    combos: list[list[str]] = []
    for r in range(1, len(SESSIONS) + 1):
        for c in itertools.combinations(SESSIONS, r):
            combos.append(list(c))
    return combos


def _setup_combos():
    """(sessions_csv, skip_may, skip_tax) for every setup-override combination."""
    for sessions in _session_combos():
        for skip_may in (0, 1):
            for skip_tax in (0, 1):
                yield ",".join(sessions), skip_may, skip_tax


async def warm(db_url: str, clear: bool = False, dry_run: bool = False):
    setup_combos = list(_setup_combos())
    n_setup = len(setup_combos)
    n_combined = n_setup * len(RISK_VALUES)
    print(f"Plan: {n_setup} setup combos × {len(RISK_VALUES)} risk levels")
    print(f"      → {n_setup * 6} single runs, {n_combined} combined + {n_combined} trade-log runs")
    if dry_run:
        return

    await db.init_db(db_url)

    if clear:
        await db.clear_backtest_runs()
        print("Cleared existing backtest_runs (cascades to setups)\n")

    t0 = time.monotonic()
    for i, (sessions_csv, skip_may, skip_tax) in enumerate(setup_combos, 1):
        setup_ov = bt._parse_setup_overrides(sessions_csv, skip_may, skip_tax)
        # First combined call builds + persists the 6 single runs for this setup
        # combo; subsequent risk levels reuse them from cache.
        for risk in RISK_VALUES:
            equity_ov = {"riskPct": risk / 100.0}
            await bt._get_or_build_combined(setup_ov, equity_ov)
            await bt._get_or_build_tradelog(setup_ov, equity_ov)

        elapsed = time.monotonic() - t0
        rate = i / elapsed if elapsed else 0
        eta = (n_setup - i) / rate if rate else 0
        print(f"  [{i:2}/{n_setup}] sessions={sessions_csv or '-':<24} "
              f"may={skip_may} tax={skip_tax}  ({elapsed:5.0f}s elapsed, ETA {eta:4.0f}s)")

    total = await db.count_backtest_runs()
    print(f"\nDone in {time.monotonic() - t0:.0f}s. {total} runs cached in backtest_runs.")


async def warm_via_api(base: str, cookie: str, dry_run: bool = False):
    """Option B — warm a *running* instance (prod) by hitting its Playground
    endpoints, so the deployed app computes + persists each combo itself. Needs an
    authenticated `session` cookie (these endpoints are behind require_auth).

    First-hit combos build 6 engine runs and can exceed the edge timeout; on a
    timeout the server usually still finishes + saves, so we retry and the retry
    hits the now-cached row. Requests are serialized to spare the live instance."""
    import aiohttp

    base = base.rstrip("/")
    setup_combos = list(_setup_combos())
    n_setup = len(setup_combos)
    n_req = n_setup * len(RISK_VALUES) * 2  # combined + tradelog per (combo, risk)
    print(f"Plan: {n_setup} setup combos × {len(RISK_VALUES)} risk → {n_req} requests to {base}")
    if dry_run:
        return

    headers = {"Cookie": cookie if "=" in cookie else f"session={cookie}"}
    timeout = aiohttp.ClientTimeout(total=300)
    done = failed = 0
    t0 = time.monotonic()

    async def hit(session, path, params):
        nonlocal done, failed
        for attempt in range(3):
            try:
                async with session.get(base + path, params=params, headers=headers,
                                       timeout=timeout) as r:
                    if r.status == 200:
                        done += 1
                        return
                    if r.status in (401, 403):
                        raise SystemExit(f"\nAuth failed ({r.status}) — the --cookie is invalid/expired.")
                    body = (await r.text())[:120]
                    print(f"    {r.status} on {path}?{params} (try {attempt+1}): {body}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"    {type(e).__name__} on {path}?{params} (try {attempt+1}) — retrying")
        failed += 1

    async with aiohttp.ClientSession() as session:
        for i, (sessions_csv, skip_may, skip_tax) in enumerate(setup_combos, 1):
            base_params = {"sessions": sessions_csv, "skip_may": skip_may, "skip_tax": skip_tax}
            for risk in RISK_VALUES:
                params = {**base_params, "risk_pct": risk}
                await hit(session, "/api/backtest/combined", params)
                await hit(session, "/api/backtest/tradelog", params)
            elapsed = time.monotonic() - t0
            eta = (n_setup - i) / (i / elapsed) if elapsed and i else 0
            print(f"  [{i:2}/{n_setup}] sessions={sessions_csv or '-':<24} "
                  f"may={skip_may} tax={skip_tax}  ({elapsed:5.0f}s, ETA {eta:4.0f}s)")

    print(f"\nDone in {time.monotonic() - t0:.0f}s. {done} requests OK, {failed} failed.")
    if failed:
        print("Re-run to retry failures — succeeded combos are cached and return instantly.")


def _arg_value(flag: str) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def _safe_target(url: str) -> str:
    """host/dbname only — never echo credentials."""
    try:
        p = urlsplit(url)
        return f"{p.hostname or '?'}{p.path or ''}"
    except Exception:
        return "?"


def _resolve_db_url() -> tuple[str, bool]:
    """Pick the target DB. Returns (url, is_prod). Precedence:
    --database-url <url>  >  --prod (PROD_DATABASE_URL)  >  local DATABASE_URL."""
    explicit = _arg_value("--database-url")
    if explicit:
        return explicit, "--prod" in sys.argv
    if "--prod" in sys.argv:
        url = os.getenv("PROD_DATABASE_URL", "")
        if not url:
            sys.exit("--prod set but PROD_DATABASE_URL is missing from .env / environment")
        return url, True
    return os.getenv("DATABASE_URL", ""), False


def main():
    load_dotenv()
    clear = "--clear" in sys.argv
    dry_run = "--dry-run" in sys.argv
    skip_confirm = "-y" in sys.argv or "--yes" in sys.argv

    # Option B — warm a running instance via its HTTP API (computes + persists in-app).
    api_base = _arg_value("--api-base")
    if api_base:
        cookie = _arg_value("--cookie") or os.getenv("WARM_SESSION_COOKIE", "")
        if not cookie and not dry_run:
            sys.exit("--api-base needs --cookie <session=...> (a logged-in session cookie) "
                     "or WARM_SESSION_COOKIE in the environment")
        print(f"Target API: {api_base}\n")
        asyncio.run(warm_via_api(api_base, cookie, dry_run=dry_run))
        return

    db_url, is_prod = _resolve_db_url()
    print(f"Target DB: {_safe_target(db_url)}{'   [PRODUCTION]' if is_prod else '   (local)'}\n")

    # Writing to prod is outward-facing — confirm unless explicitly skipped.
    if is_prod and not dry_run and not skip_confirm:
        prefix = "WIPE + warm" if clear else "Warm"
        resp = input(f"{prefix} the PRODUCTION backtest cache ({_safe_target(db_url)})? [y/N] ")
        if resp.strip().lower() not in ("y", "yes"):
            sys.exit("Aborted.")

    asyncio.run(warm(db_url, clear=clear, dry_run=dry_run))


if __name__ == "__main__":
    main()
