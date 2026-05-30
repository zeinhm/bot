from __future__ import annotations

import asyncio
import logging

from app.bot.worker import BotWorker, BotConfig

log = logging.getLogger(__name__)


class BotManager:
    def __init__(self):
        self.workers: dict[int, BotWorker] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    async def start_bot(self, user_id: int, config: BotConfig, broadcast_fn=None):
        if user_id in self.workers and self.workers[user_id].running:
            raise RuntimeError(f"Bot already running for user {user_id}")

        worker = BotWorker(user_id, config, broadcast_fn=broadcast_fn)
        self.workers[user_id] = worker
        self._tasks[user_id] = asyncio.create_task(worker.start())
        log.info("BotManager: started bot for user %d", user_id)

    async def stop_bot(self, user_id: int):
        if user_id not in self.workers:
            raise RuntimeError(f"No bot running for user {user_id}")

        worker = self.workers[user_id]
        await worker.stop()

        task = self._tasks.pop(user_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        del self.workers[user_id]
        log.info("BotManager: stopped bot for user %d", user_id)

    def get_worker(self, user_id: int) -> BotWorker | None:
        return self.workers.get(user_id)

    def get_status(self, user_id: int) -> str:
        worker = self.workers.get(user_id)
        if worker is None:
            return "stopped"
        return "running" if worker.running else "stopped"

    def get_all_statuses(self) -> dict[int, bool]:
        return {uid: w.running for uid, w in self.workers.items()}
