"""
Paper trading exchange — same interface as BinanceExchange, simulated in DB.

Uses SharedMarketData for public market streams (no API key needed).
SL/TP fills detected by checking prices against pending PaperOrders.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from config import SYMBOLS, LOT_SIZE
import app.db as db

if TYPE_CHECKING:
    from app.bot.shared_market import SharedMarketData

log = logging.getLogger(__name__)


class PaperExchange:
    def __init__(self, user_id: int, shared_market: SharedMarketData):
        self.user_id = user_id
        self._shared = shared_market
        self.client = True
        self._order_counter = 0
        self._positions: dict[str, dict] = {}

    @property
    def market_client(self):
        return self._shared.market_client

    @property
    def market_bsm(self):
        return self._shared.market_bsm

    async def connect(self):
        await self._shared.connect()
        await db.get_or_create_paper_account(self.user_id)
        log.info("PaperExchange[user=%d]: connected (virtual)", self.user_id)

    async def close(self):
        pass

    async def get_balance(self) -> float:
        return await db.get_paper_balance(self.user_id)

    async def get_position(self, symbol: str) -> dict | None:
        pos = self._positions.get(symbol)
        if pos is None:
            return None
        current_price = self._shared.get_latest_price(symbol) or pos["entry_price"]
        amt = pos["quantity"]
        if pos["side"] == "long":
            pnl = (current_price - pos["entry_price"]) * amt
        else:
            pnl = (pos["entry_price"] - current_price) * amt
        return {
            "symbol": symbol,
            "side": pos["side"],
            "quantity": amt,
            "entry_price": pos["entry_price"],
            "unrealized_pnl": round(pnl, 4),
        }

    async def get_all_positions(self) -> list[dict]:
        result = []
        for symbol in list(self._positions):
            pos = await self.get_position(symbol)
            if pos:
                result.append(pos)
        return result

    async def get_open_orders(self, symbol: str) -> list[dict]:
        orders = await db.get_pending_paper_orders(self.user_id, symbol)
        return [
            {"type": o.order_type, "orderId": o.id, "stopPrice": str(o.stop_price)}
            for o in orders
        ]

    async def place_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        price = self._shared.get_latest_price(symbol)
        if price <= 0:
            try:
                klines = await self.market_client.futures_klines(symbol=symbol, interval="1m", limit=1)
                if klines:
                    price = float(klines[-1][4])
            except Exception:
                pass
        if price <= 0:
            raise RuntimeError(f"No price available for {symbol}")

        quantity = float(self._format_qty(symbol, quantity))
        self._order_counter += 1
        order_id = self._order_counter

        if side.upper() == "BUY":
            if symbol in self._positions and self._positions[symbol]["side"] == "short":
                del self._positions[symbol]
            else:
                self._positions[symbol] = {
                    "side": "long",
                    "quantity": quantity,
                    "entry_price": price,
                }
        else:
            if symbol in self._positions and self._positions[symbol]["side"] == "long":
                del self._positions[symbol]
            else:
                self._positions[symbol] = {
                    "side": "short",
                    "quantity": quantity,
                    "entry_price": price,
                }

        log.info("PaperExchange: market %s %s %.4f @ %.2f", side, symbol, quantity, price)
        return {
            "orderId": order_id,
            "avgPrice": str(price),
            "executedQty": str(quantity),
        }

    async def place_stop_loss(self, symbol: str, side: str, quantity: float, stop_price: float) -> dict:
        self._order_counter += 1
        trade = await db.get_open_trade(self.user_id, is_paper=True)
        trade_id = trade.id if trade else 0
        order = await db.create_paper_order({
            "user_id": self.user_id,
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side.upper(),
            "order_type": "STOP_MARKET",
            "quantity": float(self._format_qty(symbol, quantity)),
            "stop_price": stop_price,
        })
        log.info("PaperExchange: SL %s %s @ %.2f — order %d", side, symbol, stop_price, order.id)
        return {"orderId": order.id}

    async def place_take_profit(self, symbol: str, side: str, quantity: float, price: float) -> dict:
        self._order_counter += 1
        trade = await db.get_open_trade(self.user_id, is_paper=True)
        trade_id = trade.id if trade else 0
        order = await db.create_paper_order({
            "user_id": self.user_id,
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side.upper(),
            "order_type": "TAKE_PROFIT_MARKET",
            "quantity": float(self._format_qty(symbol, quantity)),
            "stop_price": price,
        })
        log.info("PaperExchange: TP %s %s @ %.2f — order %d", side, symbol, price, order.id)
        return {"orderId": order.id}

    async def cancel_order(self, symbol: str, order_id: int):
        await db.cancel_paper_order(order_id)
        log.info("PaperExchange: cancelled order %d on %s", order_id, symbol)

    async def cancel_all_orders(self, symbol: str):
        await db.cancel_all_paper_orders(self.user_id, symbol)
        log.info("PaperExchange: cancelled all orders on %s", symbol)

    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[dict]:
        raw = await self.market_client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        return [
            {
                "timestamp": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
            for k in raw
        ]

    async def start_kline_socket(self, symbols: list[str], interval: str, callback):
        streams = [f"{s.lower()}@kline_{interval}" for s in symbols]
        socket = self._shared.market_bsm.futures_multiplex_socket(streams=streams)
        async with socket as stream:
            while True:
                msg = await stream.recv()
                if msg and "data" in msg:
                    await callback(msg["data"])

    async def start_user_socket(self, callback):
        """Simulate user stream by polling pending orders against live prices."""
        while True:
            try:
                await asyncio.sleep(1)
                orders = await db.get_pending_paper_orders(self.user_id)
                if not orders:
                    continue

                for order in orders:
                    price = self._shared.get_latest_price(order.symbol)
                    if price <= 0:
                        continue

                    triggered = False
                    if order.order_type == "STOP_MARKET":
                        if order.side == "SELL" and price <= order.stop_price:
                            triggered = True
                        elif order.side == "BUY" and price >= order.stop_price:
                            triggered = True
                    elif order.order_type == "TAKE_PROFIT_MARKET":
                        if order.side == "SELL" and price >= order.stop_price:
                            triggered = True
                        elif order.side == "BUY" and price <= order.stop_price:
                            triggered = True

                    if not triggered:
                        continue

                    await db.fill_paper_order(order.id)
                    fill_price = order.stop_price

                    if order.symbol in self._positions:
                        pos = self._positions[order.symbol]
                        balance = await db.get_paper_balance(self.user_id)
                        qty = pos["quantity"]
                        if pos["side"] == "long":
                            pnl = (fill_price - pos["entry_price"]) * qty
                        else:
                            pnl = (pos["entry_price"] - fill_price) * qty
                        from config import COMMISSION_PCT
                        comm = fill_price * qty * COMMISSION_PCT
                        pnl -= comm
                        await db.update_paper_balance(self.user_id, balance + pnl)
                        del self._positions[order.symbol]

                    event = {
                        "e": "ORDER_TRADE_UPDATE",
                        "o": {
                            "s": order.symbol,
                            "X": "FILLED",
                            "i": order.id,
                            "ot": order.order_type,
                            "ap": str(fill_price),
                        },
                    }
                    log.info("PaperExchange: order %d FILLED %s @ %.2f", order.id, order.order_type, fill_price)
                    await callback(event)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("PaperExchange user stream error: %s", e)
                await asyncio.sleep(5)

    def _format_qty(self, symbol: str, qty: float) -> str:
        step = LOT_SIZE.get(symbol, 0.01)
        qty = math.floor(qty / step) * step
        decimals = max(0, -int(math.floor(math.log10(step))))
        return f"{qty:.{decimals}f}"
