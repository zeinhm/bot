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
        # Per-asset open trades: symbol -> trade_id. One open trade per symbol
        # (BTC/ETH/SOL can run concurrently); no global cap.
        self._active_trades: dict[str, int] = {}
        # Per-symbol SL/TP recovery timers
        self._sltp_missing_since: dict[str, float] = {}
        self._sltp_last_alert: dict[str, float] = {}
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

        open_trades = await db.get_open_trades(self.user_id, self.is_paper)
        self._active_trades = {t.symbol: t.id for t in open_trades}

        for open_trade in open_trades:
            log.info("Found open trade #%d %s %s in DB", open_trade.id, open_trade.symbol, open_trade.direction)
            try:
                await self._recover_one_trade(open_trade)
            except Exception as e:
                log.error("Crash recovery failed for %s #%d: %s", open_trade.symbol, open_trade.id, e)

        # Sweep orphan positions on symbols that have no DB trade at all.
        # Use the original open-trades snapshot (NOT self._active_trades, which
        # _recover_one_trade may have popped on a transient get_position miss) —
        # otherwise a glitchy read could wrongly orphan-close a live position.
        if not self.is_paper:
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

        log.info("Crash recovery complete — %d open trade(s) recovered", len(self._active_trades))

    async def _recover_one_trade(self, open_trade):
        symbol = open_trade.symbol
        pos = await self.exchange.get_position(symbol)
        if pos is None:
            log.warning("Trade #%d is open in DB but no position — marking closed", open_trade.id)

            from exchange import resolve_trade_exit, r_value_for_exit

            exit_price = 0.0
            exit_comm = 0.0
            is_sl = True
            if not self.is_paper:
                # SL/TP are conditional/algo orders whose ids can't be looked up
                # directly; read the real closing fills (with realizedPnl) instead.
                exit_info = await resolve_trade_exit(
                    self.exchange.client, symbol, open_trade.direction,
                    open_trade.entry_order_id, open_trade.entry_time, open_trade.quantity,
                    open_trade.sl_price, open_trade.tp_price,
                )
                if exit_info:
                    exit_price = exit_info["exit_price"]
                    exit_comm = exit_info["exit_commission"]
                    is_sl = exit_info["is_sl"]
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
            r_value = r_value_for_exit(is_sl, open_trade.entry_price, open_trade.sl_price, open_trade.tp_price, rr)
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
            self._active_trades.pop(symbol, None)
        else:
            if not self.is_paper:
                counts = await self._count_sltp_orders(open_trade)
                if counts is None:
                    log.warning("Crash recovery: could not read orders for #%d — leaving as-is", open_trade.id)
                    return
                sl_count, tp_count = counts
            else:
                orders = await self.exchange.get_open_orders(symbol)
                sl_count = sum(1 for o in orders if o.get("type") == "STOP_MARKET")
                tp_count = sum(1 for o in orders if o.get("type") == "TAKE_PROFIT_MARKET")
            if sl_count != 1 or tp_count != 1:
                log.warning("Trade #%d has %d SL / %d TP orders (need 1/1) — cancelling all and re-placing",
                            open_trade.id, sl_count, tp_count)
                await self.exchange.cancel_all_orders(symbol)
                await self._place_sl_tp(
                    symbol, open_trade.direction,
                    pos["quantity"], open_trade.sl_price, open_trade.tp_price,
                    open_trade.id,
                )

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

        # One open trade per asset: block only if THIS symbol already has one.
        # Other symbols can still open (BTC/ETH/SOL run concurrently). Checked
        # against the DB (source of truth) to avoid any in-memory race.
        if symbol in self._active_trades:
            return
        if await db.get_open_trade_for_symbol(self.user_id, symbol, self.is_paper) is not None:
            return

        # No daily trade cap — adaptive sizing handles drawdown/loss-streaks.

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
                existing_cond = await self.exchange.get_conditional_orders(symbol)
                if existing_orders or existing_cond:
                    log.warning("Cleaning %d regular + %d conditional stale orders on %s before entry",
                                len(existing_orders), len(existing_cond), symbol)
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

            self._active_trades[symbol] = trade.id

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
            update["sl_order_id"] = str(sl_order.get("algoId") or sl_order.get("orderId") or "")
        if tp_order:
            update["tp_order_id"] = str(tp_order.get("algoId") or tp_order.get("orderId") or "")
        if update:
            await db.update_trade(trade_id, update)

        return sl_order is not None, tp_order is not None

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

        trade = await db.get_open_trade_for_symbol(self.user_id, symbol, self.is_paper)
        if trade is None:
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

        self._active_trades.pop(symbol, None)
        self._sltp_missing_since.pop(symbol, None)
        self._sltp_last_alert.pop(symbol, None)

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
        while self.running:
            poll_interval = 10 if (self._active_trades and self._sltp_missing_since) else 30
            await asyncio.sleep(poll_interval)
            try:
                open_trades = await db.get_open_trades(self.user_id, self.is_paper)
                open_symbols = {t.symbol for t in open_trades}

                # Sync in-memory state to what's actually open in the DB
                self._active_trades = {t.symbol: t.id for t in open_trades}
                for sym in list(self._sltp_missing_since):
                    if sym not in open_symbols:
                        self._sltp_missing_since.pop(sym, None)
                for sym in list(self._sltp_last_alert):
                    if sym not in open_symbols:
                        self._sltp_last_alert.pop(sym, None)

                for trade in open_trades:
                    try:
                        await self._poll_one_trade(trade)
                    except Exception as e:
                        log.error("Position poll error for %s #%d: %s", trade.symbol, trade.id, e)
            except Exception as e:
                log.error("Position poll error: %s", e)

    async def _poll_one_trade(self, trade):
        symbol = trade.symbol
        pos = await self.exchange.get_position(symbol)
        if pos is None:
            log.info("Position poll: no position found for trade #%d — checking orders", trade.id)
            from exchange import resolve_trade_exit, r_value_for_exit

            exit_price = None
            exit_comm = 0.0
            is_sl = None

            if not self.is_paper:
                # SL/TP are conditional/algo orders whose ids can't be looked up
                # directly; read the real closing fills (with realizedPnl) instead.
                exit_info = await resolve_trade_exit(
                    self.exchange.client, symbol, trade.direction,
                    trade.entry_order_id, trade.entry_time, trade.quantity,
                    trade.sl_price, trade.tp_price,
                )
                if exit_info:
                    exit_price = exit_info["exit_price"]
                    exit_comm = exit_info["exit_commission"]
                    is_sl = exit_info["is_sl"]
            else:
                paper_orders = await db.get_paper_orders_for_trade(trade.id)
                for po in paper_orders:
                    if po.status == "FILLED":
                        exit_price = po.stop_price
                        is_sl = po.order_type == "STOP_MARKET"
                        exit_comm = exit_price * trade.quantity * self.config.commission_pct
                        break

            if exit_price is None or exit_price == 0:
                # Last resort (fills unavailable): infer from price proximity to
                # SL vs TP — de-biased, never assume SL.
                sm_candles = self._shared_market.get_candles(symbol) if self._shared_market else []
                recent = sm_candles[-3:] if sm_candles else []
                if not recent:
                    return
                last_close = recent[-1]["close"]
                is_sl = abs(last_close - trade.sl_price) <= abs(last_close - trade.tp_price)
                exit_price = trade.sl_price if is_sl else trade.tp_price
                exit_comm = exit_price * trade.quantity * self.config.commission_pct

            result = "loss" if is_sl else "win"
            rr = await db.get_state("rr_ratio", self.config.strategy_params["rrr"],
                                    user_id=self.user_id, is_paper=self.is_paper)
            r_value = r_value_for_exit(is_sl, trade.entry_price, trade.sl_price, trade.tp_price, rr)

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

            await self.exchange.cancel_all_orders(symbol)
            self._active_trades.pop(symbol, None)
            self._sltp_missing_since.pop(symbol, None)
            self._sltp_last_alert.pop(symbol, None)

            self._on_trade_result(result == "win")

            log.info("Position poll: trade #%d resolved as %s", trade.id, result)

            await self._broadcast({
                "type": "trade_closed",
                "trade": {
                    "id": trade.id,
                    "symbol": symbol,
                    "result": result,
                    "pnl": pnl,
                },
            })

            balance = await self.exchange.get_balance()
            if not self.is_paper:
                await alert_exit(symbol, trade.direction, result, trade.entry_price, exit_price, pnl, r_value, balance)
                await self._self_heal_trade(trade.id)
            return

        # Position still open — verify exactly one SL + one TP (live only)
        if self.is_paper:
            return

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
        if self.is_paper:
            return
        try:
            from exchange import resolve_trade_exit, r_value_for_exit

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
