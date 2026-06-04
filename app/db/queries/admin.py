from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, func

from app.db.engine import get_session
from app.db.models import User, Trade, BotEvent


async def get_all_approved_users() -> list[User]:
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.is_approved == True).order_by(User.last_login.desc().nullslast())
        )
        return list(result.scalars().all())


async def get_pending_users() -> list[User]:
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.is_approved == False, User.is_rejected == False)
            .order_by(User.created_at.desc())
        )
        return list(result.scalars().all())


async def get_platform_today_pnl() -> float:
    async with get_session() as session:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await session.execute(
            select(func.coalesce(func.sum(Trade.pnl_usdt), 0.0))
            .where(Trade.is_paper == False, Trade.exit_time >= today, Trade.result != "open")
        )
        return float(result.scalar())


async def get_platform_trade_count() -> int:
    async with get_session() as session:
        result = await session.execute(
            select(func.count(Trade.id)).where(Trade.is_paper == False, Trade.result != "open")
        )
        return int(result.scalar())


async def get_user_trade_summary(user_id: int, is_paper: bool = False) -> dict:
    async with get_session() as session:
        result = await session.execute(
            select(Trade).where(
                Trade.user_id == user_id, Trade.is_paper == is_paper, Trade.result != "open"
            ).order_by(Trade.id.asc())
        )
        trades = list(result.scalars().all())

    wins = sum(1 for t in trades if t.result == "win")
    losses = sum(1 for t in trades if t.result == "loss")
    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0
    total_r = sum(t.r_value or 0 for t in trades)
    total_pnl = sum(t.pnl_usdt or 0 for t in trades)

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_pnl = sum(
        t.pnl_usdt or 0 for t in trades
        if t.exit_time and t.exit_time >= today
    )

    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_r": total_r,
        "total_pnl": total_pnl,
        "today_pnl": today_pnl,
    }


async def get_all_platform_trades(is_paper: bool = False) -> list[Trade]:
    async with get_session() as session:
        result = await session.execute(
            select(Trade).where(Trade.is_paper == is_paper, Trade.result != "open")
            .order_by(Trade.id.asc())
        )
        return list(result.scalars().all())


async def get_admin_events(
    limit: int = 100,
    user_id: int | None = None,
    level: str | None = None,
    category: str | None = None,
    is_paper: bool | None = None,
) -> list[tuple[BotEvent, User | None]]:
    async with get_session() as session:
        q = select(BotEvent, User).outerjoin(User, BotEvent.user_id == User.id)
        if user_id is not None:
            q = q.where(BotEvent.user_id == user_id)
        if level is not None:
            q = q.where(BotEvent.level == level)
        if category is not None:
            q = q.where(BotEvent.category == category)
        if is_paper is not None:
            q = q.where(BotEvent.is_paper == is_paper)
        q = q.order_by(BotEvent.id.desc()).limit(limit)
        result = await session.execute(q)
        return list(result.tuples().all())
