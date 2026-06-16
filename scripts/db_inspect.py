"""Quick local DB inspection — avoids hand-writing SQLAlchemy each time.

Run from the bot/ dir:
    python -m scripts.db_inspect summary          # everything at a glance
    python -m scripts.db_inspect trades           # trades by (user, paper), WR, equity
    python -m scripts.db_inspect backtest         # backtest_results summary (+ target_rr split)
    python -m scripts.db_inspect users
    python -m scripts.db_inspect candles          # historical_candles coverage

Read-only. Uses DATABASE_URL from .env.
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import text

import app.db as db
from app.db.engine import get_session


async def _q(s, sql):
    return (await s.execute(text(sql))).all()


async def users(s):
    print("== users ==")
    for r in await _q(s, "select id, email, is_admin, is_approved from users order by id"):
        print("  ", tuple(r))


async def trades(s):
    print("== trades by (user_id, is_paper) ==")
    for r in await _q(s, """select user_id, is_paper, count(*),
                            sum((result='win')::int) as wins,
                            min(entry_time)::date, max(entry_time)::date,
                            round((10000+coalesce(sum(pnl_usdt),0))::numeric,0) as equity_10k
                            from trades group by user_id, is_paper order by user_id, is_paper"""):
        u, p, n, w, lo, hi, eq = r
        wr = f"{w/n*100:.1f}%" if n else "-"
        print(f"   user={u} paper={p}  n={n}  WR={wr}  {lo}..{hi}  eq(from 10k)=${eq:,}")
    print("== trades target_rr split ==")
    for r in await _q(s, """select coalesce(target_rr::text,'NULL') as tgt, count(*)
                            from trades group by tgt order by tgt"""):
        print("  ", tuple(r))


async def backtest(s):
    print("== backtest_results ==")
    for r in await _q(s, """select count(*), sum((result='win')::int) as wins,
                            round((10000+coalesce(sum(pnl_usdt),0))::numeric,0) as equity,
                            min(entry_time)::date, max(entry_time)::date
                            from backtest_results"""):
        n, w, eq, lo, hi = r
        wr = f"{w/n*100:.1f}%" if n else "-"
        print(f"   n={n}  WR={wr}  eq(from 10k)=${eq:,}  {lo}..{hi}")
    print("== by symbol ==")
    for r in await _q(s, """select symbol, count(*), sum((result='win')::int) as wins,
                            sum((target_rr>=2.5)::int) as trend_3to1
                            from backtest_results group by symbol order by symbol"""):
        sym, n, w, t3 = r
        print(f"   {sym}: n={n}  wins={w} ({w/n*100:.1f}%)  3:1={t3}")


async def candles(s):
    print("== historical_candles ==")
    for r in await _q(s, """select interval, symbol, count(*),
                            to_timestamp(min(timestamp))::date, to_timestamp(max(timestamp))::date
                            from historical_candles group by interval, symbol order by interval, symbol"""):
        print("  ", tuple(r))


CMDS = {"users": users, "trades": trades, "backtest": backtest, "candles": candles}


async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    load_dotenv()
    await db.init_db(os.getenv("DATABASE_URL", ""))
    async with get_session() as s:
        if cmd == "summary":
            for fn in (users, trades, backtest, candles):
                await fn(s); print()
        elif cmd in CMDS:
            await CMDS[cmd](s)
        else:
            print(f"unknown command '{cmd}'. options: summary, {', '.join(CMDS)}")


if __name__ == "__main__":
    asyncio.run(main())
