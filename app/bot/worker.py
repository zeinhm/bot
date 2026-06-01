from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Awaitable

from config import ACC_RANGE_MODE
from exchange import BinanceExchange
from strategy import check_signal
from telegram_alert import alert_entry, alert_exit, alert_bot_started, alert_bot_stopped
import database as db

log = logging.getLogger(__name__)


@dataclass
class BotConfig:
    api_key: str
    api_secret: str
    testnet: bool
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


class BotWorker:
    def __init__(
        self,
        user_id: int,
        config: BotConfig,
        broadcast_fn: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self.user_id = user_id
        self.config = config
        self.exchange = BinanceExchange(
            api_key=config.api_key,
            api_secret=config.api_secret,
            testnet=config.testnet,
        )
        self.candle_buffers: dict[str, list[dict]] = {}
        self.running = False
        self.status: str = "stopped"
        self.started_at: float | None = None
        self._active_trade_id: int | None = None
        self._broadcast_fn = broadcast_fn

        self.current_streak: int = 0
        self.adaptive_active: bool = False
        self.consecutive_wins: int = 0

        self._tasks: list[asyncio.Task] = []

    async def start(self):
        self.status = "starting"
        log.info("BotWorker[user=%d] starting...", self.user_id)
        await self.exchange.connect()

        enabled = await db.get_state("bot_enabled", True)
        if not enabled:
            log.info("BotWorker[user=%d] disabled in settings, monitor-only mode", self.user_id)

        await self._load_history()
        await self._crash_recovery()

        self.running = True
        self.status = "running"
        self.started_at = time.time()
        log.info("BotWorker[user=%d] started — listening for candles", self.user_id)
        await db.log_event("Bot started", category="system")
        await alert_bot_started()

        self._tasks = [
            asyncio.create_task(self._run_kline_stream()),
            asyncio.create_task(self._run_user_stream()),
            asyncio.create_task(self._run_position_poll()),
        ]
        await asyncio.gather(*self._tasks)

    async def stop(self):
        self.running = False
        self.status = "stopped"
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        await db.log_event("Bot stopped", category="system")
        await alert_bot_stopped("shutdown")
        await self.exchange.close()
        log.info("BotWorker[user=%d] stopped", self.user_id)

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
                        "BotWorker[user=%d] adaptive sizing ACTIVATED after %d consecutive losses",
                        self.user_id, self.current_streak,
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
                        "BotWorker[user=%d] adaptive sizing DEACTIVATED after %d wins",
                        self.user_id, self.config.wins_to_recover,
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

    async def _load_history(self):
        for symbol in self.config.symbols:
            log.info("Loading history for %s...", symbol)
            candles = await self.exchange.get_klines(
                symbol=symbol,
                interval="15m",
                limit=self.config.candle_buffer_size,
            )
            self.candle_buffers[symbol] = candles
            await db.save_candles(symbol, candles)
            log.info("  %s: loaded %d candles", symbol, len(candles))

    async def _crash_recovery(self):
        log.info("Running crash recovery check...")

        open_trade = await db.get_open_trade()
        if open_trade:
            self._active_trade_id = open_trade.id
            log.info("Found open trade #%d %s %s in DB", open_trade.id, open_trade.symbol, open_trade.direction)

            pos = await self.exchange.get_position(open_trade.symbol)
            if pos is None:
                log.warning("Trade #%d is open in DB but no position on Binance — marking closed", open_trade.id)
                await db.log_event(f"Orphan trade #{open_trade.id} closed during recovery", level="warn", category="system")
                await db.update_trade(open_trade.id, {
                    "result": "loss",
                    "r_value": -1.0,
                    "exit_time": datetime.now(timezone.utc),
                    "pnl_usdt": 0.0,
                })
                self._active_trade_id = None
            else:
                orders = await self.exchange.get_open_orders(open_trade.symbol)
                has_sl = any(o["type"] == "STOP_MARKET" for o in orders)
                has_tp = any(o["type"] == "TAKE_PROFIT_MARKET" for o in orders)
                if not has_sl or not has_tp:
                    log.warning("Missing SL/TP orders for trade #%d — re-placing", open_trade.id)
                    await self.exchange.cancel_all_orders(open_trade.symbol)
                    await self._place_sl_tp(
                        open_trade.symbol, open_trade.direction,
                        pos["quantity"], open_trade.sl_price, open_trade.tp_price,
                        open_trade.id,
                    )
            return

        positions = await self.exchange.get_all_positions()
        for pos in positions:
            if pos["symbol"] in self.config.symbols:
                log.warning(
                    "Found orphan position on Binance: %s %s qty=%.4f — closing it",
                    pos["symbol"], pos["side"], pos["quantity"],
                )
                close_side = "SELL" if pos["side"] == "long" else "BUY"
                await self.exchange.place_market_order(pos["symbol"], close_side, pos["quantity"])
                await self.exchange.cancel_all_orders(pos["symbol"])

        log.info("Crash recovery complete — no open trades")

    # --- WebSocket Streams ---

    async def _run_kline_stream(self):
        while self.running:
            try:
                await self.exchange.start_kline_socket(
                    symbols=self.config.symbols,
                    interval="15m",
                    callback=self._on_kline,
                )
            except Exception as e:
                log.error("Kline WebSocket error: %s", e)
                await asyncio.sleep(5)

    async def _on_kline(self, data: dict):
        k = data.get("k", {})
        if not k.get("x", False):
            return

        symbol = k["s"]
        candle = {
            "timestamp": int(k["t"]),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
        }

        log.info("15m candle closed: %s O=%.2f H=%.2f L=%.2f C=%.2f",
                 symbol, candle["open"], candle["high"], candle["low"], candle["close"])

        if symbol not in self.candle_buffers:
            self.candle_buffers[symbol] = []
        self.candle_buffers[symbol].append(candle)
        if len(self.candle_buffers[symbol]) > self.config.candle_buffer_size:
            self.candle_buffers[symbol] = self.candle_buffers[symbol][-self.config.candle_buffer_size:]

        await db.save_candles(symbol, [candle])
        await db.trim_candle_buffer(symbol, self.config.candle_buffer_size)

        await self._process_candle(symbol)

    async def _process_candle(self, symbol: str):
        now = datetime.now(timezone.utc)

        if now.month in self.config.strategy_params["skip_months"]:
            log.info("Skipping %s — month %d is filtered", symbol, now.month)
            return

        bot_enabled = await db.get_state("bot_enabled", True)
        if not bot_enabled:
            log.info("Bot disabled — skipping signal check")
            return

        active_symbols = await db.get_state("active_symbols", self.config.symbols)
        if symbol not in active_symbols:
            return

        if self._active_trade_id is not None:
            log.info("Already in trade #%d — skipping", self._active_trade_id)
            return

        max_trades = await db.get_state("max_trades_per_day", 99)
        today_count = await db.get_today_trade_count()
        if today_count >= max_trades:
            log.info("Max trades/day reached (%d/%d) — skipping", today_count, max_trades)
            return

        rr = await db.get_state("rr_ratio", self.config.strategy_params["rrr"])
        sessions = await db.get_state("active_sessions", self.config.strategy_params["sessions"])
        params = {**self.config.strategy_params, "rrr": rr, "sessions": sessions, "acc_range_mode": ACC_RANGE_MODE.get(symbol, "wick")}

        candles = self.candle_buffers.get(symbol, [])
        signal = check_signal(candles, params)

        if signal is None:
            return

        log.info("SIGNAL: %s %s entry=%.2f sl=%.2f tp=%.2f",
                 signal["direction"], symbol, signal["entry_price"], signal["sl"], signal["tp"])

        await self._execute_trade(symbol, signal)

    # --- Trade Execution ---

    async def _execute_trade(self, symbol: str, signal: dict):
        try:
            risk_mode = await db.get_state("risk_mode", "static")
            risk_value = await db.get_state("risk_value", 10.0)

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

            side = "BUY" if signal["direction"] == "long" else "SELL"
            order = await self.exchange.place_market_order(symbol, side, quantity)

            fill_price = float(order.get("avgPrice", signal["entry_price"]))
            fill_qty = float(order.get("executedQty", quantity))

            entry_comm = fill_price * fill_qty * self.config.commission_pct

            trade = await db.create_trade({
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
            )
            await alert_entry(symbol, signal["direction"], fill_price, signal["sl"], signal["tp"], fill_qty, balance)

        except Exception as e:
            log.error("Failed to execute trade: %s", e, exc_info=True)
            await db.log_event(f"Trade execution failed: {e}", level="error", category="trade")

    async def _place_sl_tp(self, symbol: str, direction: str, quantity: float, sl: float, tp: float, trade_id: int):
        close_side = "SELL" if direction == "long" else "BUY"

        sl_order = await self.exchange.place_stop_loss(symbol, close_side, quantity, sl)
        tp_order = await self.exchange.place_take_profit(symbol, close_side, quantity, tp)

        await db.update_trade(trade_id, {
            "sl_order_id": str(sl_order.get("orderId", "")),
            "tp_order_id": str(tp_order.get("orderId", "")),
        })

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

        trade = await db.get_open_trade()
        if trade is None or trade.symbol != symbol:
            return

        is_sl = order_type == "STOP_MARKET"
        result = "loss" if is_sl else "win"
        rr = await db.get_state("rr_ratio", self.config.strategy_params["rrr"])
        r_value = -1.0 if is_sl else rr

        exit_comm = fill_price * trade.quantity * self.config.commission_pct
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

        cancel_id = trade.tp_order_id if is_sl else trade.sl_order_id
        if cancel_id:
            await self.exchange.cancel_order(symbol, int(cancel_id))

        self._active_trade_id = None

        self._on_trade_result(result == "win")

        log.info("Trade #%d closed: %s (%.2f USDT)", trade.id, result, pnl)

        await db.log_event(
            f"Closed {trade.direction.upper()} {symbol}: {result.upper()} ${pnl:+.2f}",
            level="info" if result == "win" else "warn",
            category="trade",
            details=f"Entry: ${trade.entry_price:.2f} → Exit: ${fill_price:.2f} | {r_value:+.1f}R",
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
        await alert_exit(symbol, trade.direction, result, trade.entry_price, fill_price, pnl, r_value, balance)

    # --- Fallback Position Poll ---

    async def _run_position_poll(self):
        while self.running:
            await asyncio.sleep(30)
            try:
                if self._active_trade_id is None:
                    continue

                trade = await db.get_open_trade()
                if trade is None:
                    self._active_trade_id = None
                    continue

                pos = await self.exchange.get_position(trade.symbol)
                if pos is None:
                    log.info("Position poll: no position found for trade #%d — checking orders", trade.id)

                    last_price = self.candle_buffers.get(trade.symbol, [{}])[-1].get("close", 0)
                    if last_price == 0:
                        continue

                    if trade.direction == "long":
                        is_sl = last_price <= trade.sl_price
                    else:
                        is_sl = last_price >= trade.sl_price

                    result = "loss" if is_sl else "win"
                    rr = await db.get_state("rr_ratio", self.config.strategy_params["rrr"])
                    r_value = -1.0 if is_sl else rr
                    exit_price = trade.sl_price if is_sl else trade.tp_price

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
                    await alert_exit(trade.symbol, trade.direction, result, trade.entry_price, exit_price, pnl, r_value, balance)

            except Exception as e:
                log.error("Position poll error: %s", e)
