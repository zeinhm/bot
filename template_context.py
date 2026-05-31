from __future__ import annotations

from typing import Optional

from bot import get_bot, get_bot_for_user
import database as db


async def get_global_context(user_id: Optional[int] = None) -> dict:
    if user_id is not None:
        bot = get_bot_for_user(user_id)
    else:
        bot = get_bot()

    balance = 0.0
    if bot and bot.exchange.client:
        try:
            balance = await bot.exchange.get_balance()
        except Exception:
            pass

    bot_enabled = await db.get_state("bot_enabled", True)

    return {
        "bot_running": bot is not None and bot.running,
        "bot_status": bot.status if bot else "stopped",
        "bot_enabled": bot_enabled,
        "balance": balance,
    }
