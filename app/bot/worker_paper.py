"""PaperWorker — simulated trading worker.

Implements BaseWorker's data hooks against the DB-backed PaperExchange and
PaperOrder rows. Everything live-specific (Telegram alerts, conditional/algo
order accounting, orphan sweep, force-close-breach, self-heal) is a no-op here,
inherited from BaseWorker — paper and live never share execution logic.
"""

from __future__ import annotations

import logging

import app.db as db

from app.bot.worker import BaseWorker

log = logging.getLogger(__name__)


class PaperWorker(BaseWorker):
    MODE = "paper"

    def _create_exchange(self):
        from paper_exchange import PaperExchange
        return PaperExchange(self.user_id, self._shared_market)

    async def _resolve_exit(self, trade):
        # A paper position closes when its SL/TP PaperOrder fills.
        for po in await db.get_paper_orders_for_trade(trade.id):
            if po.status == "FILLED":
                is_sl = po.order_type == "STOP_MARKET"
                exit_comm = po.stop_price * trade.quantity * self.config.commission_pct
                return po.stop_price, is_sl, exit_comm
        return None

    async def _count_active_sltp(self, trade):
        orders = await self.exchange.get_open_orders(trade.symbol)
        sl_count = sum(1 for o in orders if o.get("type") == "STOP_MARKET")
        tp_count = sum(1 for o in orders if o.get("type") == "TAKE_PROFIT_MARKET")
        return sl_count, tp_count
