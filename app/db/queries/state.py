from __future__ import annotations

import json

from sqlalchemy import select

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
        if user_id is not None:
            result = await session.execute(
                select(BotState).where(
                    BotState.key == key, BotState.user_id == user_id, BotState.is_paper == is_paper
                )
            )
        else:
            result = await session.execute(
                select(BotState).where(
                    BotState.key == key, BotState.user_id.is_(None)
                )
            )
        row = result.scalar_one_or_none()
        val_str = json.dumps(value)
        if row:
            row.value = val_str
        else:
            session.add(BotState(key=key, value=val_str, user_id=user_id, is_paper=is_paper))
        await session.commit()
