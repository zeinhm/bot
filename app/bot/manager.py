from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from app.bot.worker import BotWorker, BotConfig, make_worker

if TYPE_CHECKING:
    from app.bot.shared_market import SharedMarketData

log = logging.getLogger(__name__)

WorkerKey = tuple[int, str]


class BotManager:
    def __init__(self):
        self.workers: dict[WorkerKey, BotWorker] = {}
        self._tasks: dict[WorkerKey, asyncio.Task] = {}

    async def start_bot(
        self,
        user_id: int,
        mode: str,
        config: BotConfig,
        broadcast_fn=None,
        shared_market: SharedMarketData | None = None,
    ):
        key = (user_id, mode)
        if key in self.workers and self.workers[key].running:
            raise RuntimeError(f"Bot already running for user {user_id} mode {mode}")

        # Build the right worker for the mode (LiveWorker / PaperWorker).
        worker = make_worker(user_id, config, broadcast_fn=broadcast_fn, shared_market=shared_market)
        self.workers[key] = worker
        task = asyncio.create_task(worker.start())
        task.add_done_callback(lambda t: self._on_worker_done(key, t))
        self._tasks[key] = task
        log.info("BotManager: started %s bot for user %d", mode, user_id)

    def _on_worker_done(self, key: WorkerKey, task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            worker = self.workers.get(key)
            if worker:
                worker.status = "error"
                worker.last_error = str(exc)
                worker.last_error_time = time.time()
            log.error("BotManager: worker %s crashed: %s", key, exc)

    async def stop_bot(self, user_id: int, mode: str):
        key = (user_id, mode)
        if key not in self.workers:
            raise RuntimeError(f"No bot running for user {user_id} mode {mode}")

        worker = self.workers[key]
        await worker.stop()

        task = self._tasks.pop(key, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        del self.workers[key]
        log.info("BotManager: stopped %s bot for user %d", mode, user_id)

    def get_worker(self, user_id: int, mode: str = "live") -> BotWorker | None:
        return self.workers.get((user_id, mode))

    def get_any_worker(self, user_id: int) -> BotWorker | None:
        return self.workers.get((user_id, "live")) or self.workers.get((user_id, "paper"))

    def get_status(self, user_id: int, mode: str = "live") -> str:
        worker = self.workers.get((user_id, mode))
        if worker is None:
            return "stopped"
        return worker.status

    def get_all_statuses(self) -> dict[WorkerKey, bool]:
        return {key: w.running for key, w in self.workers.items()}

    def get_all_bot_info(self) -> list[dict]:
        result = []
        for (uid, mode), worker in self.workers.items():
            result.append({
                "user_id": uid,
                "mode": mode,
                "status": worker.status,
                "running": worker.running,
                "uptime_secs": int(time.time() - worker.started_at) if worker.started_at and worker.running else None,
                "last_error": worker.last_error,
                "last_error_time": worker.last_error_time,
                "symbols": worker.config.symbols if worker.config else [],
            })
        return result
