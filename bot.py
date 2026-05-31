"""
Compatibility shim — routes the old bot.py API through BotManager + BotWorker.

Existing routes import get_bot, register_ws, unregister_ws from here.
The actual logic now lives in app/bot/worker.py.
"""

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


def make_broadcast_fn(user_id: int):
    async def _broadcast(data: dict):
        from app.bot.websocket import ws_manager
        await ws_manager.send_to_user(user_id, data)
    return _broadcast


def get_bot() -> BotWorker | None:
    if _bot_manager is None:
        return None
    return _bot_manager.get_worker(PHASE1_USER_ID)


def get_bot_for_user(user_id: int) -> BotWorker | None:
    if _bot_manager is None:
        return None
    return _bot_manager.get_worker(user_id)
