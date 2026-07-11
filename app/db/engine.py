from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

log = logging.getLogger(__name__)

engine = None
SessionLocal = None


async def _ensure_schema(eng):
    """Add columns/tables that migrations would create, idempotently."""
    async with eng.begin() as conn:
        # users columns
        for col, default in [
            ("is_approved", "true"),
            ("is_admin", "false"),
            ("is_rejected", "false"),
            ("paper_bot_started", "false"),
            ("totp_enabled", "false"),
        ]:
            await conn.execute(text(
                f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} BOOLEAN NOT NULL DEFAULT {default}"
            ))
        # nullable TOTP secret (Fernet-encrypted) + hashed one-time backup codes
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret_enc TEXT"
        ))
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_backup_codes TEXT"
        ))

        # trades columns
        for col, typ, default in [
            ("user_id", "INTEGER NOT NULL", "1"),
            ("is_paper", "BOOLEAN NOT NULL", "false"),
        ]:
            await conn.execute(text(
                f"ALTER TABLE trades ADD COLUMN IF NOT EXISTS {col} {typ} DEFAULT {default}"
            ))
        # nullable funding-fee column (income-based PnL breakdown)
        await conn.execute(text(
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS funding_fee DOUBLE PRECISION"
        ))
        # planned reward:risk per trade (adaptive RR regime: 2 or 3)
        await conn.execute(text(
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS target_rr DOUBLE PRECISION"
        ))
        await conn.execute(text(
            "ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS target_rr DOUBLE PRECISION"
        ))
        # which strategy opened the trade / produced the setup (amd_15m / trend_5m)
        await conn.execute(text(
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy VARCHAR(20)"
        ))
        await conn.execute(text(
            "ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS strategy VARCHAR(20)"
        ))
        # AMD setup geometry (acc/manip box coords) as JSON, for the live Position chart
        await conn.execute(text(
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS setup_json TEXT"
        ))

        # bot_events columns
        await conn.execute(text(
            "ALTER TABLE bot_events ADD COLUMN IF NOT EXISTS user_id INTEGER"
        ))
        await conn.execute(text(
            "ALTER TABLE bot_events ADD COLUMN IF NOT EXISTS is_paper BOOLEAN NOT NULL DEFAULT false"
        ))

        # bot_state columns
        await conn.execute(text(
            "ALTER TABLE bot_state ADD COLUMN IF NOT EXISTS user_id INTEGER"
        ))
        await conn.execute(text(
            "ALTER TABLE bot_state ADD COLUMN IF NOT EXISTS is_paper BOOLEAN NOT NULL DEFAULT false"
        ))

        # Drop old single-column unique constraint on bot_state.key
        old_exists = await conn.execute(text(
            "SELECT 1 FROM pg_constraint WHERE conname = 'bot_state_key_key'"
        ))
        if old_exists.scalar():
            await conn.execute(text(
                "ALTER TABLE bot_state DROP CONSTRAINT bot_state_key_key"
            ))
            log.info("Dropped old bot_state_key_key constraint")

        # bot_state unique constraint (required by set_state upsert)
        exists = await conn.execute(text(
            "SELECT 1 FROM pg_constraint WHERE conname = 'uq_bot_state_scoped'"
        ))
        if not exists.scalar():
            await conn.execute(text(
                "DELETE FROM bot_state a USING bot_state b "
                "WHERE a.id > b.id AND a.key = b.key "
                "AND a.user_id IS NOT DISTINCT FROM b.user_id "
                "AND a.is_paper = b.is_paper"
            ))
            await conn.execute(text(
                "ALTER TABLE bot_state ADD CONSTRAINT uq_bot_state_scoped UNIQUE (key, user_id, is_paper)"
            ))
            log.info("Created missing constraint uq_bot_state_scoped")

    log.info("Schema columns ensured")


async def init_db(database_url: str):
    global engine, SessionLocal

    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(database_url, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    await _ensure_schema(engine)

    from app.db.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    log.info("Database initialized")


def get_session() -> AsyncSession:
    return SessionLocal()
