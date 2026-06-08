"""
Binance Futures API wrapper.

Always connects to LIVE Binance. Paper trading is handled by PaperExchange.
"""

from __future__ import annotations

import asyncio
import logging
import math
from binance import AsyncClient, BinanceSocketManager

from config import SYMBOLS, LEVERAGE, LOT_SIZE

log = logging.getLogger(__name__)


class BinanceExchange:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.client: AsyncClient | None = None
        self.bsm: BinanceSocketManager | None = None
        self.hedge_mode: bool = False

    async def connect(self):
        self.client = await AsyncClient.create(
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        log.info("Connected to Binance LIVE")

        self.bsm = BinanceSocketManager(self.client)

        await self._detect_position_mode()
        await self._set_leverage()

    async def _detect_position_mode(self):
        try:
            result = await self.client.futures_get_position_mode()
            self.hedge_mode = result.get("dualSidePosition", False)
            log.info("Position mode: %s", "hedge" if self.hedge_mode else "one-way")
        except Exception as e:
            self.hedge_mode = False
            log.warning("Failed to detect position mode, assuming one-way: %s", e)

    async def _set_leverage(self):
        for symbol in SYMBOLS:
            try:
                await self.client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
                log.info("Set %s leverage to %dx", symbol, LEVERAGE)
            except Exception as e:
                log.warning("Failed to set leverage for %s: %s", symbol, e)

    async def close(self):
        if self.client:
            await self.client.close_connection()

    # --- Trading operations ---

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

    async def get_conditional_orders(self, symbol: str, strict: bool = False) -> list[dict]:
        """Open conditional/algo orders (STOP_MARKET / TAKE_PROFIT_MARKET).

        python-binance auto-routes STOP/TP orders to Binance's algo endpoint,
        so SL/TP live in this separate bucket — invisible to get_open_orders().

        strict=True re-raises on error so a caller making a safety-critical
        decision (e.g. force-close) can tell "no SL exists" apart from
        "couldn't fetch". Default swallows errors and returns [] (best-effort).
        """
        try:
            res = await self.client.futures_get_open_orders(symbol=symbol, conditional=True)
            return res or []
        except Exception as e:
            if strict:
                raise
            log.warning("Failed to get conditional orders on %s: %s", symbol, e)
            return []

    async def place_market_order(self, symbol: str, side: str, quantity: float, position_side: str | None = None) -> dict:
        params = dict(
            symbol=symbol,
            side=side.upper(),
            type="MARKET",
            quantity=self._format_qty(symbol, quantity),
            newOrderRespType="RESULT",
        )
        if self.hedge_mode:
            ps = position_side.upper() if position_side else ("LONG" if side.upper() == "BUY" else "SHORT")
            params["positionSide"] = ps
        order = await self.client.futures_create_order(**params)
        log.info("Market %s %s %.4f — order %s (avg %.2f)", side, symbol, quantity, order["orderId"], float(order.get("avgPrice", 0)))
        return order

    async def place_stop_loss(self, symbol: str, side: str, quantity: float, stop_price: float) -> dict:
        params = dict(
            symbol=symbol,
            side=side.upper(),
            type="STOP_MARKET",
            stopPrice=self._format_price(symbol, stop_price),
            quantity=self._format_qty(symbol, quantity),
        )
        if self.hedge_mode:
            params["positionSide"] = "LONG" if side.upper() == "SELL" else "SHORT"
        else:
            params["reduceOnly"] = "true"
        order = await self.client.futures_create_order(**params)
        # STOP_MARKET is routed to the algo endpoint, whose response has algoId (no orderId)
        log.info("SL %s %s @ %.2f — order %s", side, symbol, stop_price, order.get("algoId") or order.get("orderId"))
        return order

    async def place_take_profit(self, symbol: str, side: str, quantity: float, price: float) -> dict:
        params = dict(
            symbol=symbol,
            side=side.upper(),
            type="TAKE_PROFIT_MARKET",
            stopPrice=self._format_price(symbol, price),
            quantity=self._format_qty(symbol, quantity),
        )
        if self.hedge_mode:
            params["positionSide"] = "LONG" if side.upper() == "SELL" else "SHORT"
        else:
            params["reduceOnly"] = "true"
        order = await self.client.futures_create_order(**params)
        # TAKE_PROFIT_MARKET is routed to the algo endpoint, whose response has algoId (no orderId)
        log.info("TP %s %s @ %.2f — order %s", side, symbol, price, order.get("algoId") or order.get("orderId"))
        return order

    async def get_order(self, symbol: str, order_id: int) -> dict | None:
        try:
            return await self.client.futures_get_order(symbol=symbol, orderId=order_id)
        except Exception as e:
            log.warning("Failed to get order %s on %s: %s", order_id, symbol, e)
            return None

    async def get_trades_for_order(self, symbol: str, order_id: int) -> list[dict]:
        try:
            trades = await self.client.futures_account_trades(symbol=symbol)
            return [t for t in trades if int(t.get("orderId", 0)) == order_id]
        except Exception as e:
            log.warning("Failed to get trades for order %s: %s", order_id, e)
            return []

    async def cancel_order(self, symbol: str, order_id: int):
        try:
            await self.client.futures_cancel_order(symbol=symbol, orderId=order_id)
            log.info("Cancelled order %s on %s", order_id, symbol)
        except Exception as e:
            log.warning("Failed to cancel order %s: %s", order_id, e)

    async def cancel_all_orders(self, symbol: str):
        # Two separate Binance buckets: regular orders AND conditional/algo orders
        # (SL/TP live in the algo bucket). Both must be cleared.
        try:
            await self.client.futures_cancel_all_open_orders(symbol=symbol)
        except Exception as e:
            log.warning("Failed to cancel regular orders on %s: %s", symbol, e)
        try:
            await self.client.futures_cancel_all_open_orders(symbol=symbol, conditional=True)
        except Exception as e:
            # Benign when there are no conditional orders to cancel
            log.warning("Failed to cancel conditional orders on %s: %s", symbol, e)
        log.info("Cancelled all open orders (regular + conditional) on %s", symbol)

    # --- Market data operations ---

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

    # --- Helpers ---

    def _format_qty(self, symbol: str, qty: float) -> str:
        step = LOT_SIZE.get(symbol, 0.01)
        qty = math.floor(qty / step) * step
        decimals = max(0, -int(math.floor(math.log10(step))))
        return f"{qty:.{decimals}f}"

    def _format_price(self, symbol: str, price: float) -> str:
        tick = {"BTCUSDT": 0.10, "ETHUSDT": 0.01, "SOLUSDT": 0.01}.get(symbol, 0.01)
        rounded = round(price / tick) * tick
        if tick >= 0.1:
            return f"{rounded:.1f}"
        return f"{rounded:.2f}"
