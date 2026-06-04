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
        ]:
            await conn.execute(text(
                f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} BOOLEAN NOT NULL DEFAULT {default}"
            ))

        # trades columns
        for col, typ, default in [
            ("user_id", "INTEGER NOT NULL", "1"),
            ("is_paper", "BOOLEAN NOT NULL", "false"),
        ]:
            await conn.execute(text(
                f"ALTER TABLE trades ADD COLUMN IF NOT EXISTS {col} {typ} DEFAULT {default}"
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
