from __future__ import annotations

from sqlalchemy import select, delete

from app.db.engine import get_session
from app.db.models import BacktestResult


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
