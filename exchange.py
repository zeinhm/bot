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


def r_value_for_exit(
    is_sl: bool,
    entry_price: float | None,
    sl_price: float | None,
    tp_price: float | None,
    fallback_r: float | None = None,
) -> float:
    """R multiple for a resolved trade.

    Loss = -1R. Win = the actual reward:risk implied by the price levels
    (``|tp-entry| / |entry-sl|``) — which recovers the configured RRR since TP
    is placed at that multiple. Falls back to ``fallback_r`` (if positive) or 2.0
    when levels are missing. Guards against the ``-1.0 or 2.0`` truthiness trap.
    """
    if is_sl:
        return -1.0
    try:
        sl_dist = abs((entry_price or 0) - (sl_price or 0))
        tp_dist = abs((tp_price or 0) - (entry_price or 0))
        if sl_dist > 0 and tp_dist > 0:
            return round(tp_dist / sl_dist, 2)
    except Exception:
        pass
    if fallback_r and fallback_r > 0:
        return float(fallback_r)
    return 2.0


async def resolve_trade_exit(
    client: "AsyncClient",
    symbol: str,
    direction: str,
    entry_order_id: str | int | None,
    entry_time,
    entry_quantity: float | None,
    sl_price: float | None,
    tp_price: float | None,
) -> dict | None:
    """Determine how a closed position actually exited, straight from Binance fills.

    SL/TP are placed on Binance's conditional/algo endpoint, so the stored
    sl_order_id / tp_order_id are ``algoId``s. When such an order triggers it
    produces a brand-new *regular* order/trade with its own orderId — so a
    ``futures_get_order(algoId)`` lookup fails with ``-2013`` and the bot never
    learns the real outcome. This reads the account's own trade fills
    (``/fapi/v1/userTrades``, which carries ``realizedPnl``) and isolates the
    closing fills for this position, independent of any order id.

    Returns ``{exit_price, exit_commission, realized_pnl, is_sl, exit_qty}`` or
    ``None`` if the closing fills could not be found (caller should fall back).
    """
    if not entry_time:
        return None
    start_ms = int(entry_time.timestamp() * 1000) - 1000  # small buffer
    try:
        fills = await client.futures_account_trades(symbol=symbol, startTime=start_ms, limit=1000)
    except Exception as e:
        log.warning("resolve_trade_exit: could not fetch fills for %s: %s", symbol, e)
        return None
    if not fills:
        return None

    entry_oid = int(entry_order_id) if entry_order_id else None
    # Entry side is the side that opened the position; the exit is the opposite.
    exit_side = "BUY" if direction == "short" else "SELL"
    # In hedge mode the closing fill carries the position's own side; tolerate
    # one-way mode ("BOTH") and any account where it's absent.
    expected_ps = "SHORT" if direction == "short" else "LONG"

    def _ps_ok(f) -> bool:
        ps = f.get("positionSide")
        return ps is None or ps in ("BOTH", expected_ps)

    # Closing fills: opposite side, this position's side, not the entry order,
    # in chronological order.
    candidates = sorted(
        (
            f for f in fills
            if f.get("side") == exit_side
            and _ps_ok(f)
            and (entry_oid is None or int(f.get("orderId", 0)) != entry_oid)
        ),
        key=lambda f: f.get("time", 0),
    )
    if not candidates:
        return None

    # Accumulate only up to this position's size so we don't absorb a later
    # trade's fills on the same symbol (one trade per asset at a time).
    target_qty = float(entry_quantity) if entry_quantity else None
    selected = []
    acc = 0.0
    for f in candidates:
        selected.append(f)
        acc += float(f["qty"])
        if target_qty and acc >= target_qty - 1e-9:
            break

    total_qty = sum(float(f["qty"]) for f in selected)
    if total_qty <= 0:
        return None

    exit_price = sum(float(f["price"]) * float(f["qty"]) for f in selected) / total_qty
    exit_comm = sum(float(f.get("commission", 0)) for f in selected)
    realized = sum(float(f.get("realizedPnl", 0)) for f in selected)

    # Decide SL vs TP by which target the real exit price is closest to. This
    # matches the strategy's semantics (TP hit = win) even if fees nudge a
    # marginal win negative. Fall back to realized-PnL sign if a level is unset.
    if sl_price and tp_price:
        is_sl = abs(exit_price - sl_price) <= abs(exit_price - tp_price)
    else:
        is_sl = realized < 0

    return {
        "exit_price": exit_price,
        "exit_commission": exit_comm,
        "realized_pnl": realized,
        "is_sl": is_sl,
        "exit_qty": total_qty,
    }
