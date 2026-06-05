from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.bot.worker import BotWorker

log = logging.getLogger(__name__)

_ws_clients: set = set()
_bot_manager = None

PHASE1_USER_ID = 1


def set_bot_manager(manager):
    global _bot_manager
    _bot_manager = manager


def register_ws(ws):
    _ws_clients.add(ws)


def unregister_ws(ws):
    _ws_clients.discard(ws)


async def broadcast(data: dict):
    from app.bot.websocket import ws_manager
    await ws_manager.send_to_user(PHASE1_USER_ID, data)

    msg = json.dumps(data)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _ws_clients -= dead


def make_broadcast_fn(user_id: int, mode: str = "live"):
    async def _broadcast(data: dict):
        from app.bot.websocket import ws_manager
        data["mode"] = mode
        await ws_manager.send_to_user(user_id, data)
    return _broadcast


def get_bot() -> BotWorker | None:
    if _bot_manager is None:
        return None
    return _bot_manager.get_worker(PHASE1_USER_ID, "live")


def get_bot_for_user(user_id: int, mode: str = "live") -> BotWorker | None:
    if _bot_manager is None:
        return None
    return _bot_manager.get_worker(user_id, mode)


def build_user_config(api_key: str, api_secret: str):
    from app.bot.worker import BotConfig
    from config import (
        SYMBOLS, STRATEGY_PARAMS, LEVERAGE, COMMISSION_PCT,
        SLIPPAGE_TICKS, TICK_SIZE, CANDLE_BUFFER_SIZE, LOT_SIZE,
    )
    return BotConfig(
        api_key=api_key,
        api_secret=api_secret,
        symbols=SYMBOLS,
        strategy_params=STRATEGY_PARAMS,
        leverage=LEVERAGE,
        commission_pct=COMMISSION_PCT,
        slippage_ticks=SLIPPAGE_TICKS,
        tick_size=TICK_SIZE,
        lot_size=LOT_SIZE,
        candle_buffer_size=CANDLE_BUFFER_SIZE,
        is_paper=False,
    )


def build_paper_config(paper_balance: float = 10000.0):
    from app.bot.worker import BotConfig
    from config import (
        SYMBOLS, STRATEGY_PARAMS, LEVERAGE, COMMISSION_PCT,
        SLIPPAGE_TICKS, TICK_SIZE, CANDLE_BUFFER_SIZE, LOT_SIZE,
    )
    return BotConfig(
        api_key="",
        api_secret="",
        symbols=SYMBOLS,
        strategy_params=STRATEGY_PARAMS,
        leverage=LEVERAGE,
        commission_pct=COMMISSION_PCT,
        slippage_ticks=SLIPPAGE_TICKS,
        tick_size=TICK_SIZE,
        lot_size=LOT_SIZE,
        candle_buffer_size=CANDLE_BUFFER_SIZE,
        is_paper=True,
        paper_balance=paper_balance,
    )
