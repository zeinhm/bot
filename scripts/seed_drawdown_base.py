"""Seed the drawdown base for existing live accounts.

The drawdown is measured from the account's REAL balance when the bot started
trading. For accounts that connected before we captured it, we read that balance
from Binance's FUTURES account snapshot (its actual daily asset value; ~last 30
days) as of the day BEFORE the first bot trade, and store it as `dd_base` +
`dd_base_time` state. New accounts capture this at connect instead (see settings).

Read-only by default (prints what it WOULD store). Pass --commit to write.
Runs where the accounts' API keys are IP-whitelisted (the server for most users).

    python -m scripts.seed_drawdown_base [--prod] [--user N] [--commit]
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

import app.db as db
from app.auth import decrypt
from exchange import BinanceExchange


async def seed_one(uid: int, commit: bool) -> None:
    cfg = await db.get_user_config(uid)
    if not cfg or not cfg.binance_api_key_enc:
        print(f"  user {uid}: no live key — skip")
        return
    trades = await db.get_all_trades(uid, is_paper=False)
    closed = sorted([t for t in trades if t.result in ("win", "loss") and t.exit_time],
                    key=lambda t: t.exit_time)
    if not closed:
        print(f"  user {uid}: no closed live trades — skip")
        return
    first = closed[0].exit_time
    first_ms = int(first.timestamp() * 1000)

    from binance import AsyncClient
    ex = BinanceExchange(decrypt(cfg.binance_api_key_enc), decrypt(cfg.binance_api_secret_enc))
    try:
        ex.client = await AsyncClient.create(api_key=ex.api_key, api_secret=ex.api_secret)
        base = await ex.get_wallet_balance_on(first_ms)
    except Exception as e:
        print(f"  user {uid}: Binance fetch failed ({type(e).__name__}: {str(e)[:50]}) — skip")
        return
    finally:
        if ex.client:
            await ex.client.close_connection()

    if base is None:
        print(f"  user {uid}: first trade {first.date()} is outside the ~30-day snapshot "
              f"window — can't seed base, skip")
        return

    base_time = int(first.timestamp()) - 1
    print(f"  user {uid}: base=${base:.2f} at {first.date()} (before first trade) "
          f"{'-> WRITING' if commit else '(dry-run)'}")
    if commit:
        await db.set_state("dd_base", round(base, 8), user_id=uid, is_paper=False)
        await db.set_state("dd_base_time", base_time, user_id=uid, is_paper=False)


async def run(prod: bool, user: int | None, commit: bool):
    load_dotenv()
    await db.init_db(os.getenv("PROD_DATABASE_URL" if prod else "DATABASE_URL", ""))
    print(f"Seeding drawdown base ({'PROD' if prod else 'local'}, "
          f"{'COMMIT' if commit else 'dry-run'})")
    if user is not None:
        await seed_one(user, commit)
    else:
        for u, _cfg in await db.get_all_configured_users():
            await seed_one(u.id, commit)


def main():
    prod = "--prod" in sys.argv
    commit = "--commit" in sys.argv
    user = None
    if "--user" in sys.argv:
        user = int(sys.argv[sys.argv.index("--user") + 1])
    asyncio.run(run(prod, user, commit))


if __name__ == "__main__":
    main()
