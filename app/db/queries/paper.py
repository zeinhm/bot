from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update

from app.db.engine import get_session
from app.db.models import PaperAccount, PaperOrder


async def get_or_create_paper_account(user_id: int, default_balance: float = 10000.0) -> PaperAccount:
    async with get_session() as session:
        result = await session.execute(
            select(PaperAccount).where(PaperAccount.user_id == user_id)
        )
        account = result.scalar_one_or_none()
        if account is None:
            account = PaperAccount(user_id=user_id, balance=default_balance)
            session.add(account)
            await session.commit()
            await session.refresh(account)
        return account


async def update_paper_balance(user_id: int, new_balance: float):
    async with get_session() as session:
        await session.execute(
            update(PaperAccount)
            .where(PaperAccount.user_id == user_id)
            .values(balance=new_balance)
        )
        await session.commit()


async def get_paper_balance(user_id: int) -> float:
    account = await get_or_create_paper_account(user_id)
    return account.balance


async def create_paper_order(order_data: dict) -> PaperOrder:
    async with get_session() as session:
        order = PaperOrder(**order_data)
        session.add(order)
        await session.commit()
        await session.refresh(order)
        return order


async def get_pending_paper_orders(user_id: int, symbol: str | None = None) -> list[PaperOrder]:
    async with get_session() as session:
        q = select(PaperOrder).where(
            PaperOrder.user_id == user_id,
            PaperOrder.status == "NEW",
        )
        if symbol:
            q = q.where(PaperOrder.symbol == symbol)
        result = await session.execute(q)
        return list(result.scalars().all())


async def get_paper_orders_for_trade(trade_id: int) -> list[PaperOrder]:
    async with get_session() as session:
        result = await session.execute(
            select(PaperOrder).where(PaperOrder.trade_id == trade_id)
        )
        return list(result.scalars().all())


async def fill_paper_order(order_id: int):
    async with get_session() as session:
        await session.execute(
            update(PaperOrder)
            .where(PaperOrder.id == order_id, PaperOrder.status == "NEW")
            .values(status="FILLED", filled_at=datetime.now(timezone.utc))
        )
        await session.commit()


async def cancel_paper_order(order_id: int):
    async with get_session() as session:
        await session.execute(
            update(PaperOrder)
            .where(PaperOrder.id == order_id, PaperOrder.status == "NEW")
            .values(status="CANCELLED")
        )
        await session.commit()


async def cancel_all_paper_orders(user_id: int, symbol: str):
    async with get_session() as session:
        await session.execute(
            update(PaperOrder)
            .where(
                PaperOrder.user_id == user_id,
                PaperOrder.symbol == symbol,
                PaperOrder.status == "NEW",
            )
            .values(status="CANCELLED")
        )
        await session.commit()
