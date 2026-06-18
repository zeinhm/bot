from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, func, delete

from app.db.engine import get_session
from app.db.models import Trade

log = logging.getLogger(__name__)


async def clear_user_trades(user_id: int, is_paper: bool = False):
    """Delete a user's trades for one mode. Used by the seeder to refresh the
    admin's seeded 'live history' (user_id=1, is_paper=False) — strictly scoped so
    real paper/live trades for other users are untouched."""
    async with get_session() as session:
        await session.execute(
            delete(Trade).where(Trade.user_id == user_id, Trade.is_paper == is_paper)
        )
        await session.commit()


async def bulk_create_trades(rows: list[dict]):
    """Insert many fully-formed Trade rows (used by the seeder)."""
    async with get_session() as session:
        for r in rows:
            session.add(Trade(**r))
        await session.commit()


async def create_trade(trade_data: dict) -> Trade:
    async with get_session() as session:
        trade = Trade(**trade_data)
        session.add(trade)
        await session.commit()
        await session.refresh(trade)
        log.info("Created trade #%s %s %s", trade.id, trade.symbol, trade.direction)
        return trade


async def update_trade(trade_id: int, updates: dict):
    async with get_session() as session:
        trade = await session.get(Trade, trade_id)
        if trade:
            for k, v in updates.items():
                setattr(trade, k, v)
            await session.commit()


async def get_trade(trade_id: int) -> Trade | None:
    async with get_session() as session:
        return await session.get(Trade, trade_id)


async def get_open_trade(user_id: int, is_paper: bool = False) -> Trade | None:
    async with get_session() as session:
        result = await session.execute(
            select(Trade)
            .where(Trade.user_id == user_id, Trade.is_paper == is_paper, Trade.result == "open")
            .limit(1)
        )
        return result.scalar_one_or_none()


async def get_open_trades(user_id: int, is_paper: bool = False) -> list[Trade]:
    """All currently-open trades for a user (one per asset under per-asset mode)."""
    async with get_session() as session:
        result = await session.execute(
            select(Trade)
            .where(Trade.user_id == user_id, Trade.is_paper == is_paper, Trade.result == "open")
        )
        return list(result.scalars().all())


async def get_open_trade_for_symbol(user_id: int, symbol: str, is_paper: bool = False) -> Trade | None:
    """The open trade for a specific symbol (used to close the right trade on fill)."""
    async with get_session() as session:
        result = await session.execute(
            select(Trade)
            .where(
                Trade.user_id == user_id,
                Trade.is_paper == is_paper,
                Trade.result == "open",
                Trade.symbol == symbol,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()


async def get_recent_trades(limit: int = 50, user_id: int | None = None, is_paper: bool = False) -> list[Trade]:
    async with get_session() as session:
        q = select(Trade).where(Trade.is_paper == is_paper)
        if user_id is not None:
            q = q.where(Trade.user_id == user_id)
        q = q.order_by(Trade.id.desc()).limit(limit)
        result = await session.execute(q)
        return list(result.scalars().all())


async def get_all_trades(user_id: int, is_paper: bool = False) -> list[Trade]:
    async with get_session() as session:
        result = await session.execute(
            select(Trade)
            .where(Trade.user_id == user_id, Trade.is_paper == is_paper)
            .order_by(Trade.id.asc())
        )
        return list(result.scalars().all())


async def get_trades_filtered(
    user_id: int,
    is_paper: bool = False,
    symbol: str | None = None,
    direction: str | None = None,
    result_filter: str | None = None,
) -> list[Trade]:
    async with get_session() as session:
        q = select(Trade).where(Trade.user_id == user_id, Trade.is_paper == is_paper)
        if symbol:
            q = q.where(Trade.symbol == symbol)
        if direction:
            q = q.where(Trade.direction == direction)
        if result_filter:
            q = q.where(Trade.result == result_filter)
        q = q.order_by(Trade.id.desc())
        result = await session.execute(q)
        return list(result.scalars().all())


async def get_today_pnl(user_id: int, is_paper: bool = False) -> float:
    async with get_session() as session:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await session.execute(
            select(func.coalesce(func.sum(Trade.pnl_usdt), 0.0))
            .where(Trade.user_id == user_id, Trade.is_paper == is_paper)
            .where(Trade.exit_time >= today)
            .where(Trade.result != "open")
        )
        return float(result.scalar())


async def get_today_trade_count(user_id: int, is_paper: bool = False) -> int:
    async with get_session() as session:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await session.execute(
            select(func.count(Trade.id))
            .where(Trade.user_id == user_id, Trade.is_paper == is_paper)
            .where(Trade.entry_time >= today)
        )
        return int(result.scalar())
