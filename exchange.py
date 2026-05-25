"""
Binance Futures API wrapper.

Handles testnet vs live, order placement, position queries, and WebSocket streams.
"""

from __future__ import annotations

import asyncio
import logging
from binance import AsyncClient, BinanceSocketManager

log = logging.getLogger(__name__)

TESTNET_URL = "https://demo-fapi.binance.com"
TESTNET_WS = "wss://dstream.binancefuture.com"


class BinanceExchange:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.client: AsyncClient | None = None
        self.bsm: BinanceSocketManager | None = None

    async def connect(self):
        self.client = await AsyncClient.create(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.testnet,
        )
        self.bsm = BinanceSocketManager(self.client)
        log.info("Connected to Binance %s", "testnet" if self.testnet else "LIVE")

    async def close(self):
        if self.client:
            await self.client.close_connection()

    async def get_balance(self) -> float:
        balances = await self.client.futures_account_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return float(b["balance"])
        return 0.0

    async def get_position(self, symbol: str) -> dict | None:
        positions = await self.client.futures_position_information(symbol=symbol)
        for p in positions:
            amt = float(p["positionAmt"])
            if amt != 0:
                return {
                    "symbol": p["symbol"],
                    "side": "long" if amt > 0 else "short",
                    "quantity": abs(amt),
                    "entry_price": float(p["entryPrice"]),
                    "unrealized_pnl": float(p["unRealizedProfit"]),
                }
        return None

    async def get_all_positions(self) -> list[dict]:
        positions = await self.client.futures_position_information()
        result = []
        for p in positions:
            amt = float(p["positionAmt"])
            if amt != 0:
                result.append({
                    "symbol": p["symbol"],
                    "side": "long" if amt > 0 else "short",
                    "quantity": abs(amt),
                    "entry_price": float(p["entryPrice"]),
                    "unrealized_pnl": float(p["unRealizedProfit"]),
                })
        return result

    async def get_open_orders(self, symbol: str) -> list[dict]:
        return await self.client.futures_get_open_orders(symbol=symbol)

    async def place_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        order = await self.client.futures_create_order(
            symbol=symbol,
            side=side.upper(),
            type="MARKET",
            quantity=self._format_qty(symbol, quantity),
        )
        log.info("Market %s %s %.4f — order %s", side, symbol, quantity, order["orderId"])
        return order

    async def place_stop_loss(self, symbol: str, side: str, quantity: float, stop_price: float) -> dict:
        order = await self.client.futures_create_order(
            symbol=symbol,
            side=side.upper(),
            type="STOP_MARKET",
            stopPrice=self._format_price(symbol, stop_price),
            quantity=self._format_qty(symbol, quantity),
            closePosition="false",
        )
        log.info("SL %s %s @ %.2f — order %s", side, symbol, stop_price, order["orderId"])
        return order

    async def place_take_profit(self, symbol: str, side: str, quantity: float, price: float) -> dict:
        order = await self.client.futures_create_order(
            symbol=symbol,
            side=side.upper(),
            type="TAKE_PROFIT_MARKET",
            stopPrice=self._format_price(symbol, price),
            quantity=self._format_qty(symbol, quantity),
            closePosition="false",
        )
        log.info("TP %s %s @ %.2f — order %s", side, symbol, price, order["orderId"])
        return order

    async def cancel_order(self, symbol: str, order_id: int):
        try:
            await self.client.futures_cancel_order(symbol=symbol, orderId=order_id)
            log.info("Cancelled order %s on %s", order_id, symbol)
        except Exception as e:
            log.warning("Failed to cancel order %s: %s", order_id, e)

    async def cancel_all_orders(self, symbol: str):
        try:
            await self.client.futures_cancel_all_open_orders(symbol=symbol)
            log.info("Cancelled all open orders on %s", symbol)
        except Exception as e:
            log.warning("Failed to cancel all orders on %s: %s", symbol, e)

    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[dict]:
        raw = await self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        candles = []
        for k in raw:
            candles.append({
                "timestamp": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        return candles

    async def start_kline_socket(self, symbols: list[str], interval: str, callback):
        streams = [f"{s.lower()}@kline_{interval}" for s in symbols]
        socket = self.bsm.futures_multiplex_socket(streams=streams)
        async with socket as stream:
            while True:
                msg = await stream.recv()
                if msg and "data" in msg:
                    await callback(msg["data"])

    async def start_user_socket(self, callback):
        socket = self.bsm.futures_user_socket()
        async with socket as stream:
            while True:
                msg = await stream.recv()
                if msg:
                    await callback(msg)

    def _format_qty(self, symbol: str, qty: float) -> str:
        if symbol == "BTCUSDT":
            return f"{qty:.3f}"
        return f"{qty:.2f}"

    def _format_price(self, symbol: str, price: float) -> str:
        tick = {"BTCUSDT": 0.10, "ETHUSDT": 0.01, "SOLUSDT": 0.01}.get(symbol, 0.01)
        rounded = round(price / tick) * tick
        if tick >= 0.1:
            return f"{rounded:.1f}"
        return f"{rounded:.2f}"
