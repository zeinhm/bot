from __future__ import annotations

import json

from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError

from app.db.engine import get_session
from app.db.models import BacktestResult, BacktestRun, BacktestSetup


async def get_backtest_results(symbol: str | None = None) -> list[BacktestResult]:
    async with get_session() as session:
        q = select(BacktestResult).order_by(BacktestResult.entry_time.asc())
        if symbol:
            q = q.where(BacktestResult.symbol == symbol)
        result = await session.execute(q)
        return list(result.scalars().all())


async def save_backtest_results(results: list[dict]):
    async with get_session() as session:
        for row in results:
            session.add(BacktestResult(**row))
        await session.commit()


async def clear_backtest_results():
    async with get_session() as session:
        await session.execute(delete(BacktestResult))
        await session.commit()


# ── Backtest run cache (keyed by strategy+params+data signature) ──────────────

async def get_backtest_run(signature: str) -> BacktestRun | None:
    async with get_session() as session:
        result = await session.execute(
            select(BacktestRun).where(BacktestRun.signature == signature)
        )
        return result.scalar_one_or_none()


async def save_backtest_run(run: dict, setups: list[dict]) -> int:
    """Persist a run + its setups. Concurrency-safe on the unique signature:
    a racing duplicate returns the existing run_id without re-inserting. Old
    versions for the same (strategy, symbol, interval) are intentionally NOT
    pruned here — deleting a run_id that another in-flight request/cache still
    references would drop its setups (corrupted the combined total)."""
    async with get_session() as session:
        row = BacktestRun(**run)
        session.add(row)
        try:
            await session.flush()  # assigns row.id; raises on duplicate signature
        except IntegrityError:
            await session.rollback()
            existing = await session.execute(
                select(BacktestRun).where(BacktestRun.signature == run["signature"])
            )
            return existing.scalar_one().id

        run_id = row.id
        for ordinal, s in enumerate(setups):
            session.add(BacktestSetup(
                run_id=run_id,
                ordinal=ordinal,
                entry_time=int(s["entryTime"]),
                data=json.dumps(s),
            ))
        await session.commit()
        return run_id


async def get_run_setups_all(run_id: int) -> list[dict]:
    async with get_session() as session:
        result = await session.execute(
            select(BacktestSetup)
            .where(BacktestSetup.run_id == run_id)
            .order_by(BacktestSetup.ordinal.asc())
        )
        return [json.loads(r.data) for r in result.scalars().all()]


async def get_run_setups_page(run_id: int, limit: int = 10, offset: int = 0) -> list[dict]:
    """Newest-first page (ordinal DESC), returned in ascending order for the chart."""
    async with get_session() as session:
        result = await session.execute(
            select(BacktestSetup)
            .where(BacktestSetup.run_id == run_id)
            .order_by(BacktestSetup.ordinal.desc())
            .limit(limit).offset(offset)
        )
        rows = list(result.scalars().all())
        rows.reverse()
        return [{**json.loads(r.data), "_ordinal": r.ordinal} for r in rows]


async def get_run_setups_range(run_id: int, from_ts: int, to_ts: int) -> list[dict]:
    async with get_session() as session:
        result = await session.execute(
            select(BacktestSetup)
            .where(
                BacktestSetup.run_id == run_id,
                BacktestSetup.entry_time >= from_ts,
                BacktestSetup.entry_time <= to_ts,
            )
            .order_by(BacktestSetup.entry_time.asc())
        )
        return [{**json.loads(r.data), "_ordinal": r.ordinal} for r in result.scalars().all()]
