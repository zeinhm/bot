from __future__ import annotations

from typing import Optional

from app.bot import get_bot, get_bot_for_user
import app.db as db


async def get_global_context(user_id: Optional[int] = None, mode: str = "live") -> dict:
    is_paper = (mode == "paper")

    if user_id is not None:
        bot = get_bot_for_user(user_id, mode)
    else:
        bot = get_bot()

    balance = 0.0
    if bot and bot.exchange.client:
        try:
            balance = await bot.exchange.get_balance()
        except Exception:
            pass

    bot_enabled = await db.get_state("bot_enabled", True, user_id=user_id, is_paper=is_paper)

    return {
        "bot_running": bot is not None and bot.running,
        "bot_status": bot.status if bot else "stopped",
        "bot_enabled": bot_enabled,
        "balance": balance,
        "trading_mode": mode,
        "is_paper": is_paper,
    }
