from __future__ import annotations

import logging
import os

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

log = logging.getLogger(__name__)

engine = None
SessionLocal = None


def _run_migrations(database_url: str):
    sync_url = database_url
    if sync_url.startswith("postgresql+asyncpg://"):
        sync_url = sync_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    elif sync_url.startswith("postgresql://"):
        sync_url = sync_url.replace("postgresql://", "postgresql+psycopg2://", 1)

    alembic_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "alembic")
    alembic_ini = os.path.join(os.path.dirname(alembic_dir), "alembic.ini")

    cfg = Config(alembic_ini)
    cfg.set_main_option("sqlalchemy.url", sync_url)
    cfg.set_main_option("script_location", alembic_dir)
    command.upgrade(cfg, "head")
    log.info("Alembic migrations applied")


async def init_db(database_url: str):
    global engine, SessionLocal

    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    _run_migrations(database_url)

    engine = create_async_engine(database_url, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from app.db.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    log.info("Database initialized")


def get_session() -> AsyncSession:
    return SessionLocal()
