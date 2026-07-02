"""Backfill static R on existing trades.

A win banks its planned target RR (2 or 3); a loss is -1R. Historical rows carry
R recomputed from tick-rounded / wick-widened SL/TP geometry (e.g. 1.76R), which
the app now shows as static. This normalizes stored `r_value` to match.

Safe: only closed trades; wins only where `target_rr` is known (legacy rows
without it are left alone); PnL is never touched (that's real money).

    python -m scripts.backfill_static_r          # local DATABASE_URL
    python -m scripts.backfill_static_r --prod   # PROD_DATABASE_URL
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import text

import app.db as db
from app.db.engine import get_session


async def run(prod: bool):
    load_dotenv()
    url = os.getenv("PROD_DATABASE_URL" if prod else "DATABASE_URL", "")
    await db.init_db(url)
    async with get_session() as s:
        win = await s.execute(text(
            "update trades set r_value = target_rr "
            "where result='win' and target_rr is not null and r_value is distinct from target_rr"))
        loss = await s.execute(text(
            "update trades set r_value = -1.0 where result='loss' and r_value is distinct from -1.0"))
        await s.commit()
        print(f"{'PROD' if prod else 'LOCAL'}: wins set to target_rr = {win.rowcount}, "
              f"losses normalized to -1 = {loss.rowcount}")


if __name__ == "__main__":
    asyncio.run(run("--prod" in sys.argv))
