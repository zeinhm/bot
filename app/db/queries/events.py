from __future__ import annotations

from sqlalchemy import select

from app.db.engine import get_session
from app.db.models import BotEvent


async def log_event(
    message: str,
    level: str = "info",
    category: str = "system",
    details: str | None = None,
    user_id: int | None = None,
    is_paper: bool = False,
):
    async with get_session() as session:
        session.add(BotEvent(
            message=message,
            level=level,
            category=category,
            details=details,
            user_id=user_id,
            is_paper=is_paper,
        ))
        await session.commit()


async def get_recent_events(
    limit: int = 50,
    user_id: int | None = None,
    is_paper: bool | None = None,
) -> list[BotEvent]:
    async with get_session() as session:
        q = select(BotEvent)
        if user_id is not None:
            q = q.where(BotEvent.user_id == user_id)
        if is_paper is not None:
            q = q.where(BotEvent.is_paper == is_paper)
        q = q.order_by(BotEvent.id.desc()).limit(limit)
        result = await session.execute(q)
        return list(result.scalars().all())
