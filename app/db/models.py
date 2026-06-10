from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, Float, String, DateTime, Text, Boolean, UniqueConstraint,
    ForeignKey, Index,
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
    pnl_usdt = Column(Float)         # net realized PnL (matches Binance Position History)
    commission = Column(Float)       # trading fee, positive USDT cost
    funding_fee = Column(Float)      # funding over the position's life, signed USDT
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
    paper_bot_started = Column(Boolean, default=False, nullable=False)


class UserConfig(Base):
    __tablename__ = "user_configs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    binance_api_key_enc = Column(Text)
    binance_api_secret_enc = Column(Text)
    binance_testnet = Column(Boolean, default=False)
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


class BacktestRun(Base):
    """Cached result of one amd_engine run, keyed by (strategy, params, data) signature.

    Lets the backtester skip re-simulating ~210k bars on every call. A row with
    symbol="COMBINED" stores the multi-asset combined stats (no setup rows).
    """
    __tablename__ = "backtest_runs"
    __table_args__ = (
        UniqueConstraint("signature", name="uq_backtest_run_sig"),
    )

    id = Column(Integer, primary_key=True)
    signature = Column(String(64), nullable=False, index=True)  # sha256 hex
    strategy = Column(String(50), nullable=False, default="amd_fvg_v1")
    symbol = Column(String(20), nullable=False)
    interval = Column(String(5), nullable=False)
    params_hash = Column(String(64), nullable=False)
    data_first_ts = Column(Integer)
    data_last_ts = Column(Integer)
    candle_count = Column(Integer)
    total_setups = Column(Integer, nullable=False, default=0)
    stats = Column(Text)  # json.dumps of the stats dict
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class BacktestSetup(Base):
    """One setup of a cached BacktestRun. Indexed for nav (ordinal) and chart (entry_time)."""
    __tablename__ = "backtest_setups"
    __table_args__ = (
        Index("ix_bt_setup_run_ordinal", "run_id", "ordinal"),
        Index("ix_bt_setup_run_entry", "run_id", "entry_time"),
    )

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False)
    ordinal = Column(Integer, nullable=False)      # 0..N-1 in time order
    entry_time = Column(Integer, nullable=False)   # seconds UTC — range queries
    data = Column(Text, nullable=False)            # json.dumps of the full setup dict
