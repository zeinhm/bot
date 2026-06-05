from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db.engine import get_session
from app.db.models import User, UserConfig


async def get_user(user_id: int) -> User | None:
    async with get_session() as session:
        return await session.get(User, user_id)


async def get_user_by_google_id(google_id: str) -> User | None:
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.google_id == google_id)
        )
        return result.scalar_one_or_none()


async def upsert_user_from_google(google_id: str, email: str, name: str, avatar_url: str) -> User:
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.google_id == google_id)
        )
        user = result.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if user:
            user.email = email
            user.name = name
            user.avatar_url = avatar_url
            user.last_login = now
        else:
            user = User(
                google_id=google_id,
                email=email,
                name=name,
                avatar_url=avatar_url,
                last_login=now,
            )
            session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def get_user_config(user_id: int) -> UserConfig | None:
    async with get_session() as session:
        result = await session.execute(
            select(UserConfig).where(UserConfig.user_id == user_id)
        )
        return result.scalar_one_or_none()


async def save_user_config(
    user_id: int,
    api_key_enc: str,
    api_secret_enc: str,
) -> UserConfig:
    async with get_session() as session:
        result = await session.execute(
            select(UserConfig).where(UserConfig.user_id == user_id)
        )
        cfg = result.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if cfg:
            cfg.binance_api_key_enc = api_key_enc
            cfg.binance_api_secret_enc = api_secret_enc
            cfg.binance_testnet = False
            cfg.updated_at = now
        else:
            cfg = UserConfig(
                user_id=user_id,
                binance_api_key_enc=api_key_enc,
                binance_api_secret_enc=api_secret_enc,
                binance_testnet=False,
            )
            session.add(cfg)
        await session.commit()
        await session.refresh(cfg)
        return cfg


async def get_all_configured_users() -> list[tuple[User, UserConfig]]:
    async with get_session() as session:
        result = await session.execute(
            select(User, UserConfig).join(UserConfig, User.id == UserConfig.user_id)
            .where(UserConfig.binance_api_key_enc.isnot(None))
        )
        return list(result.all())
