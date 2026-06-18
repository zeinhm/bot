from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Awaitable

from binance import AsyncClient, BinanceSocketManager

from config import SYMBOLS

log = logging.getLogger(__name__)

# How many candles to keep per (symbol, interval) buffer.
BUFFER_LEN = 400


class SharedMarketData:
    """One shared Binance connection feeding candle buffers for every interval the
    bot trades. Buffers are keyed by (symbol, interval) so the 15m and 5m strategies
    each read their own timeframe. Closed candles are dispatched to subscribers as
    (symbol, interval, candle) so a worker can route each close to the matching
    strategy.
    """

    def __init__(self):
        self.market_client: AsyncClient | None = None
        self.market_bsm: BinanceSocketManager | None = None
        self._connected = False
        self._latest_prices: dict[str, float] = {}
        # (symbol, interval) -> candles
        self._candle_buffers: dict[tuple[str, str], list[dict]] = {}
        self._candle_callbacks: list[Callable[[str, str, dict], Awaitable[None]]] = []
        self._kline_tasks: list[asyncio.Task] = []
        # (symbol, htf_hours, adx_threshold) -> (htf_bucket, is_trend). The threshold
        # is part of the key because different strategies gate the SAME symbol+TF on
        # different ADX levels (15m→40, 5m→50); a symbol-only key would let the first
        # caller poison the regime for the other.
        self._regime_cache: dict[tuple[str, float, float], tuple[int, bool]] = {}

    async def connect(self):
        if self._connected:
            return
        self.market_client = await AsyncClient.create(api_key="", api_secret="")
        self.market_bsm = BinanceSocketManager(self.market_client)
        self._connected = True
        log.info("SharedMarketData: connected to Binance public API")

    async def close(self):
        for t in self._kline_tasks:
            t.cancel()
        self._kline_tasks.clear()
        if self.market_client:
            await self.market_client.close_connection()
            self._connected = False
            log.info("SharedMarketData: disconnected")

    def get_latest_price(self, symbol: str) -> float:
        return self._latest_prices.get(symbol, 0.0)

    def update_price(self, symbol: str, price: float):
        self._latest_prices[symbol] = price

    @property
    def connected(self) -> bool:
        return self._connected

    # --- Shared candle buffers ---

    async def load_history(self, symbols: list[str], interval: str = "15m", limit: int = 400):
        for symbol in symbols:
            raw = await self.market_client.futures_klines(
                symbol=symbol, interval=interval, limit=limit
            )
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
            if candles:
                candles = candles[:-1]
            self._candle_buffers[(symbol, interval)] = candles
            log.info("SharedMarketData: loaded %d %s candles for %s", len(candles), interval, symbol)

    async def start_kline_stream(self, symbols: list[str], interval: str = "15m"):
        self._kline_tasks.append(
            asyncio.create_task(self._run_kline_stream(symbols, interval))
        )

    async def _run_kline_stream(self, symbols: list[str], interval: str):
        while True:
            try:
                streams = [f"{s.lower()}@kline_{interval}" for s in symbols]
                socket = self.market_bsm.futures_multiplex_socket(streams=streams)
                async with socket as stream:
                    log.info("SharedMarketData: %s kline stream started for %s", interval, symbols)
                    while True:
                        msg = await stream.recv()
                        if msg and "data" in msg:
                            await self._on_kline(msg["data"], interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("SharedMarketData: %s kline stream error: %s", interval, e)
                await asyncio.sleep(5)

    async def _on_kline(self, data: dict, interval: str):
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

        key = (symbol, interval)
        buf = self._candle_buffers.setdefault(key, [])
        buf.append(candle)
        if len(buf) > BUFFER_LEN:
            del buf[:-BUFFER_LEN]

        log.info("SharedMarketData: %s %s candle closed O=%.2f H=%.2f L=%.2f C=%.2f",
                 symbol, interval, candle["open"], candle["high"], candle["low"], candle["close"])

        # The candle_buffer DB table is keyed by symbol only, so persist the 15m
        # interval there (its historical role); the 5m feed lives in memory only.
        if interval == "15m":
            import app.db as db
            await db.save_candles(symbol, [candle])
            await db.trim_candle_buffer(symbol, BUFFER_LEN)

        for cb in self._candle_callbacks:
            try:
                await cb(symbol, interval, candle)
            except Exception as e:
                log.error("SharedMarketData: candle callback error: %s", e)

    def get_candles(self, symbol: str, interval: str = "15m") -> list[dict]:
        return list(self._candle_buffers.get((symbol, interval), []))

    async def get_trend_regime(self, symbol: str, htf_hours: float = 6,
                               adx_period: int = 14, adx_threshold: float = 40) -> bool:
        """True if the PREVIOUS completed higher-timeframe (e.g. 6h) bar is trending
        (ADX(adx_period) >= adx_threshold). Drives the trend gate / adaptive RR.
        Cached once per HTF bucket per (symbol, htf_hours, threshold). Fail-safe: any
        error → False (range / 2:1).
        """
        bucket_sec = int(htf_hours * 3600)
        now_bucket = int(time.time()) // bucket_sec
        cache_key = (symbol, htf_hours, adx_threshold)
        cached = self._regime_cache.get(cache_key)
        if cached is not None and cached[0] == now_bucket:
            return cached[1]
        try:
            if not self.market_client:
                return False
            raw = await self.market_client.futures_klines(
                symbol=symbol, interval=f"{int(htf_hours)}h", limit=60
            )
            if len(raw) < adx_period * 2 + 2:
                return False
            raw = raw[:-1]  # drop the in-progress bar → last is the most recent completed
            highs = [float(k[2]) for k in raw]
            lows = [float(k[3]) for k in raw]
            closes = [float(k[4]) for k in raw]
            from strategy import _adx
            adx_vals, _, _ = _adx(highs, lows, closes, adx_period)
            is_trend = bool(adx_vals[-1] >= adx_threshold)
            self._regime_cache[cache_key] = (now_bucket, is_trend)
            log.info("SharedMarketData: %s %dh ADX=%.1f (thr %.0f) → %s", symbol, int(htf_hours),
                     adx_vals[-1], adx_threshold, "TREND" if is_trend else "range")
            return is_trend
        except Exception as e:
            log.error("SharedMarketData: trend-regime error for %s: %s → default range", symbol, e)
            return False

    def on_candle_close(self, callback: Callable[[str, str, dict], Awaitable[None]]):
        self._candle_callbacks.append(callback)

    def remove_candle_callback(self, callback: Callable[[str, str, dict], Awaitable[None]]):
        if callback in self._candle_callbacks:
            self._candle_callbacks.remove(callback)
