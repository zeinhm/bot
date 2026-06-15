from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Awaitable

from binance import AsyncClient, BinanceSocketManager

from config import SYMBOLS

log = logging.getLogger(__name__)


class SharedMarketData:
    def __init__(self):
        self.market_client: AsyncClient | None = None
        self.market_bsm: BinanceSocketManager | None = None
        self._connected = False
        self._latest_prices: dict[str, float] = {}
        self._candle_buffers: dict[str, list[dict]] = {}
        self._candle_callbacks: list[Callable[[str, dict], Awaitable[None]]] = []
        self._kline_task: asyncio.Task | None = None
        self._regime_cache: dict[str, tuple[int, bool]] = {}  # symbol -> (htf_bucket, is_trend)

    async def connect(self):
        if self._connected:
            return
        self.market_client = await AsyncClient.create(api_key="", api_secret="")
        self.market_bsm = BinanceSocketManager(self.market_client)
        self._connected = True
        log.info("SharedMarketData: connected to Binance public API")

    async def close(self):
        if self._kline_task:
            self._kline_task.cancel()
            self._kline_task = None
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
            self._candle_buffers[symbol] = candles
            log.info("SharedMarketData: loaded %d candles for %s", len(candles), symbol)

    async def start_kline_stream(self, symbols: list[str], interval: str = "15m"):
        self._kline_task = asyncio.create_task(self._run_kline_stream(symbols, interval))

    async def _run_kline_stream(self, symbols: list[str], interval: str):
        while True:
            try:
                streams = [f"{s.lower()}@kline_{interval}" for s in symbols]
                socket = self.market_bsm.futures_multiplex_socket(streams=streams)
                async with socket as stream:
                    log.info("SharedMarketData: kline stream started for %s", symbols)
                    while True:
                        msg = await stream.recv()
                        if msg and "data" in msg:
                            await self._on_kline(msg["data"])
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("SharedMarketData: kline stream error: %s", e)
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

        if symbol not in self._candle_buffers:
            self._candle_buffers[symbol] = []
        self._candle_buffers[symbol].append(candle)
        if len(self._candle_buffers[symbol]) > 400:
            self._candle_buffers[symbol] = self._candle_buffers[symbol][-400:]

        log.info("SharedMarketData: %s 15m candle closed O=%.2f H=%.2f L=%.2f C=%.2f",
                 symbol, candle["open"], candle["high"], candle["low"], candle["close"])

        import app.db as db
        await db.save_candles(symbol, [candle])
        await db.trim_candle_buffer(symbol, 400)

        for cb in self._candle_callbacks:
            try:
                await cb(symbol, candle)
            except Exception as e:
                log.error("SharedMarketData: candle callback error: %s", e)

    def get_candles(self, symbol: str) -> list[dict]:
        return list(self._candle_buffers.get(symbol, []))

    async def get_trend_regime(self, symbol: str, htf_hours: float = 6,
                               adx_period: int = 14, adx_threshold: float = 40) -> bool:
        """True if the PREVIOUS completed higher-timeframe (e.g. 6h) bar is trending
        (ADX(adx_period) >= adx_threshold). Drives the adaptive reward:risk: trend → 3:1,
        else 2:1. Cached once per HTF bucket per symbol. Fail-safe: any error → False (2:1).
        """
        bucket_sec = int(htf_hours * 3600)
        now_bucket = int(time.time()) // bucket_sec
        cached = self._regime_cache.get(symbol)
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
            self._regime_cache[symbol] = (now_bucket, is_trend)
            log.info("SharedMarketData: %s %dh ADX=%.1f → %s", symbol, int(htf_hours),
                     adx_vals[-1], "TREND 3:1" if is_trend else "range 2:1")
            return is_trend
        except Exception as e:
            log.error("SharedMarketData: trend-regime error for %s: %s → default 2:1", symbol, e)
            return False

    def on_candle_close(self, callback: Callable[[str, dict], Awaitable[None]]):
        self._candle_callbacks.append(callback)

    def remove_candle_callback(self, callback: Callable[[str, dict], Awaitable[None]]):
        if callback in self._candle_callbacks:
            self._candle_callbacks.remove(callback)
