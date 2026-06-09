"""LiveWorker — the real-money Binance Futures trading worker.

Implements BaseWorker's mode hooks against Binance: real market orders,
conditional/algo SL/TP orders, exit resolution from account fills, Telegram
alerts, post-close self-heal, orphan sweep, and the force-close-breach safety
net. None of this touches the paper path (PaperWorker).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from exchange import resolve_trade_exit, r_value_for_exit
from telegram_alert import alert_entry, alert_exit, alert_bot_started, alert_bot_stopped, send_private
import app.db as db

from app.bot.worker import BaseWorker

log = logging.getLogger(__name__)


class LiveWorker(BaseWorker):
    MODE = "live"

    def _create_exchange(self):
        from exchange import BinanceExchange
        return BinanceExchange(api_key=self.config.api_key, api_secret=self.config.api_secret)

    # --- Data hooks ---
    async def _resolve_exit(self, trade):
        # SL/TP are conditional/algo orders whose ids can't be looked up directly;
        # read the real closing fills (with realizedPnl) instead.
        info = await resolve_trade_exit(
            self.exchange.client, trade.symbol, trade.direction,
            trade.entry_order_id, trade.entry_time, trade.quantity,
            trade.sl_price, trade.tp_price,
        )
        if info is None:
            return None
        return info["exit_price"], info["is_sl"], info["exit_commission"]

    async def _count_active_sltp(self, trade):
        return await self._count_sltp_orders(trade)

    # --- Value hooks (read real fills) ---
    async def _entry_commission(self, symbol, order, fill_price, fill_qty):
        comm = fill_price * fill_qty * self.config.commission_pct
        trades = await self.exchange.get_trades_for_order(symbol, int(order.get("orderId", 0)))
        if trades:
            comm = sum(float(t.get("commission", 0)) for t in trades)
        return comm

    async def _exit_fill(self, symbol, order_id, fill_price, qty):
        exit_comm = fill_price * qty * self.config.commission_pct
        if order_id:
            exit_trades = await self.exchange.get_trades_for_order(symbol, int(order_id))
            if exit_trades:
                total_qty = sum(float(t["qty"]) for t in exit_trades)
                if total_qty > 0:
                    fill_price = sum(float(t["price"]) * float(t["qty"]) for t in exit_trades) / total_qty
                exit_comm = sum(float(t.get("commission", 0)) for t in exit_trades)
        return fill_price, exit_comm

    # --- Side-effect hooks ---
    async def _alert_started(self):
        await alert_bot_started()

    async def _alert_stopped(self, reason: str):
        await alert_bot_stopped(reason)

    async def _alert_entry(self, symbol, signal, fill_price, fill_qty, balance):
        await alert_entry(symbol, signal["direction"], fill_price, signal["sl"], signal["tp"], fill_qty, balance)

    async def _on_trade_closed(self, trade, result, exit_price, pnl, r_value, balance):
        await alert_exit(trade.symbol, trade.direction, result, trade.entry_price, exit_price, pnl, r_value, balance)
        await self._self_heal_trade(trade.id)

    async def _prepare_entry(self, symbol):
        existing_orders = await self.exchange.get_open_orders(symbol)
        existing_cond = await self.exchange.get_conditional_orders(symbol)
        if existing_orders or existing_cond:
            log.warning("Cleaning %d regular + %d conditional stale orders on %s before entry",
                        len(existing_orders), len(existing_cond), symbol)
            await self.exchange.cancel_all_orders(symbol)

    async def _before_place_sl_tp(self, symbol):
        await self.exchange.cancel_all_orders(symbol)

    async def _sweep_orphan_positions(self, open_trades):
        tracked = {t.symbol for t in open_trades}
        positions = await self.exchange.get_all_positions()
        for pos in positions:
            if pos["symbol"] in self.config.symbols and pos["symbol"] not in tracked:
                log.warning(
                    "Found orphan position: %s %s qty=%.4f — closing it",
                    pos["symbol"], pos["side"], pos["quantity"],
                )
                close_side = "SELL" if pos["side"] == "long" else "BUY"
                pos_side = "LONG" if pos["side"] == "long" else "SHORT"
                await self.exchange.place_market_order(pos["symbol"], close_side, pos["quantity"], position_side=pos_side)
                await self.exchange.cancel_all_orders(pos["symbol"])

    # --- Live-only monitoring / safety ---
    async def _count_sltp_orders(self, trade) -> tuple[int, int] | None:
        """Count active SL/TP orders in the conditional/algo bucket.

        Binance routes STOP_MARKET / TAKE_PROFIT_MARKET to its conditional (algo)
        order system, so SL/TP never appear in regular open orders and have no
        regular orderId pre-trigger. Match by order type and, in hedge mode,
        position side. A healthy position has exactly one of each; any other
        count (missing OR duplicated) makes the caller cancel both buckets and
        re-place exactly one SL + one TP — which also clears stray duplicates.

        Returns None if the orders couldn't be read (strict fetch raised). Callers
        MUST NOT cancel/replace on None — acting on a failed read could disturb a
        healthy position's SL/TP.
        """
        try:
            orders = await self.exchange.get_conditional_orders(trade.symbol, strict=True)
        except Exception as e:
            log.warning("Could not read conditional orders for %s (#%d): %s — skipping SL/TP check",
                        trade.symbol, trade.id, e)
            return None
        expected_ps = ("LONG" if trade.direction == "long" else "SHORT") if self.exchange.hedge_mode else None
        sl_count = 0
        tp_count = 0
        for o in orders:
            if expected_ps and o.get("positionSide") != expected_ps:
                continue
            otype = o.get("orderType") or o.get("type")
            if otype == "STOP_MARKET":
                sl_count += 1
            elif otype == "TAKE_PROFIT_MARKET":
                tp_count += 1
        return sl_count, tp_count

    async def _poll_open_position(self, trade, pos):
        symbol = trade.symbol
        counts = await self._count_sltp_orders(trade)
        if counts is None:
            return  # couldn't read orders — do nothing this cycle (never act on a failed read)
        sl_count, tp_count = counts

        if sl_count == 1 and tp_count == 1:
            if symbol in self._sltp_missing_since:
                log.info("Position poll: SL/TP confirmed for trade #%d", trade.id)
            self._sltp_missing_since.pop(symbol, None)
            self._sltp_last_alert.pop(symbol, None)
            return

        if sl_count > 1 or tp_count > 1:
            log.warning("Position poll: trade #%d has %d SL / %d TP (need 1/1) — cleaning duplicates",
                        trade.id, sl_count, tp_count)
        now = time.time()
        self._sltp_missing_since.setdefault(symbol, now)

        # Cancel all open orders (both buckets), then place exactly one SL + one TP.
        # _place_sl_tp cancels first, so this can never accumulate duplicate orders.
        sl_ok, tp_ok = await self._place_sl_tp(
            symbol, trade.direction, pos["quantity"],
            trade.sl_price, trade.tp_price, trade.id,
        )

        if sl_ok and tp_ok:
            log.info("Position poll: re-placed SL/TP for trade #%d", trade.id)
            self._sltp_missing_since.pop(symbol, None)
            self._sltp_last_alert.pop(symbol, None)
        else:
            # Safety net: SL could not be established AND it was genuinely absent
            # (sl_count == 0 — not a real SL we just cancelled). If price has also
            # breached the stop, force-close so the position isn't left unprotected.
            if not sl_ok and sl_count == 0:
                if await self._maybe_force_close_breach(trade, pos):
                    return

            elapsed = now - self._sltp_missing_since[symbol]
            if elapsed >= 60 and (now - self._sltp_last_alert.get(symbol, 0)) >= 60:
                missing = []
                if not sl_ok:
                    missing.append("SL")
                if not tp_ok:
                    missing.append("TP")
                user_info = await db.get_user(self.user_id)
                user_label = f"{user_info.name} (#{self.user_id})" if user_info and user_info.name else f"#{self.user_id}"
                msg = (
                    f"⚠️ Failed to place {'/'.join(missing)} for {symbol} "
                    f"({trade.direction.upper()}) — {user_label}\nCheck position manually"
                )
                log.error(msg)
                await send_private(msg)
                self._sltp_last_alert[symbol] = now

    async def _maybe_force_close_breach(self, trade, pos) -> bool:
        """Force-close a live position ONLY when there is genuinely no stop order
        AND price has already breached the SL level. Every gate must independently
        confirm a real breach — a data/fetch error must never cause a close.
        Returns True if the position was force-closed.
        """
        symbol = trade.symbol

        # Gate 5: authoritatively re-confirm there is no STOP order (strict fetch).
        # A fetch error -> unknown state -> do NOT close.
        try:
            cond = await self.exchange.get_conditional_orders(symbol, strict=True)
        except Exception as e:
            log.warning("Force-close check: conditional fetch failed for %s — skipping: %s", symbol, e)
            return False
        expected_ps = ("LONG" if trade.direction == "long" else "SHORT") if self.exchange.hedge_mode else None
        for o in cond:
            if expected_ps and o.get("positionSide") != expected_ps:
                continue
            if (o.get("orderType") or o.get("type")) == "STOP_MARKET":
                return False  # an SL exists after all — never force-close

        # Gate 6: reliable current price, direction-aware breach of the SL level
        price = self._shared_market.get_latest_price(symbol) if self._shared_market else 0.0
        if price <= 0:
            return False
        if trade.direction == "long":
            breached = price <= trade.sl_price
        else:
            breached = price >= trade.sl_price
        if not breached:
            return False

        # Gate 7: Binance's own position PnL confirms the loss is at SL magnitude (~1R),
        # not merely negative. Rejects a single bad price tick.
        expected_loss_at_sl = abs(trade.entry_price - trade.sl_price) * trade.quantity
        if expected_loss_at_sl <= 0:
            return False
        if pos.get("unrealized_pnl", 0.0) > -(expected_loss_at_sl * 0.9):
            return False

        await self._force_close_breached(trade, pos, price)
        return True

    async def _force_close_breached(self, trade, pos, price: float):
        symbol = trade.symbol
        log.error("FORCE-CLOSE: %s %s has no stop order and price %.4f breached SL %.4f — market-closing",
                  symbol, trade.direction, price, trade.sl_price)

        close_side = "SELL" if trade.direction == "long" else "BUY"
        pos_side = "LONG" if trade.direction == "long" else "SHORT"
        try:
            order = await self.exchange.place_market_order(symbol, close_side, pos["quantity"], position_side=pos_side)
        except Exception as e:
            log.error("Force-close market order FAILED for %s: %s", symbol, e)
            await send_private(
                f"🛑 Force-close FAILED for {symbol} ({trade.direction.upper()}) — price {price} past SL "
                f"{trade.sl_price} with no stop order. CLOSE MANUALLY NOW."
            )
            return

        await self.exchange.cancel_all_orders(symbol)

        exit_price = float(order.get("avgPrice") or 0) or price or trade.sl_price
        exit_comm = exit_price * trade.quantity * self.config.commission_pct
        total_comm = (trade.commission or 0) + exit_comm
        if trade.direction == "long":
            raw_pnl = (exit_price - trade.entry_price) * trade.quantity
        else:
            raw_pnl = (trade.entry_price - exit_price) * trade.quantity
        pnl = raw_pnl - total_comm

        await db.update_trade(trade.id, {
            "exit_time": datetime.now(timezone.utc),
            "exit_price": exit_price,
            "result": "loss",
            "r_value": -1.0,
            "pnl_usdt": pnl,
            "commission": total_comm,
        })

        self._active_trades.pop(symbol, None)
        self._sltp_missing_since.pop(symbol, None)
        self._sltp_last_alert.pop(symbol, None)
        self._on_trade_result(False)

        await db.log_event(
            f"Force-closed {symbol}: price breached SL with no stop order on exchange",
            level="error", category="trade",
            user_id=self.user_id, is_paper=self.is_paper,
        )
        await self._broadcast({
            "type": "trade_closed",
            "trade": {"id": trade.id, "symbol": symbol, "result": "loss", "pnl": pnl},
        })

        balance = await self.exchange.get_balance()
        await alert_exit(symbol, trade.direction, "loss", trade.entry_price, exit_price, pnl, -1.0, balance)
        await send_private(
            f"🛑 Force-closed {symbol} ({trade.direction.upper()}) @ {exit_price:.4f} — price breached SL "
            f"{trade.sl_price} with no stop order on the exchange"
        )
        await self._self_heal_trade(trade.id)

    async def _self_heal_trade(self, trade_id: int):
        try:
            trade = await db.get_trade(trade_id)
            if not trade or trade.result not in ("win", "loss"):
                return

            update = {}
            binance_entry_price = None
            binance_qty = None
            binance_entry_comm = 0.0

            if trade.entry_order_id:
                entry_fills = await self.exchange.get_trades_for_order(trade.symbol, int(trade.entry_order_id))
                if entry_fills:
                    binance_qty = sum(float(f["qty"]) for f in entry_fills)
                    binance_entry_price = sum(float(f["price"]) * float(f["qty"]) for f in entry_fills) / binance_qty if binance_qty else 0
                    binance_entry_comm = sum(float(f.get("commission", 0)) for f in entry_fills)

                    if binance_entry_price > 0 and abs((trade.entry_price or 0) - binance_entry_price) > 0.01:
                        update["entry_price"] = binance_entry_price
                    if binance_qty > 0 and abs((trade.quantity or 0) - binance_qty) > 0.0001:
                        update["quantity"] = binance_qty

            binance_exit_price = None
            binance_exit_comm = 0.0
            binance_is_sl = None

            # SL/TP are conditional/algo orders whose ids can't be looked up
            # directly; read the real closing fills (with realizedPnl) instead.
            exit_info = await resolve_trade_exit(
                self.exchange.client, trade.symbol, trade.direction,
                trade.entry_order_id, trade.entry_time,
                binance_qty or trade.quantity,
                trade.sl_price, trade.tp_price,
            )
            if exit_info:
                binance_exit_price = exit_info["exit_price"]
                binance_exit_comm = exit_info["exit_commission"]
                binance_is_sl = exit_info["is_sl"]

            if binance_exit_price and binance_exit_price > 0:
                if abs((trade.exit_price or 0) - binance_exit_price) > 0.01:
                    update["exit_price"] = binance_exit_price
                correct_result = "loss" if binance_is_sl else "win"
                if trade.result != correct_result:
                    rr = await db.get_state("rr_ratio", self.config.strategy_params["rrr"],
                                            user_id=self.user_id, is_paper=self.is_paper)
                    update["result"] = correct_result
                    update["r_value"] = r_value_for_exit(binance_is_sl, update.get("entry_price", trade.entry_price),
                                                         trade.sl_price, trade.tp_price, rr)

            entry_p = update.get("entry_price", trade.entry_price) or (binance_entry_price or 0)
            exit_p = update.get("exit_price", trade.exit_price) or (binance_exit_price or 0)
            qty = update.get("quantity", trade.quantity) or (binance_qty or 0)
            if entry_p > 0 and exit_p > 0 and qty > 0:
                if trade.direction == "long":
                    raw_pnl = (exit_p - entry_p) * qty
                else:
                    raw_pnl = (entry_p - exit_p) * qty
                total_comm = (binance_entry_comm or trade.commission or 0) + binance_exit_comm
                correct_pnl = raw_pnl - total_comm
                if trade.pnl_usdt is None or abs((trade.pnl_usdt or 0) - correct_pnl) > 0.01:
                    update["pnl_usdt"] = correct_pnl
                    update["commission"] = total_comm

            if update:
                await db.update_trade(trade_id, update)
                log.warning("Self-heal: fixed trade #%d — %s", trade_id, update)
                await db.log_event(
                    f"Self-healed trade #{trade_id}: {', '.join(update.keys())}",
                    level="warn", category="system",
                    user_id=self.user_id, is_paper=False,
                )
        except Exception as e:
            log.error("Self-heal failed for trade #%d: %s", trade_id, e)
