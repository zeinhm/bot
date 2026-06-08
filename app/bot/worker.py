from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Awaitable, TYPE_CHECKING

from config import ACC_RANGE_MODE
from strategy import check_signal
from telegram_alert import alert_entry, alert_exit, alert_bot_started, alert_bot_stopped, send_private
import app.db as db

if TYPE_CHECKING:
    from app.bot.shared_market import SharedMarketData

log = logging.getLogger(__name__)


@dataclass
class BotConfig:
    api_key: str
    api_secret: str
    symbols: list[str]
    strategy_params: dict
    leverage: int
    commission_pct: float
    slippage_ticks: int
    tick_size: dict[str, float]
    lot_size: dict[str, float]
    candle_buffer_size: int
    telegram_chat_id: str = ""
    telegram_owner_id: str = ""
    loss_streak_threshold: int = 4
    reduced_risk_pct: float = 0.25
    wins_to_recover: int = 2
    is_paper: bool = False
    paper_balance: float = 10000.0


class BotWorker:
    def __init__(
        self,
        user_id: int,
        config: BotConfig,
        broadcast_fn: Callable[[dict], Awaitable[None]] | None = None,
        shared_market: SharedMarketData | None = None,
    ):
        self.user_id = user_id
        self.config = config
        self.is_paper = config.is_paper

        self._shared_market = shared_market

        if config.is_paper:
            from paper_exchange import PaperExchange
            self.exchange = PaperExchange(user_id, shared_market)
        else:
            from exchange import BinanceExchange
            self.exchange = BinanceExchange(
                api_key=config.api_key,
                api_secret=config.api_secret,
            )
        self.running = False
        self.status: str = "stopped"
        self.started_at: float | None = None
        self.last_error: str | None = None
        self.last_error_time: float | None = None
        self._active_trade_id: int | None = None
        self._broadcast_fn = broadcast_fn

        self.current_streak: int = 0
        self.adaptive_active: bool = False
        self.consecutive_wins: int = 0

        self._tasks: list[asyncio.Task] = []

    def _mode_label(self) -> str:
        return "paper" if self.is_paper else "live"

    async def start(self):
        self.status = "starting"
        log.info("BotWorker[user=%d/%s] starting...", self.user_id, self._mode_label())
        await self.exchange.connect()

        enabled = await db.get_state("bot_enabled", True, user_id=self.user_id, is_paper=self.is_paper)
        if not enabled:
            log.info("BotWorker[user=%d/%s] disabled in settings, monitor-only mode", self.user_id, self._mode_label())

        await self._crash_recovery()

        self.running = True
        self.status = "running"
        self.started_at = time.time()
        self.last_error = None
        self.last_error_time = None
        log.info("BotWorker[user=%d/%s] started — listening for candles", self.user_id, self._mode_label())
        await db.log_event("Bot started", category="system", user_id=self.user_id, is_paper=self.is_paper)
        if not self.is_paper:
            await alert_bot_started()

        if self._shared_market:
            self._shared_market.on_candle_close(self._on_shared_candle)

        self._tasks = [
            asyncio.create_task(self._run_user_stream()),
            asyncio.create_task(self._run_position_poll()),
        ]
        await asyncio.gather(*self._tasks)

    async def stop(self):
        self.running = False
        self.status = "stopped"
        if self._shared_market:
            self._shared_market.remove_candle_callback(self._on_shared_candle)
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        await db.log_event("Bot stopped", category="system", user_id=self.user_id, is_paper=self.is_paper)
        if not self.is_paper:
            await alert_bot_stopped("shutdown")
        await self.exchange.close()
        log.info("BotWorker[user=%d/%s] stopped", self.user_id, self._mode_label())

    def _get_effective_risk(self) -> float:
        if self.adaptive_active:
            return self.config.reduced_risk_pct
        return None

    def _on_trade_result(self, won: bool):
        if not won:
            self.current_streak += 1
            self.consecutive_wins = 0
            if self.current_streak >= self.config.loss_streak_threshold:
                if not self.adaptive_active:
                    self.adaptive_active = True
                    log.info(
                        "BotWorker[user=%d/%s] adaptive sizing ACTIVATED after %d consecutive losses",
                        self.user_id, self._mode_label(), self.current_streak,
                    )
                    asyncio.create_task(self._broadcast({
                        "type": "adaptive_sizing",
                        "active": True,
                        "streak": self.current_streak,
                        "wins_needed": self.config.wins_to_recover,
                    }))
        else:
            self.current_streak = 0
            if self.adaptive_active:
                self.consecutive_wins += 1
                if self.consecutive_wins >= self.config.wins_to_recover:
                    self.adaptive_active = False
                    self.consecutive_wins = 0
                    log.info(
                        "BotWorker[user=%d/%s] adaptive sizing DEACTIVATED after %d wins",
                        self.user_id, self._mode_label(), self.config.wins_to_recover,
                    )
                    asyncio.create_task(self._broadcast({
                        "type": "adaptive_sizing",
                        "active": False,
                        "streak": 0,
                        "wins_needed": 0,
                    }))

    async def _broadcast(self, data: dict):
        if self._broadcast_fn:
            await self._broadcast_fn(data)

    # --- Startup ---

    async def _crash_recovery(self):
        log.info("Running crash recovery check...")

        open_trade = await db.get_open_trade(self.user_id, self.is_paper)
        if open_trade:
            self._active_trade_id = open_trade.id
            log.info("Found open trade #%d %s %s in DB", open_trade.id, open_trade.symbol, open_trade.direction)

            pos = await self.exchange.get_position(open_trade.symbol)
            if pos is None:
                log.warning("Trade #%d is open in DB but no position — marking closed", open_trade.id)

                exit_price = 0.0
                exit_comm = 0.0
                is_sl = True
                if not self.is_paper:
                    for oid_str in [open_trade.sl_order_id, open_trade.tp_order_id]:
                        if not oid_str:
                            continue
                        order_info = await self.exchange.get_order(open_trade.symbol, int(oid_str))
                        if order_info and order_info.get("status") == "FILLED":
                            exit_price = float(order_info.get("avgPrice", 0))
                            is_sl = order_info.get("type") == "STOP_MARKET"
                            exit_trades = await self.exchange.get_trades_for_order(open_trade.symbol, int(oid_str))
                            if exit_trades:
                                total_qty = sum(float(t["qty"]) for t in exit_trades)
                                if total_qty > 0:
                                    exit_price = sum(float(t["price"]) * float(t["qty"]) for t in exit_trades) / total_qty
                                exit_comm = sum(float(t.get("commission", 0)) for t in exit_trades)
                            break
                else:
                    paper_orders = await db.get_paper_orders_for_trade(open_trade.id)
                    for po in paper_orders:
                        if po.status == "FILLED":
                            exit_price = po.stop_price
                            is_sl = po.order_type == "STOP_MARKET"
                            exit_comm = exit_price * open_trade.quantity * self.config.commission_pct
                            break

                result = "loss" if is_sl else "win"
                rr = self.config.strategy_params.get("rrr", 2.0)
                r_value = -1.0 if is_sl else rr
                total_comm = (open_trade.commission or 0) + exit_comm

                if exit_price > 0:
                    if open_trade.direction == "long":
                        raw_pnl = (exit_price - open_trade.entry_price) * open_trade.quantity
                    else:
                        raw_pnl = (open_trade.entry_price - exit_price) * open_trade.quantity
                    pnl = raw_pnl - total_comm
                else:
                    pnl = 0.0

                await db.log_event(
                    f"Orphan trade #{open_trade.id} closed during recovery",
                    level="warn", category="system",
                    user_id=self.user_id, is_paper=self.is_paper,
                )
                await db.update_trade(open_trade.id, {
                    "result": result,
                    "r_value": r_value,
                    "exit_time": datetime.now(timezone.utc),
                    "exit_price": exit_price if exit_price > 0 else None,
                    "pnl_usdt": pnl,
                    "commission": total_comm,
                })
                self._active_trade_id = None
            else:
                if not self.is_paper:
                    has_sl, has_tp = await self._check_sltp_orders(open_trade)
                else:
                    orders = await self.exchange.get_open_orders(open_trade.symbol)
                    has_sl = any(o.get("type") == "STOP_MARKET" for o in orders)
                    has_tp = any(o.get("type") == "TAKE_PROFIT_MARKET" for o in orders)
                if not has_sl or not has_tp:
                    log.warning("Missing SL/TP orders for trade #%d — re-placing", open_trade.id)
                    await self.exchange.cancel_all_orders(open_trade.symbol)
                    await self._place_sl_tp(
                        open_trade.symbol, open_trade.direction,
                        pos["quantity"], open_trade.sl_price, open_trade.tp_price,
                        open_trade.id,
                    )
            return

        if not self.is_paper:
            positions = await self.exchange.get_all_positions()
            for pos in positions:
                if pos["symbol"] in self.config.symbols:
                    log.warning(
                        "Found orphan position: %s %s qty=%.4f — closing it",
                        pos["symbol"], pos["side"], pos["quantity"],
                    )
                    close_side = "SELL" if pos["side"] == "long" else "BUY"
                    pos_side = "LONG" if pos["side"] == "long" else "SHORT"
                    await self.exchange.place_market_order(pos["symbol"], close_side, pos["quantity"], position_side=pos_side)
                    await self.exchange.cancel_all_orders(pos["symbol"])

        log.info("Crash recovery complete — no open trades")

    # --- Candle handling (shared) ---

    async def _on_shared_candle(self, symbol: str, candle: dict):
        if not self.running:
            return
        if symbol not in self.config.symbols:
            return
        await self._process_candle(symbol)

    async def _process_candle(self, symbol: str):
        now = datetime.now(timezone.utc)

        if now.month in self.config.strategy_params["skip_months"]:
            return

        bot_enabled = await db.get_state("bot_enabled", True, user_id=self.user_id, is_paper=self.is_paper)
        if not bot_enabled:
            return

        active_symbols = await db.get_state("active_symbols", self.config.symbols, user_id=self.user_id, is_paper=self.is_paper)
        if symbol not in active_symbols:
            return

        if self._active_trade_id is not None:
            return

        max_trades = await db.get_state("max_trades_per_day", 99, user_id=self.user_id, is_paper=self.is_paper)
        today_count = await db.get_today_trade_count(self.user_id, self.is_paper)
        if today_count >= max_trades:
            return

        rr = await db.get_state("rr_ratio", self.config.strategy_params["rrr"], user_id=self.user_id, is_paper=self.is_paper)
        sessions = await db.get_state("active_sessions", self.config.strategy_params["sessions"], user_id=self.user_id, is_paper=self.is_paper)
        params = {**self.config.strategy_params, "rrr": rr, "sessions": sessions, "acc_range_mode": ACC_RANGE_MODE.get(symbol, "wick")}

        candles = self._shared_market.get_candles(symbol) if self._shared_market else []
        signal = check_signal(candles, params)

        if signal is None:
            return

        log.info("[%s] SIGNAL: %s %s entry=%.2f sl=%.2f tp=%.2f",
                 self._mode_label(), signal["direction"], symbol, signal["entry_price"], signal["sl"], signal["tp"])

        await self._execute_trade(symbol, signal)

    # --- Trade Execution ---

    async def _execute_trade(self, symbol: str, signal: dict):
        try:
            risk_mode = await db.get_state("risk_mode", "static", user_id=self.user_id, is_paper=self.is_paper)
            risk_value = await db.get_state("risk_value", 10.0, user_id=self.user_id, is_paper=self.is_paper)

            effective_risk = self._get_effective_risk()

            if risk_mode == "dynamic":
                balance = await self.exchange.get_balance()
                if effective_risk is not None:
                    risk_amt = balance * (effective_risk / 100.0)
                else:
                    risk_amt = balance * (risk_value / 100.0)
            else:
                risk_amt = float(risk_value)

            sl_dist = signal["sl_distance"]
            if sl_dist <= 0:
                log.warning("Invalid SL distance %.4f — skipping", sl_dist)
                return

            tick = self.config.tick_size.get(symbol, 0.01)
            slip = self.config.slippage_ticks * tick
            if signal["direction"] == "long":
                entry_adj = signal["entry_price"] + slip
            else:
                entry_adj = signal["entry_price"] - slip

            quantity = risk_amt / sl_dist
            min_qty = self.config.lot_size.get(symbol, 0.01)
            quantity = max(quantity, min_qty)

            if not self.is_paper:
                existing_orders = await self.exchange.get_open_orders(symbol)
                if existing_orders:
                    log.warning("Cleaning %d stale orders on %s before entry", len(existing_orders), symbol)
                    await self.exchange.cancel_all_orders(symbol)

            side = "BUY" if signal["direction"] == "long" else "SELL"
            order = await self.exchange.place_market_order(symbol, side, quantity)

            fill_price = float(order.get("avgPrice") or 0) or signal["entry_price"]
            fill_qty = float(order.get("executedQty") or 0) or quantity

            entry_comm = fill_price * fill_qty * self.config.commission_pct
            if not self.is_paper:
                trades = await self.exchange.get_trades_for_order(symbol, int(order.get("orderId", 0)))
                if trades:
                    entry_comm = sum(float(t.get("commission", 0)) for t in trades)

            trade = await db.create_trade({
                "user_id": self.user_id,
                "is_paper": self.is_paper,
                "symbol": symbol,
                "direction": signal["direction"],
                "entry_time": datetime.now(timezone.utc),
                "entry_price": fill_price,
                "sl_price": signal["sl"],
                "tp_price": signal["tp"],
                "quantity": fill_qty,
                "result": "open",
                "commission": entry_comm,
                "entry_order_id": str(order.get("orderId", "")),
            })

            self._active_trade_id = trade.id

            await self._place_sl_tp(symbol, signal["direction"], fill_qty, signal["sl"], signal["tp"], trade.id)

            await self._broadcast({
                "type": "trade_opened",
                "trade": {
                    "id": trade.id,
                    "symbol": symbol,
                    "direction": signal["direction"],
                    "entry_price": fill_price,
                    "sl": signal["sl"],
                    "tp": signal["tp"],
                    "quantity": fill_qty,
                },
            })

            balance = await self.exchange.get_balance()
            await db.log_event(
                f"Opened {signal['direction'].upper()} {symbol} @ ${fill_price:.2f}",
                category="trade",
                details=f"SL: ${signal['sl']:.2f} | TP: ${signal['tp']:.2f} | Qty: {fill_qty}",
                user_id=self.user_id, is_paper=self.is_paper,
            )
            if not self.is_paper:
                await alert_entry(symbol, signal["direction"], fill_price, signal["sl"], signal["tp"], fill_qty, balance)

        except Exception as e:
            log.error("Failed to execute trade: %s", e, exc_info=True)
            await db.log_event(f"Trade execution failed: {e}", level="error", category="trade",
                               user_id=self.user_id, is_paper=self.is_paper)

    async def _place_sl_tp(self, symbol: str, direction: str, quantity: float, sl: float, tp: float, trade_id: int):
        close_side = "SELL" if direction == "long" else "BUY"

        if not self.is_paper:
            await self.exchange.cancel_all_orders(symbol)

        sl_order = None
        tp_order = None
        for attempt in range(3):
            try:
                if sl_order is None:
                    sl_order = await self.exchange.place_stop_loss(symbol, close_side, quantity, sl)
                if tp_order is None:
                    tp_order = await self.exchange.place_take_profit(symbol, close_side, quantity, tp)
                break
            except Exception as e:
                log.warning("SL/TP placement attempt %d failed: %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(1)

        update = {}
        if sl_order:
            update["sl_order_id"] = str(sl_order.get("orderId", ""))
        if tp_order:
            update["tp_order_id"] = str(tp_order.get("orderId", ""))
        if update:
            await db.update_trade(trade_id, update)

    async def _check_sltp_orders(self, trade) -> tuple[bool, bool]:
        """Check if SL/TP orders exist by querying individual order IDs (most reliable)."""
        has_sl = False
        has_tp = False

        if trade.sl_order_id and trade.sl_order_id.isdigit():
            sl_info = await self.exchange.get_order(trade.symbol, int(trade.sl_order_id))
            if sl_info is not None:
                sl_status = sl_info.get("status")
                has_sl = sl_status in ("NEW", "PARTIALLY_FILLED")
                if not has_sl:
                    log.warning("User %d: SL order %s has status=%s (not active)",
                                self.user_id, trade.sl_order_id, sl_status)
            else:
                log.warning("User %d: SL order %s returned None from get_order",
                            self.user_id, trade.sl_order_id)
        else:
            log.info("User %d: trade #%d has no SL order ID stored (sl_order_id=%r)",
                     self.user_id, trade.id, trade.sl_order_id)

        if trade.tp_order_id and trade.tp_order_id.isdigit():
            tp_info = await self.exchange.get_order(trade.symbol, int(trade.tp_order_id))
            if tp_info is not None:
                tp_status = tp_info.get("status")
                has_tp = tp_status in ("NEW", "PARTIALLY_FILLED")
                if not has_tp:
                    log.warning("User %d: TP order %s has status=%s (not active)",
                                self.user_id, trade.tp_order_id, tp_status)
            else:
                log.warning("User %d: TP order %s returned None from get_order",
                            self.user_id, trade.tp_order_id)
        else:
            log.info("User %d: trade #%d has no TP order ID stored (tp_order_id=%r)",
                     self.user_id, trade.id, trade.tp_order_id)

        if (not trade.sl_order_id or not trade.tp_order_id) and (not has_sl or not has_tp):
            orders = await self.exchange.get_open_orders(trade.symbol)
            expected_ps = ("LONG" if trade.direction == "long" else "SHORT") if self.exchange.hedge_mode else None
            for o in orders:
                otype = o.get("type", "")
                orig = o.get("origType", "")
                if expected_ps and o.get("positionSide") != expected_ps:
                    continue
                if not has_sl and (otype in ("STOP_MARKET", "STOP") or orig in ("STOP_MARKET", "STOP")):
                    has_sl = True
                if not has_tp and (otype in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT") or orig in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT")):
                    has_tp = True
            if not has_sl or not has_tp:
                order_summary = [(o.get("type"), o.get("origType"), o.get("positionSide")) for o in orders]
                log.warning("User %d: fallback scan found %d orders, has_sl=%s has_tp=%s, orders=%s",
                            self.user_id, len(orders), has_sl, has_tp, order_summary)

        return has_sl, has_tp

    # --- Order Fill Monitoring ---

    async def _run_user_stream(self):
        while self.running:
            try:
                await self.exchange.start_user_socket(self._on_user_event)
            except Exception as e:
                log.error("User WebSocket error: %s", e)
                await asyncio.sleep(5)

    async def _on_user_event(self, data: dict):
        if data.get("e") != "ORDER_TRADE_UPDATE":
            return

        order = data.get("o", {})
        status = order.get("X", "")
        if status != "FILLED":
            return

        order_id = str(order.get("i", ""))
        symbol = order.get("s", "")
        order_type = order.get("ot", "")
        fill_price = float(order.get("ap", 0))

        if order_type not in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            return

        trade = await db.get_open_trade(self.user_id, self.is_paper)
        if trade is None or trade.symbol != symbol:
            return

        is_sl = order_type == "STOP_MARKET"
        result = "loss" if is_sl else "win"
        rr = await db.get_state("rr_ratio", self.config.strategy_params["rrr"],
                                user_id=self.user_id, is_paper=self.is_paper)
        r_value = -1.0 if is_sl else rr

        exit_comm = fill_price * trade.quantity * self.config.commission_pct
        if not self.is_paper and order_id:
            exit_trades = await self.exchange.get_trades_for_order(symbol, int(order_id))
            if exit_trades:
                total_qty = sum(float(t["qty"]) for t in exit_trades)
                if total_qty > 0:
                    fill_price = sum(float(t["price"]) * float(t["qty"]) for t in exit_trades) / total_qty
                exit_comm = sum(float(t.get("commission", 0)) for t in exit_trades)

        total_comm = (trade.commission or 0) + exit_comm

        if trade.direction == "long":
            raw_pnl = (fill_price - trade.entry_price) * trade.quantity
        else:
            raw_pnl = (trade.entry_price - fill_price) * trade.quantity
        pnl = raw_pnl - total_comm

        await db.update_trade(trade.id, {
            "exit_time": datetime.now(timezone.utc),
            "exit_price": fill_price,
            "result": result,
            "r_value": r_value,
            "pnl_usdt": pnl,
            "commission": total_comm,
        })

        await self.exchange.cancel_all_orders(symbol)

        self._active_trade_id = None

        self._on_trade_result(result == "win")

        log.info("[%s] Trade #%d closed: %s (%.2f USDT)", self._mode_label(), trade.id, result, pnl)

        await db.log_event(
            f"Closed {trade.direction.upper()} {symbol}: {result.upper()} ${pnl:+.2f}",
            level="info" if result == "win" else "warn",
            category="trade",
            details=f"Entry: ${trade.entry_price:.2f} → Exit: ${fill_price:.2f} | {r_value:+.1f}R",
            user_id=self.user_id, is_paper=self.is_paper,
        )

        await self._broadcast({
            "type": "trade_closed",
            "trade": {
                "id": trade.id,
                "symbol": symbol,
                "result": result,
                "pnl": pnl,
                "r_value": r_value,
            },
        })

        balance = await self.exchange.get_balance()
        if not self.is_paper:
            await alert_exit(symbol, trade.direction, result, trade.entry_price, fill_price, pnl, r_value, balance)
            await self._self_heal_trade(trade.id)

    # --- Fallback Position Poll ---

    async def _run_position_poll(self):
        self._sltp_missing_since: float | None = None
        self._sltp_last_alert: float = 0

        while self.running:
            has_active = self._active_trade_id is not None
            poll_interval = 10 if (has_active and self._sltp_missing_since) else 30
            await asyncio.sleep(poll_interval)
            try:
                if self._active_trade_id is None:
                    self._sltp_missing_since = None
                    continue

                trade = await db.get_open_trade(self.user_id, self.is_paper)
                if trade is None:
                    self._active_trade_id = None
                    self._sltp_missing_since = None
                    continue

                pos = await self.exchange.get_position(trade.symbol)
                if pos is None:
                    log.info("Position poll: no position found for trade #%d — checking orders", trade.id)

                    exit_price = None
                    exit_comm = 0.0
                    exit_order_id = None

                    if not self.is_paper:
                        for oid_str in [trade.sl_order_id, trade.tp_order_id]:
                            if not oid_str:
                                continue
                            order_info = await self.exchange.get_order(trade.symbol, int(oid_str))
                            if order_info and order_info.get("status") == "FILLED":
                                exit_order_id = int(oid_str)
                                exit_price = float(order_info.get("avgPrice", 0))
                                exit_trades = await self.exchange.get_trades_for_order(trade.symbol, exit_order_id)
                                if exit_trades:
                                    total_qty = sum(float(t["qty"]) for t in exit_trades)
                                    if total_qty > 0:
                                        exit_price = sum(float(t["price"]) * float(t["qty"]) for t in exit_trades) / total_qty
                                    exit_comm = sum(float(t.get("commission", 0)) for t in exit_trades)
                                is_sl = order_info.get("type") == "STOP_MARKET"
                                break
                    else:
                        paper_orders = await db.get_paper_orders_for_trade(trade.id)
                        for po in paper_orders:
                            if po.status == "FILLED":
                                exit_price = po.stop_price
                                is_sl = po.order_type == "STOP_MARKET"
                                exit_comm = exit_price * trade.quantity * self.config.commission_pct
                                break

                    if exit_price is None or exit_price == 0:
                        sm_candles = self._shared_market.get_candles(trade.symbol) if self._shared_market else []
                        recent = sm_candles[-3:] if sm_candles else []
                        if not recent:
                            continue
                        if trade.direction == "long":
                            sl_hit = any(c["low"] <= trade.sl_price for c in recent)
                            tp_hit = any(c["high"] >= trade.tp_price for c in recent)
                        else:
                            sl_hit = any(c["high"] >= trade.sl_price for c in recent)
                            tp_hit = any(c["low"] <= trade.tp_price for c in recent)
                        is_sl = sl_hit or not tp_hit
                        exit_price = trade.sl_price if is_sl else trade.tp_price
                        exit_comm = exit_price * trade.quantity * self.config.commission_pct

                    result = "loss" if is_sl else "win"
                    rr = await db.get_state("rr_ratio", self.config.strategy_params["rrr"],
                                            user_id=self.user_id, is_paper=self.is_paper)
                    r_value = -1.0 if is_sl else rr

                    total_comm = (trade.commission or 0) + exit_comm

                    if trade.direction == "long":
                        raw_pnl = (exit_price - trade.entry_price) * trade.quantity
                    else:
                        raw_pnl = (trade.entry_price - exit_price) * trade.quantity
                    pnl = raw_pnl - total_comm

                    await db.update_trade(trade.id, {
                        "exit_time": datetime.now(timezone.utc),
                        "exit_price": exit_price,
                        "result": result,
                        "r_value": r_value,
                        "pnl_usdt": pnl,
                        "commission": total_comm,
                    })

                    await self.exchange.cancel_all_orders(trade.symbol)
                    self._active_trade_id = None

                    self._on_trade_result(result == "win")

                    log.info("Position poll: trade #%d resolved as %s", trade.id, result)

                    await self._broadcast({
                        "type": "trade_closed",
                        "trade": {
                            "id": trade.id,
                            "symbol": trade.symbol,
                            "result": result,
                            "pnl": pnl,
                        },
                    })

                    balance = await self.exchange.get_balance()
                    if not self.is_paper:
                        await alert_exit(trade.symbol, trade.direction, result, trade.entry_price, exit_price, pnl, r_value, balance)
                        await self._self_heal_trade(trade.id)

                    self._sltp_missing_since = None
                else:
                    if not self.is_paper:
                        has_sl, has_tp = await self._check_sltp_orders(trade)

                        if not has_sl or not has_tp:
                            now = time.time()
                            if self._sltp_missing_since is None:
                                self._sltp_missing_since = now

                            close_side = "SELL" if trade.direction == "long" else "BUY"
                            if not has_sl:
                                try:
                                    sl_order = await self.exchange.place_stop_loss(trade.symbol, close_side, pos["quantity"], trade.sl_price)
                                    await db.update_trade(trade.id, {"sl_order_id": str(sl_order.get("orderId", ""))})
                                    has_sl = True
                                except Exception as e:
                                    if "-4045" in str(e):
                                        log.info("User %d: SL confirmed on Binance (order limit reached)", self.user_id)
                                        has_sl = True
                                    else:
                                        log.warning("Position poll: failed to re-place SL: %s", e)
                            if not has_tp:
                                try:
                                    tp_order = await self.exchange.place_take_profit(trade.symbol, close_side, pos["quantity"], trade.tp_price)
                                    await db.update_trade(trade.id, {"tp_order_id": str(tp_order.get("orderId", ""))})
                                    has_tp = True
                                except Exception as e:
                                    if "-4045" in str(e):
                                        log.info("User %d: TP confirmed on Binance (order limit reached)", self.user_id)
                                        has_tp = True
                                    else:
                                        log.warning("Position poll: failed to re-place TP: %s", e)

                            if not has_sl or not has_tp:
                                elapsed = now - self._sltp_missing_since
                                if elapsed >= 60 and (now - self._sltp_last_alert) >= 60:
                                    missing = []
                                    if not has_sl:
                                        missing.append("SL")
                                    if not has_tp:
                                        missing.append("TP")
                                    user_info = await db.get_user(self.user_id)
                                    user_label = f"{user_info.name} (#{self.user_id})" if user_info and user_info.name else f"#{self.user_id}"
                                    msg = (
                                        f"⚠️ Failed to place {'/'.join(missing)} for {trade.symbol} "
                                        f"({trade.direction.upper()}) — {user_label}\nCheck position manually"
                                    )
                                    log.error(msg)
                                    await send_private(msg)
                                    self._sltp_last_alert = now
                            else:
                                if self._sltp_missing_since is not None:
                                    log.info("Position poll: SL/TP confirmed for trade #%d", trade.id)
                                self._sltp_missing_since = None
                                self._sltp_last_alert = 0
                        else:
                            self._sltp_missing_since = None

            except Exception as e:
                log.error("Position poll error: %s", e)

    async def _self_heal_trade(self, trade_id: int):
        if self.is_paper:
            return
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

            for oid_str, is_sl in [(trade.sl_order_id, True), (trade.tp_order_id, False)]:
                if not oid_str:
                    continue
                order_info = await self.exchange.get_order(trade.symbol, int(oid_str))
                if order_info and order_info.get("status") == "FILLED":
                    exit_fills = await self.exchange.get_trades_for_order(trade.symbol, int(oid_str))
                    if exit_fills:
                        total_qty = sum(float(f["qty"]) for f in exit_fills)
                        binance_exit_price = sum(float(f["price"]) * float(f["qty"]) for f in exit_fills) / total_qty if total_qty else 0
                        binance_exit_comm = sum(float(f.get("commission", 0)) for f in exit_fills)
                        binance_is_sl = is_sl
                    break

            if binance_exit_price and binance_exit_price > 0:
                if abs((trade.exit_price or 0) - binance_exit_price) > 0.01:
                    update["exit_price"] = binance_exit_price
                correct_result = "loss" if binance_is_sl else "win"
                if trade.result != correct_result:
                    update["result"] = correct_result
                    rr = await db.get_state("rr_ratio", self.config.strategy_params["rrr"],
                                            user_id=self.user_id, is_paper=self.is_paper)
                    update["r_value"] = -1.0 if binance_is_sl else rr

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
