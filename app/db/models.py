from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, Float, String, DateTime, Text, Boolean, UniqueConstraint,
    ForeignKey,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    is_paper = Column(Boolean, default=False, nullable=False)
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
    __table_args__ = (
        UniqueConstraint("key", "user_id", "is_paper", name="uq_bot_state_scoped"),
    )

    id = Column(Integer, primary_key=True)
    key = Column(String(100), nullable=False)
    value = Column(Text)
    user_id = Column(Integer, nullable=True)
    is_paper = Column(Boolean, default=False, nullable=False)


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
    user_id = Column(Integer, nullable=True)
    is_paper = Column(Boolean, default=False, nullable=False)
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

    is_approved = Column(Boolean, default=False, nullable=False)
    is_rejected = Column(Boolean, default=False, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)


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


class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    direction = Column(String(10), nullable=False)
    entry_time = Column(DateTime(timezone=True))
    exit_time = Column(DateTime(timezone=True))
    entry_price = Column(Float)
    exit_price = Column(Float)
    sl_price = Column(Float)
    tp_price = Column(Float)
    quantity = Column(Float)
    result = Column(String(10), nullable=False)
    r_value = Column(Float)
    pnl_usdt = Column(Float)
    commission = Column(Float)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class RejectionLog(Base):
    __tablename__ = "rejection_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    email = Column(String(255), nullable=False)
    name = Column(String(255))
    avatar_url = Column(Text)
    rejected_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    status = Column(String(20), nullable=False, default="rejected")
    allowed_at = Column(DateTime(timezone=True), nullable=True)


class PaperAccount(Base):
    __tablename__ = "paper_accounts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    balance = Column(Float, nullable=False, default=10000.0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PaperOrder(Base):
    __tablename__ = "paper_orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    trade_id = Column(Integer, ForeignKey("trades.id"), nullable=False)
    symbol = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)
    order_type = Column(String(30), nullable=False)
    quantity = Column(Float, nullable=False)
    stop_price = Column(Float, nullable=False)
    status = Column(String(20), nullable=False, default="NEW")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    filled_at = Column(DateTime(timezone=True), nullable=True)
