"""
SQLAlchemy models and async database operations.

Tables: trades, bot_state, candle_buffer
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, Float, String, DateTime, Text, Boolean, UniqueConstraint,
    ForeignKey, select, delete, func,
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    direction = Column(String(10), nullable=False)
    entry_time = Column(DateTime(timezone=True))
    exit_time = Column(DateTime(timezone=True))
    entry_price = Column(Float)
    exit_price = Column(Float)
    sl_price = Column(Float)
    tp_price = Column(Float)
    quantity = Column(Float)
    result = Column(String(10), default="open")
    r_value = Column(Float)
    pnl_usdt = Column(Float)
    commission = Column(Float)
    sl_order_id = Column(String(50))
    tp_order_id = Column(String(50))
    entry_order_id = Column(String(50))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class BotState(Base):
    __tablename__ = "bot_state"

    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text)


class HistoricalCandle(Base):
    __tablename__ = "historical_candles"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "timestamp", name="uq_hist_candle"),
    )

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    interval = Column(String(5), nullable=False, index=True)
    timestamp = Column(Integer, nullable=False, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)


class BotEvent(Base):
    __tablename__ = "bot_events"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    level = Column(String(10), nullable=False, default="info")
    category = Column(String(30), nullable=False, default="system")
    message = Column(Text, nullable=False)
    details = Column(Text)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    google_id = Column(String(100), unique=True, nullable=False)
    email = Column(String(255), nullable=False)
    name = Column(String(255))
    avatar_url = Column(Text)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_login = Column(DateTime(timezone=True))


class UserConfig(Base):
    __tablename__ = "user_configs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    binance_api_key_enc = Column(Text)
    binance_api_secret_enc = Column(Text)
    binance_testnet = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True))


class CandleBuffer(Base):
    __tablename__ = "candle_buffer"
    __table_args__ = (
        UniqueConstraint("symbol", "timestamp", name="uq_candle"),
    )

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)


engine = None
SessionLocal = None


async def init_db(database_url: str):
    global engine, SessionLocal

    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(database_url, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    log.info("Database initialized")


def get_session() -> AsyncSession:
    return SessionLocal()


# --- Trade CRUD ---

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


async def get_open_trade() -> Trade | None:
    async with get_session() as session:
        result = await session.execute(
            select(Trade).where(Trade.result == "open").limit(1)
        )
        return result.scalar_one_or_none()


async def get_recent_trades(limit: int = 50) -> list[Trade]:
    async with get_session() as session:
        result = await session.execute(
            select(Trade).order_by(Trade.id.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def get_all_trades() -> list[Trade]:
    async with get_session() as session:
        result = await session.execute(
            select(Trade).order_by(Trade.id.asc())
        )
        return list(result.scalars().all())


async def get_trades_filtered(
    symbol: str | None = None,
    direction: str | None = None,
    result_filter: str | None = None,
) -> list[Trade]:
    async with get_session() as session:
        q = select(Trade)
        if symbol:
            q = q.where(Trade.symbol == symbol)
        if direction:
            q = q.where(Trade.direction == direction)
        if result_filter:
            q = q.where(Trade.result == result_filter)
        q = q.order_by(Trade.id.desc())
        result = await session.execute(q)
        return list(result.scalars().all())


async def get_today_pnl() -> float:
    async with get_session() as session:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await session.execute(
            select(func.coalesce(func.sum(Trade.pnl_usdt), 0.0))
            .where(Trade.exit_time >= today)
            .where(Trade.result != "open")
        )
        return float(result.scalar())


async def get_today_trade_count() -> int:
    async with get_session() as session:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await session.execute(
            select(func.count(Trade.id)).where(Trade.entry_time >= today)
        )
        return int(result.scalar())


# --- Bot State CRUD ---

async def get_state(key: str, default=None):
    async with get_session() as session:
        result = await session.execute(
            select(BotState).where(BotState.key == key)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return default
        try:
            return json.loads(row.value)
        except (json.JSONDecodeError, TypeError):
            return row.value


async def set_state(key: str, value):
    async with get_session() as session:
        result = await session.execute(
            select(BotState).where(BotState.key == key)
        )
        row = result.scalar_one_or_none()
        val_str = json.dumps(value)
        if row:
            row.value = val_str
        else:
            session.add(BotState(key=key, value=val_str))
        await session.commit()


# --- Candle Buffer CRUD ---

async def save_candles(symbol: str, candles: list[dict]):
    async with get_session() as session:
        timestamps = []
        for c in candles:
            ts = c["timestamp"]
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            timestamps.append(ts)

        existing_result = await session.execute(
            select(CandleBuffer.timestamp).where(
                CandleBuffer.symbol == symbol,
                CandleBuffer.timestamp.in_(timestamps),
            )
        )
        existing_ts = {row[0] for row in existing_result.all()}

        for c, ts in zip(candles, timestamps):
            if ts in existing_ts:
                continue
            session.add(CandleBuffer(
                symbol=symbol,
                timestamp=ts,
                open=c["open"],
                high=c["high"],
                low=c["low"],
                close=c["close"],
                volume=c["volume"],
            ))
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


# --- Historical Candles ---

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


# --- Bot Events ---

async def log_event(
    message: str,
    level: str = "info",
    category: str = "system",
    details: str | None = None,
):
    async with get_session() as session:
        session.add(BotEvent(
            message=message,
            level=level,
            category=category,
            details=details,
        ))
        await session.commit()


async def get_recent_events(limit: int = 50) -> list[BotEvent]:
    async with get_session() as session:
        result = await session.execute(
            select(BotEvent).order_by(BotEvent.id.desc()).limit(limit)
        )
        return list(result.scalars().all())


# --- User CRUD ---

async def get_user(user_id: int) -> User | None:
    async with get_session() as session:
        return await session.get(User, user_id)


async def get_user_by_google_id(google_id: str) -> User | None:
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.google_id == google_id)
        )
        return result.scalar_one_or_none()


async def upsert_user_from_google(google_id: str, email: str, name: str, avatar_url: str) -> User:
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.google_id == google_id)
        )
        user = result.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if user:
            user.email = email
            user.name = name
            user.avatar_url = avatar_url
            user.last_login = now
        else:
            user = User(
                google_id=google_id,
                email=email,
                name=name,
                avatar_url=avatar_url,
                last_login=now,
            )
            session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


# --- UserConfig CRUD ---

async def get_user_config(user_id: int) -> UserConfig | None:
    async with get_session() as session:
        result = await session.execute(
            select(UserConfig).where(UserConfig.user_id == user_id)
        )
        return result.scalar_one_or_none()


async def save_user_config(
    user_id: int,
    api_key_enc: str,
    api_secret_enc: str,
    testnet: bool,
) -> UserConfig:
    async with get_session() as session:
        result = await session.execute(
            select(UserConfig).where(UserConfig.user_id == user_id)
        )
        cfg = result.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if cfg:
            cfg.binance_api_key_enc = api_key_enc
            cfg.binance_api_secret_enc = api_secret_enc
            cfg.binance_testnet = testnet
            cfg.updated_at = now
        else:
            cfg = UserConfig(
                user_id=user_id,
                binance_api_key_enc=api_key_enc,
                binance_api_secret_enc=api_secret_enc,
                binance_testnet=testnet,
            )
            session.add(cfg)
        await session.commit()
        await session.refresh(cfg)
        return cfg


async def get_all_configured_users() -> list[tuple[User, UserConfig]]:
    async with get_session() as session:
        result = await session.execute(
            select(User, UserConfig).join(UserConfig, User.id == UserConfig.user_id)
            .where(UserConfig.binance_api_key_enc.isnot(None))
        )
        return list(result.all())
