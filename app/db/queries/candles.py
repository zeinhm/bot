from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.engine import get_session
from app.db.models import CandleBuffer, HistoricalCandle


async def save_candles(symbol: str, candles: list[dict]):
    if not candles:
        return
    async with get_session() as session:
        rows = []
        for c in candles:
            ts = c["timestamp"]
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            rows.append({
                "symbol": symbol,
                "timestamp": ts,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c["volume"],
            })

        stmt = pg_insert(CandleBuffer).values(rows)
        stmt = stmt.on_conflict_do_nothing(constraint="uq_candle")
        await session.execute(stmt)
        await session.commit()


async def get_candles(symbol: str, limit: int = 400) -> list[dict]:
    async with get_session() as session:
        result = await session.execute(
            select(CandleBuffer)
            .where(CandleBuffer.symbol == symbol)
            .order_by(CandleBuffer.timestamp.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())
        rows.reverse()
        return [
            {
                "timestamp": int(r.timestamp.timestamp() * 1000),
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
            }
            for r in rows
        ]


async def trim_candle_buffer(symbol: str, keep: int = 500):
    async with get_session() as session:
        count_result = await session.execute(
            select(func.count(CandleBuffer.id)).where(CandleBuffer.symbol == symbol)
        )
        count = count_result.scalar()
        if count <= keep:
            return
        cutoff_result = await session.execute(
            select(CandleBuffer.timestamp)
            .where(CandleBuffer.symbol == symbol)
            .order_by(CandleBuffer.timestamp.desc())
            .offset(keep)
            .limit(1)
        )
        cutoff = cutoff_result.scalar()
        if cutoff:
            await session.execute(
                delete(CandleBuffer).where(
                    CandleBuffer.symbol == symbol,
                    CandleBuffer.timestamp < cutoff,
                )
            )
            await session.commit()


async def get_historical_candles(
    symbol: str,
    interval: str,
    end: int | None = None,
    limit: int = 500,
) -> tuple[list[dict], bool]:
    async with get_session() as session:
        q = (
            select(HistoricalCandle)
            .where(HistoricalCandle.symbol == symbol, HistoricalCandle.interval == interval)
        )
        if end is not None:
            q = q.where(HistoricalCandle.timestamp <= end)
        q = q.order_by(HistoricalCandle.timestamp.desc()).limit(limit + 1)
        result = await session.execute(q)
        rows = list(result.scalars().all())

        has_more = len(rows) > limit
        rows = rows[:limit]
        rows.reverse()

        candles = [
            {"time": r.timestamp, "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "volume": r.volume}
            for r in rows
        ]
        return candles, has_more


async def get_historical_candle_range(symbol: str, interval: str) -> tuple:
    async with get_session() as session:
        from sqlalchemy import func as sqlfunc
        result = await session.execute(
            select(sqlfunc.min(HistoricalCandle.timestamp), sqlfunc.max(HistoricalCandle.timestamp))
            .where(HistoricalCandle.symbol == symbol, HistoricalCandle.interval == interval)
        )
        row = result.one()
        return row[0], row[1]


async def get_historical_candle_count(symbol: str, interval: str) -> int:
    async with get_session() as session:
        result = await session.execute(
            select(func.count(HistoricalCandle.id))
            .where(HistoricalCandle.symbol == symbol, HistoricalCandle.interval == interval)
        )
        return int(result.scalar() or 0)


async def get_all_historical_candles(symbol: str, interval: str) -> list[dict]:
    async with get_session() as session:
        result = await session.execute(
            select(HistoricalCandle)
            .where(HistoricalCandle.symbol == symbol, HistoricalCandle.interval == interval)
            .order_by(HistoricalCandle.timestamp.asc())
        )
        return [
            {"time": r.timestamp, "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "volume": r.volume}
            for r in result.scalars().all()
        ]
