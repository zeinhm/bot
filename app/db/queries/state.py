from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.engine import get_session
from app.db.models import BotState


async def get_state(key: str, default=None, user_id: int | None = None, is_paper: bool = False):
    async with get_session() as session:
        if user_id is not None:
            result = await session.execute(
                select(BotState).where(
                    BotState.key == key, BotState.user_id == user_id, BotState.is_paper == is_paper
                )
            )
            row = result.scalar_one_or_none()
            if row is not None:
                try:
                    return json.loads(row.value)
                except (json.JSONDecodeError, TypeError):
                    return row.value

        result = await session.execute(
            select(BotState).where(
                BotState.key == key, BotState.user_id.is_(None)
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return default
        try:
            return json.loads(row.value)
        except (json.JSONDecodeError, TypeError):
            return row.value


async def set_state(key: str, value, user_id: int | None = None, is_paper: bool = False):
    async with get_session() as session:
        val_str = json.dumps(value)
        if user_id is not None:
            stmt = pg_insert(BotState).values(
                key=key, value=val_str, user_id=user_id, is_paper=is_paper
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_bot_state_scoped",
                set_={"value": val_str},
            )
            await session.execute(stmt)
        else:
            # NULL user_id: PG treats NULLs as distinct so ON CONFLICT won't match
            result = await session.execute(
                select(BotState).where(
                    BotState.key == key,
                    BotState.user_id.is_(None),
                    BotState.is_paper == is_paper,
                )
            )
            row = result.scalar_one_or_none()
            if row is not None:
                row.value = val_str
            else:
                session.add(BotState(key=key, value=val_str, user_id=None, is_paper=is_paper))
        await session.commit()
