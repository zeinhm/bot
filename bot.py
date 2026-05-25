"""
Bot engine: connects to Binance, processes 15m candles, executes trades.

Startup:
  1. Load historical candles from Binance REST API
  2. Check for existing open positions (crash recovery)
  3. Connect to WebSocket for real-time candles + order fills

On each 15m candle close:
  1. Append new candle to buffer
  2. Skip if month == May
  3. Skip if already in a trade
  4. Run strategy, place orders if signal fires
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    SYMBOLS, STRATEGY_PARAMS, COMMISSION_PCT, SLIPPAGE_TICKS,
    TICK_SIZE, CANDLE_BUFFER_SIZE,
)
from exchange import BinanceExchange
from strategy import check_signal
from telegram_alert import alert_entry, alert_exit, alert_bot_started, alert_bot_stopped
import database as db

log = logging.getLogger(__name__)

_ws_clients: set = set()


def register_ws(ws):
    _ws_clients.add(ws)


def unregister_ws(ws):
    _ws_clients.discard(ws)


async def broadcast(data: dict):
    import json
    msg = json.dumps(data)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _ws_clients -= dead


class TradingBot:
    def __init__(self):
        self.exchange = BinanceExchange(
            api_key=BINANCE_API_KEY,
            api_secret=BINANCE_API_SECRET,
            testnet=BINANCE_TESTNET,
        )
        self.candle_buffers: dict[str, list[dict]] = {}
        self.running = False
        self._active_trade_id: int | None = None

    async def start(self):
        log.info("Starting bot...")
        await self.exchange.connect()

        enabled = await db.get_state("bot_enabled", True)
        if not enabled:
            log.info("Bot is disabled in settings, running in monitor-only mode")

        await self._load_history()
        await self._crash_recovery()

        self.running = True
        log.info("Bot started — listening for candles")
        await alert_bot_started()

        await asyncio.gather(
            self._run_kline_stream(),
            self._run_user_stream(),
            self._run_position_poll(),
        )

    async def stop(self):
        self.running = False
        await alert_bot_stopped("shutdown")
        await self.exchange.close()
        log.info("Bot stopped")

    # --- Startup ---

    async def _load_history(self):
        for symbol in SYMBOLS:
            log.info("Loading history for %s...", symbol)
            candles = await self.exchange.get_klines(
                symbol=symbol,
                interval="15m",
                limit=CANDLE_BUFFER_SIZE,
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
            if pos["symbol"] in SYMBOLS:
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
                    symbols=SYMBOLS,
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
        if len(self.candle_buffers[symbol]) > CANDLE_BUFFER_SIZE:
            self.candle_buffers[symbol] = self.candle_buffers[symbol][-CANDLE_BUFFER_SIZE:]

        await db.save_candles(symbol, [candle])
        await db.trim_candle_buffer(symbol, CANDLE_BUFFER_SIZE)

        await self._process_candle(symbol)

    async def _process_candle(self, symbol: str):
        now = datetime.now(timezone.utc)

        if now.month in STRATEGY_PARAMS["skip_months"]:
            log.info("Skipping %s — month %d is filtered", symbol, now.month)
            return

        bot_enabled = await db.get_state("bot_enabled", True)
        if not bot_enabled:
            log.info("Bot disabled — skipping signal check")
            return

        active_symbols = await db.get_state("active_symbols", SYMBOLS)
        if symbol not in active_symbols:
            return

        if self._active_trade_id is not None:
            log.info("Already in trade #%d — skipping", self._active_trade_id)
            return

        candles = self.candle_buffers.get(symbol, [])
        signal = check_signal(candles, STRATEGY_PARAMS)

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

            if risk_mode == "dynamic":
                balance = await self.exchange.get_balance()
                risk_amt = balance * (risk_value / 100.0)
            else:
                risk_amt = float(risk_value)

            sl_dist = signal["sl_distance"]
            if sl_dist <= 0:
                log.warning("Invalid SL distance %.4f — skipping", sl_dist)
                return

            tick = TICK_SIZE.get(symbol, 0.01)
            slip = SLIPPAGE_TICKS * tick
            entry_adj = signal["entry_price"] + slip if signal["direction"] == "long" else signal["entry_price"] - slip

            quantity = risk_amt / sl_dist
            quantity = max(quantity, 0.001 if symbol == "BTCUSDT" else 0.01)

            side = "BUY" if signal["direction"] == "long" else "SELL"
            order = await self.exchange.place_market_order(symbol, side, quantity)

            fill_price = float(order.get("avgPrice", signal["entry_price"]))
            fill_qty = float(order.get("executedQty", quantity))

            entry_comm = fill_price * fill_qty * COMMISSION_PCT

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

            await broadcast({
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
            await alert_entry(symbol, signal["direction"], fill_price, signal["sl"], signal["tp"], fill_qty, balance)

        except Exception as e:
            log.error("Failed to execute trade: %s", e, exc_info=True)

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
        r_value = -1.0 if is_sl else STRATEGY_PARAMS["rrr"]

        exit_comm = fill_price * trade.quantity * COMMISSION_PCT
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

        log.info("Trade #%d closed: %s (%.2f USDT)", trade.id, result, pnl)

        await broadcast({
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
                    orders = await self.exchange.get_open_orders(trade.symbol)

                    last_price = self.candle_buffers.get(trade.symbol, [{}])[-1].get("close", 0)
                    if last_price == 0:
                        continue

                    if trade.direction == "long":
                        is_sl = last_price <= trade.sl_price
                    else:
                        is_sl = last_price >= trade.sl_price

                    result = "loss" if is_sl else "win"
                    r_value = -1.0 if is_sl else STRATEGY_PARAMS["rrr"]
                    exit_price = trade.sl_price if is_sl else trade.tp_price

                    exit_comm = exit_price * trade.quantity * COMMISSION_PCT
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

                    log.info("Position poll: trade #%d resolved as %s", trade.id, result)

                    await broadcast({
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


bot_instance: TradingBot | None = None


async def start_bot():
    global bot_instance
    bot_instance = TradingBot()
    await bot_instance.start()


def get_bot() -> TradingBot | None:
    return bot_instance
